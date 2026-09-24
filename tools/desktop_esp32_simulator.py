#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 眼镜本地硬件测试中心。

使用电脑摄像头、麦克风、扬声器和模拟 IMU 复现 ESP32 协议。摄像头不可用时，
可循环播放视频文件或自动生成动态测试画面，便于先完成本机闭环验证。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import queue
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

if os.name == "nt":
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8")


IMU_UDP_PORT = 12345
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHUNK_MS = 20
AUDIO_FRAMES_PER_CHUNK = AUDIO_SAMPLE_RATE * AUDIO_CHUNK_MS // 1000
AUDIO_BYTES_PER_CHUNK = AUDIO_FRAMES_PER_CHUNK * 2
LOG_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=500)

PROFILES = {
    "smooth": {
        "name": "流畅",
        "framesize": "QVGA",
        "width": 320,
        "height": 240,
        "fps": 25.0,
        "jpeg_quality": 78,
    },
    "balanced": {
        "name": "平衡",
        "framesize": "VGA",
        "width": 640,
        "height": 480,
        "fps": 20.0,
        "jpeg_quality": 80,
    },
    "quality": {
        "name": "清晰",
        "framesize": "SVGA",
        "width": 800,
        "height": 600,
        "fps": 15.0,
        "jpeg_quality": 82,
    },
}
FRAMESIZES = {
    "QQVGA": (160, 120),
    "HQVGA": (240, 176),
    "QVGA": (320, 240),
    "CIF": (400, 296),
    "VGA": (640, 480),
    "SVGA": (800, 600),
    "XGA": (1024, 768),
}


def log(channel: str, message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] [{channel}] {message}"
    print(line, flush=True)
    with contextlib.suppress(queue.Full):
        LOG_QUEUE.put_nowait(line)


def _device_index(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    head = text.split(":", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


class RateMeter:
    def __init__(self, window_sec: float = 2.0):
        self.window_sec = window_sec
        self.events: deque[float] = deque()

    def tick(self, now: Optional[float] = None) -> float:
        now = now or time.time()
        self.events.append(now)
        cutoff = now - self.window_sec
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        if len(self.events) < 2:
            return 0.0
        elapsed = self.events[-1] - self.events[0]
        return 0.0 if elapsed <= 0 else (len(self.events) - 1) / elapsed


class RuntimeOptions:
    def __init__(self, args: argparse.Namespace):
        profile = PROFILES[args.profile]
        self._lock = threading.Lock()
        self.camera_index = args.camera_index
        self.camera_backend = args.camera_backend
        self.video_file = os.path.abspath(args.video_file) if args.video_file else ""
        self.synthetic = bool(args.synthetic)
        self.profile = args.profile
        self.width = int(args.width or profile["width"])
        self.height = int(args.height or profile["height"])
        self.fps = float(args.fps or profile["fps"])
        self.jpeg_quality = int(args.jpeg_quality if args.jpeg_quality is not None else profile["jpeg_quality"])
        self.input_device = args.input_device
        self.output_device = args.output_device
        self.generation = 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "camera_index": self.camera_index,
                "camera_backend": self.camera_backend,
                "video_file": self.video_file,
                "synthetic": self.synthetic,
                "profile": self.profile,
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "jpeg_quality": self.jpeg_quality,
                "input_device": self.input_device,
                "output_device": self.output_device,
                "generation": self.generation,
            }

    def update_source(
        self,
        *,
        camera_index: Optional[int] = None,
        camera_backend: Optional[str] = None,
        video_file: Optional[str] = None,
        synthetic: Optional[bool] = None,
    ) -> None:
        with self._lock:
            if camera_index is not None:
                self.camera_index = int(camera_index)
            if camera_backend is not None:
                self.camera_backend = str(camera_backend)
            if video_file is not None:
                self.video_file = os.path.abspath(video_file) if video_file else ""
            if synthetic is not None:
                self.synthetic = bool(synthetic)
            self.generation += 1

    def set_audio_devices(self, input_device: Any = None, output_device: Any = None) -> None:
        with self._lock:
            self.input_device = input_device
            self.output_device = output_device

    def apply_profile(self, key: str) -> dict:
        key = key if key in PROFILES else "balanced"
        profile = PROFILES[key]
        with self._lock:
            self.profile = key
            self.width = int(profile["width"])
            self.height = int(profile["height"])
            self.fps = float(profile["fps"])
            self.jpeg_quality = int(profile["jpeg_quality"])
            self.generation += 1
        return dict(profile)

    def apply_camera_command(self, raw: str) -> bool:
        if not raw.startswith("SET:") or "=" not in raw:
            return False
        key, value = raw[4:].split("=", 1)
        key = key.strip().upper()
        value = value.strip()
        with self._lock:
            if key == "FRAMESIZE":
                size = FRAMESIZES.get(value.upper())
                if not size:
                    return False
                self.width, self.height = size
            elif key == "QUALITY":
                # ESP32 数值越小质量越高；OpenCV 数值越大质量越高。
                esp_quality = max(4, min(63, int(float(value))))
                self.jpeg_quality = max(25, min(95, 100 - esp_quality))
            elif key == "FPS":
                self.fps = max(1.0, min(60.0, float(value)))
            else:
                return False
            for profile_key, profile in PROFILES.items():
                if (
                    self.width == int(profile["width"])
                    and self.height == int(profile["height"])
                    and abs(self.fps - float(profile["fps"])) < 0.1
                    and self.jpeg_quality == int(profile["jpeg_quality"])
                ):
                    self.profile = profile_key
                    break
            self.generation += 1
        return True


@dataclass
class ImuState:
    accel: Dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 9.807, "z": 0.0})
    gyro: Dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0, "z": 0.0})
    wiggle_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            accel = dict(self.accel)
            gyro = dict(self.gyro)
            wiggle_until = self.wiggle_until
        now = time.time()
        if wiggle_until > now:
            phase = now * 5.0
            accel["x"] += math.sin(phase) * 0.8
            accel["z"] += math.cos(phase * 0.8) * 0.5
            gyro["x"] += math.sin(phase * 1.3) * 8.0
            gyro["z"] += math.cos(phase) * 10.0
        return {"accel": accel, "gyro": gyro}

    def set_axis(self, group: str, axis: str, value: float) -> None:
        with self._lock:
            target = self.accel if group == "accel" else self.gyro
            target[axis] = float(value)

    def set_values(self, accel: Dict[str, float], gyro: Dict[str, float], wiggle_seconds: float = 0.0) -> None:
        with self._lock:
            self.accel.update({k: float(v) for k, v in accel.items()})
            self.gyro.update({k: float(v) for k, v in gyro.items()})
            self.wiggle_until = time.time() + wiggle_seconds if wiggle_seconds else 0.0


class SimulatorState:
    def __init__(self):
        self._lock = threading.Lock()
        self._capture_meter = RateMeter()
        self._send_meter = RateMeter()
        self.capture_fps = 0.0
        self.send_fps = 0.0
        self.encode_ms = 0.0
        self.send_ms = 0.0
        self.jpeg_bytes = 0
        self.dropped = 0
        self.source = "等待画面"
        self.camera_status = "尚未启动"
        self.audio_status = "麦克风已关闭"
        self.playback_status = "后端语音未播放"
        self.preview = None

    def on_capture(self, encode_ms: float, jpeg_bytes: int, source: str, preview: Any) -> None:
        with self._lock:
            self.capture_fps = self._capture_meter.tick()
            self.encode_ms = float(encode_ms)
            self.jpeg_bytes = int(jpeg_bytes)
            self.source = source
            self.camera_status = "采集正常"
            self.preview = preview

    def on_send(self, send_ms: float) -> None:
        with self._lock:
            self.send_fps = self._send_meter.tick()
            self.send_ms = float(send_ms)
            self.camera_status = "上传正常"

    def on_drop(self) -> None:
        with self._lock:
            self.dropped += 1

    def set_status(self, channel: str, text: str) -> None:
        with self._lock:
            if channel == "camera":
                self.camera_status = text
            elif channel == "audio":
                self.audio_status = text
            elif channel == "playback":
                self.playback_status = text

    def snapshot(self, include_preview: bool = False) -> dict:
        with self._lock:
            result = {
                "capture_fps": round(self.capture_fps, 1),
                "send_fps": round(self.send_fps, 1),
                "encode_ms": round(self.encode_ms, 1),
                "send_ms": round(self.send_ms, 1),
                "jpeg_bytes": self.jpeg_bytes,
                "dropped": self.dropped,
                "source": self.source,
                "camera_status": self.camera_status,
                "audio_status": self.audio_status,
                "playback_status": self.playback_status,
            }
            if include_preview and self.preview is not None:
                result["preview"] = self.preview.copy()
            return result


class LatestJpegSlot:
    def __init__(self, state: SimulatorState):
        self._cond = threading.Condition()
        self._data: Optional[bytes] = None
        self._version = 0
        self._taken_version = 0
        self._state = state

    def offer(self, data: bytes) -> None:
        with self._cond:
            if self._version > self._taken_version:
                self._state.on_drop()
            self._version += 1
            self._data = data
            self._cond.notify_all()

    def wait_next(self, last_version: int, timeout: float = 0.5) -> Optional[Tuple[int, bytes]]:
        deadline = time.time() + timeout
        with self._cond:
            while self._version <= last_version:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(min(0.05, remaining))
            self._taken_version = self._version
            return self._version, self._data or b""


def _backend_code(cv2: Any, name: str) -> int:
    key = str(name or "auto").lower()
    if key == "msmf" and hasattr(cv2, "CAP_MSMF"):
        return cv2.CAP_MSMF
    if key == "dshow" and hasattr(cv2, "CAP_DSHOW"):
        return cv2.CAP_DSHOW
    return cv2.CAP_ANY


def scan_cameras(max_index: int = 6, backend: str = "auto") -> List[dict]:
    try:
        import cv2
    except Exception:
        return []
    found = []
    api = _backend_code(cv2, backend)
    for index in range(max_index):
        cap = cv2.VideoCapture(index, api)
        try:
            if cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    h, w = frame.shape[:2]
                    found.append({"index": index, "name": f"摄像头 {index}", "width": w, "height": h})
        finally:
            cap.release()
    return found


def scan_audio_devices() -> Tuple[List[dict], List[dict]]:
    inputs: List[dict] = []
    outputs: List[dict] = []
    try:
        import sounddevice as sd

        for index, item in enumerate(sd.query_devices()):
            entry = {"index": index, "name": str(item.get("name") or f"设备 {index}")}
            if int(item.get("max_input_channels") or 0) > 0:
                inputs.append(entry)
            if int(item.get("max_output_channels") or 0) > 0:
                outputs.append(entry)
        return inputs, outputs
    except Exception:
        pass
    try:
        import pyaudio

        pa = pyaudio.PyAudio()
        try:
            for index in range(pa.get_device_count()):
                item = pa.get_device_info_by_index(index)
                entry = {"index": index, "name": str(item.get("name") or f"设备 {index}")}
                if int(item.get("maxInputChannels") or 0) > 0:
                    inputs.append(entry)
                if int(item.get("maxOutputChannels") or 0) > 0:
                    outputs.append(entry)
        finally:
            pa.terminate()
    except Exception:
        pass
    return inputs, outputs


def _synthetic_frame(cv2: Any, width: int, height: int, tick: int):
    import numpy as np

    x = np.linspace(0, 1, width, dtype=np.float32)
    y = np.linspace(0, 1, height, dtype=np.float32)[:, None]
    phase = tick * 0.035
    b = ((x[None, :] * 110 + y * 35 + 35) % 255).astype(np.uint8)
    g = ((y * 130 + 45 + math.sin(phase) * 25) % 255).astype(np.uint8)
    g = np.repeat(g, width, axis=1)
    r = (((1 - x)[None, :] * 90 + 40 + math.cos(phase) * 20) % 255).astype(np.uint8)
    r = np.repeat(r, height, axis=0)
    frame = np.dstack((b, g, r))
    cx = int((math.sin(phase) * 0.38 + 0.5) * width)
    cy = int((math.cos(phase * 0.8) * 0.28 + 0.5) * height)
    cv2.circle(frame, (cx, cy), max(18, min(width, height) // 11), (0, 235, 255), -1)
    cv2.rectangle(frame, (width // 12, height // 8), (width // 3, height // 3), (80, 240, 90), 3)
    cv2.putText(
        frame,
        f"LOCAL TEST  {width}x{height}",
        (16, max(28, height - 22)),
        cv2.FONT_HERSHEY_SIMPLEX,
        max(0.45, width / 1100),
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return frame


def camera_capture_worker(
    runtime: RuntimeOptions,
    state: SimulatorState,
    slot: LatestJpegSlot,
    stop_event: threading.Event,
) -> None:
    try:
        import cv2
    except Exception as exc:
        log("摄像头", f"无法加载 OpenCV：{exc}")
        state.set_status("camera", "OpenCV 不可用")
        return

    cap = None
    generation = -1
    source_kind = "synthetic"
    source_name = "动态测试画面"
    retry_camera_at = 0.0
    tick = 0
    next_frame_at = time.monotonic()
    try:
        while not stop_event.is_set():
            cfg = runtime.snapshot()
            if cfg["generation"] != generation:
                generation = cfg["generation"]
                if cap is not None:
                    cap.release()
                    cap = None
                source_kind = "synthetic"
                source_name = "动态测试画面"
                if cfg["video_file"]:
                    cap = cv2.VideoCapture(cfg["video_file"])
                    if cap.isOpened():
                        source_kind = "video"
                        source_name = f"视频文件：{os.path.basename(cfg['video_file'])}"
                        log("摄像头", f"正在循环播放视频：{cfg['video_file']}")
                    else:
                        cap.release()
                        cap = None
                        log("摄像头", "视频文件无法打开，已回退动态测试画面")
                elif not cfg["synthetic"]:
                    api = _backend_code(cv2, cfg["camera_backend"])
                    cap = cv2.VideoCapture(int(cfg["camera_index"]), api)
                    if cap.isOpened():
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(cfg["width"]))
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(cfg["height"]))
                        cap.set(cv2.CAP_PROP_FPS, float(cfg["fps"]))
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        source_kind = "camera"
                        source_name = f"本机摄像头 {cfg['camera_index']} / {cfg['camera_backend'].upper()}"
                        log("摄像头", f"已打开{source_name}")
                    else:
                        cap.release()
                        cap = None
                        retry_camera_at = time.time() + 5.0
                        log("摄像头", "未发现可用摄像头，已自动回退动态测试画面")

            if source_kind == "synthetic" and not cfg["synthetic"] and not cfg["video_file"] and time.time() >= retry_camera_at:
                runtime.update_source(camera_index=int(cfg["camera_index"]))
                retry_camera_at = time.time() + 5.0
                continue

            frame = None
            if cap is not None:
                ok, frame = cap.read()
                if not ok or frame is None:
                    if source_kind == "video":
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok, frame = cap.read()
                    if not ok or frame is None:
                        log("摄像头", "读取画面失败，已回退动态测试画面")
                        cap.release()
                        cap = None
                        source_kind = "synthetic"
                        source_name = "动态测试画面"
                        retry_camera_at = time.time() + 5.0
            if frame is None:
                frame = _synthetic_frame(cv2, int(cfg["width"]), int(cfg["height"]), tick)
            elif frame.shape[1] != int(cfg["width"]) or frame.shape[0] != int(cfg["height"]):
                frame = cv2.resize(frame, (int(cfg["width"]), int(cfg["height"])), interpolation=cv2.INTER_AREA)

            started = time.perf_counter()
            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), max(20, min(95, int(cfg["jpeg_quality"])))],
            )
            encode_ms = (time.perf_counter() - started) * 1000.0
            if ok:
                jpeg = encoded.tobytes()
                slot.offer(jpeg)
                preview = cv2.resize(frame, (480, 360), interpolation=cv2.INTER_AREA)
                state.on_capture(encode_ms, len(jpeg), source_name, preview)

            tick += 1
            interval = 1.0 / max(1.0, float(cfg["fps"]))
            next_frame_at += interval
            delay = next_frame_at - time.monotonic()
            if delay > 0:
                stop_event.wait(delay)
            else:
                next_frame_at = time.monotonic()
    except Exception as exc:
        log("摄像头", f"采集线程异常：{exc}")
        state.set_status("camera", f"采集异常：{exc}")
    finally:
        if cap is not None:
            cap.release()
        log("摄像头", "采集线程已停止")


async def camera_sender(
    args: argparse.Namespace,
    runtime: RuntimeOptions,
    state: SimulatorState,
    slot: LatestJpegSlot,
    stop_event: threading.Event,
) -> None:
    try:
        import websockets
    except Exception as exc:
        log("摄像头", f"无法加载 WebSocket：{exc}")
        return
    uri = f"ws://{args.host}:{args.port}/ws/camera"
    retry_delay = 1.0
    last_version = 0

    async def receive_commands(ws) -> None:
        async for message in ws:
            if isinstance(message, str) and runtime.apply_camera_command(message):
                log("摄像头", f"已应用后端参数：{message}")

    while not stop_event.is_set():
        receiver = None
        try:
            state.set_status("camera", "正在连接后端")
            log("摄像头", f"正在连接 {uri}")
            async with websockets.connect(uri, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                log("摄像头", "后端连接成功")
                state.set_status("camera", "已连接，等待发送画面")
                receiver = asyncio.create_task(receive_commands(ws))
                last_stat_at = 0.0
                while not stop_event.is_set():
                    item = await asyncio.to_thread(slot.wait_next, last_version, 0.25)
                    if item is not None:
                        last_version, jpeg = item
                        started = time.perf_counter()
                        await ws.send(jpeg)
                        state.on_send((time.perf_counter() - started) * 1000.0)
                    now = time.time()
                    if now - last_stat_at >= 2.0:
                        stats = state.snapshot()
                        stats.update(
                            {
                                "source": stats["source"],
                                "local_simulator": True,
                                "profile": runtime.snapshot()["profile"],
                            }
                        )
                        await ws.send("STAT:" + json.dumps(stats, ensure_ascii=False, separators=(",", ":")))
                        last_stat_at = now
        except asyncio.CancelledError:
            break
        except Exception as exc:
            if not stop_event.is_set():
                state.set_status("camera", "后端连接断开，正在重连")
                log("摄像头", f"连接中断：{exc}；{retry_delay:.1f} 秒后重连")
                await asyncio.sleep(retry_delay)
                retry_delay = min(8.0, retry_delay * 1.5)
        finally:
            if receiver is not None:
                receiver.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await receiver
    log("摄像头", "上传任务已停止")


def open_audio_input(device: Any) -> Optional[Tuple[str, Callable[[], bytes], Callable[[], None]]]:
    index = _device_index(device)
    try:
        import sounddevice as sd

        stream = sd.RawInputStream(
            samplerate=AUDIO_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=AUDIO_FRAMES_PER_CHUNK,
            device=index,
        )
        stream.start()

        def read_sd() -> bytes:
            data, overflowed = stream.read(AUDIO_FRAMES_PER_CHUNK)
            if overflowed:
                log("麦克风", "输入缓冲区溢出，已丢弃过期音频")
            return bytes(data)

        def close_sd() -> None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()

        return "sounddevice", read_sd, close_sd
    except Exception as first_exc:
        log("麦克风", f"sounddevice 打开失败，尝试 PyAudio：{first_exc}")
    try:
        import pyaudio

        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=AUDIO_SAMPLE_RATE,
            input=True,
            input_device_index=index,
            frames_per_buffer=AUDIO_FRAMES_PER_CHUNK,
        )

        def read_pa() -> bytes:
            return stream.read(AUDIO_FRAMES_PER_CHUNK, exception_on_overflow=False)

        def close_pa() -> None:
            with contextlib.suppress(Exception):
                stream.stop_stream()
            with contextlib.suppress(Exception):
                stream.close()
            with contextlib.suppress(Exception):
                pa.terminate()

        return "PyAudio", read_pa, close_pa
    except Exception as exc:
        log("麦克风", f"无法打开麦克风：{exc}")
        return None


async def audio_sender(
    args: argparse.Namespace,
    runtime: RuntimeOptions,
    state: SimulatorState,
    stop_event: threading.Event,
    enabled_event: threading.Event,
) -> None:
    try:
        import websockets
    except Exception as exc:
        log("麦克风", f"无法加载 WebSocket：{exc}")
        return
    uri = f"ws://{args.host}:{args.port}/ws_audio"
    retry_delay = 1.0
    restart_requested = False

    while not stop_event.is_set():
        audio_input = None
        streaming = False
        reply_task = None
        try:
            log("麦克风", f"正在连接 {uri}")
            async with websockets.connect(uri, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                state.set_status("audio", "已连接，麦克风默认关闭")
                log("麦克风", "后端连接成功；按住说话按钮后才会开始识别")

                async def read_replies() -> None:
                    nonlocal restart_requested
                    async for reply in ws:
                        if isinstance(reply, str):
                            log("麦克风", f"后端指令：{reply}")
                            if reply.strip().upper() == "RESTART":
                                restart_requested = True

                reply_task = asyncio.create_task(read_replies())
                while not stop_event.is_set():
                    if restart_requested:
                        restart_requested = False
                        if streaming:
                            with contextlib.suppress(Exception):
                                await ws.send("STOP")
                        streaming = False
                        if audio_input is not None:
                            audio_input[2]()
                            audio_input = None

                    if not enabled_event.is_set():
                        if streaming:
                            with contextlib.suppress(Exception):
                                await ws.send("STOP")
                            streaming = False
                            state.set_status("audio", "麦克风已关闭")
                            log("麦克风", "已停止语音识别")
                        if audio_input is not None:
                            audio_input[2]()
                            audio_input = None
                        await asyncio.sleep(0.05)
                        continue

                    if audio_input is None:
                        audio_input = await asyncio.to_thread(open_audio_input, runtime.snapshot()["input_device"])
                        if audio_input is None:
                            enabled_event.clear()
                            state.set_status("audio", "麦克风不可用")
                            continue
                        await ws.send("START")
                        streaming = True
                        state.set_status("audio", "正在发送音频并识别")
                        log("麦克风", f"已通过 {audio_input[0]} 开始语音识别")

                    chunk = await asyncio.to_thread(audio_input[1])
                    if chunk:
                        await ws.send(chunk)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            if not stop_event.is_set():
                state.set_status("audio", "连接断开，正在重连")
                log("麦克风", f"连接中断：{exc}；稍后重连")
                await asyncio.sleep(retry_delay)
        finally:
            if reply_task is not None:
                reply_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reply_task
            if audio_input is not None:
                audio_input[2]()
    log("麦克风", "上传任务已停止")


def _read_exact(stream: Any, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = stream.read(length - len(data))
        if not chunk:
            raise EOFError("音频流提前结束")
        data.extend(chunk)
    return bytes(data)


def parse_wav_stream_header(stream: Any) -> Tuple[int, int, int]:
    header = _read_exact(stream, 12)
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError("后端返回的不是 WAV 音频流")
    sample_rate = 0
    channels = 0
    sample_width = 0
    while True:
        chunk_header = _read_exact(stream, 8)
        chunk_id = chunk_header[:4]
        chunk_size = struct.unpack("<I", chunk_header[4:])[0]
        if chunk_id == b"fmt ":
            fmt = _read_exact(stream, chunk_size)
            audio_format, channels, sample_rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", fmt[:16])
            if audio_format != 1:
                raise ValueError(f"暂不支持 WAV 编码格式 {audio_format}")
            sample_width = bits // 8
        elif chunk_id == b"data":
            if not sample_rate or not channels or not sample_width:
                raise ValueError("WAV 头缺少 fmt 信息")
            return sample_rate, channels, sample_width
        else:
            _read_exact(stream, chunk_size + (chunk_size % 2))


def read_stream_chunk(stream: Any, length: int) -> bytes:
    """Read currently available streamed audio without waiting for a large block."""
    read1 = getattr(stream, "read1", None)
    if callable(read1):
        return read1(length)
    return stream.read(length)


def open_audio_output(
    sample_rate: int,
    channels: int,
    sample_width: int,
    device: Any,
) -> Optional[Tuple[Callable[[bytes], None], Callable[[], None]]]:
    if sample_width != 2:
        log("扬声器", f"暂不支持 {sample_width * 8} 位音频")
        return None
    index = _device_index(device)
    try:
        import sounddevice as sd

        stream = sd.RawOutputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=0,
            device=index,
        )
        stream.start()

        def play_sd(data: bytes) -> None:
            stream.write(data)

        def close_sd() -> None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()

        return play_sd, close_sd
    except Exception as first_exc:
        log("扬声器", f"sounddevice 打开失败，尝试 PyAudio：{first_exc}")
    try:
        import pyaudio

        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=sample_rate,
            output=True,
            output_device_index=index,
        )

        def play_pa(data: bytes) -> None:
            stream.write(data)

        def close_pa() -> None:
            with contextlib.suppress(Exception):
                stream.stop_stream()
            with contextlib.suppress(Exception):
                stream.close()
            with contextlib.suppress(Exception):
                pa.terminate()

        return play_pa, close_pa
    except Exception as exc:
        log("扬声器", f"无法打开扬声器：{exc}")
        return None


def play_test_tone(runtime: RuntimeOptions, state: SimulatorState) -> None:
    import array

    sample_rate = 44100
    duration = 0.55
    samples = array.array(
        "h",
        (
            int(32767 * 0.18 * math.sin(2 * math.pi * 660 * i / sample_rate))
            for i in range(int(sample_rate * duration))
        ),
    )
    player = open_audio_output(sample_rate, 1, 2, runtime.snapshot()["output_device"])
    if player is None:
        state.set_status("playback", "测试音播放失败")
        return
    try:
        state.set_status("playback", "正在播放本地测试音")
        player[0](samples.tobytes())
        state.set_status("playback", "本地测试音播放完成")
        log("扬声器", "本地测试音播放完成")
    finally:
        player[1]()


async def stream_wav_player(
    args: argparse.Namespace,
    runtime: RuntimeOptions,
    state: SimulatorState,
    stop_event: threading.Event,
    enabled_event: threading.Event,
) -> None:
    uri = f"http://{args.host}:{args.port}/stream.wav"
    while not stop_event.is_set():
        if not enabled_event.is_set():
            state.set_status("playback", "后端语音播放已关闭")
            await asyncio.sleep(0.1)
            continue
        response = None
        player = None
        try:
            state.set_status("playback", "正在连接后端语音")
            response = await asyncio.to_thread(urllib.request.urlopen, uri, None, 60)
            sample_rate, channels, sample_width = await asyncio.to_thread(parse_wav_stream_header, response)
            player = await asyncio.to_thread(
                open_audio_output,
                sample_rate,
                channels,
                sample_width,
                runtime.snapshot()["output_device"],
            )
            if player is None:
                await asyncio.sleep(1.0)
                continue
            log("扬声器", f"开始播放后端语音：{sample_rate} Hz / {channels} 声道 / {sample_width * 8} 位")
            state.set_status("playback", f"正在播放后端语音（{sample_rate} Hz）")
            read_size = max(
                sample_width * channels,
                int(sample_rate * channels * sample_width * 0.040),
            )
            while enabled_event.is_set() and not stop_event.is_set():
                data = await asyncio.to_thread(read_stream_chunk, response, read_size)
                if not data:
                    break
                await asyncio.to_thread(player[0], data)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            if not stop_event.is_set() and enabled_event.is_set():
                state.set_status("playback", "后端语音连接中断")
                log("扬声器", f"播放中断：{exc}；稍后重连")
                await asyncio.sleep(1.0)
        finally:
            if response is not None:
                with contextlib.suppress(Exception):
                    response.close()
            if player is not None:
                with contextlib.suppress(Exception):
                    player[1]()
        await asyncio.sleep(0.1)
    log("扬声器", "播放任务已停止")


async def imu_udp_sender(args: argparse.Namespace, imu: ImuState, stop_event: threading.Event) -> None:
    target = (args.host, IMU_UDP_PORT)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    log("惯导", f"正在向 {target[0]}:{target[1]} 发送模拟数据")
    try:
        while not stop_event.is_set():
            snap = imu.snapshot()
            payload = {
                "ts": int(time.time() * 1000),
                "accel": snap["accel"],
                "gyro": snap["gyro"],
            }
            with contextlib.suppress(Exception):
                sock.sendto(json.dumps(payload, separators=(",", ":")).encode("utf-8"), target)
            await asyncio.sleep(0.02)
    finally:
        sock.close()
        log("惯导", "发送任务已停止")


async def run_async(
    args: argparse.Namespace,
    runtime: RuntimeOptions,
    state: SimulatorState,
    imu: ImuState,
    slot: LatestJpegSlot,
    stop_event: threading.Event,
    audio_enabled_event: threading.Event,
    playback_enabled_event: threading.Event,
) -> None:
    tasks = []
    if not args.no_camera:
        tasks.append(asyncio.create_task(camera_sender(args, runtime, state, slot, stop_event)))
    if not args.no_audio:
        tasks.append(asyncio.create_task(audio_sender(args, runtime, state, stop_event, audio_enabled_event)))
    if not args.no_playback:
        tasks.append(
            asyncio.create_task(
                stream_wav_player(args, runtime, state, stop_event, playback_enabled_event)
            )
        )
    if not args.no_imu:
        tasks.append(asyncio.create_task(imu_udp_sender(args, imu, stop_event)))
    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def post_json(url: str, payload: dict, timeout: float = 8.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class TestCenterWindow:
    def __init__(
        self,
        args: argparse.Namespace,
        runtime: RuntimeOptions,
        state: SimulatorState,
        imu: ImuState,
        stop_event: threading.Event,
        audio_enabled_event: threading.Event,
        playback_enabled_event: threading.Event,
    ):
        import tkinter as tk
        from tkinter import filedialog, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.args = args
        self.runtime = runtime
        self.state = state
        self.imu = imu
        self.stop_event = stop_event
        self.audio_enabled_event = audio_enabled_event
        self.playback_enabled_event = playback_enabled_event
        self.root = tk.Tk()
        self.root.title("AI 眼镜本地硬件测试中心")
        self.root.geometry("1180x800")
        self.root.minsize(980, 700)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.preview_photo = None
        self.metric_vars: Dict[str, Any] = {}
        self.imu_vars: Dict[str, Any] = {}
        self._build()
        self.refresh_devices()
        self._poll()

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        root = self.root
        root.columnconfigure(0, weight=3)
        root.columnconfigure(1, weight=2)
        root.rowconfigure(0, weight=1)

        left = ttk.Frame(root, padding=10)
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(
            left,
            text=f"本机闭环目标：{self.args.host}:{self.args.port}",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.preview_label = ttk.Label(left, text="等待本机摄像头或动态测试画面", anchor="center")
        self.preview_label.grid(row=1, column=0, sticky="nsew")

        metrics = ttk.LabelFrame(left, text="实时性能", padding=8)
        metrics.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        names = [
            ("capture_fps", "采集帧率"),
            ("send_fps", "上传帧率"),
            ("encode_ms", "编码耗时"),
            ("send_ms", "发送耗时"),
            ("jpeg_bytes", "JPEG 大小"),
            ("dropped", "主动丢帧"),
            ("source", "画面来源"),
            ("camera_status", "摄像头状态"),
        ]
        for i, (key, label) in enumerate(names):
            var = tk.StringVar(value="--")
            self.metric_vars[key] = var
            ttk.Label(metrics, text=label).grid(row=i // 4 * 2, column=(i % 4) * 2, sticky="w", padx=(2, 4))
            ttk.Label(metrics, textvariable=var).grid(row=i // 4 * 2 + 1, column=(i % 4) * 2, sticky="w", padx=(2, 14))

        right = ttk.Frame(root, padding=(0, 10, 10, 10))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(right)
        notebook.grid(row=0, column=0, sticky="nsew")

        device_tab = ttk.Frame(notebook, padding=10)
        mode_tab = ttk.Frame(notebook, padding=10)
        imu_tab = ttk.Frame(notebook, padding=10)
        log_tab = ttk.Frame(notebook, padding=8)
        notebook.add(device_tab, text="设备与音频")
        notebook.add(mode_tab, text="模式测试")
        notebook.add(imu_tab, text="模拟惯导")
        notebook.add(log_tab, text="运行日志")
        self._build_device_tab(device_tab)
        self._build_mode_tab(mode_tab)
        self._build_imu_tab(imu_tab)
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_tab, wrap="word", height=20, font=("Microsoft YaHei UI", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _build_device_tab(self, tab: Any) -> None:
        tk, ttk = self.tk, self.ttk
        tab.columnconfigure(1, weight=1)
        self.camera_var = tk.StringVar(value=str(self.runtime.snapshot()["camera_index"]))
        self.backend_var = tk.StringVar(value=self.runtime.snapshot()["camera_backend"])
        self.profile_var = tk.StringVar(value=self.runtime.snapshot()["profile"])
        self.synthetic_var = tk.BooleanVar(value=self.runtime.snapshot()["synthetic"])
        self.video_var = tk.StringVar(value=self.runtime.snapshot()["video_file"])
        self.input_var = tk.StringVar(value=str(self.runtime.snapshot()["input_device"] or ""))
        self.output_var = tk.StringVar(value=str(self.runtime.snapshot()["output_device"] or ""))
        self.continuous_mic_var = tk.BooleanVar(value=self.audio_enabled_event.is_set())
        self.playback_var = tk.BooleanVar(value=self.playback_enabled_event.is_set())
        self.audio_state_var = tk.StringVar(value="麦克风已关闭")
        self.playback_state_var = tk.StringVar(value="后端语音播放已开启")

        row = 0
        ttk.Label(tab, text="摄像头设备").grid(row=row, column=0, sticky="w", pady=4)
        self.camera_combo = ttk.Combobox(tab, textvariable=self.camera_var, state="readonly")
        self.camera_combo.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(tab, text="刷新设备", command=self.refresh_devices).grid(row=row, column=2, padx=(6, 0))
        row += 1
        ttk.Label(tab, text="采集后端").grid(row=row, column=0, sticky="w", pady=4)
        backend_combo = ttk.Combobox(
            tab, textvariable=self.backend_var, values=["auto", "msmf", "dshow"], state="readonly"
        )
        backend_combo.grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(tab, text="性能档位").grid(row=row, column=0, sticky="w", pady=4)
        profile_combo = ttk.Combobox(
            tab,
            textvariable=self.profile_var,
            values=["smooth", "balanced", "quality"],
            state="readonly",
        )
        profile_combo.grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Checkbutton(
            tab,
            text="强制使用动态测试画面",
            variable=self.synthetic_var,
            command=self.apply_camera_options,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
        row += 1
        ttk.Label(tab, text="循环视频").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(tab, textvariable=self.video_var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(tab, text="选择文件", command=self.choose_video).grid(row=row, column=2, padx=(6, 0))
        row += 1
        ttk.Button(tab, text="应用画面设置", command=self.apply_camera_options).grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(6, 12)
        )
        profile_combo.bind("<<ComboboxSelected>>", lambda _e: self.change_profile())

        ttk.Separator(tab).grid(row=row + 1, column=0, columnspan=3, sticky="ew", pady=6)
        row += 2
        ttk.Label(tab, text="麦克风").grid(row=row, column=0, sticky="w", pady=4)
        self.input_combo = ttk.Combobox(tab, textvariable=self.input_var, state="readonly")
        self.input_combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        row += 1
        self.ptt_button = ttk.Button(tab, text="按住说话")
        self.ptt_button.grid(row=row, column=0, sticky="ew", pady=4)
        self.ptt_button.bind("<ButtonPress-1>", self.ptt_start)
        self.ptt_button.bind("<ButtonRelease-1>", self.ptt_stop)
        ttk.Checkbutton(
            tab,
            text="持续开启麦克风（可能产生识别费用）",
            variable=self.continuous_mic_var,
            command=self.toggle_continuous_mic,
        ).grid(row=row, column=1, columnspan=2, sticky="w", padx=(8, 0))
        row += 1
        ttk.Label(tab, textvariable=self.audio_state_var).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        row += 1
        ttk.Label(tab, text="扬声器").grid(row=row, column=0, sticky="w", pady=4)
        self.output_combo = ttk.Combobox(tab, textvariable=self.output_var, state="readonly")
        self.output_combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        row += 1
        ttk.Button(tab, text="播放本地测试音", command=self.start_test_tone).grid(
            row=row, column=0, sticky="ew", pady=4
        )
        ttk.Checkbutton(
            tab,
            text="播放后端语音",
            variable=self.playback_var,
            command=self.toggle_playback,
        ).grid(row=row, column=1, sticky="w", padx=(8, 0))
        row += 1
        ttk.Label(tab, textvariable=self.playback_state_var).grid(
            row=row, column=0, columnspan=3, sticky="w"
        )
        self.input_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_audio_devices())
        self.output_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_audio_devices())

    def _build_mode_tab(self, tab: Any) -> None:
        tk, ttk = self.tk, self.ttk
        tab.columnconfigure(1, weight=1)
        ttk.Label(
            tab,
            text="这些按钮直接调用本机开发控制接口，不经过语音识别，也不会产生 ASR 费用。",
            wraplength=420,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(tab, text="寻找目标").grid(row=1, column=0, sticky="w")
        self.target_var = tk.StringVar(value="手机")
        ttk.Entry(tab, textvariable=self.target_var).grid(row=1, column=1, sticky="ew")
        buttons = [
            ("开始寻找", "find"),
            ("停止找物", "stop_find"),
            ("盲道导航", "blindpath"),
            ("红绿灯检测", "traffic"),
            ("过马路模式", "crossing"),
            ("返回聊天", "chat"),
        ]
        for row, (label, command) in enumerate(buttons, start=2):
            ttk.Button(tab, text=label, command=lambda c=command: self.send_command(c)).grid(
                row=row, column=0, columnspan=2, sticky="ew", pady=4
            )
        self.command_status_var = tk.StringVar(value="等待测试命令")
        ttk.Label(tab, textvariable=self.command_status_var, wraplength=420).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )

    def _build_imu_tab(self, tab: Any) -> None:
        tk, ttk = self.tk, self.ttk
        specs = [
            ("ax", "加速度 X", -20.0, 20.0, "accel", "x", 0.0),
            ("ay", "加速度 Y", -20.0, 20.0, "accel", "y", 9.807),
            ("az", "加速度 Z", -20.0, 20.0, "accel", "z", 0.0),
            ("gx", "角速度 X", -180.0, 180.0, "gyro", "x", 0.0),
            ("gy", "角速度 Y", -180.0, 180.0, "gyro", "y", 0.0),
            ("gz", "角速度 Z", -180.0, 180.0, "gyro", "z", 0.0),
        ]
        tab.columnconfigure(1, weight=1)
        for row, (key, label, low, high, group, axis, initial) in enumerate(specs):
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", pady=4)
            var = tk.DoubleVar(value=initial)
            self.imu_vars[key] = var
            ttk.Scale(
                tab,
                from_=low,
                to=high,
                variable=var,
                command=lambda value, g=group, a=axis: self.imu.set_axis(g, a, float(value)),
            ).grid(row=row, column=1, sticky="ew", pady=4)
            value_var = tk.StringVar(value=f"{initial:.2f}")
            var.trace_add("write", lambda *_a, v=var, out=value_var: out.set(f"{v.get():.2f}"))
            ttk.Label(tab, textvariable=value_var, width=8).grid(row=row, column=2, sticky="e")
        presets = [
            ("静止", {"ax": 0, "ay": 9.807, "az": 0, "gx": 0, "gy": 0, "gz": 0}, 0),
            ("前倾", {"ax": -3, "ay": 9.2, "az": 0, "gx": 0, "gy": 0, "gz": 0}, 0),
            ("左转", {"ax": 0, "ay": 9.807, "az": 0, "gx": 0, "gy": 35, "gz": 0}, 0),
            ("右转", {"ax": 0, "ay": 9.807, "az": 0, "gx": 0, "gy": -35, "gz": 0}, 0),
            ("轻微晃动", {"ax": 0, "ay": 9.807, "az": 0, "gx": 0, "gy": 0, "gz": 0}, 4),
        ]
        for col, (label, values, wiggle) in enumerate(presets):
            ttk.Button(
                tab,
                text=label,
                command=lambda v=values, w=wiggle: self.apply_imu_preset(v, w),
            ).grid(row=7, column=col % 3, sticky="ew", padx=3, pady=4)

    def refresh_devices(self) -> None:
        def worker() -> None:
            cameras = scan_cameras(6, self.backend_var.get() if hasattr(self, "backend_var") else "auto")
            inputs, outputs = scan_audio_devices()
            self.root.after(0, lambda: self._set_device_lists(cameras, inputs, outputs))

        threading.Thread(target=worker, name="device-scan", daemon=True).start()

    def _set_device_lists(self, cameras: List[dict], inputs: List[dict], outputs: List[dict]) -> None:
        camera_values = [f"{item['index']}: {item['name']}（{item['width']}×{item['height']}）" for item in cameras]
        if not camera_values:
            camera_values = ["0: 未检测到摄像头（自动使用测试画面）"]
        self.camera_combo["values"] = camera_values
        if not self.camera_var.get() or self.camera_var.get().isdigit():
            self.camera_var.set(camera_values[0])
        input_values = [f"{item['index']}: {item['name']}" for item in inputs] or ["默认麦克风"]
        output_values = [f"{item['index']}: {item['name']}" for item in outputs] or ["默认扬声器"]
        self.input_combo["values"] = input_values
        self.output_combo["values"] = output_values
        if not self.input_var.get():
            self.input_var.set(input_values[0])
        if not self.output_var.get():
            self.output_var.set(output_values[0])
        self.apply_audio_devices()
        log("设备", f"扫描完成：摄像头 {len(cameras)}，麦克风 {len(inputs)}，扬声器 {len(outputs)}")

    def choose_video(self) -> None:
        path = self.filedialog.askopenfilename(
            title="选择循环测试视频",
            filetypes=[("视频文件", "*.mp4 *.avi *.mov *.mkv"), ("所有文件", "*.*")],
        )
        if path:
            self.video_var.set(path)
            self.synthetic_var.set(False)
            self.apply_camera_options()

    def apply_camera_options(self) -> None:
        index = _device_index(self.camera_var.get())
        self.runtime.update_source(
            camera_index=0 if index is None else index,
            camera_backend=self.backend_var.get(),
            video_file=self.video_var.get().strip(),
            synthetic=self.synthetic_var.get(),
        )
        log("设备", "画面来源设置已应用")

    def change_profile(self) -> None:
        key = self.profile_var.get()
        profile = self.runtime.apply_profile(key)
        self.apply_camera_options()

        def worker() -> None:
            try:
                result = post_json(
                    f"http://{self.args.host}:{self.args.port}/api/runtime-config",
                    {"performance_profile": key},
                )
                name = result.get("performance_profile", {}).get("name_zh", profile["name"])
                log("性能", f"已切换到{name}档并通知后端")
            except Exception as exc:
                log("性能", f"本机已切换档位，但通知后端失败：{exc}")

        threading.Thread(target=worker, name="profile-update", daemon=True).start()

    def apply_audio_devices(self) -> None:
        self.runtime.set_audio_devices(self.input_var.get(), self.output_var.get())

    def ptt_start(self, _event: Any) -> None:
        self.apply_audio_devices()
        self.audio_enabled_event.set()
        self.audio_state_var.set("正在说话：麦克风已开启")

    def ptt_stop(self, _event: Any) -> None:
        if not self.continuous_mic_var.get():
            self.audio_enabled_event.clear()
            self.audio_state_var.set("麦克风已关闭")

    def toggle_continuous_mic(self) -> None:
        if self.continuous_mic_var.get():
            self.audio_enabled_event.set()
            self.audio_state_var.set("持续识别已开启，可能产生语音识别费用")
        else:
            self.audio_enabled_event.clear()
            self.audio_state_var.set("麦克风已关闭")

    def toggle_playback(self) -> None:
        if self.playback_var.get():
            self.playback_enabled_event.set()
            self.playback_state_var.set("后端语音播放已开启")
        else:
            self.playback_enabled_event.clear()
            self.playback_state_var.set("后端语音播放已关闭")

    def start_test_tone(self) -> None:
        self.apply_audio_devices()
        threading.Thread(
            target=play_test_tone,
            args=(self.runtime, self.state),
            name="test-tone",
            daemon=True,
        ).start()

    def send_command(self, command: str) -> None:
        target = self.target_var.get().strip() or "手机"
        self.command_status_var.set("正在发送测试命令…")

        def worker() -> None:
            try:
                result = post_json(
                    f"http://{self.args.host}:{self.args.port}/api/dev/command",
                    {"command": command, "target": target},
                )
                text = str(result.get("message") or "命令已执行")
            except urllib.error.HTTPError as exc:
                try:
                    detail = json.loads(exc.read().decode("utf-8")).get("detail")
                except Exception:
                    detail = str(exc)
                text = f"命令失败：{detail}"
            except Exception as exc:
                text = f"命令失败：{exc}"
            self.root.after(0, lambda: self.command_status_var.set(text))
            log("模式", text)

        threading.Thread(target=worker, name=f"dev-command-{command}", daemon=True).start()

    def apply_imu_preset(self, values: Dict[str, float], wiggle: float) -> None:
        for key, value in values.items():
            self.imu_vars[key].set(float(value))
        self.imu.set_values(
            accel={"x": values["ax"], "y": values["ay"], "z": values["az"]},
            gyro={"x": values["gx"], "y": values["gy"], "z": values["gz"]},
            wiggle_seconds=wiggle,
        )

    def _poll(self) -> None:
        if self.stop_event.is_set():
            with contextlib.suppress(Exception):
                self.root.destroy()
            return
        snap = self.state.snapshot(include_preview=True)
        self.metric_vars["capture_fps"].set(f"{snap['capture_fps']:.1f} 帧/秒")
        self.metric_vars["send_fps"].set(f"{snap['send_fps']:.1f} 帧/秒")
        self.metric_vars["encode_ms"].set(f"{snap['encode_ms']:.1f} 毫秒")
        self.metric_vars["send_ms"].set(f"{snap['send_ms']:.1f} 毫秒")
        self.metric_vars["jpeg_bytes"].set(f"{snap['jpeg_bytes'] / 1024:.1f} KB")
        self.metric_vars["dropped"].set(str(snap["dropped"]))
        self.metric_vars["source"].set(snap["source"])
        self.metric_vars["camera_status"].set(snap["camera_status"])
        self.audio_state_var.set(snap["audio_status"])
        self.playback_state_var.set(snap["playback_status"])
        preview = snap.get("preview")
        if preview is not None:
            try:
                from PIL import Image, ImageTk

                rgb = preview[:, :, ::-1]
                image = Image.fromarray(rgb)
                self.preview_photo = ImageTk.PhotoImage(image=image)
                self.preview_label.configure(image=self.preview_photo, text="")
            except Exception:
                pass
        while True:
            try:
                line = LOG_QUEUE.get_nowait()
            except queue.Empty:
                break
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
        self.root.after(180, self._poll)

    def close(self) -> None:
        self.audio_enabled_event.clear()
        self.playback_enabled_event.clear()
        self.stop_event.set()
        self.root.after(50, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def list_devices(args: argparse.Namespace) -> int:
    print("摄像头设备：")
    cameras = scan_cameras(args.scan_camera_count, args.camera_backend)
    if cameras:
        for item in cameras:
            print(f"  {item['index']}: {item['name']}（{item['width']}×{item['height']}）")
    else:
        print("  未检测到可用摄像头；可使用 --synthetic 或 --video-file。")
    inputs, outputs = scan_audio_devices()
    print("麦克风设备：")
    for item in inputs:
        print(f"  {item['index']}: {item['name']}")
    if not inputs:
        print("  未检测到。")
    print("扬声器设备：")
    for item in outputs:
        print(f"  {item['index']}: {item['name']}")
    if not outputs:
        print("  未检测到。")
    return 0


def run_self_test(args: argparse.Namespace) -> int:
    failures = []
    print("开始本机模拟器自检：")
    try:
        import cv2

        frame = _synthetic_frame(cv2, 320, 240, 1)
        ok, encoded = cv2.imencode(".jpg", frame)
        if not ok or not encoded.size:
            raise RuntimeError("JPEG 编码失败")
        print("  [通过] OpenCV 与动态测试画面")
    except Exception as exc:
        failures.append(f"OpenCV：{exc}")
        print(f"  [失败] OpenCV：{exc}")
    try:
        import websockets  # noqa: F401

        print("  [通过] WebSocket 依赖")
    except Exception as exc:
        failures.append(f"WebSocket：{exc}")
        print(f"  [失败] WebSocket：{exc}")
    inputs, outputs = scan_audio_devices()
    print(f"  [信息] 麦克风 {len(inputs)} 个，扬声器 {len(outputs)} 个")
    cameras = scan_cameras(args.scan_camera_count, args.camera_backend)
    print(f"  [信息] 摄像头 {len(cameras)} 个；无摄像头时会自动使用测试画面")
    try:
        with urllib.request.urlopen(f"http://{args.host}:{args.port}/api/health", timeout=3) as response:
            if response.read().decode("utf-8").strip() != "OK":
                raise RuntimeError("健康检查返回异常")
        print("  [通过] 后端健康检查")
    except Exception as exc:
        failures.append(f"后端连接：{exc}")
        print(f"  [失败] 后端连接：{exc}")
    if failures:
        print("自检未通过：" + "；".join(failures))
        return 1
    print("自检全部通过。")
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI 眼镜本地硬件测试中心")
    parser.add_argument("--host", default="127.0.0.1", help="后端主机或 IP")
    parser.add_argument("--port", type=int, default=8081, help="后端端口")
    parser.add_argument("--profile", choices=PROFILES, default="balanced", help="性能档位")
    parser.add_argument("--camera-index", type=int, default=0, help="摄像头序号")
    parser.add_argument("--camera-backend", choices=["auto", "msmf", "dshow"], default="auto")
    parser.add_argument("--width", type=int, default=None, help="覆盖档位宽度")
    parser.add_argument("--height", type=int, default=None, help="覆盖档位高度")
    parser.add_argument("--fps", type=float, default=None, help="覆盖档位采集帧率")
    parser.add_argument("--jpeg-quality", type=int, default=None, help="OpenCV JPEG 质量 1-100")
    parser.add_argument("--video-file", default="", help="循环播放本地视频文件")
    parser.add_argument("--synthetic", action="store_true", help="强制使用动态测试画面")
    parser.add_argument("--input-device", default=None, help="麦克风序号或名称")
    parser.add_argument("--output-device", default=None, help="扬声器序号或名称")
    parser.add_argument("--audio-on-start", action="store_true", help="启动后持续开启麦克风（默认关闭）")
    parser.add_argument("--no-camera", action="store_true", help="关闭摄像头模拟")
    parser.add_argument("--no-audio", action="store_true", help="关闭麦克风模拟")
    parser.add_argument("--no-playback", action="store_true", help="关闭后端语音播放")
    parser.add_argument("--no-imu", action="store_true", help="关闭 IMU 模拟")
    parser.add_argument("--headless", action="store_true", help="不显示测试中心窗口")
    parser.add_argument("--list-devices", action="store_true", help="列出本机音视频设备后退出")
    parser.add_argument("--self-test", action="store_true", help="执行依赖、设备和后端自检后退出")
    parser.add_argument("--scan-camera-count", type=int, default=6, help="扫描摄像头序号数量")
    args = parser.parse_args(argv)
    if args.jpeg_quality is not None:
        args.jpeg_quality = max(1, min(100, args.jpeg_quality))
    if args.fps is not None:
        args.fps = max(1.0, min(60.0, args.fps))
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_devices:
        return list_devices(args)
    if args.self_test:
        return run_self_test(args)
    if args.no_camera and args.no_audio and args.no_playback and args.no_imu:
        log("系统", "所有硬件通道均已关闭，没有可运行的测试")
        return 0

    runtime = RuntimeOptions(args)
    state = SimulatorState()
    imu = ImuState()
    slot = LatestJpegSlot(state)
    stop_event = threading.Event()
    audio_enabled_event = threading.Event()
    playback_enabled_event = threading.Event()
    if args.audio_on_start and not args.no_audio:
        audio_enabled_event.set()
    if not args.no_playback:
        playback_enabled_event.set()

    capture_thread = None
    if not args.no_camera:
        capture_thread = threading.Thread(
            target=camera_capture_worker,
            args=(runtime, state, slot, stop_event),
            name="local-camera-capture",
            daemon=True,
        )
        capture_thread.start()

    async_thread = threading.Thread(
        target=lambda: asyncio.run(
            run_async(
                args,
                runtime,
                state,
                imu,
                slot,
                stop_event,
                audio_enabled_event,
                playback_enabled_event,
            )
        ),
        name="simulator-network",
        daemon=True,
    )
    async_thread.start()
    log("系统", f"测试中心已启动，后端地址为 {args.host}:{args.port}")
    log("系统", "麦克风默认关闭；按住说话按钮时才会启动语音识别")

    try:
        if args.headless:
            while not stop_event.is_set():
                time.sleep(0.2)
        else:
            window = TestCenterWindow(
                args,
                runtime,
                state,
                imu,
                stop_event,
                audio_enabled_event,
                playback_enabled_event,
            )
            window.run()
    except KeyboardInterrupt:
        log("系统", "收到停止指令")
    finally:
        stop_event.set()
        audio_enabled_event.clear()
        playback_enabled_event.clear()
        async_thread.join(timeout=5.0)
        if capture_thread is not None:
            capture_thread.join(timeout=3.0)
        log("系统", "本地硬件测试中心已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
