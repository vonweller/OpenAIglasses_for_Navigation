"""Low-latency K230 device client. Backend inference remains on the host."""

import gc
import json
import os
import sys
import time
import _thread
import network

import config
from audio_io import AudioIO
from camera import Camera
from device_protocol import CameraSettings
from ws_client import WSClient
from wav_stream import WavStream


def elapsed(start):
    return time.ticks_diff(time.ticks_ms(), start)


def log(component, message):
    print("[K230:%s] %s" % (component, message))


class Client:
    def __init__(self):
        self.private = config.load_secrets()
        self.running = True
        self.closed = False
        self.wifi = network.WLAN(network.STA_IF)
        self.camera = Camera()
        self.audio = AudioIO(config)
        self.settings = CameraSettings(config.FRAME_SIZE, config.JPEG_QUALITY, config.TARGET_FPS)
        self.mic_done = True
        self.play_done = True
        self.cam_ws = None
        self.mic_ws = None
        self.wav = None
        self.asr_ready = False
        self.audio_sent = 0
        self.audio_dropped = 0
        self.played_bytes = 0
        self.play_connects = 0
        self.wifi_attempt = time.ticks_add(time.ticks_ms(), -config.RECONNECT_MS)

    def websocket(self, path, queue=3):
        return WSClient(
            self.private["SERVER_HOST"], self.private["SERVER_PORT"], path,
            max_queue=queue, max_message=524288,
            connect_timeout_ms=1000, handshake_timeout_ms=1500,
        )

    def ensure_wifi(self):
        if self.wifi.isconnected():
            return True
        if elapsed(self.wifi_attempt) >= config.RECONNECT_MS:
            self.wifi_attempt = time.ticks_ms()
            try:
                self.wifi.connect(self.private["WIFI_SSID"], self.private["WIFI_PASSWORD"])
            except OSError:
                log("wifi", "connection attempt failed")
        return False

    def microphone_worker(self):
        ws = self.websocket(config.AUD_WS_PATH, 12)
        self.mic_ws = ws
        retry = time.ticks_add(time.ticks_ms(), -config.RECONNECT_MS)
        last_start = time.ticks_ms()
        try:
            while self.running:
                if not self.wifi.isconnected():
                    ws.close()
                    self.asr_ready = False
                elif not ws.connected and elapsed(retry) >= config.RECONNECT_MS:
                    retry = time.ticks_ms()
                    if ws.connect():
                        ws.queue_text("START")
                        last_start = time.ticks_ms()
                        self.asr_ready = False
                        log("mic", "connected; waiting for ASR acknowledgement")
                if ws.connected:
                    for message in ws.poll():
                        if message == "OK:STARTED":
                            self.asr_ready = True
                            log("mic", "ASR ready")
                        elif message in ("RESTART", "RESET"):
                            # RESET resets the backend session, never reboots the board.
                            self.asr_ready = False
                            ws.close()
                            retry = time.ticks_add(time.ticks_ms(), -config.RECONNECT_MS)
                        elif message.startswith("ERR:"):
                            self.asr_ready = False
                            log("mic", message.split("\n", 1)[0][:80])
                            ws.close()
                            retry = time.ticks_ms()
                    if not self.asr_ready and elapsed(last_start) > 10000:
                        ws.close()
                pcm = self.audio.read_mic()
                if pcm is not None:
                    if ws.connected and self.asr_ready:
                        if ws.queue_audio(pcm):
                            self.audio_sent += 1
                        else:
                            self.audio_dropped += 1
                    else:
                        self.audio_dropped += 1
                time.sleep_ms(1)
        except Exception as exc:
            log("mic", "stopped: " + type(exc).__name__)
            sys.print_exception(exc)
        finally:
            self.asr_ready = False
            ws.close()
            self.mic_done = True

    def playback_worker(self):
        stream = WavStream(
            self.private["SERVER_HOST"], self.private["SERVER_PORT"],
            config.WAV_STREAM_PATH, max_buffer=3200, header_timeout_ms=3000,
        )
        self.wav = stream
        retry = time.ticks_add(time.ticks_ms(), -config.RECONNECT_MS)
        pending = b""
        pending_at = time.ticks_ms()
        try:
            while self.running:
                if not self.wifi.isconnected():
                    stream.close()
                    pending = b""
                elif not stream.connected and elapsed(retry) >= config.RECONNECT_MS:
                    retry = time.ticks_ms()
                    pending = b""
                    if stream.connect():
                        self.play_connects += 1
                        log("speaker", "stream connected")
                if stream.connected:
                    stream.poll()
                    pcm = stream.read_pcm(320 - len(pending))
                    if pcm:
                        if not pending:
                            pending_at = time.ticks_ms()
                        pending += pcm
                if pending and (len(pending) == 320 or elapsed(pending_at) >= 40):
                    self.audio.play(pending)
                    self.played_bytes += len(pending)
                    pending = b""
                time.sleep_ms(2)
        except Exception as exc:
            log("speaker", "stopped: " + type(exc).__name__)
            sys.print_exception(exc)
        finally:
            stream.close()
            self.play_done = True

    def snapshot(self, ws, restore):
        deadline = time.ticks_ms()
        while ws.connected and ws.pending and elapsed(deadline) < 2000:
            ws.poll()
            time.sleep_ms(2)
        if not ws.connected or ws.pending:
            return
        try:
            self.camera.open(config.SNAP_WIDTH, config.SNAP_HEIGHT, config.SNAP_QUALITY)
            frame = self.camera.capture(1000)
            if frame is None:
                return
            if not ws.queue_text("SNAP:BEGIN"):
                return
            if not ws.queue_binary(frame):
                ws.close()
                return
            if not ws.queue_text("SNAP:END"):
                ws.close()
                return
            deadline = time.ticks_ms()
            while ws.connected and ws.pending and elapsed(deadline) < 5000:
                ws.poll()
                time.sleep_ms(2)
        finally:
            self.camera.open(*restore)

    def run(self, duration=0):
        os.exitpoint(os.EXITPOINT_ENABLE)
        start = time.ticks_ms()
        while not self.ensure_wifi():
            os.exitpoint()
            if elapsed(start) > config.WIFI_TIMEOUT_S * 1000:
                log("wifi", "not connected; retrying without resetting the board")
                start = time.ticks_ms()
            time.sleep_ms(100)
        log("wifi", "connected")
        self.audio.open()
        self.mic_done = False
        _thread.start_new_thread(self.microphone_worker, ())
        if config.SPEAKER_ENABLED:
            self.play_done = False
            _thread.start_new_thread(self.playback_worker, ())
        ws = self.websocket(config.CAM_WS_PATH, 3)
        self.cam_ws = ws
        ws.send_chunk = 16384
        retry = time.ticks_add(time.ticks_ms(), -config.RECONNECT_MS)
        started = time.ticks_ms()
        stats_at = started
        next_frame = started
        last_gc = started
        changed_at = started
        active = None
        snapshot_requested = False
        timing = {"capture": 0, "network": 0}
        captures = sends = 0
        completed_frames = 0
        jpeg_bytes = 0
        try:
            while self.running:
                os.exitpoint()
                if duration and elapsed(started) >= duration * 1000:
                    break
                if not self.ensure_wifi():
                    ws.close()
                    time.sleep_ms(50)
                    continue
                if not ws.connected and elapsed(retry) >= config.RECONNECT_MS:
                    retry = time.ticks_ms()
                    if ws.connect():
                        changed_at = time.ticks_ms()
                        log("camera", "connected")
                if ws.connected:
                    network_at = time.ticks_ms()
                    messages = ws.poll()
                    timing["network"] += elapsed(network_at)
                    for message in messages:
                        if message == "SNAP:HQ":
                            snapshot_requested = True
                        elif self.settings.apply(message):
                            changed_at = time.ticks_ms()
                desired = self.settings.dimensions + (self.settings.jpeg_quality, self.settings.fps)
                if desired != active and elapsed(changed_at) >= 150:
                    self.camera.open(*desired)
                    active = desired
                    log("camera", "%dx%d hardware JPEG Q%d @ %dfps" % active)
                if snapshot_requested and active is not None and ws.connected:
                    snapshot_requested = False
                    self.snapshot(ws, active)
                    next_frame = time.ticks_ms()
                if active is not None and (not ws.connected or ws.pending == 0):
                    capture_at = time.ticks_ms()
                    frame = self.camera.capture(0)
                    timing["capture"] += elapsed(capture_at)
                    if frame is not None:
                        captures += 1
                        now = time.ticks_ms()
                        due = not self.settings.fps or time.ticks_diff(now, next_frame) >= 0
                        if ws.connected and due:
                            if ws.queue_binary(frame, latest=True):
                                sends += 1
                                period = max(1, 1000 // max(1, self.settings.fps))
                                next_frame = time.ticks_add(next_frame, period)
                                if time.ticks_diff(now, next_frame) > period:
                                    next_frame = time.ticks_add(now, period)
                                jpeg_bytes = len(frame)
                if elapsed(stats_at) >= config.STATS_INTERVAL_MS:
                    seconds = max(0.001, elapsed(stats_at) / 1000)
                    status = {
                        "device": "k230", "encoder": "hardware_jpeg",
                        "capture_fps": round(captures / seconds, 1),
                        "capture_ms": round(timing["capture"] / max(1, captures), 1),
                        "send_ms": round(timing["network"] / max(1, sends), 1),
                        "send_fps": round(max(0, ws.stats.get("binary_sent", 0) - completed_frames) / seconds, 1),
                        "queued_fps": round(sends / seconds, 1),
                        "jpeg_bytes": jpeg_bytes, "free_heap": gc.mem_free(),
                        "dropped": ws.stats.get("dropped", 0),
                        "target_fps": self.settings.fps, "jpeg_quality": self.settings.quality,
                        "framesize_name": self.settings.framesize,
                        "audio_blocks": self.audio_sent,
                        "audio_dropped": self.audio_dropped + (self.mic_ws.stats.get("dropped", 0) if self.mic_ws else 0),
                        "audio_sent_frames": self.mic_ws.stats.get("sent", 0) if self.mic_ws else 0,
                        "mic_channels": self.audio.input_channels, "mic_volume": config.MIC_VOLUME,
                        "mic_ans": config.MIC_ENABLE_ANS,
                        "asr_ready": self.asr_ready, "played_bytes": self.played_bytes,
                        "play_connects": self.play_connects,
                        "mic_worker": not self.mic_done,
                        "speaker_worker": config.SPEAKER_ENABLED and not self.play_done,
                    }
                    if ws.connected:
                        ws.queue_text("STAT:" + json.dumps(status))
                    log("stats", json.dumps(status))
                    captures = sends = 0
                    completed_frames = ws.stats.get("binary_sent", 0)
                    timing = {"capture": 0, "network": 0}
                    stats_at = time.ticks_ms()
                if elapsed(last_gc) >= 1000:
                    gc.collect()
                    last_gc = time.ticks_ms()
                time.sleep_ms(1)
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.running = False
        if self.cam_ws is not None:
            self.cam_ws.close()
        self.camera.close()
        # Worker-owned sockets have bounded calls; don't free audio while in use.
        start = time.ticks_ms()
        while not (self.mic_done and self.play_done) and elapsed(start) < 8000:
            time.sleep_ms(20)
        if self.mic_done and self.play_done:
            self.audio.close()
            self.closed = True
            log("system", "all workers stopped; media released")
        else:
            log("system", "worker still active; audio resources deliberately retained")
        gc.collect()


def main(duration=0):
    client = Client()
    try:
        client.run(duration or config.RUN_SECONDS)
    except KeyboardInterrupt:
        log("system", "interrupted")
    except Exception as exc:
        sys.print_exception(exc)
    finally:
        client.close()


if __name__ == "__main__":
    main()
