"""UDP broadcast discovery of the aiglasses backend.

Broadcasts AIGLASS_DISCOVER to 255.255.255.255:<DISCOVER_PORT> and waits for a
unicast reply "AIGLASS_SERVER:<port>" from the server; the reply source address
is the server IP. Mirrors compile/compile.ino and k10_glasses behaviour so a
host IP change never requires editing board files.
"""

import socket
import time

DISCOVER_PORT = 12345
MAGIC = b"AIGLASS_DISCOVER"
PREFIX = "AIGLASS_SERVER:"


def resolve_server(timeout_ms=4000, attempts_delay_ms=120):
    """Return (host, port) or (None, None) on timeout."""
    dest = ("255.255.255.255", DISCOVER_PORT)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # 【关键】CanMV/lwip 必须显式打开 SO_BROADCAST，否则 sendto 到 255.255.255.255 被静默丢弃
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:
        pass
    try:
        sock.bind(("0.0.0.0", DISCOVER_PORT))
    except OSError:
        try:
            sock.close()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.bind(("0.0.0.0", 0))
        except OSError:
            return None, None
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    last_send = 0
    try:
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            now = time.ticks_ms()
            if time.ticks_diff(now, last_send) >= attempts_delay_ms:
                try:
                    sock.sendto(MAGIC, dest)
                except OSError:
                    pass
                last_send = now
            try:
                data, peer = sock.recvfrom(256)
                text = data.decode("utf-8", "ignore").strip()
                if text.startswith(PREFIX):
                    try:
                        port = int(text[len(PREFIX):].strip() or 8081)
                    except ValueError:
                        port = 8081
                    return peer[0], port
            except OSError:
                time.sleep_ms(20)
    finally:
        sock.close()
    return None, None
