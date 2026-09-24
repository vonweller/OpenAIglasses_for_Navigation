# -*- coding: utf-8 -*-
"""Transport tests for the K230 WebSocket client and WAV reader.

Sockets are in-memory. Nothing binds a port and nothing touches a board.
"""

import base64
import hashlib
import os
import struct
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "firmware", "k230"))

import ws_client
import wav_stream
from ws_client import OP_BIN, OP_PING, OP_TEXT, WSClient, apply_mask
from wav_stream import WavStream


class MemSock:
    """Byte pipe with a programmable send window and recv slices."""

    def __init__(self, recv_slices=None, send_window=None, fail_send_errno=None):
        self.recv_slices = list(recv_slices or [])
        self.send_window = send_window
        self.fail_send_errno = fail_send_errno
        self.sent = bytearray()
        self.closed = False
        self.timeout = None
        self.blocking = True
        self.so_error = 0

    def fileno(self):
        return -1

    def setblocking(self, blocking):
        self.blocking = bool(blocking)

    def settimeout(self, value):
        self.timeout = value

    def connect(self, addr):
        self.connected_to = addr

    def getsockopt(self, level, opt):
        return self.so_error

    def send(self, data):
        if self.closed:
            return 0
        if self.fail_send_errno is not None:
            err = self.fail_send_errno
            self.fail_send_errno = None
            raise OSError(err, "eagain")
        buf = bytes(data)
        n = len(buf) if self.send_window is None else min(len(buf), self.send_window)
        if n == 0:
            return 0
        self.sent += buf[:n]
        return n

    def recv(self, n):
        if not self.recv_slices:
            if self.closed:
                return b""
            return None
        piece = self.recv_slices.pop(0)
        if piece is None:
            raise OSError(11, "eagain")
        if piece == b"":
            self.closed = True
            return b""
        if len(piece) > n:
            self.recv_slices.insert(0, piece[n:])
            piece = piece[:n]
        return piece

    def close(self):
        self.closed = True


def _accept(key, extra=b""):
    digest = base64.b64encode(hashlib.sha1(key + ws_client._WS_GUID).digest())
    head = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + digest + b"\r\n\r\n"
    )
    return head + extra


def _feed_accept(sock, splits, extra=b""):
    # connect() writes the request into sock.sent before we can answer, so the
    # test peeks the key after connect returns. This helper is for preloading
    # only when the key is already known.
    sock.recv_slices.extend(splits)


def _client(sock, **kw):
    defaults = dict(
        host="server.invalid",
        port=9,
        path="/ws/camera",
        max_queue=3,
        connect_timeout_ms=200,
        handshake_timeout_ms=200,
        send_stall_ms=50,
        send_chunk=5,
        sock_factory=lambda: sock,
    )
    defaults.update(kw)
    return WSClient(**defaults)


def _connect(sock, **kw):
    c = _client(sock, **kw)
    # The handshake reads after the request is sent. Prepend the 101 once the
    # key is on the wire by wrapping recv.
    real_recv = sock.recv

    def recv(n):
        if not getattr(sock, "_armed", False):
            blob = bytes(sock.sent)
            key = blob.split(b"Sec-WebSocket-Key: ", 1)[1].split(b"\r\n", 1)[0]
            extra = getattr(sock, "extra", b"")
            parts = getattr(sock, "parts", None)
            payload = _accept(key, extra)
            if parts:
                sock.recv_slices = _split(payload, parts) + sock.recv_slices
            else:
                sock.recv_slices = [payload] + sock.recv_slices
            sock._armed = True
        return real_recv(n)

    sock.recv = recv
    ok = c.connect()
    return c, ok


def _split(data, n):
    return [data[i:i + n] for i in range(0, len(data), n)] or [b""]


def _unmask_frames(blob):
    frames = []
    i = 0
    while i + 2 <= len(blob):
        b1 = blob[i]
        b2 = blob[i + 1]
        ln = b2 & 0x7F
        masked = (b2 & 0x80) != 0
        i += 2
        if ln == 126:
            ln = int.from_bytes(blob[i:i + 2], "big")
            i += 2
        elif ln == 127:
            ln = int.from_bytes(blob[i:i + 8], "big")
            i += 8
        mask = b"\0\0\0\0"
        if masked:
            mask = bytes(blob[i:i + 4])
            i += 4
        payload = apply_mask(blob[i:i + ln], mask, 0)
        i += ln
        frames.append((b1 & 0x0F, (b1 & 0x80) != 0, payload))
    return frames


class MaskTests(unittest.TestCase):
    def test_phase_independent_of_chunk_length(self):
        data = bytes(range(256)) * 3 + b"\x10\x20\x30"
        mask = b"\x11\x22\x33\x44"
        whole = apply_mask(data, mask, 0)
        for chunk in (1, 3, 4, 5, 7, 4096):
            out = bytearray()
            off = 0
            while off < len(data):
                part = data[off:off + chunk]
                out += apply_mask(part, mask, off)
                off += chunk
            self.assertEqual(bytes(out), whole)

    def test_matches_byte_xor_and_stdlib_when_present(self):
        data = os.urandom(30000)
        mask = os.urandom(4)
        expect = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self.assertEqual(apply_mask(data, mask, 0), expect)
        try:
            from websocket._abnf import _mask as c_mask
        except Exception:
            c_mask = None
        if c_mask is not None:
            self.assertEqual(apply_mask(data, mask, 0), bytes(c_mask(data, mask)))
            # stdlib helper has no phase; our offset path must still be right.
            phased = apply_mask(data[1:], mask, 1)
            expect_p = bytes(b ^ mask[(i + 1) & 3] for i, b in enumerate(data[1:]))
            self.assertEqual(phased, expect_p)

    def test_rejects_bad_mask(self):
        self.assertEqual(apply_mask(b"", b"abcd"), b"")
        with self.assertRaises(ValueError):
            apply_mask(b"a", b"ab")


class WebSocketTests(unittest.TestCase):
    def test_handshake_split_and_accept(self):
        sock = MemSock()
        sock.parts = 7
        sock.extra = b""
        c, ok = _connect(sock)
        self.assertTrue(ok)
        self.assertTrue(c.connected)
        sent = bytes(sock.sent)
        self.assertIn(b"GET /ws/camera HTTP/1.1\r\n", sent)
        self.assertIn(b"Sec-WebSocket-Version: 13\r\n", sent)
        self.assertNotIn(b"Sec-WebSocket-Accept", sent)

    def test_handshake_rejects_bad_accept(self):
        sock = MemSock(recv_slices=[
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Sec-WebSocket-Accept: AAAA\r\n\r\n"
        ])
        c = _client(sock)
        self.assertFalse(c.connect())
        self.assertFalse(c.connected)
        self.assertTrue(sock.closed)

    def test_partial_send_and_mask_across_chunks(self):
        sock = MemSock()
        sock.parts = 3
        sock.send_window = 3
        c, ok = _connect(sock)
        self.assertTrue(ok)
        before = len(sock.sent)
        payload = bytes([i & 0xFF for i in range(50)])
        self.assertTrue(c.queue_binary(payload, latest=False))
        for _ in range(40):
            c.poll()
        frames = _unmask_frames(bytes(sock.sent)[before:])
        self.assertEqual(frames, [(OP_BIN, True, payload)])
        self.assertEqual(c.stats["sent"], 1)

    def test_send_zero_drops_link(self):
        sock = MemSock()
        c, ok = _connect(sock)
        self.assertTrue(ok)
        sock.send_window = 0
        self.assertTrue(c.queue_text("START"))
        c.poll()
        self.assertFalse(c.connected)
        self.assertGreaterEqual(c.stats["send_fail"], 1)

    def test_eagain_then_success(self):
        sock = MemSock()
        c, ok = _connect(sock)
        sock.fail_send_errno = 11
        before = len(sock.sent)
        self.assertTrue(c.queue_text("PINGME"))
        c.poll()  # EAGAIN, still connected
        self.assertTrue(c.connected)
        c.poll()
        frames = _unmask_frames(bytes(sock.sent)[before:])
        self.assertEqual(frames[0][2], b"PINGME")

    def test_text_not_dropped_video_latest_is(self):
        sock = MemSock()
        c, ok = _connect(sock, max_queue=2)
        self.assertTrue(ok)
        sock.send_window = 0
        self.assertTrue(c.queue_binary(b"old-frame-aaaa", latest=True))
        self.assertTrue(c.queue_binary(b"newer-frame-bb", latest=True))
        # Second latest replaces the first unsent video frame.
        self.assertEqual(c.stats["dropped"], 1)
        self.assertTrue(c.queue_text("SET:NO"))
        # Queue is full of [video, text]. Another video is rejected, text drops video.
        self.assertFalse(c.queue_binary(b"third", latest=False))
        self.assertTrue(c.queue_text("STAT"))
        kinds = [item.opcode for item in c._queue]
        self.assertEqual(kinds, [OP_TEXT, OP_TEXT])

    def test_ping_pong_and_fragment_text(self):
        sock = MemSock()
        c, ok = _connect(sock)
        self.assertTrue(ok)
        before = len(sock.sent)
        ping = bytes([0x89, 0x03]) + b"abc"  # server ping, unmasked
        part1 = bytes([0x01, 0x03]) + b"SET"
        part2 = bytes([0x80, 0x03]) + b":OK"
        sock.recv_slices = _split(ping + part1 + part2, 2)
        texts = []
        for _ in range(30):
            texts.extend(c.poll())
        self.assertEqual(texts, ["SET:OK"])
        frames = _unmask_frames(bytes(sock.sent)[before:])
        self.assertTrue(any(op == 0xA and payload == b"abc" for op, _fin, payload in frames))

    def test_close_and_oversize(self):
        sock = MemSock()
        c, ok = _connect(sock, max_message=8)
        sock.recv_slices = [bytes([0x82, 0x09]) + b"012345678"]
        c.poll()
        self.assertFalse(c.connected)
        c2, ok2 = _connect(MemSock(), max_message=4)
        self.assertTrue(ok2)
        self.assertFalse(c2.queue_binary(b"12345"))
        self.assertEqual(c2.stats["rejected"], 1)

    def test_binary_server_frame_ignored_text_returned(self):
        sock = MemSock()
        c, ok = _connect(sock)
        frame = bytes([0x82, 0x02]) + b"JJ"
        text = bytes([0x81, 0x05]) + b"HELLO"
        sock.recv_slices = [frame + text]
        self.assertEqual(c.poll(), ["HELLO"])
        self.assertTrue(c.connected)

    def test_poll_when_disconnected_is_empty(self):
        c = WSClient("server.invalid", 9, "/ws_audio")
        self.assertEqual(c.poll(), [])
        self.assertFalse(c.queue_text("START"))

    def test_server_close_and_masked_text(self):
        sock = MemSock()
        c, ok = _connect(sock)
        self.assertTrue(ok)
        mask = b"\x01\x02\x03\x04"
        payload = apply_mask(b"OK", mask, 0)
        frame = bytes([0x81, 0x80 | 2]) + mask + payload
        close = bytes([0x88, 0x02, 0x03, 0xE8])
        sock.recv_slices = [frame, close]
        self.assertEqual(c.poll(), ["OK"])
        c.poll()
        self.assertFalse(c.connected)

    def test_close_writes_masked_close_frame(self):
        sock = MemSock()
        c, ok = _connect(sock)
        before = len(sock.sent)
        c.close()
        frames = _unmask_frames(bytes(sock.sent)[before:])
        self.assertEqual(frames[0][0], 0x8)
        self.assertEqual(frames[0][2], (1000).to_bytes(2, "big"))
        self.assertFalse(c.connected)

    def test_connect_tolerates_einprogress(self):
        sock = MemSock()
        real = sock.connect

        def connect(addr):
            raise OSError(115, "in progress")

        sock.connect = connect
        c, ok = _connect(sock, connect_timeout_ms=200)
        self.assertTrue(ok)
        self.assertTrue(c.connected)


def _wav_header(rate=8000, channels=1, bits=16, extra=b"", data=b""):
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * bits // 8,
                       channels * bits // 8, bits)
    chunks = b"fmt " + struct.pack("<I", 16) + fmt
    if extra:
        chunks += b"LIST" + struct.pack("<I", len(extra)) + extra
        if len(extra) & 1:
            chunks += b"\x00"
    chunks += b"data" + struct.pack("<I", len(data) if data else 0x7FFFFFF0)
    riff = b"RIFF" + struct.pack("<I", 4 + len(chunks) + len(data)) + b"WAVE" + chunks
    return riff + data


def _http(body, status=b"200 OK", chunked=False, splits=1):
    if chunked:
        raw = b""
        # one byte per chunk to force short reads, plus a final grouped chunk
        step = max(1, splits)
        i = 0
        while i < len(body):
            part = body[i:i + step]
            raw += (b"%x" % len(part)) + b"\r\n" + part + b"\r\n"
            i += step
        head = (
            b"HTTP/1.1 " + status + b"\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        return head + raw
    head = (
        b"HTTP/1.1 " + status + b"\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
    )
    return head + body


class WavTests(unittest.TestCase):
    def _stream(self, blob, splits=1, **kw):
        sock = MemSock(recv_slices=_split(blob, splits))
        w = WavStream("server.invalid", 9, sock_factory=lambda: sock,
                      header_timeout_ms=500, connect_timeout_ms=200, **kw)
        self.assertTrue(w.connect())
        return w, sock

    def test_content_length_short_reads_and_extra_chunk(self):
        pcm = struct.pack("<8h", 1, -1, 2, -2, 3, -3, 4, -4)
        extra = b"INFO"
        body = _wav_header(extra=extra, data=pcm)
        blob = _http(body)
        w, _sock = self._stream(blob, splits=3, max_buffer=3200)
        got = bytearray()
        for _ in range(50):
            w.poll()
            got += w.read_pcm(4)
        self.assertTrue(w.format_ok)
        self.assertEqual(bytes(got), pcm)
        self.assertEqual(w.stats["sample_rate"], 8000)
        self.assertEqual(w.stats["dropped_bytes"], 0)

    def test_chunked_keepalive_silence_does_not_drop(self):
        silence = b"\x00" * 320
        body = _wav_header() + silence
        # Even chunk size so a 16-bit sample is not left pending.
        blob = _http(body, chunked=True, splits=4)
        now = {"t": 0}
        ws_client._clock = lambda: now["t"]
        try:
            w, sock = self._stream(blob, splits=4, silence_timeout_ms=1000)
            for _ in range(400):
                now["t"] += 10
                w.poll()
                if not sock.recv_slices and w.format_ok and w.stats["buffered"] >= 320:
                    break
            self.assertTrue(w.connected)
            self.assertGreaterEqual(w.stats["silence_ms"], 20)
            now["t"] += 6000
            w.poll()
            self.assertTrue(w.connected)
            self.assertEqual(w.read_pcm(320), silence)
        finally:
            ws_client._clock = None

    def test_buffer_drops_oldest(self):
        pcm = b"\x01\x00" * 1000  # 2000 bytes
        body = _wav_header(data=pcm)
        w, _sock = self._stream(_http(body), splits=9, max_buffer=400)
        for _ in range(30):
            w.poll()
        self.assertLessEqual(w.stats["buffered"], 400)
        self.assertGreater(w.stats["dropped_bytes"], 0)
        out = bytearray()
        while True:
            piece = w.read_pcm(320)
            if not piece:
                break
            out += piece
        self.assertTrue(pcm.endswith(bytes(out)))
        self.assertLess(len(out), len(pcm))

    def test_rejects_stereo_and_bad_status(self):
        bad = _http(_wav_header(channels=2, data=b"\x00\x00"))
        w, _sock = self._stream(bad, splits=6)
        for _ in range(20):
            w.poll()
        self.assertFalse(w.connected)
        self.assertFalse(w.format_ok)

        sock = MemSock(recv_slices=[b"HTTP/1.1 404 Nope\r\nContent-Length: 0\r\n\r\n"])
        w2 = WavStream("server.invalid", 9, sock_factory=lambda: sock)
        self.assertTrue(w2.connect())
        w2.poll()
        self.assertFalse(w2.connected)

    def test_read_pcm_nonblocking_and_odd_split(self):
        pcm = struct.pack("<h", 0x1234) * 3
        body = _wav_header(data=pcm)
        # Chunk size 1 forces an odd byte between polls.
        blob = _http(body, chunked=True, splits=1)
        w, _sock = self._stream(blob, splits=2, max_buffer=3200)
        got = bytearray()
        empty = 0
        for _ in range(80):
            w.poll()
            piece = w.read_pcm(max_bytes=320)
            if piece:
                got += piece
            else:
                empty += 1
        self.assertEqual(bytes(got), pcm)
        self.assertGreater(empty, 0)

    def test_nonsilent_stall_drops(self):
        tone = b"\x01\x00" * 160
        body = _wav_header(data=tone)
        # No Content-Length: a finite response must stay open so the stall
        # watchdog, not EOF, is what drops a non-silent stream.
        head = b"HTTP/1.1 200 OK\r\n\r\n" + body
        now = {"t": 0}
        ws_client._clock = lambda: now["t"]
        try:
            w, sock = self._stream(head, splits=8, silence_timeout_ms=1000)
            for _ in range(400):
                now["t"] += 10
                w.poll()
                if not sock.recv_slices and w.stats["buffered"] >= 320:
                    break
            self.assertTrue(w.connected)
            self.assertGreater(w.stats["buffered"], 0)
            now["t"] += 6000
            w.poll()
            self.assertTrue(w.connected)
            now["t"] += 10000
            w.poll()
            self.assertFalse(w.connected)
        finally:
            ws_client._clock = None

    def test_terminal_chunk_disconnects(self):
        body = _wav_header(data=b"\x02\x00")
        blob = _http(body, chunked=True, splits=4) + b"0\r\n\r\n"
        w, _sock = self._stream(blob, splits=5)
        for _ in range(40):
            w.poll()
        self.assertFalse(w.connected)
        self.assertEqual(w.read_pcm(320), b"\x02\x00")


if __name__ == "__main__":
    unittest.main()
