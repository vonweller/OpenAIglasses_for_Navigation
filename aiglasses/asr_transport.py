"""Queue-backed adapter for DashScope's legacy spinning input generator."""

import queue
import threading
import time

from dashscope.audio.asr import Recognition


class QueuedRecognition(Recognition):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pcm_queue = queue.Queue(maxsize=100)
        self._pcm_ready = threading.Event()
        self._pcm_stop = threading.Event()
        self._last_pcm_at = time.monotonic()
        self.dropped_frames = 0
        self.sent_frames = 0

    def start(self, *args, **kwargs):
        self._pcm_stop.clear()
        self._pcm_ready.clear()
        while not self._pcm_queue.empty():
            try:
                self._pcm_queue.get_nowait()
            except queue.Empty:
                break
        try:
            result = super().start(*args, **kwargs)
        except BaseException:
            self._pcm_stop.set()
            self._pcm_ready.set()
            raise
        self._last_pcm_at = time.monotonic()
        self._pcm_ready.set()
        return result

    def send_audio_frame(self, buffer: bytes) -> None:
        if not self._running or self._pcm_stop.is_set():
            raise RuntimeError("Speech recognition has stopped")
        frame = bytes(buffer)
        try:
            self._pcm_queue.put_nowait(frame)
        except queue.Full:
            try:
                self._pcm_queue.get_nowait()
                self.dropped_frames += 1
            except queue.Empty:
                pass
            self._pcm_queue.put_nowait(frame)
        self._last_pcm_at = time.monotonic()

    def _input_stream_cycle(self):
        # The SDK starts its worker before assigning _running=True.
        self._pcm_ready.wait()
        while not self._pcm_stop.is_set():
            if not self._running:
                break
            try:
                frame = self._pcm_queue.get(timeout=0.1)
            except queue.Empty:
                if time.monotonic() - self._last_pcm_at > self.SILENCE_TIMEOUT_S:
                    self._running = False
                    break
                continue
            if frame is None:
                break
            self.sent_frames += 1
            yield frame
        while True:
            try:
                frame = self._pcm_queue.get_nowait()
            except queue.Empty:
                break
            if frame is not None:
                self.sent_frames += 1
                yield frame

    def _silence_stop_timer(self):
        # One SDK timer is enough; don't create a new thread for every 20ms frame.
        self._silence_timer = None

    def stop(self):
        self._pcm_stop.set()
        self._pcm_ready.set()
        try:
            self._pcm_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._running:
            return super().stop()
        if self._silence_timer is not None:
            self._silence_timer.cancel()
            self._silence_timer = None
