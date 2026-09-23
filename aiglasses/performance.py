"""Runtime performance profiles and lightweight pipeline metrics."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Dict


DEFAULT_PREVIEW_FPS = 30
DEFAULT_OVERLAY_JPEG_QUALITY = 92


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
    preview_fps: int = DEFAULT_PREVIEW_FPS
    overlay_jpeg_quality: int = DEFAULT_OVERLAY_JPEG_QUALITY
    device_family: str = "esp32"


PROFILES: Dict[str, PerformanceProfile] = {
    "smooth": PerformanceProfile("smooth", "流畅", "VGA", 640, 480, 24, 14, 384, 12.0),
    "balanced": PerformanceProfile("balanced", "平衡", "VGA", 640, 480, 20, 12, 512, 10.0),
    "quality": PerformanceProfile("quality", "清晰", "SVGA", 800, 600, 15, 10, 640, 7.5),
    # K230 names are protocol tokens. Board firmware must recognize them before use.
    # jpeg_quality stays on the ESP scale (lower is sharper). 24 -> board encoder 76.
    "k230_hd": PerformanceProfile(
        "k230_hd", "K230 720p", "K230_HD", 1280, 720, 30, 24, 640, 8.0,
        preview_fps=30, device_family="k230",
    ),
    "k230_1k": PerformanceProfile(
        "k230_1k", "K230 1K", "K230_1K", 1280, 960, 25, 24, 640, 8.0,
        preview_fps=25, device_family="k230",
    ),
    "k230_1_5k": PerformanceProfile(
        "k230_1_5k", "K230 1.5K", "K230_1_5K", 1536, 864, 25, 24, 640, 6.0,
        preview_fps=25, device_family="k230",
    ),
    "k230_fhd": PerformanceProfile(
        "k230_fhd", "K230 1080p", "K230_FHD", 1920, 1080, 20, 26, 640, 5.0,
        preview_fps=20, device_family="k230",
    ),
}
DEFAULT_PROFILE = "balanced"


def normalize_profile(value: str) -> str:
    key = str(value or "").strip().lower()
    return key if key in PROFILES else DEFAULT_PROFILE


def profile_from_env() -> PerformanceProfile:
    return PROFILES[normalize_profile(os.getenv("AIGLASS_PERFORMANCE_PROFILE", DEFAULT_PROFILE))]


def profile_payload(value: str) -> dict:
    profile = PROFILES[normalize_profile(value)]
    payload = asdict(profile)
    payload["preview_fps"] = int(profile.preview_fps or profile.camera_fps or DEFAULT_PREVIEW_FPS)
    return payload


def preview_fps_for(value: str) -> int:
    """Display cadence. Independent from inference_hz; falls back to camera_fps."""
    profile = PROFILES[normalize_profile(value)]
    fps = int(profile.preview_fps or profile.camera_fps or DEFAULT_PREVIEW_FPS)
    return max(1, fps)


def overlay_jpeg_quality_for(value: str) -> int:
    """OpenCV quality for annotated frames only. Raw capture bytes stay untouched."""
    profile = PROFILES[normalize_profile(value)]
    return max(1, min(100, int(profile.overlay_jpeg_quality)))


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
