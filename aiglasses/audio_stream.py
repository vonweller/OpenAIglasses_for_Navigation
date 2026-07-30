# audio_stream.py
# -*- coding: utf-8 -*-
import asyncio
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from fastapi import Request
from fastapi.responses import StreamingResponse

STREAM_SR = 8000
STREAM_CH = 1
STREAM_SW = 2
BYTES_PER_20MS_16K = STREAM_SR * STREAM_SW * 20 // 1000
STREAM_IDLE_SILENCE = b"\x00" * BYTES_PER_20MS_16K
STREAM_QUEUE_MAX = int(os.getenv("AIGLASS_STREAM_QUEUE_MAX", "240"))
STREAM_PREROLL_CHUNKS = max(0, int(os.getenv("AIGLASS_STREAM_PREROLL_CHUNKS", "3")))
STREAM_START_BUFFER_CHUNKS = max(1, int(os.getenv("AIGLASS_STREAM_START_BUFFER_CHUNKS", "8")))
STREAM_REBUFFER_CHUNKS = max(1, int(os.getenv("AIGLASS_STREAM_REBUFFER_CHUNKS", "4")))
STREAM_KEEPALIVE_SEC = max(1.0, float(os.getenv("AIGLASS_STREAM_KEEPALIVE_SEC", "5")))

current_ai_task: Optional[asyncio.Task] = None


async def cancel_current_ai():
    global current_ai_task
    task = current_ai_task
    current_ai_task = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


def is_playing_now() -> bool:
    task = current_ai_task
    return task is not None and not task.done()


@dataclass(frozen=True)
class StreamClient:
    q: asyncio.Queue
    abort_event: asyncio.Event
    flush_event: asyncio.Event


stream_clients: "Set[StreamClient]" = set()
last_broadcast_at: Optional[float] = None
last_broadcast_bytes: int = 0
total_broadcast_bytes: int = 0
_pcm_buffer_lock = threading.Lock()
_pending_pcm16 = bytearray()


def _wav_header_unknown_size(sr=STREAM_SR, ch=STREAM_CH, sw=STREAM_SW) -> bytes:
    import struct

    byte_rate = sr * ch * sw
    block_align = ch * sw
    data_size = 0x7FFFFFF0
    riff_size = 36 + data_size
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        ch,
        sr,
        byte_rate,
        block_align,
        sw * 8,
        b"data",
        data_size,
    )


async def hard_reset_audio(reason: str = ""):
    for sc in list(stream_clients):
        try:
            sc.abort_event.set()
        except Exception:
            pass
    stream_clients.clear()

    await cancel_current_ai()
    with _pcm_buffer_lock:
        _pending_pcm16.clear()

    if reason:
        print(f"[HARD-RESET] {reason}")


async def soft_reset_audio(reason: str = ""):
    await cancel_current_ai()
    with _pcm_buffer_lock:
        _pending_pcm16.clear()
    for sc in list(stream_clients):
        if sc.abort_event.is_set():
            continue
        try:
            while True:
                sc.q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        except Exception:
            pass
        sc.flush_event.clear()
    if reason:
        print(f"[SOFT-RESET] {reason}")


def _enqueue_pcm_piece(piece: bytes) -> None:
    if not piece:
        return
    dead: List[StreamClient] = []
    for sc in list(stream_clients):
        if sc.abort_event.is_set():
            dead.append(sc)
            continue
        try:
            if sc.q.full():
                try:
                    sc.q.get_nowait()
                except Exception:
                    pass
            sc.q.put_nowait(piece)
        except Exception:
            dead.append(sc)
    for sc in dead:
        stream_clients.discard(sc)


async def broadcast_pcm16_realtime(pcm16: bytes):
    global last_broadcast_at, last_broadcast_bytes, total_broadcast_bytes
    if pcm16:
        import time

        last_broadcast_at = time.time()
        last_broadcast_bytes = len(pcm16)
        total_broadcast_bytes += len(pcm16)

    try:
        from . import sync_recorder

        sync_recorder.record_audio(pcm16, text="[Omni对话]")
    except Exception:
        pass

    # Network fragments rarely align to 20 ms. Preserve the remainder instead
    # of padding each fragment with silence, which caused audible dropouts.
    ready: List[bytes] = []
    with _pcm_buffer_lock:
        _pending_pcm16.extend(pcm16)
        while len(_pending_pcm16) >= BYTES_PER_20MS_16K:
            ready.append(bytes(_pending_pcm16[:BYTES_PER_20MS_16K]))
            del _pending_pcm16[:BYTES_PER_20MS_16K]
    for piece in ready:
        _enqueue_pcm_piece(piece)


async def finish_pcm16_stream() -> None:
    """Flush the final partial audio frame once at the end of a response."""
    with _pcm_buffer_lock:
        piece = bytes(_pending_pcm16)
        _pending_pcm16.clear()
    if piece and len(piece) < BYTES_PER_20MS_16K:
        piece += b"\x00" * (BYTES_PER_20MS_16K - len(piece))
    if piece:
        _enqueue_pcm_piece(piece)
    for sc in list(stream_clients):
        if not sc.abort_event.is_set():
            sc.flush_event.set()


def get_stream_status() -> Dict[str, Any]:
    import time

    age = None if not last_broadcast_at else max(0.0, time.time() - last_broadcast_at)
    return {
        "clients": len(stream_clients),
        "last_broadcast_age_sec": age,
        "last_broadcast_bytes": last_broadcast_bytes,
        "total_broadcast_bytes": total_broadcast_bytes,
    }


def register_stream_route(app):
    @app.get("/stream.wav")
    async def stream_wav(_: Request):
        for sc in list(stream_clients):
            try:
                sc.abort_event.set()
            except Exception:
                pass
        stream_clients.clear()

        q: "asyncio.Queue[bytes | None]" = asyncio.Queue(maxsize=STREAM_QUEUE_MAX)
        abort_event = asyncio.Event()
        flush_event = asyncio.Event()
        sc = StreamClient(q=q, abort_event=abort_event, flush_event=flush_event)
        stream_clients.add(sc)

        async def gen():
            yield _wav_header_unknown_size(STREAM_SR, STREAM_CH, STREAM_SW)
            try:
                for _ in range(STREAM_PREROLL_CHUNKS):
                    if abort_event.is_set():
                        return
                    yield STREAM_IDLE_SILENCE
                    await asyncio.sleep(0.020)

                # 等待积累少量真实语音，再以固定 20ms 节拍播放，抵御上游分片抖动。
                buffered = False
                started_once = False
                next_tick = asyncio.get_running_loop().time()
                last_yield_at = next_tick
                while True:
                    if abort_event.is_set():
                        break
                    if flush_event.is_set() and q.empty():
                        flush_event.clear()
                        buffered = False
                    if not buffered:
                        target = (
                            1
                            if flush_event.is_set()
                            else (STREAM_REBUFFER_CHUNKS if started_once else STREAM_START_BUFFER_CHUNKS)
                        )
                        if q.qsize() >= target:
                            buffered = True
                            started_once = True
                            next_tick = asyncio.get_running_loop().time()
                        else:
                            # Do not shield Event.wait() here: every timeout would
                            # leave an orphan task and eventually stall the server.
                            now = asyncio.get_running_loop().time()
                            if now - last_yield_at >= STREAM_KEEPALIVE_SEC:
                                yield STREAM_IDLE_SILENCE
                                last_yield_at = now
                            await asyncio.sleep(0.010)
                            continue
                    try:
                        chunk = q.get_nowait()
                    except asyncio.QueueEmpty:
                        buffered = False
                        continue
                    if abort_event.is_set():
                        break
                    if chunk is None:
                        continue
                    yield chunk or STREAM_IDLE_SILENCE
                    last_yield_at = asyncio.get_running_loop().time()
                    next_tick += 0.020
                    delay = next_tick - asyncio.get_running_loop().time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    else:
                        next_tick = asyncio.get_running_loop().time()
                    if q.empty() and not flush_event.is_set():
                        buffered = False
            finally:
                stream_clients.discard(sc)

        return StreamingResponse(gen(), media_type="audio/wav")
