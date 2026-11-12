"""Bounded, ordered PCM delivery without blocking the FastAPI event loop."""

import asyncio
import audioop
import inspect
import math
import time
from collections import deque
from typing import Callable, Optional


class PcmFramer:
    def __init__(self, sample_rate: int = 16000, chunk_ms: int = 20):
        self.frame_bytes = sample_rate * chunk_ms // 1000 * 2
        self.pending = bytearray()
        self.messages = 0
        self.fragmented_messages = 0
        self.frames = 0
        self.bytes_received = 0
        self.muted_frames = 0
        self.last_received = 0.0
        self.max_gap_ms = 0.0
        self.rms = 0
        self.peak = 0
        self.dc = 0
        self._levels = deque(maxlen=50)

    def feed(self, data: bytes, muted: bool = False, now: Optional[float] = None) -> list:
        if not data:
            return []
        now = time.monotonic() if now is None else now
        if self.last_received:
            self.max_gap_ms = max(self.max_gap_ms, (now - self.last_received) * 1000)
        self.last_received = now
        self.messages += 1
        self.bytes_received += len(data)
        if len(data) != self.frame_bytes:
            self.fragmented_messages += 1
        self.pending.extend(data)
        frames = []
        while len(self.pending) >= self.frame_bytes:
            pcm = bytes(self.pending[:self.frame_bytes])
            del self.pending[:self.frame_bytes]
            self.rms = audioop.rms(pcm, 2)
            self.peak = audioop.max(pcm, 2)
            self.dc = audioop.avg(pcm, 2)
            self._levels.append(self.rms)
            self.frames += 1
            if muted:
                self.muted_frames += 1
                pcm = bytes(self.frame_bytes)
            frames.append(pcm)
        return frames

    def snapshot(self) -> dict:
        rms = sum(self._levels) / max(1, len(self._levels))
        return {
            "pcm_frames": self.frames, "pcm_bytes": self.bytes_received,
            "fragmented_messages": self.fragmented_messages,
            "pending_bytes": len(self.pending), "muted_frames": self.muted_frames,
            "input_rms": round(rms, 1), "input_dbfs": round(20 * math.log10(max(1, rms) / 32768), 1),
            "input_peak": self.peak, "input_dc": self.dc,
            "max_packet_gap_ms": round(self.max_gap_ms, 1),
        }


class OrderedAudioSender:
    def __init__(self, send: Callable[[bytes], None], on_error: Callable,
                 frame_bytes: int = 640, max_frames: int = 25,
                 idle_seconds: float = 0.5, offload: bool = True):
        self.send = send
        self.offload = offload
        self.on_error = on_error
        self.frame_bytes = frame_bytes
        self.queue = asyncio.Queue(maxsize=max_frames)
        self.idle_seconds = idle_seconds
        self.dropped_frames = 0
        self.sent_frames = 0
        self.keepalive_frames = 0
        self.last_send_ms = 0.0
        self.task = None
        self.closed = False

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    def offer(self, pcm: bytes) -> None:
        if self.closed:
            return
        if len(pcm) != self.frame_bytes:
            raise ValueError("ASR frames must have exactly %d bytes" % self.frame_bytes)
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped_frames += 1
        self.queue.put_nowait(pcm)

    async def _run(self) -> None:
        try:
            while not self.closed:
                try:
                    pcm = await asyncio.wait_for(self.queue.get(), timeout=self.idle_seconds)
                    keepalive = False
                except asyncio.TimeoutError:
                    pcm = bytes(self.frame_bytes)
                    keepalive = True
                started = time.monotonic()
                if self.offload:
                    await asyncio.to_thread(self.send, pcm)
                else:
                    self.send(pcm)
                self.last_send_ms = (time.monotonic() - started) * 1000
                if keepalive:
                    self.keepalive_frames += 1
                else:
                    self.sent_frames += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = self.on_error(str(exc))
            if inspect.isawaitable(result):
                await result

    async def close(self) -> None:
        self.closed = True
        task, self.task = self.task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def snapshot(self) -> dict:
        return {
            "asr_sent_frames": self.sent_frames,
            "asr_queue_frames": self.queue.qsize(),
            "asr_dropped_frames": self.dropped_frames,
            "keepalive_frames": self.keepalive_frames,
            "asr_send_ms": round(self.last_send_ms, 1),
        }
