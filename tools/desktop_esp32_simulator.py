#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Desktop ESP32 simulator for the AI glasses backend.

It emulates the hardware-side ESP32 endpoints:
- Camera: sends JPEG frames to ws://host:port/ws/camera
- Audio: sends START then 16 kHz / 16-bit / mono PCM to ws://host:port/ws_audio
- IMU: sends UDP JSON packets to host:12345 at 50 Hz

The IMU values are controlled by a small Tkinter window.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple


IMU_UDP_PORT = 12345
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHUNK_MS = 20
AUDIO_FRAMES_PER_CHUNK = AUDIO_SAMPLE_RATE * AUDIO_CHUNK_MS // 1000
AUDIO_BYTES_PER_CHUNK = AUDIO_FRAMES_PER_CHUNK * 2
STREAM_SAMPLE_RATE = 8000


def log(channel: str, message: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{channel}] {message}", flush=True)


class SharedFlag:
    def __init__(self, value: bool = False):
        self._value = value
        self._lock = threading.Lock()

    def set(self, value: bool) -> None:
        with self._lock:
            self._value = bool(value)

    def get(self) -> bool:
        with self._lock:
            return self._value


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

    def set_values(
        self,
        *,
        accel: Optional[Dict[str, float]] = None,
        gyro: Optional[Dict[str, float]] = None,
        wiggle_seconds: float = 0.0,
    ) -> None:
        with self._lock:
            if accel is not None:
                self.accel.update({k: float(v) for k, v in accel.items()})
            if gyro is not None:
                self.gyro.update({k: float(v) for k, v in gyro.items()})
            self.wiggle_until = time.time() + wiggle_seconds if wiggle_seconds > 0 else 0.0


async def camera_sender(args: argparse.Namespace, stop_event: threading.Event) -> None:
    try:
        import cv2
    except Exception as exc:
        log("CAMERA", f"OpenCV import failed; camera disabled: {exc}")
        return

    try:
        import websockets
    except Exception as exc:
        log("CAMERA", f"websockets import failed; camera disabled: {exc}")
        return

    uri = f"ws://{args.host}:{args.port}/ws/camera"
    frame_interval = 1.0 / max(1.0, float(args.fps))
    retry_delay = 2.0

    while not stop_event.is_set():
        capture = None
        try:
            log("CAMERA", f"connecting {uri}")
            async with websockets.connect(uri, max_size=None) as ws:
                log("CAMERA", "connected")
                capture = cv2.VideoCapture(args.camera_index)
                if not capture.isOpened():
                    log("CAMERA", f"could not open camera index {args.camera_index}; retrying")
                    await asyncio.sleep(retry_delay)
                    continue

                capture.set(cv2.CAP_PROP_FPS, float(args.fps))
                next_frame_at = time.monotonic()
                sent = 0

                while not stop_event.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        log("CAMERA", "frame capture failed; retrying")
                        await asyncio.sleep(0.2)
                        continue

                    ok, encoded = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), int(args.jpeg_quality)],
                    )
                    if not ok:
                        await asyncio.sleep(frame_interval)
                        continue

                    await ws.send(encoded.tobytes())
                    sent += 1
                    if sent % max(1, int(args.fps) * 5) == 0:
                        log("CAMERA", f"sent {sent} frames")

                    next_frame_at += frame_interval
                    sleep_for = next_frame_at - time.monotonic()
                    if sleep_for > 0:
                        await asyncio.sleep(sleep_for)
                    else:
                        next_frame_at = time.monotonic()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            if not stop_event.is_set():
                log("CAMERA", f"disconnected/error: {exc}; reconnecting in {retry_delay:.1f}s")
                await asyncio.sleep(retry_delay)
        finally:
            if capture is not None:
                capture.release()

    log("CAMERA", "stopped")


def open_audio_input() -> Optional[Tuple[str, Callable[[], bytes], Callable[[], None]]]:
    try:
        import pyaudio
    except Exception as exc:
        log("AUDIO", f"PyAudio import failed, trying sounddevice fallback: {exc}")
    else:
        pa = None
        stream = None
        try:
            pa = pyaudio.PyAudio()
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=AUDIO_SAMPLE_RATE,
                input=True,
                frames_per_buffer=AUDIO_FRAMES_PER_CHUNK,
            )

            def read_pyaudio() -> bytes:
                return stream.read(AUDIO_FRAMES_PER_CHUNK, exception_on_overflow=False)

            def close_pyaudio() -> None:
                with contextlib.suppress(Exception):
                    stream.stop_stream()
                with contextlib.suppress(Exception):
                    stream.close()
                with contextlib.suppress(Exception):
                    pa.terminate()

            return "PyAudio", read_pyaudio, close_pyaudio
        except Exception as exc:
            log("AUDIO", f"PyAudio microphone open failed, trying sounddevice fallback: {exc}")
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
            if pa is not None:
                with contextlib.suppress(Exception):
                    pa.terminate()

    try:
        import sounddevice as sd
    except Exception as exc:
        log("AUDIO", f"sounddevice import failed; audio disabled: {exc}")
        log("AUDIO", "Install hint: pip install sounddevice, or pip install pyaudio")
        return None

    stream = None
    try:
        stream = sd.RawInputStream(
            samplerate=AUDIO_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=AUDIO_FRAMES_PER_CHUNK,
        )
        stream.start()

        def read_sounddevice() -> bytes:
            data, overflowed = stream.read(AUDIO_FRAMES_PER_CHUNK)
            if overflowed:
                log("AUDIO", "sounddevice input overflow")
            return bytes(data)

        def close_sounddevice() -> None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()

        return "sounddevice", read_sounddevice, close_sounddevice
    except Exception as exc:
        log("AUDIO", f"sounddevice microphone open failed; audio disabled: {exc}")
        log("AUDIO", "Check Windows microphone permission/default input device, or run with --no-audio")
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()
        return None


async def audio_sender(
    args: argparse.Namespace,
    stop_event: threading.Event,
    audio_enabled_event: threading.Event,
) -> None:
    try:
        import websockets
    except Exception as exc:
        log("AUDIO", f"websockets import failed; audio disabled: {exc}")
        return

    uri = f"ws://{args.host}:{args.port}/ws_audio"
    retry_delay = 2.0
    restart_requested = False

    async def log_ws_replies(ws) -> None:
        nonlocal restart_requested
        try:
            async for reply in ws:
                if isinstance(reply, str):
                    log("AUDIO", f"server reply: {reply}")
                    if reply.strip().upper() == "RESTART":
                        restart_requested = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not stop_event.is_set():
                log("AUDIO", f"server reply reader stopped: {exc}")

    while not stop_event.is_set():
        audio_input = None
        streaming = False
        reply_task = None
        try:
            log("AUDIO", f"connecting {uri}")
            async with websockets.connect(uri, max_size=None) as ws:
                log("AUDIO", "connected; waiting for microphone switch")
                reply_task = asyncio.create_task(log_ws_replies(ws))
                sent = 0
                while not stop_event.is_set():
                    if restart_requested:
                        restart_requested = False
                        log("AUDIO", "server requested ASR restart; reopening microphone stream")
                        if streaming:
                            with contextlib.suppress(Exception):
                                await ws.send("STOP")
                            streaming = False
                        if audio_input is not None:
                            _, _, close_audio = audio_input
                            close_audio()
                            audio_input = None
                        await asyncio.sleep(0.2)
                        continue

                    if not audio_enabled_event.is_set():
                        if streaming:
                            log("AUDIO", "switch OFF; sending STOP")
                            with contextlib.suppress(Exception):
                                await ws.send("STOP")
                            streaming = False
                            if audio_input is not None:
                                _, _, close_audio = audio_input
                                close_audio()
                                audio_input = None
                        await asyncio.sleep(0.1)
                        continue

                    if audio_input is None:
                        audio_input = open_audio_input()
                        if audio_input is None:
                            log("AUDIO", "microphone unavailable; switch OFF and retry manually")
                            audio_enabled_event.clear()
                            continue
                        backend_name, _read_audio, _close_audio = audio_input
                        log("AUDIO", f"capturing microphone with {backend_name}; sending START")
                        await ws.send("START")
                        streaming = True
                        sent = 0

                    _, read_audio, _close_audio = audio_input
                    chunk = await asyncio.to_thread(read_audio)
                    if len(chunk) != AUDIO_BYTES_PER_CHUNK:
                        log("AUDIO", f"unexpected chunk size {len(chunk)}")
                    await ws.send(chunk)
                    sent += 1
                    if sent % 250 == 0:
                        log("AUDIO", f"sent {sent} audio chunks")

                if streaming:
                    with contextlib.suppress(Exception):
                        await ws.send("STOP")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            if not stop_event.is_set():
                log("AUDIO", f"disconnected/error: {exc}; reconnecting in {retry_delay:.1f}s")
                await asyncio.sleep(retry_delay)
        finally:
            if reply_task is not None:
                reply_task.cancel()
                with contextlib.suppress(Exception):
                    await reply_task
            if audio_input is not None:
                _, _, close_audio = audio_input
                close_audio()

    log("AUDIO", "stopped")


async def imu_udp_sender(args: argparse.Namespace, state: ImuState, stop_event: threading.Event) -> None:
    target = (args.host, IMU_UDP_PORT)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / 50.0
    sent = 0
    log("IMU", f"sending UDP packets to {target[0]}:{target[1]}")

    try:
        while not stop_event.is_set():
            snap = state.snapshot()
            payload = {
                "ts": int(time.time() * 1000),
                "accel": snap["accel"],
                "gyro": snap["gyro"],
            }
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            try:
                sock.sendto(data, target)
                sent += 1
                if sent % 250 == 0:
                    log("IMU", f"sent {sent} packets")
            except Exception as exc:
                log("IMU", f"send failed: {exc}")
            await asyncio.sleep(interval)
    finally:
        sock.close()
        log("IMU", "stopped")


def _pcm16_to_float32(pcm: bytes) -> "numpy.ndarray":
    import numpy as np

    data = np.frombuffer(pcm, dtype=np.int16)
    if data.size == 0:
        return data.astype(np.float32)
    return (data.astype(np.float32) / 32768.0).copy()


def _resample_to_44100(pcm16: bytes, src_rate: int) -> bytes:
    import numpy as np

    if not pcm16:
        return b""
    data = np.frombuffer(pcm16, dtype=np.int16)
    if data.size == 0:
        return b""
    if src_rate == 44100:
        return pcm16
    duration = data.size / float(src_rate)
    src_x = np.linspace(0.0, duration, num=data.size, endpoint=False)
    dst_count = max(1, int(duration * 44100))
    dst_x = np.linspace(0.0, duration, num=dst_count, endpoint=False)
    resampled = np.interp(dst_x, src_x, data.astype(np.float32)).astype(np.int16)
    return resampled.tobytes()


def open_stream_player() -> Optional[Tuple[Callable[[bytes], None], Callable[[], None]]]:
    try:
        import sounddevice as sd
    except Exception as exc:
        log("STREAM", f"sounddevice import failed for playback: {exc}")
        return None

    stream = None
    try:
        stream = sd.RawOutputStream(
            samplerate=44100,
            channels=1,
            dtype="int16",
            blocksize=1024,
        )
        stream.start()

        def play_pcm16(pcm16: bytes) -> None:
            if not pcm16:
                return
            stream.write(_resample_to_44100(pcm16, STREAM_SAMPLE_RATE))

        def close_player() -> None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()

        return play_pcm16, close_player
    except Exception as exc:
        log("STREAM", f"sounddevice playback open failed: {exc}")
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()
        return None


async def stream_wav_player(args: argparse.Namespace, stop_event: threading.Event, enabled_event: threading.Event) -> None:
    try:
        import urllib.request
    except Exception as exc:
        log("STREAM", f"urllib import failed; playback disabled: {exc}")
        return

    uri = f"http://{args.host}:{args.port}/stream.wav"
    retry_delay = 2.0

    while not stop_event.is_set():
        player = None
        try:
            while not enabled_event.is_set() and not stop_event.is_set():
                await asyncio.sleep(0.2)
            if stop_event.is_set():
                break
            log("STREAM", f"connecting {uri}")
            response = await asyncio.to_thread(urllib.request.urlopen, uri, timeout=10)
            log("STREAM", "connected")
            header = await asyncio.to_thread(response.read, 44)
            if not header.startswith(b"RIFF"):
                log("STREAM", "unexpected stream header; continuing anyway")
            player = open_stream_player()
            if player is None:
                log("STREAM", "output device unavailable; playback disabled")
                return
            play_pcm16, close_player = player
            while not stop_event.is_set() and enabled_event.is_set():
                chunk = await asyncio.to_thread(response.read, 4096)
                if not chunk:
                    break
                await asyncio.to_thread(play_pcm16, chunk)
            with contextlib.suppress(Exception):
                response.close()
            close_player()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            if not stop_event.is_set():
                log("STREAM", f"disconnected/error: {exc}; reconnecting in {retry_delay:.1f}s")
                await asyncio.sleep(retry_delay)
        finally:
            if player is not None:
                _, close_player = player
                with contextlib.suppress(Exception):
                    close_player()
        await asyncio.sleep(0.2)

    log("STREAM", "stopped")


async def run_async(args: argparse.Namespace, state: ImuState, stop_event: threading.Event) -> None:
    tasks = []
    if not args.no_camera:
        tasks.append(asyncio.create_task(camera_sender(args, stop_event)))
    if not args.no_audio:
        tasks.append(asyncio.create_task(audio_sender(args, stop_event, args.audio_enabled_event)))
    if not args.no_imu:
        tasks.append(asyncio.create_task(imu_udp_sender(args, state, stop_event)))
    if not args.no_playback:
        tasks.append(asyncio.create_task(stream_wav_player(args, stop_event, args.playback_enabled_event)))

    if not tasks:
        log("MAIN", "all channels are disabled; nothing to run")
        return

    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class ImuControlWindow:
    def __init__(
        self,
        args: argparse.Namespace,
        state: ImuState,
        stop_event: threading.Event,
        audio_enabled_event: threading.Event,
        playback_enabled_event: threading.Event,
    ):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.args = args
        self.state = state
        self.stop_event = stop_event
        self.audio_enabled_event = audio_enabled_event
        self.playback_enabled_event = playback_enabled_event
        self.root = tk.Tk()
        self.root.title("Desktop ESP32 Simulator - IMU")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.vars: Dict[str, tk.DoubleVar] = {}
        self.audio_var = tk.BooleanVar(value=audio_enabled_event.is_set())
        self.playback_var = tk.BooleanVar(value=playback_enabled_event.is_set())
        self.audio_status_var = tk.StringVar(value="")
        self.playback_status_var = tk.StringVar(value="")

        self._build()
        self._apply_values({"ax": 0.0, "ay": 9.807, "az": 0.0, "gx": 0.0, "gy": 0.0, "gz": 0.0})
        self._sync_audio_status()
        self._sync_playback_status()

    def _build(self) -> None:
        root = self.root
        ttk = self.ttk
        tk = self.tk

        frame = ttk.Frame(root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        title = ttk.Label(frame, text=f"Target: {self.args.host}:{self.args.port}  |  IMU UDP: {self.args.host}:{IMU_UDP_PORT}")
        title.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        specs = [
            ("ax", "Accel X", -20.0, 20.0, "accel", "x"),
            ("ay", "Accel Y", -20.0, 20.0, "accel", "y"),
            ("az", "Accel Z", -20.0, 20.0, "accel", "z"),
            ("gx", "Gyro X", -180.0, 180.0, "gyro", "x"),
            ("gy", "Gyro Y", -180.0, 180.0, "gyro", "y"),
            ("gz", "Gyro Z", -180.0, 180.0, "gyro", "z"),
        ]

        for row, (key, label, low, high, group, axis) in enumerate(specs, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            var = tk.DoubleVar(value=0.0)
            self.vars[key] = var
            scale = ttk.Scale(
                frame,
                from_=low,
                to=high,
                variable=var,
                command=lambda value, g=group, a=axis: self.state.set_axis(g, a, float(value)),
            )
            scale.grid(row=row, column=1, sticky="ew", pady=4)
            value_label = ttk.Label(frame, width=8)
            value_label.grid(row=row, column=2, sticky="e", padx=(8, 0), pady=4)
            var.trace_add("write", lambda *_args, v=var, lbl=value_label: lbl.config(text=f"{v.get():.2f}"))

        frame.columnconfigure(1, weight=1)

        audio_box = ttk.LabelFrame(frame, text="Audio / ASR", padding=8)
        audio_box.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        audio_box.columnconfigure(1, weight=1)

        self.audio_toggle = ttk.Checkbutton(
            audio_box,
            text="Mic stream recognition",
            variable=self.audio_var,
            command=self.toggle_audio,
        )
        self.audio_toggle.grid(row=0, column=0, sticky="w")
        ttk.Label(audio_box, textvariable=self.audio_status_var).grid(row=0, column=1, sticky="w", padx=(12, 0))
        if self.args.no_audio:
            self.audio_toggle.state(["disabled"])

        playback_box = ttk.LabelFrame(frame, text="Playback", padding=8)
        playback_box.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        playback_box.columnconfigure(1, weight=1)

        self.playback_toggle = ttk.Checkbutton(
            playback_box,
            text="Play backend speech",
            variable=self.playback_var,
            command=self.toggle_playback,
        )
        self.playback_toggle.grid(row=0, column=0, sticky="w")
        ttk.Label(playback_box, textvariable=self.playback_status_var).grid(row=0, column=1, sticky="w", padx=(12, 0))
        if self.args.no_playback:
            self.playback_toggle.state(["disabled"])

        presets = ttk.LabelFrame(frame, text="Presets", padding=8)
        presets.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        for col in range(5):
            presets.columnconfigure(col, weight=1)

        ttk.Button(presets, text="Still", command=self.preset_still).grid(row=0, column=0, sticky="ew", padx=3)
        ttk.Button(presets, text="Tilt Forward", command=self.preset_forward).grid(row=0, column=1, sticky="ew", padx=3)
        ttk.Button(presets, text="Turn Left", command=self.preset_left).grid(row=0, column=2, sticky="ew", padx=3)
        ttk.Button(presets, text="Turn Right", command=self.preset_right).grid(row=0, column=3, sticky="ew", padx=3)
        ttk.Button(presets, text="Wiggle", command=self.preset_wiggle).grid(row=0, column=4, sticky="ew", padx=3)

        status = ttk.Label(frame, text="Close this window or press Ctrl+C in the terminal to stop.")
        status.grid(row=10, column=0, columnspan=4, sticky="w", pady=(10, 0))

    def _sync_audio_status(self) -> None:
        if self.args.no_audio:
            self.audio_status_var.set("disabled by --no-audio")
            return
        if self.audio_enabled_event.is_set():
            self.audio_status_var.set("ON: sending START + microphone PCM")
        else:
            self.audio_status_var.set("OFF: ASR stream stopped")

    def _sync_playback_status(self) -> None:
        if self.args.no_playback:
            self.playback_status_var.set("disabled")
        elif self.playback_enabled_event.is_set():
            self.playback_status_var.set("ON: playing /stream.wav")
        else:
            self.playback_status_var.set("OFF: speech muted")

    def toggle_audio(self) -> None:
        if self.audio_var.get():
            self.audio_enabled_event.set()
        else:
            self.audio_enabled_event.clear()
        self._sync_audio_status()

    def toggle_playback(self) -> None:
        if self.playback_var.get():
            self.playback_enabled_event.set()
        else:
            self.playback_enabled_event.clear()
        self._sync_playback_status()

    def _apply_values(self, values: Dict[str, float], wiggle_seconds: float = 0.0) -> None:
        for key, value in values.items():
            if key in self.vars:
                self.vars[key].set(float(value))
        self.state.set_values(
            accel={"x": self.vars["ax"].get(), "y": self.vars["ay"].get(), "z": self.vars["az"].get()},
            gyro={"x": self.vars["gx"].get(), "y": self.vars["gy"].get(), "z": self.vars["gz"].get()},
            wiggle_seconds=wiggle_seconds,
        )

    def preset_still(self) -> None:
        self._apply_values({"ax": 0.0, "ay": 9.807, "az": 0.0, "gx": 0.0, "gy": 0.0, "gz": 0.0})

    def preset_forward(self) -> None:
        self._apply_values({"ax": -3.0, "ay": 9.2, "az": 0.0, "gx": 0.0, "gy": 0.0, "gz": 0.0})

    def preset_left(self) -> None:
        self._apply_values({"ax": 0.0, "ay": 9.807, "az": 0.0, "gx": 0.0, "gy": 35.0, "gz": 0.0})

    def preset_right(self) -> None:
        self._apply_values({"ax": 0.0, "ay": 9.807, "az": 0.0, "gx": 0.0, "gy": -35.0, "gz": 0.0})

    def preset_wiggle(self) -> None:
        self._apply_values({"ax": 0.0, "ay": 9.807, "az": 0.0, "gx": 0.0, "gy": 0.0, "gz": 0.0}, wiggle_seconds=4.0)

    def close(self) -> None:
        self.audio_enabled_event.clear()
        self.playback_enabled_event.clear()
        self.stop_event.set()
        self.root.after(50, self.root.destroy)

    def run(self) -> None:
        def poll_stop() -> None:
            if self.stop_event.is_set():
                with contextlib.suppress(Exception):
                    self.root.destroy()
                return
            self.root.after(100, poll_stop)

        self.root.after(100, poll_stop)
        self.root.mainloop()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Desktop ESP32 hardware simulator")
    parser.add_argument("--host", default="127.0.0.1", help="Backend host or IP")
    parser.add_argument("--port", type=int, default=8081, help="Backend WebSocket/HTTP port")
    parser.add_argument("--camera-index", type=int, default=0, help="OpenCV camera index")
    parser.add_argument("--fps", type=float, default=15.0, help="Camera frame rate")
    parser.add_argument("--jpeg-quality", type=int, default=80, help="JPEG quality 1-100")
    parser.add_argument("--no-audio", action="store_true", help="Disable microphone simulation")
    parser.add_argument("--no-playback", action="store_true", help="Disable backend speech playback")
    parser.add_argument(
        "--audio-on-start",
        action="store_true",
        help="Start microphone stream immediately (default when audio is enabled)",
    )
    parser.add_argument("--no-camera", action="store_true", help="Disable camera simulation")
    parser.add_argument("--no-imu", action="store_true", help="Disable IMU UDP simulation")
    args = parser.parse_args(argv)
    args.jpeg_quality = max(1, min(100, int(args.jpeg_quality)))
    args.fps = max(1.0, float(args.fps))
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.no_camera and args.no_audio and args.no_imu and args.no_playback:
        log("MAIN", "all channels are disabled; nothing to run")
        return 0

    state = ImuState()
    stop_event = threading.Event()
    audio_enabled_event = threading.Event()
    playback_enabled_event = threading.Event()
    args.audio_enabled_event = audio_enabled_event
    args.playback_enabled_event = playback_enabled_event
    if not args.no_audio:
        audio_enabled_event.set()
    if not args.no_playback:
        playback_enabled_event.set()

    log("MAIN", f"camera ws: ws://{args.host}:{args.port}/ws/camera")
    log("MAIN", f"audio  ws: ws://{args.host}:{args.port}/ws_audio")
    log("MAIN", f"playback: http://{args.host}:{args.port}/stream.wav")
    log("MAIN", f"imu   udp: {args.host}:{IMU_UDP_PORT}")

    loop_thread = threading.Thread(
        target=lambda: asyncio.run(run_async(args, state, stop_event)),
        name="simulator-asyncio",
        daemon=True,
    )
    loop_thread.start()

    try:
        if args.no_imu:
            while not stop_event.is_set():
                time.sleep(0.2)
        else:
            try:
                window = ImuControlWindow(args, state, stop_event, audio_enabled_event, playback_enabled_event)
                window.run()
            except Exception as exc:
                log("IMU", f"Tkinter window failed; IMU still sends default values: {exc}")
                while not stop_event.is_set():
                    time.sleep(0.2)
    except KeyboardInterrupt:
        log("MAIN", "Ctrl+C received")
    finally:
        stop_event.set()
        loop_thread.join(timeout=5.0)
        log("MAIN", "stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
