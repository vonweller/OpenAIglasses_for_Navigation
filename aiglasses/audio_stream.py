# audio_stream.py
# -*- coding: utf-8 -*-
import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from fastapi import Request
from fastapi.responses import StreamingResponse

STREAM_SR = 8000
STREAM_CH = 1
STREAM_SW = 2
BYTES_PER_20MS_16K = STREAM_SR * STREAM_SW * 20 // 1000
STREAM_IDLE_SILENCE = b"\x00" * BYTES_PER_20MS_16K
STREAM_QUEUE_MAX = int(os.getenv("AIGLASS_STREAM_QUEUE_MAX", "160"))
STREAM_PREROLL_CHUNKS = max(0, int(os.getenv("AIGLASS_STREAM_PREROLL_CHUNKS", "10")))

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


stream_clients: "Set[StreamClient]" = set()
last_broadcast_at: Optional[float] = None
last_broadcast_bytes: int = 0
total_broadcast_bytes: int = 0


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

    if reason:
        print(f"[HARD-RESET] {reason}")


async def soft_reset_audio(reason: str = ""):
    await cancel_current_ai()
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
    if reason:
        print(f"[SOFT-RESET] {reason}")


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

    loop = asyncio.get_event_loop()
    next_tick = loop.time()
    off = 0
    while off < len(pcm16):
        take = min(BYTES_PER_20MS_16K, len(pcm16) - off)
        piece = pcm16[off : off + take]

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
            try:
                stream_clients.discard(sc)
            except Exception:
                pass

        next_tick += 0.020
        now = loop.time()
        if now < next_tick:
            await asyncio.sleep(next_tick - now)
        else:
            next_tick = now
        off += take


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
        sc = StreamClient(q=q, abort_event=abort_event)
        stream_clients.add(sc)

        async def gen():
            yield _wav_header_unknown_size(STREAM_SR, STREAM_CH, STREAM_SW)
            try:
                for _ in range(STREAM_PREROLL_CHUNKS):
                    if abort_event.is_set():
                        return
                    yield STREAM_IDLE_SILENCE
                    await asyncio.sleep(0.020)

                while True:
                    if abort_event.is_set():
                        break
                    try:
                        chunk = await asyncio.wait_for(q.get(), timeout=0.020)
                    except asyncio.TimeoutError:
                        chunk = STREAM_IDLE_SILENCE
                    if abort_event.is_set():
                        break
                    if chunk is None:
                        continue
                    yield chunk or STREAM_IDLE_SILENCE
            finally:
                stream_clients.discard(sc)

        return StreamingResponse(gen(), media_type="audio/wav")
