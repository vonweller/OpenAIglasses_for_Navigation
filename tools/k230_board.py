"""Back up, deploy, and inspect CanMV boards through their MicroPython REPL."""

import argparse
import base64
from datetime import datetime
import hashlib
from pathlib import Path
import time


class Board:
    def __init__(self, port: str, timeout: float = 15.0):
        import serial

        self.serial = serial.Serial(port, 115200, timeout=0.1, write_timeout=3)
        self.timeout = timeout

    def __enter__(self):
        try:
            self.serial.write(b"\r\x03\x03")
            deadline = time.monotonic() + 12
            response = bytearray()
            while time.monotonic() < deadline:
                response.extend(self.serial.read(self.serial.in_waiting or 1))
                if bytes(response).rstrip().endswith(b">"):
                    break
            else:
                raise TimeoutError("The running board program did not stop")
            self.serial.write(b"\x01")
            response = self._until(b"raw REPL; CTRL-B to exit\r\n>")
            if b"raw REPL" not in response:
                raise RuntimeError("The device did not enter the MicroPython raw REPL")
            return self
        except BaseException:
            self.serial.close()
            raise

    def __exit__(self, *_):
        self.serial.write(b"\x02")
        self.serial.close()

    def _until(self, suffix: bytes, timeout=None) -> bytes:
        result = bytearray()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            result.extend(self.serial.read(self.serial.in_waiting or 1))
            if result.endswith(suffix):
                return bytes(result)
        raise TimeoutError("Timed out waiting for the board; no reset was performed")

    def execute(self, code: str, timeout=None) -> bytes:
        encoded = code.encode("utf-8")
        for offset in range(0, len(encoded), 256):
            self.serial.write(encoded[offset:offset + 256])
            time.sleep(0.002)
        self.serial.write(b"\x04")
        response = self._until(b"\x04>", timeout)
        if not response.startswith(b"OK"):
            raise RuntimeError("The board rejected the raw REPL command")
        stdout, stderr, _ = response[2:-1].split(b"\x04", 2)
        if stderr:
            raise RuntimeError(stderr.decode("utf-8", "replace"))
        return stdout

    def read_file(self, remote: str) -> bytes:
        output = self.execute(
            "import binascii\n"
            "with open(%r, 'rb') as f:\n"
            " while True:\n"
            "  b = f.read(1536)\n"
            "  if not b: break\n"
            "  print(binascii.b2a_base64(b).decode().strip())\n" % remote
        )
        return b"".join(base64.b64decode(line) for line in output.splitlines() if line)

    def write_file(self, remote: str, content: bytes) -> None:
        suffix = str(time.time_ns())
        temporary = remote + ".upload-" + suffix
        self.execute("f = open(%r, 'wb')" % temporary)
        try:
            for offset in range(0, len(content), 768):
                chunk = base64.b64encode(content[offset:offset + 768])
                self.execute("import binascii; f.write(binascii.a2b_base64(%r))" % chunk)
            self.execute("f.close()")
            if self.read_file(temporary) != content:
                raise RuntimeError("Uploaded file verification failed")
            previous = remote + ".previous-" + suffix
            self.execute(
                "import os\n"
                "try: os.stat(%r); exists = True\n"
                "except OSError: exists = False\n"
                "if exists: os.rename(%r, %r)\n"
                "os.rename(%r, %r)\n"
                % (remote, remote, previous, temporary, remote)
            )
        except Exception:
            try:
                self.execute("f.close()")
            except Exception:
                pass
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--timeout", type=float, default=15)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info")
    execute = commands.add_parser("exec")
    execute.add_argument("file", type=Path)
    backup = commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("paths", nargs="+")
    deploy = commands.add_parser("deploy")
    deploy.add_argument("source", type=Path)
    deploy.add_argument("--destination", default="/sdcard")
    deploy.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()

    with Board(args.port, args.timeout) as board:
        if args.command == "info":
            print(board.execute("import os, sys; print(os.uname()); print(sys.version); print(os.listdir('/sdcard'))").decode())
        elif args.command == "exec":
            print(board.execute(args.file.read_text("utf-8"), args.timeout).decode("utf-8", "replace"))
        elif args.command == "backup":
            args.output.mkdir(parents=True, exist_ok=False)
            for remote in args.paths:
                data = board.read_file(remote)
                (args.output / Path(remote).name).write_bytes(data)
                print(Path(remote).name, len(data), hashlib.sha256(data).hexdigest())
        elif args.command == "deploy":
            target = args.backup / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            target.mkdir(parents=True, exist_ok=False)
            files = sorted(args.source.glob("*.py"))
            files = [p for p in files if p.name not in ("k230_secrets.py", "secrets.example.py")]
            for local in files:
                remote = args.destination.rstrip("/") + "/" + local.name
                exists = board.execute("import os; print(%r in os.listdir(%r))" % (local.name, args.destination)).strip() == b"True"
                if exists:
                    (target / local.name).write_bytes(board.read_file(remote))
            # Install the boot entry only after all of its dependencies verify.
            for local in sorted(files, key=lambda path: path.name == "main.py"):
                board.write_file(args.destination.rstrip("/") + "/" + local.name, local.read_bytes())
                print("Verified:", local.name)
            print("Previous files saved to:", target)


if __name__ == "__main__":
    main()
