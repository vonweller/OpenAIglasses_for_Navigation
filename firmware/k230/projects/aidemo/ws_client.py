# -*- coding: utf-8 -*-
"""RFC6455 WebSocket client for CPython and CanMV MicroPython.

No threads. The caller owns the loop and must call poll() often.
Client frames are always masked. Masking uses a 4-byte phase so a
partial send chunk never rotates the mask, regardless of chunk length.
"""

try:
    import usocket as socket
except ImportError:
    import socket

try:
    import uselect as select
except ImportError:
    import select

try:
    import uos as os
except ImportError:
    import os

try:
    import utime as time
except ImportError:
    import time

try:
    import ubinascii as binascii
except ImportError:
    import binascii

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_EAGAIN = (11, 35, 10035)  # EAGAIN/EWOULDBLOCK: RT-Smart, POSIX, WinSock
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_CTRL = 125


def _ticks():
    fn = _clock
    if fn is not None:
        return int(fn())
    native = getattr(time, "ticks_ms", None)
    if native is not None:
        return native()
    return int(time.time() * 1000)


# Tests inject a millisecond clock. None means the platform clock.
_clock = None


def _diff(a, b):
    if _clock is not None:
        return a - b
    fn = getattr(time, "ticks_diff", None)
    if fn is not None:
        return fn(a, b)
    return a - b


def _add(a, ms):
    if _clock is not None:
        return a + ms
    fn = getattr(time, "ticks_add", None)
    if fn is not None:
        return fn(a, ms)
    return a + ms


def _b64(raw):
    if hasattr(binascii, "b2a_base64"):
        out = binascii.b2a_base64(raw)
        if isinstance(out, str):
            out = out.encode("ascii")
        return out.strip()
    return binascii.b2a_base64(raw, newline=False)


def _sha1(data):
    # hashlib is CPython; CanMV builds that include uhashlib work the same.
    try:
        import uhashlib as hashlib
    except ImportError:
        import hashlib
    return hashlib.sha1(data).digest()


def _errno(exc):
    n = getattr(exc, "errno", None)
    if n is None and exc.args:
        n = exc.args[0]
    return n


def _tcp_connect(sock, addr, timeout_ms):
    """Connect with a deadline, then leave the socket non-blocking."""
    if not hasattr(sock, "getsockopt"):
        # CanMV v1.8 has no SO_ERROR/fileno and nonblocking connect returns EAGAIN.
        try:
            sock.settimeout(max(0.1, timeout_ms / 1000.0))
            sock.connect(addr)
            return True
        except OSError:
            return False
        finally:
            sock.setblocking(False)
    try:
        sock.connect(addr)
        return True
    except OSError as exc:
        # EINPROGRESS 115 (Linux/RT-Smart), EALREADY, EWOULDBLOCK/EAGAIN.
        if _errno(exc) not in (11, 35, 36, 37, 114, 115, 10035):
            return False
    # Sockets that select cannot register (unit-test pipes, some MicroPython
    # builds) are decided by SO_ERROR. Zero means the handshake completed.
    try:
        fileno = sock.fileno()
    except Exception:
        fileno = -1
    if not isinstance(fileno, int) or fileno < 0:
        try:
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        except Exception:
            return False
        return not err
    deadline = _add(_ticks(), timeout_ms)
    poller = select.poll()
    try:
        poller.register(sock, select.POLLOUT)
    except Exception:
        try:
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        except Exception:
            return False
        return not err
    try:
        while _diff(deadline, _ticks()) > 0:
            if not poller.poll(50):
                continue
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            return not err
        return False
    finally:
        try:
            poller.unregister(sock)
        except Exception:
            pass


def _wait_readable(sock, deadline):
    """Block in poll until the socket is readable or deadline passes."""
    left = _diff(deadline, _ticks())
    if left <= 0:
        return False
    try:
        poller = select.poll()
        poller.register(sock, select.POLLIN)
        try:
            return bool(poller.poll(left))
        finally:
            try:
                poller.unregister(sock)
            except Exception:
                pass
    except Exception:
        return True


def _write_bounded(sock, data, timeout_ms):
    """Send all of data. EAGAIN waits in poll. send()==0 is a dead peer."""
    view = memoryview(data)
    off = 0
    deadline = _add(_ticks(), timeout_ms)
    while off < len(view):
        if _diff(deadline, _ticks()) <= 0:
            return False
        try:
            n = sock.send(view[off:])
        except TypeError:
            n = sock.send(bytes(view[off:]))
        except OSError as exc:
            if _errno(exc) in _EAGAIN:
                if not _wait_writable(sock, deadline):
                    return False
                continue
            return False
        if n is None:
            if not _wait_writable(sock, deadline):
                return False
            continue
        if n == 0:
            return False
        off += n
    return True


def _wait_writable(sock, deadline):
    left = _diff(deadline, _ticks())
    if left <= 0:
        return False
    try:
        poller = select.poll()
        poller.register(sock, select.POLLOUT)
        try:
            return bool(poller.poll(left))
        finally:
            try:
                poller.unregister(sock)
            except Exception:
                pass
    except Exception:
        return True


def apply_mask(data, mask, offset=0):
    """XOR data with the repeating 4-byte mask, starting at phase offset.

    Phase is offset & 3, so a short send chunk cannot rotate the mask.
    CanMV's native big-integer XOR avoids a Python loop per payload byte.
    """
    if not data:
        return b""
    if not isinstance(mask, (bytes, bytearray)) or len(mask) != 4:
        raise ValueError("mask must be 4 bytes")
    raw = bytes(data)
    phase = offset & 3
    rotated = bytes(mask[phase:] + mask[:phase])
    repeated = (rotated * ((len(raw) + 3) // 4))[:len(raw)]
    return (int.from_bytes(raw, "little") ^ int.from_bytes(repeated, "little")).to_bytes(len(raw), "little")


class _Frame:
    __slots__ = ("opcode", "data", "latest", "sent", "mask", "hdr")

    def __init__(self, opcode, data, latest):
        self.opcode = opcode
        self.data = data
        self.latest = latest
        self.sent = 0
        self.mask = None
        self.hdr = None


class WSClient:
    """Single-connection RFC6455 client. poll() never blocks on I/O."""

    def __init__(self, host, port, path, max_queue=4, max_message=65536,
                 connect_timeout_ms=1000, handshake_timeout_ms=1000,
                 send_stall_ms=5000, send_chunk=4096, sock_factory=None):
        self.host = host
        self.port = int(port)
        self.path = path if path.startswith("/") else "/" + path
        self.max_queue = max(1, int(max_queue))
        self.max_message = max(1, int(max_message))
        self.connect_timeout_ms = int(connect_timeout_ms)
        self.handshake_timeout_ms = int(handshake_timeout_ms)
        self.send_stall_ms = int(send_stall_ms)
        self.send_chunk = max(1, int(send_chunk))
        self._sock_factory = sock_factory
        self._sock = None
        self._connected = False
        self._queue = []
        self._texts = []
        self._rx = bytearray()
        self._frag_op = None
        self._frag = bytearray()
        self._stall_deadline = None
        self.stats = {
            "queued": 0,
            "sent": 0,
            "dropped": 0,
            "rejected": 0,
            "send_fail": 0,
            "recv_text": 0,
            "bytes_sent": 0,
            "binary_sent": 0,
        }

    @property
    def connected(self):
        return self._connected

    @property
    def pending(self):
        return len(self._queue)

    def connect(self):
        """TCP connect plus HTTP upgrade. Each stage is bounded to 1s by default."""
        self.close()
        self.stats = {k: 0 for k in self.stats}
        self._texts = []
        self._rx = bytearray()
        self._frag_op = None
        self._frag = bytearray()
        sock = None
        try:
            if self._sock_factory is not None:
                sock = self._sock_factory()
                addr = (self.host, self.port)
            else:
                addr = socket.getaddrinfo(
                    self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
                )[0][-1]
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Non-blocking before connect so the 101 cannot sit on a blocking
            # socket. The connect itself is bounded by POLLOUT.
            self._set_blocking(sock, False)
            if not _tcp_connect(sock, addr, self.connect_timeout_ms):
                self._hard_close(sock)
                return False
            if not self._handshake(sock):
                self._hard_close(sock)
                return False
            self._sock = sock
            self._connected = True
            return True
        except Exception:
            if sock is not None:
                self._hard_close(sock)
            self._sock = None
            self._connected = False
            return False

    def close(self):
        sock = self._sock
        self._connected = False
        self._sock = None
        self._queue = []
        self._stall_deadline = None
        self._frag_op = None
        self._frag = bytearray()
        if sock is not None:
            try:
                self._set_blocking(sock, False)
                payload = (1000).to_bytes(2, "big")
                frame = self._build_frame(OP_CLOSE, payload)
                self._send_some(sock, memoryview(frame), 0)
            except Exception:
                pass
            self._hard_close(sock)

    def queue_binary(self, payload, latest=False):
        data = self._as_bytes(payload)
        if data is None or len(data) > self.max_message:
            self.stats["rejected"] += 1
            return False
        if not self._connected:
            self.stats["rejected"] += 1
            return False
        if latest:
            for item in self._queue:
                if item.latest and item.sent == 0:
                    item.data = data
                    item.opcode = OP_BIN
                    item.hdr = None
                    item.mask = None
                    self.stats["dropped"] += 1
                    self.stats["queued"] += 1
                    return True
        if len(self._queue) >= self.max_queue:
            if not latest or not self._drop_one_latest():
                self.stats["rejected"] += 1
                return False
        self._queue.append(_Frame(OP_BIN, data, bool(latest)))
        self.stats["queued"] += 1
        return True

    def queue_audio(self, payload):
        if len(self._queue) >= self.max_queue:
            for i, item in enumerate(self._queue):
                if item.opcode == OP_BIN and item.sent == 0:
                    del self._queue[i]
                    self.stats["dropped"] += 1
                    break
        return self.queue_binary(payload, latest=False)

    def queue_text(self, text):
        if isinstance(text, str):
            data = text.encode("utf-8")
        else:
            data = self._as_bytes(text)
        if data is None or len(data) > self.max_message:
            self.stats["rejected"] += 1
            return False
        if not self._connected:
            self.stats["rejected"] += 1
            return False
        # Text is control. Make room by dropping one unsent video frame.
        # If every slot is already control, refuse instead of discarding it.
        if len(self._queue) >= self.max_queue and not self._drop_one_latest():
            self.stats["rejected"] += 1
            return False
        self._queue.append(_Frame(OP_TEXT, data, False))
        self.stats["queued"] += 1
        return True

    def poll(self):
        """Pump socket I/O. Returns complete text messages received this call."""
        if not self._connected or self._sock is None:
            return []
        self._texts = []
        try:
            self._recv_ready()
            self._drain_send()
        except Exception:
            self.stats["send_fail"] += 1
            self._drop_link()
        out = self._texts
        self._texts = []
        return out

    # ---------------------------------------------------------------- connect

    def _handshake(self, sock):
        key_raw = os.urandom(16)
        key = _b64(key_raw).decode("ascii")
        expect = _b64(_sha1(key.encode("ascii") + _WS_GUID)).decode("ascii")
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ) % (self.path, self.host, self.port, key)
        data = req.encode("ascii")
        if not _write_bounded(sock, data, self.handshake_timeout_ms):
            return False
        buf = bytearray()
        deadline = _add(_ticks(), self.handshake_timeout_ms)
        while b"\r\n\r\n" not in buf:
            if _diff(deadline, _ticks()) <= 0:
                return False
            chunk = self._read_some(sock, 256)
            if chunk is None:
                if not _wait_readable(sock, deadline):
                    return False
                continue
            if chunk == b"":
                return False
            buf += chunk
            if len(buf) > 8192:
                return False
        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status = lines[0]
        parts = status.split()
        if len(parts) < 2 or parts[1] != b"101":
            return False
        accept = None
        upgrade = False
        for line in lines[1:]:
            low = line.lower()
            if low.startswith(b"sec-websocket-accept:"):
                accept = line.split(b":", 1)[1].strip().decode("ascii", "ignore")
            elif low.startswith(b"upgrade:") and b"websocket" in low:
                upgrade = True
        if accept != expect or not upgrade:
            return False
        if rest:
            self._rx += rest
        return True

    # ------------------------------------------------------------------- queue

    def _drop_one_latest(self):
        # Any unsent binary frame is replaceable video. In-flight bytes stay.
        for i, item in enumerate(self._queue):
            if item.opcode == OP_BIN and item.latest and item.sent == 0:
                del self._queue[i]
                self.stats["dropped"] += 1
                return True
        return False

    def _drain_send(self):
        started = _ticks()
        while self._queue and self._connected:
            if _diff(_ticks(), started) >= 5:
                return
            item = self._queue[0]
            if item.hdr is None:
                item.mask = os.urandom(4)
                item.hdr = self._header(item.opcode, len(item.data), item.mask)
            if item.sent < len(item.hdr):
                n = self._send_piece(memoryview(item.hdr), item.sent)
                if n <= 0:
                    return
                item.sent += n
            else:
                payload_off = item.sent - len(item.hdr)
                end = min(payload_off + self.send_chunk, len(item.data))
                piece = apply_mask(item.data[payload_off:end], item.mask, payload_off)
                n = self._send_piece(memoryview(piece), 0)
                if n <= 0:
                    return
                item.sent += n
            if item.sent >= len(item.hdr) + len(item.data):
                self._queue.pop(0)
                self.stats["sent"] += 1
                if item.opcode == OP_BIN:
                    self.stats["binary_sent"] += 1
                self.stats["bytes_sent"] += len(item.data)
                self._stall_deadline = None

    def _send_piece(self, view, off):
        if self._stall_deadline is None:
            self._stall_deadline = _add(_ticks(), self.send_stall_ms)
        try:
            n = self._send_some(self._sock, view, off)
        except OSError as exc:
            if _errno(exc) in _EAGAIN:
                if _diff(self._stall_deadline, _ticks()) <= 0:
                    raise OSError("send stall")
                return -1
            self.stats["send_fail"] += 1
            raise
        if n == 0:
            self.stats["send_fail"] += 1
            raise OSError("send returned 0")
        if n < 0:
            if _diff(self._stall_deadline, _ticks()) <= 0:
                raise OSError("send stall")
            return -1
        self._stall_deadline = _add(_ticks(), self.send_stall_ms)
        return n

    def _header(self, opcode, length, mask):
        hdr = bytearray()
        hdr.append(0x80 | (opcode & 0x0F))
        if length <= 125:
            hdr.append(0x80 | length)
        elif length <= 0xFFFF:
            hdr.append(0x80 | 126)
            hdr += length.to_bytes(2, "big")
        else:
            hdr.append(0x80 | 127)
            hdr += length.to_bytes(8, "big")
        hdr += mask
        return bytes(hdr)

    def _build_frame(self, opcode, payload):
        mask = os.urandom(4)
        return self._header(opcode, len(payload), mask) + apply_mask(payload, mask, 0)

    # -------------------------------------------------------------------- recv

    def _recv_ready(self):
        # Bound the work of one poll so a flood cannot stall the caller.
        for _ in range(8):
            if not self._read_into(512):
                break
        self._parse_frames()

    def _read_into(self, n):
        chunk = self._read_some(self._sock, n)
        if chunk is None:
            return False
        if chunk == b"":
            raise OSError("peer closed")
        if len(self._rx) + len(chunk) > self.max_message + 16:
            # Header overhead is tiny; the payload cap is enforced per frame.
            if len(self._rx) > self.max_message + 14:
                raise OSError("rx overflow")
        self._rx += chunk
        return True

    def _parse_frames(self):
        while True:
            frame = self._take_frame()
            if frame is None:
                return
            opcode, fin, payload = frame
            self._dispatch(opcode, fin, payload)

    def _take_frame(self):
        buf = self._rx
        if len(buf) < 2:
            return None
        b1 = buf[0]
        b2 = buf[1]
        masked = (b2 & 0x80) != 0
        ln = b2 & 0x7F
        need = 2
        if ln == 126:
            need += 2
        elif ln == 127:
            need += 8
        if masked:
            need += 4
        if len(buf) < need:
            return None
        idx = 2
        if ln == 126:
            ln = int.from_bytes(buf[2:4], "big")
            idx = 4
        elif ln == 127:
            ln = int.from_bytes(buf[2:10], "big")
            idx = 10
            if ln > 0x7FFFFFFF:
                raise OSError("frame too large")
        mask = None
        if masked:
            mask = bytes(buf[idx:idx + 4])
            idx += 4
        if ln > self.max_message:
            raise OSError("frame exceeds max_message")
        if len(buf) < idx + ln:
            return None
        payload = bytes(buf[idx:idx + ln])
        self._rx = buf[idx + ln:]
        if mask is not None:
            payload = apply_mask(payload, mask, 0)
        opcode = b1 & 0x0F
        fin = (b1 & 0x80) != 0
        return opcode, fin, payload

    def _dispatch(self, opcode, fin, payload):
        if opcode == OP_CLOSE:
            raise OSError("close")
        if opcode == OP_PING:
            if len(payload) > _MAX_CTRL:
                raise OSError("ping too large")
            self._queue_front(OP_PONG, payload)
            return
        if opcode == OP_PONG:
            return
        if opcode == OP_CONT:
            if self._frag_op is None:
                raise OSError("unexpected continuation")
            self._frag += payload
            if len(self._frag) > self.max_message:
                raise OSError("fragment overflow")
            if fin:
                self._emit(self._frag_op, bytes(self._frag))
                self._frag_op = None
                self._frag = bytearray()
            return
        if opcode in (OP_TEXT, OP_BIN):
            if not fin:
                if self._frag_op is not None:
                    raise OSError("nested fragment")
                self._frag_op = opcode
                self._frag = bytearray(payload)
                return
            self._emit(opcode, payload)
            return
        raise OSError("bad opcode")

    def _emit(self, opcode, payload):
        if opcode == OP_TEXT:
            try:
                text = payload.decode("utf-8")
            except Exception:
                raise OSError("bad utf-8")
            self._texts.append(text)
            self.stats["recv_text"] += 1
        # Binary from the server is ignored; this client only consumes text.

    def _queue_front(self, opcode, payload):
        # Pong is control and must not be dropped. If the cap is hit, drop one
        # unsent latest video frame; if none exists, refuse by dropping the link
        # rather than silently losing the pong.
        if len(self._queue) >= self.max_queue and not self._drop_one_latest():
            raise OSError("no room for pong")
        frame = _Frame(opcode, payload, False)
        # A control frame may not split an already partially written data frame.
        index = 1 if self._queue and self._queue[0].sent else 0
        self._queue.insert(index, frame)

    # -------------------------------------------------------------------- sock

    def _send_some(self, sock, view, off):
        data = view[off:]
        try:
            n = sock.send(data)
        except TypeError:
            n = sock.send(bytes(data))
        if n is None:
            return -1
        return n

    def _read_some(self, sock, n):
        try:
            poller = select.poll()
            poller.register(sock, select.POLLIN)
        except (AttributeError, TypeError, ValueError):
            poller = None
        if poller is not None and not poller.poll(0):
            return None
        try:
            data = sock.recv(n)
        except AttributeError:
            data = sock.read(n)
        except OSError as exc:
            if _errno(exc) in _EAGAIN:
                return None
            raise
        if data is None:
            return None
        return data

    def _drop_link(self):
        sock = self._sock
        self._connected = False
        self._sock = None
        self._queue = []
        self._stall_deadline = None
        if sock is not None:
            self._hard_close(sock)

    def _hard_close(self, sock):
        try:
            sock.close()
        except Exception:
            pass

    def _set_blocking(self, sock, blocking):
        if hasattr(sock, "setblocking"):
            sock.setblocking(blocking)
            return
        sock.settimeout(None if blocking else 0)

    def _set_timeout(self, sock, ms):
        sock.settimeout(ms / 1000.0)

    def _as_bytes(self, payload):
        if isinstance(payload, str):
            return payload.encode("utf-8")
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        if isinstance(payload, memoryview):
            return bytes(payload)
        return None
