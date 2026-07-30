"""Runtime performance profiles and lightweight pipeline metrics."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Dict


@dataclass(frozen=True)
class PerformanceProfile:
    key: str
    name_zh: str
    framesize: str
    width: int
    height: int
    camera_fps: int
    jpeg_quality: int
    yolo_imgsz: int
    inference_hz: float


PROFILES: Dict[str, PerformanceProfile] = {
    "smooth": PerformanceProfile("smooth", "流畅", "QVGA", 320, 240, 25, 22, 320, 12.5),
    "balanced": PerformanceProfile("balanced", "平衡", "VGA", 640, 480, 20, 20, 416, 10.0),
    "quality": PerformanceProfile("quality", "清晰", "SVGA", 800, 600, 15, 18, 512, 7.5),
}
DEFAULT_PROFILE = "balanced"


def normalize_profile(value: str) -> str:
    key = str(value or "").strip().lower()
    return key if key in PROFILES else DEFAULT_PROFILE


def profile_from_env() -> PerformanceProfile:
    return PROFILES[normalize_profile(os.getenv("AIGLASS_PERFORMANCE_PROFILE", DEFAULT_PROFILE))]


def profile_payload(value: str) -> dict:
    return asdict(PROFILES[normalize_profile(value)])


class RateMeter:
    def __init__(self, window_sec: float = 2.0):
        self.window_sec = max(0.25, float(window_sec))
        self._events = deque()

    def tick(self, now: float) -> float:
        self._events.append(now)
        cutoff = now - self.window_sec
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        if len(self._events) < 2:
            return 0.0
        elapsed = self._events[-1] - self._events[0]
        return 0.0 if elapsed <= 0 else (len(self._events) - 1) / elapsed


class PipelineMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._capture = RateMeter()
        self._processed = RateMeter()
        self._broadcast = RateMeter()
        self._capture_fps = 0.0
        self._processed_fps = 0.0
        self._broadcast_fps = 0.0
        self._inference_ms = 0.0
        self._latency_ms = 0.0
        self._output_dropped = 0
        self._input_bytes = 0
        self._last_frame_at = None
        self._esp32_camera = {}

    def on_capture(self, size_bytes: int, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._capture_fps = self._capture.tick(now)
            self._input_bytes += max(0, int(size_bytes))
            self._last_frame_at = now

    def on_processed(
        self,
        *,
        captured_at: float | None = None,
        inference_ms: float | None = None,
        now: float | None = None,
    ) -> None:
        now = now or time.time()
        with self._lock:
            self._processed_fps = self._processed.tick(now)
            if captured_at:
                self._latency_ms = max(0.0, (now - captured_at) * 1000.0)
            if inference_ms is not None:
                self._inference_ms = max(0.0, float(inference_ms))

    def on_broadcast(self, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._broadcast_fps = self._broadcast.tick(now)

    def on_output_drop(self) -> None:
        with self._lock:
            self._output_dropped += 1

    def update_esp32_camera(self, payload: dict) -> None:
        with self._lock:
            self._esp32_camera = dict(payload or {})

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "capture_fps": round(self._capture_fps, 1),
                "processed_fps": round(self._processed_fps, 1),
                "broadcast_fps": round(self._broadcast_fps, 1),
                "inference_ms": round(self._inference_ms, 1),
                "latency_ms": round(self._latency_ms, 1),
                "output_dropped": self._output_dropped,
                "input_megabytes": round(self._input_bytes / 1024 / 1024, 2),
                "last_frame_at": self._last_frame_at,
                "esp32_camera": dict(self._esp32_camera),
            }


pipeline_metrics = PipelineMetrics()
