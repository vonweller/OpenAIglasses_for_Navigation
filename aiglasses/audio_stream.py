# audio_stream.py
# -*- coding: utf-8 -*-
import asyncio
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from fastapi import Request
from fastapi.responses import StreamingResponse

from .playback_clock import PlaybackClock

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

PLAYBACK_SERVER = "server"
PLAYBACK_ESP32 = "esp32"
PLAYBACK_BOTH = "both"
DEFAULT_PLAYBACK_TARGET = PLAYBACK_SERVER

current_ai_task: Optional[asyncio.Task] = None
_playback_target = DEFAULT_PLAYBACK_TARGET
_local_player_lock = threading.Lock()
_local_thread_lock = threading.Lock()
_local_player = None
_local_player_error = ""
_local_pcm_queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=240)
_local_player_thread: Optional[threading.Thread] = None
_local_player_stop = threading.Event()
_local_player_autostart = True
_last_audible_output_at = 0.0
# 本地喇叭按 20ms 节拍播放。入队时按字节预估结束时刻，避免把「队列非空」
# 当成整段仍在响，也避免 server+device 两路把同一段 PCM 的时长加两次。
_local_play_until = 0.0
_local_play_lock = threading.Lock()
MIC_MUTE_HANGOVER_SEC = max(0.0, float(os.getenv("AIGLASS_MIC_MUTE_HANGOVER_SEC", "0.35")))


def normalize_playback_target(value: Optional[str]) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("both", "all", "dual"):
        return PLAYBACK_BOTH
    if raw in ("esp32", "device", "glasses", "speaker", "xiao", "k230"):
        return PLAYBACK_ESP32
    return PLAYBACK_SERVER


def get_playback_target() -> str:
    return _playback_target


def _targets_server(target: Optional[str] = None) -> bool:
    chosen = _playback_target if target is None else target
    return chosen in (PLAYBACK_SERVER, PLAYBACK_BOTH)


def _targets_device(target: Optional[str] = None) -> bool:
    chosen = _playback_target if target is None else target
    return chosen in (PLAYBACK_ESP32, PLAYBACK_BOTH)


def _pcm_duration_sec(piece: bytes) -> float:
    """8 kHz mono PCM16 的播放时长。空或非偶数字节不计。"""
    if not piece:
        return 0.0
    samples = len(piece) // STREAM_SW
    if samples <= 0:
        return 0.0
    return samples / float(STREAM_SR)


def _schedule_local_play(piece: bytes) -> None:
    """把本机队列里新增的 PCM 接到当前预计结束时刻之后。

    只累加本机这一路。device 队列是同一批样本的另一份拷贝，不能再加一次。
    """
    global _local_play_until
    duration = _pcm_duration_sec(piece)
    if duration <= 0.0 or not _pcm_has_signal(piece):
        return
    now = time.monotonic()
    with _local_play_lock:
        start = _local_play_until if _local_play_until > now else now
        _local_play_until = start + duration


def _clear_local_play_schedule() -> None:
    global _local_play_until
    with _local_play_lock:
        _local_play_until = 0.0


def local_play_remaining_sec() -> float:
    with _local_play_lock:
        until = _local_play_until
    return max(0.0, until - time.monotonic())


def _stop_local_player_thread() -> None:
    global _local_player_thread
    _local_player_stop.set()
    try:
        _local_pcm_queue.put_nowait(None)
    except Exception:
        pass
    thread = _local_player_thread
    _local_player_thread = None
    if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
        thread.join(timeout=1.0)
    while True:
        try:
            _local_pcm_queue.get_nowait()
        except queue.Empty:
            break
    _clear_local_play_schedule()


def _close_local_player() -> None:
    global _local_player
    with _local_player_lock:
        player = _local_player
        _local_player = None
    if player is None:
        return
    try:
        player.stop()
    except Exception:
        pass
    try:
        player.close()
    except Exception:
        pass


def _ensure_local_player():
    global _local_player, _local_player_error
    with _local_player_lock:
        if _local_player is not None:
            return _local_player
        try:
            import sounddevice as sd

            player = sd.RawOutputStream(
                samplerate=STREAM_SR,
                channels=STREAM_CH,
                dtype="int16",
                blocksize=BYTES_PER_20MS_16K // STREAM_SW,
                latency="low",
            )
            player.start()
            _local_player = player
            _local_player_error = ""
            print("[AUDIO] 本机扬声器已打开（8kHz PCM16）", flush=True)
            return player
        except Exception as exc:
            _local_player = None
            _local_player_error = str(exc)
            print(f"[AUDIO] 本机扬声器打开失败: {exc}", flush=True)
            return None


def _local_player_loop() -> None:
    global _local_written_bytes, _local_underruns
    while not _local_player_stop.is_set():
        try:
            chunk = _local_pcm_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if chunk is None:
            continue
        player = _ensure_local_player()
        if player is None:
            continue
        try:
            underflow = player.write(chunk)
            _local_written_bytes += len(chunk)
            _local_underruns += int(bool(underflow))
        except Exception as exc:
            print(f"[AUDIO] 本机播放失败: {exc}", flush=True)
            _close_local_player()
            continue
        # 入队时已经把这段时长算进 _local_play_until。这里只给真实写出的
        # 非零样本打尾音点，静音垫片和未写出的排队数据都不打。
        _mark_audible_output(chunk)


def _ensure_local_player_thread() -> None:
    global _local_player_thread
    with _local_thread_lock:
        if _local_player_thread is not None and _local_player_thread.is_alive():
            return
        _local_player_stop.clear()
        _local_player_thread = threading.Thread(
            target=_local_player_loop,
            name="local-speaker",
            daemon=True,
        )
        _local_player_thread.start()


def _enqueue_local_pcm(piece: bytes) -> None:
    if not piece or not _targets_server():
        return
    # 测试把 _local_player_autostart 设为 False，只入队、不打开声卡。
    if _local_player_autostart:
        _ensure_local_player_thread()
    # Speech is ordered content, not a latest-video-frame buffer. A full queue
    # must apply backpressure in the producer instead of removing the beginning.
    _local_pcm_queue.put_nowait(piece)
    _schedule_local_play(piece)


def _unschedule_local_play(piece: bytes) -> None:
    """队列溢出丢掉的帧不再占用本机播放时间。"""
    global _local_play_until
    duration = _pcm_duration_sec(piece)
    if duration <= 0.0:
        return
    now = time.monotonic()
    with _local_play_lock:
        _local_play_until = max(now, _local_play_until - duration)


def set_playback_target(value: Optional[str]) -> str:
    global _playback_target
    target = normalize_playback_target(value)
    previous = _playback_target
    if previous != target:
        _invalidate_pcm()
    _playback_target = target
    os.environ["AIGLASS_PLAYBACK_TARGET"] = target
    # both 与 server 都要本机喇叭。只有切到纯 device 才停本地线程。
    if not _targets_server(target):
        _stop_local_player_thread()
        _close_local_player()
    elif _local_player_autostart and not _targets_server(previous):
        _ensure_local_player_thread()
    if previous != target:
        print(f"[AUDIO] playback target -> {target}", flush=True)
    return target


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


def _pcm_has_signal(piece: bytes) -> bool:
    return bool(piece) and any(piece)


def _mark_audible_output(piece: bytes) -> None:
    global _last_audible_output_at
    if _pcm_has_signal(piece):
        _last_audible_output_at = time.time()


def is_output_playing(hangover_sec: Optional[float] = None) -> bool:
    """电脑喇叭或 /stream.wav 正在出真实声音（含短尾音）。

    全零保活、空队列、以及已经播完的排队字节都不算在播。
    K230 放在电脑旁时两只喇叭都会回灌，所以 server 与 device 都计入。
    本地一路用预计结束时刻，不用「队列非空」；device 一路只把含非零样本的
    排队字节算进剩余时长，两路不把同一段 PCM 的时长加两次。
    """
    hangover = MIC_MUTE_HANGOVER_SEC if hangover_sec is None else max(0.0, float(hangover_sec))
    if local_play_remaining_sec() > 0.0:
        return True
    if _device_queued_audible_sec() > 0.0:
        return True
    try:
        from .audio_player import is_voice_playing

        if is_voice_playing():
            return True
    except Exception:
        pass
    if _last_audible_output_at and (time.time() - _last_audible_output_at) < hangover:
        return True
    return False


def _device_queued_audible_sec() -> float:
    """尚未写进 /stream.wav 的非零 PCM 时长。全零保活帧不计。"""
    total = 0.0
    for sc in list(stream_clients):
        try:
            pending = list(sc.q._queue)
        except Exception:
            continue
        for piece in pending:
            if piece and _pcm_has_signal(piece):
                total += _pcm_duration_sec(piece)
    return total


def playback_timing() -> Dict[str, Any]:
    """给状态接口和测试用的时间/队列统计。时长只计本机预计值与设备有声排队。"""
    local_queued = 0
    try:
        local_queued = _local_pcm_queue.qsize()
    except Exception:
        local_queued = 0
    device_queued = 0
    device_audible_sec = 0.0
    for sc in list(stream_clients):
        try:
            pending = list(sc.q._queue)
        except Exception:
            continue
        device_queued += len(pending)
        for piece in pending:
            if piece and _pcm_has_signal(piece):
                device_audible_sec += _pcm_duration_sec(piece)
    return {
        "playback_target": _playback_target,
        "local_queued_chunks": local_queued,
        "local_remaining_sec": round(local_play_remaining_sec(), 4),
        "device_queued_chunks": device_queued,
        "device_audible_sec": round(device_audible_sec, 4),
        "last_audible_age_sec": (
            None
            if not _last_audible_output_at
            else round(max(0.0, time.time() - _last_audible_output_at), 4)
        ),
        "mic_mute_hangover_sec": MIC_MUTE_HANGOVER_SEC,
        "output_playing": is_output_playing(),
    }


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
_playback_clock = PlaybackClock(STREAM_SR * STREAM_SW, STREAM_START_BUFFER_CHUNKS * 0.02)
_dispatch_lock = threading.Lock()
_playback_epoch = 0
_stream_loop: Optional[asyncio.AbstractEventLoop] = None
_local_enqueued_bytes = 0
_local_written_bytes = 0
_device_enqueued_bytes = 0
_cancelled_pcm_bytes = 0
_device_stalls = 0
_local_underruns = 0


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


def _flush_local_pcm() -> None:
    global _last_audible_output_at
    while True:
        try:
            _local_pcm_queue.get_nowait()
        except queue.Empty:
            break
    _clear_local_play_schedule()
    _last_audible_output_at = 0.0


def _invalidate_pcm() -> None:
    global _playback_epoch
    with _pcm_buffer_lock:
        _playback_epoch += 1
        _pending_pcm16.clear()
        _playback_clock.reset()
    _flush_local_pcm()


async def hard_reset_audio(reason: str = ""):
    _invalidate_pcm()
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
    _invalidate_pcm()
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
    for sc in list(stream_clients):
        if sc.abort_event.is_set():
            stream_clients.discard(sc)
        else:
            sc.q.put_nowait(piece)


async def _on_stream_loop(coro):
    loop = _stream_loop
    if loop is None or loop.is_closed() or not loop.is_running() or loop is asyncio.get_running_loop():
        return await coro
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        future.cancel()
        raise


async def _wait_for_playback_room(epoch: int) -> bool:
    global _device_stalls
    started = time.monotonic()
    if _targets_server() and _local_player_autostart:
        _ensure_local_player_thread()
    while epoch == _playback_epoch:
        local_full = _targets_server() and _local_pcm_queue.full()
        full_clients = [sc for sc in list(stream_clients)
                        if _targets_device() and not sc.abort_event.is_set() and sc.q.full()]
        if not local_full and not full_clients:
            return True
        waited = time.monotonic() - started
        if waited >= 1.0 and full_clients:
            for sc in full_clients:
                sc.abort_event.set()
                stream_clients.discard(sc)
                _device_stalls += 1
        if local_full and waited >= 5.0:
            raise TimeoutError("Computer audio output is not consuming PCM")
        await asyncio.sleep(0.005)
    return False


async def _dispatch_pcm(ready: List[bytes], epoch: int) -> None:
    global _local_enqueued_bytes, _device_enqueued_bytes, _cancelled_pcm_bytes
    while not _dispatch_lock.acquire(blocking=False):
        if epoch != _playback_epoch:
            _cancelled_pcm_bytes += sum(map(len, ready))
            return
        await asyncio.sleep(0.005)
    try:
        for index, piece in enumerate(ready):
            if epoch != _playback_epoch:
                _cancelled_pcm_bytes += sum(map(len, ready[index:]))
                return
            dispatch_at = _playback_clock.reserve(len(piece))
            while epoch == _playback_epoch:
                delay = dispatch_at - _playback_clock.clock()
                if delay <= 0:
                    break
                await asyncio.sleep(min(delay, 0.02))
            if not await _wait_for_playback_room(epoch):
                _cancelled_pcm_bytes += sum(map(len, ready[index:]))
                return
            if _targets_server():
                _enqueue_local_pcm(piece)
                _local_enqueued_bytes += len(piece)
            if _targets_device():
                _enqueue_pcm_piece(piece)
                _device_enqueued_bytes += len(piece)
    finally:
        _dispatch_lock.release()


async def broadcast_pcm16_realtime(pcm16: bytes):
    await _on_stream_loop(_broadcast_pcm(pcm16))


async def _broadcast_pcm(pcm16: bytes):
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
        epoch = _playback_epoch
        _pending_pcm16.extend(pcm16)
        while len(_pending_pcm16) >= BYTES_PER_20MS_16K:
            ready.append(bytes(_pending_pcm16[:BYTES_PER_20MS_16K]))
            del _pending_pcm16[:BYTES_PER_20MS_16K]
    await _dispatch_pcm(ready, epoch)


async def finish_pcm16_stream() -> None:
    """Flush the final partial audio frame once at the end of a response."""
    await _on_stream_loop(_finish_pcm())


async def _finish_pcm() -> None:
    with _pcm_buffer_lock:
        epoch = _playback_epoch
        piece = bytes(_pending_pcm16)
        _pending_pcm16.clear()
    if piece and len(piece) < BYTES_PER_20MS_16K:
        piece += b"\x00" * (BYTES_PER_20MS_16K - len(piece))
    if piece:
        await _dispatch_pcm([piece], epoch)
    if epoch != _playback_epoch:
        return
    if _targets_device():
        for sc in list(stream_clients):
            if not sc.abort_event.is_set():
                sc.flush_event.set()


def get_stream_status() -> Dict[str, Any]:
    age = None if not last_broadcast_at else max(0.0, time.time() - last_broadcast_at)
    status = {
        "clients": len(stream_clients),
        "last_broadcast_age_sec": age,
        "last_broadcast_bytes": last_broadcast_bytes,
        "total_broadcast_bytes": total_broadcast_bytes,
        "playback_target": _playback_target,
        "local_player_error": _local_player_error,
        "local_enqueued_bytes": _local_enqueued_bytes,
        "local_written_bytes": _local_written_bytes,
        "device_enqueued_bytes": _device_enqueued_bytes,
        "cancelled_pcm_bytes": _cancelled_pcm_bytes,
        "device_stalls": _device_stalls,
        "local_underruns": _local_underruns,
        "dispatch_ahead_sec": round(max(0.0, _playback_clock.end_at - _playback_clock.clock()), 3),
        "local_player_active": _targets_server(),
        "output_playing": is_output_playing(),
    }
    status.update(playback_timing())
    return status


def register_stream_route(app):
    @app.on_event("startup")
    async def bind_stream_loop():
        global _stream_loop
        _stream_loop = asyncio.get_running_loop()

    @app.on_event("shutdown")
    async def unbind_stream_loop():
        global _stream_loop
        _stream_loop = None

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
                    out = chunk or STREAM_IDLE_SILENCE
                    # _mark_audible_output 只认非零样本。全零保活不能打点，
                    # 否则会把麦克风静音窗口无声地续上。
                    if _pcm_has_signal(out):
                        _mark_audible_output(out)
                    yield out
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
