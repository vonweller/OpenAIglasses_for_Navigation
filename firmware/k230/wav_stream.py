# -*- coding: utf-8 -*-
"""Non-blocking HTTP reader for /stream.wav.

Parses a normal or chunked response and a standard WAV container (fmt/data
plus unknown chunks). Only 8000 Hz, mono, PCM16 is accepted. poll() and
read_pcm() never block. The backend's five-second silence keepalive is valid;
only prolonged network inactivity, EOF, or malformed data closes the stream.
"""

try:
    import usocket as socket
except ImportError:
    import socket

try:
    import utime as time
except ImportError:
    import time

import ws_client as _ws

_EAGAIN = (11, 35, 10035)
_RATE = 8000
_CHANNELS = 1
_BITS = 16


def _ticks():
    return _ws._ticks()


def _diff(a, b):
    return _ws._diff(a, b)


def _add(a, ms):
    return _ws._add(a, ms)


def _errno(exc):
    n = getattr(exc, "errno", None)
    if n is None and exc.args:
        n = exc.args[0]
    return n


class WavStream:
    def __init__(self, host, port, path="/stream.wav", max_buffer=3200,
                 silence_timeout_ms=5000, connect_timeout_ms=1000,
                 header_timeout_ms=1000, sock_factory=None):
        self.host = host
        self.port = int(port)
        self.path = path if path.startswith("/") else "/" + path
        self.max_buffer = max(2, int(max_buffer) & ~1)
        self.silence_timeout_ms = int(silence_timeout_ms)
        self.connect_timeout_ms = int(connect_timeout_ms)
        self.header_timeout_ms = int(header_timeout_ms)
        self._sock_factory = sock_factory
        self._sock = None
        self._connected = False
        self.format_ok = False
        self._raw = bytearray()
        self._body = bytearray()
        self._pcm = bytearray()
        self._chunked = False
        self._body_left = -1
        self._chunk_left = 0
        self._need_crlf = False
        self._chunk_done = False
        self._wav_pos = 0
        self._fmt = None
        self._stage = "idle"  # headers / wav / body
        self._deadline = 0
        self._last_rx_ms = 0
        self._silent_run = 0
        self._pcm_odd = b""
        self.stats = self._fresh_stats()

    @property
    def connected(self):
        return self._connected

    def connect(self):
        """Open TCP and send GET. Returns False if connect or the request stalls."""
        self.close()
        self.stats = self._fresh_stats()
        self.format_ok = False
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
            sock.setblocking(False)
            # Local import keeps this module runnable as a lone /sdcard file.
            from ws_client import _tcp_connect
            if not _tcp_connect(sock, addr, self.connect_timeout_ms):
                self._hard_close(sock)
                return False
            req = (
                "GET %s HTTP/1.1\r\n"
                "Host: %s:%d\r\n"
                "Accept: audio/wav\r\n"
                "Connection: close\r\n\r\n"
            ) % (self.path, self.host, self.port)
            from ws_client import _write_bounded
            if not _write_bounded(sock, req.encode("ascii"), self.header_timeout_ms):
                self._hard_close(sock)
                return False
            self._sock = sock
            self._connected = True
            self._stage = "headers"
            now = _ticks()
            self._deadline = _add(now, self.header_timeout_ms)
            self._last_rx_ms = now
            return True
        except Exception:
            if sock is not None:
                self._hard_close(sock)
            return False

    def close(self):
        sock = self._sock
        self._sock = None
        self._connected = False
        self.format_ok = False
        self._raw = bytearray()
        self._body = bytearray()
        self._pcm = bytearray()
        self._chunked = False
        self._body_left = -1
        self._chunk_left = 0
        self._need_crlf = False
        self._chunk_done = False
        self._wav_pos = 0
        self._fmt = None
        self._stage = "idle"
        self._silent_run = 0
        self._pcm_odd = b""
        if sock is not None:
            self._hard_close(sock)

    def poll(self):
        """Read whatever the socket has and decode it. Never blocks."""
        if not self._connected or self._sock is None:
            return
        try:
            self._fill()
            if self._stage == "headers":
                self._parse_headers()
            if self._stage in ("wav", "body"):
                self._drain_http()
            if self._stage == "wav":
                self._parse_wav()
            if self._stage == "body":
                self._take_pcm()
            if self._chunk_done or (not self._chunked and self._body_left == 0):
                raise OSError("eof")
        except Exception:
            # PCM already decoded in this call stays readable after close.
            self._fail()

    def read_pcm(self, max_bytes=320):
        """Return up to max_bytes of PCM. Short or empty when nothing is queued."""
        if max_bytes <= 0 or not self._pcm:
            return b""
        # Keep samples aligned.
        n = min(len(self._pcm), int(max_bytes))
        n -= n & 1
        if n <= 0:
            return b""
        out = bytes(self._pcm[:n])
        self._pcm = self._pcm[n:]
        self.stats["buffered"] = len(self._pcm)
        return out

    # ------------------------------------------------------------------ http

    def _fill(self):
        # One poll reads a bounded amount so a burst cannot grow without limit
        # before the buffer trim runs.
        for _ in range(4):
            chunk = self._read_some(self._sock, 512)
            if chunk is None:
                self._check_stall()
                return
            if chunk == b"":
                raise OSError("eof")
            self._raw += chunk
            now = _ticks()
            self._last_rx_ms = now
            if self._stage in ("headers", "wav"):
                self._deadline = _add(now, self.header_timeout_ms)
            if len(self._raw) > self.max_buffer * 4:
                return

    def _check_stall(self):
        if self._stage in ("headers", "wav"):
            if _diff(self._deadline, _ticks()) <= 0:
                raise OSError("header stall")
            return
        # The backend can send just 10 ms of silence once every five seconds.
        # Network inactivity, not the values or amount of queued PCM, detects loss.
        if _diff(_ticks(), self._last_rx_ms) > max(15000, self.silence_timeout_ms * 3):
            raise OSError("audio stream inactive")

    def _parse_headers(self):
        buf = self._raw
        idx = buf.find(b"\r\n\r\n")
        if idx < 0:
            if len(buf) > 8192:
                raise OSError("headers too large")
            return
        head = bytes(buf[:idx])
        self._raw = buf[idx + 4:]
        lines = head.split(b"\r\n")
        status = lines[0].split()
        if len(status) < 2 or status[1] != b"200":
            raise OSError("http status")
        chunked = False
        length = -1
        for line in lines[1:]:
            low = line.lower()
            if low.startswith(b"transfer-encoding:") and b"chunked" in low:
                chunked = True
            elif low.startswith(b"content-length:"):
                try:
                    length = int(low.split(b":", 1)[1].strip() or b"0")
                except ValueError:
                    raise OSError("bad content-length")
        self._chunked = chunked
        self._body_left = length
        self._stage = "wav"
        # Remainder stays in _raw. Chunked mode still has size lines to parse;
        # identity mode is moved by _drain_http.

    def _drain_http(self):
        """Move decoded body bytes from the socket buffer into _body."""
        if not self._chunked:
            if not self._raw:
                return
            if self._body_left == 0:
                raise OSError("content done")
            take = len(self._raw)
            if self._body_left > 0:
                take = min(take, self._body_left)
                self._body_left -= take
            self._body += self._raw[:take]
            self._raw = self._raw[take:]
            return
        guard = 0
        while self._raw and guard < 64:
            guard += 1
            if self._need_crlf:
                if len(self._raw) < 2:
                    return
                if bytes(self._raw[:2]) != b"\r\n":
                    raise OSError("bad chunk crlf")
                self._raw = self._raw[2:]
                self._need_crlf = False
                continue
            if self._chunk_left == 0:
                idx = self._raw.find(b"\r\n")
                if idx < 0:
                    if len(self._raw) > 64:
                        raise OSError("bad chunk size")
                    return
                line = bytes(self._raw[:idx]).split(b";", 1)[0].strip()
                try:
                    size = int(line.decode("ascii") or "0", 16)
                except (ValueError, UnicodeError):
                    raise OSError("bad chunk size")
                self._raw = self._raw[idx + 2:]
                if size == 0:
                    self._chunk_done = True
                    return
                if size > self.max_buffer * 8:
                    raise OSError("chunk too large")
                self._chunk_left = size
            take = min(self._chunk_left, len(self._raw))
            self._body += self._raw[:take]
            self._raw = self._raw[take:]
            self._chunk_left -= take
            if self._chunk_left == 0:
                self._need_crlf = True

    def _parse_wav(self):
        """Walk RIFF from _wav_pos. A chunk is consumed only when complete."""
        raw = self._body
        if self._wav_pos == 0:
            if len(raw) < 12:
                return
            if bytes(raw[:4]) != b"RIFF" or bytes(raw[8:12]) != b"WAVE":
                raise OSError("not wave")
            self._wav_pos = 12
        while True:
            if len(raw) < self._wav_pos + 8:
                return
            base = self._wav_pos
            cid = bytes(raw[base:base + 4])
            size = int.from_bytes(raw[base + 4:base + 8], "little")
            if cid != b"data" and (size < 0 or size > 16 * 1024 * 1024):
                raise OSError("bad chunk")
            pad = size & 1
            total = 8 + size + pad
            if cid == b"data":
                if self._fmt is None:
                    raise OSError("data before fmt")
                # Streaming WAV advertises 0x7FFFFFFF-class sizes. Do not try
                # to consume that many bytes; everything after the header is PCM.
                self._body = raw[base + 8:]
                self._wav_pos = 0
                self.format_ok = True
                self._stage = "body"
                self.stats["sample_rate"] = self._fmt[0]
                self.stats["channels"] = self._fmt[1]
                self.stats["bits"] = self._fmt[2]
                return
            if len(raw) < base + total:
                return
            if cid == b"fmt ":
                if size < 16:
                    raise OSError("short fmt")
                info = bytes(raw[base + 8:base + 24])
                audio_format = info[0] | (info[1] << 8)
                channels = info[2] | (info[3] << 8)
                rate = int.from_bytes(info[4:8], "little")
                bits = info[14] | (info[15] << 8)
                if not (audio_format == 1 and channels == _CHANNELS
                        and rate == _RATE and bits == _BITS):
                    raise OSError("unsupported wav")
                self._fmt = (rate, channels, bits)
            self._wav_pos = base + total

    def _take_pcm(self):
        if not self._body:
            return
        piece = bytes(self._body)
        self._body = bytearray()
        self._accept_pcm(piece)

    def _accept_pcm(self, piece):
        if self._pcm_odd:
            piece = self._pcm_odd + piece
            self._pcm_odd = b""
        if len(piece) & 1:
            self._pcm_odd = piece[-1:]
            piece = piece[:-1]
        if not piece:
            return
        self.stats["received_bytes"] += len(piece)
        if self._is_silence(piece):
            self._silent_run += len(piece)
        else:
            self._silent_run = 0
        # silence_ms counts queued silence, including samples already buffered.
        queued_silence = self._silent_run
        if self._pcm and self._is_silence(self._pcm):
            queued_silence += len(self._pcm)
        self.stats["silence_ms"] = (queued_silence * 1000) // (_RATE * 2)
        self._pcm += piece
        overflow = len(self._pcm) - self.max_buffer
        if overflow > 0:
            overflow += overflow & 1
            self._pcm = self._pcm[overflow:]
            self.stats["dropped_bytes"] += overflow
        self.stats["buffered"] = len(self._pcm)

    def _is_silence(self, data):
        # Silence is every byte zero. One non-zero sample resets the run.
        for b in data:
            if b:
                return False
        return True

    # ------------------------------------------------------------------ sock

    def _read_some(self, sock, n):
        from ws_client import select
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

    def _fail(self):
        sock = self._sock
        self._sock = None
        self._connected = False
        self._stage = "idle"
        if sock is not None:
            self._hard_close(sock)

    def _hard_close(self, sock):
        try:
            sock.close()
        except Exception:
            pass

    def _fresh_stats(self):
        return {
            "buffered": 0,
            "dropped_bytes": 0,
            "received_bytes": 0,
            "silence_ms": 0,
            "sample_rate": 0,
            "channels": 0,
            "bits": 0,
        }
