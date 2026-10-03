"""Public defaults; connection details belong only in k230_secrets.py."""

SERVER_PORT = 8081
CAM_WS_PATH = "/ws/camera?device=k230"
AUD_WS_PATH = "/ws_audio?device=k230"
WAV_STREAM_PATH = "/stream.wav"
FRAME_SIZE = "K230_1_5K"
JPEG_QUALITY = 24
TARGET_FPS = 25
SNAP_WIDTH = 1920
SNAP_HEIGHT = 1080
SNAP_QUALITY = 85
MIC_SAMPLE_RATE = 16000
MIC_CHUNK_MS = 20
MIC_CHANNELS = 1
MIC_CHANNEL = 1
MIC_VOLUME = 60
MIC_ENABLE_ANS = True
SPEAKER_ENABLED = True
SPK_VOLUME = 60
WIFI_TIMEOUT_S = 15
RECONNECT_MS = 2000
STATS_INTERVAL_MS = 2000
RUN_SECONDS = 0


def load_secrets():
    import k230_secrets

    values = {}
    for name in ("WIFI_SSID", "WIFI_PASSWORD", "SERVER_HOST", "SERVER_PORT"):
        if hasattr(k230_secrets, name):
            values[name] = getattr(k230_secrets, name)
    for name in ("WIFI_SSID", "WIFI_PASSWORD", "SERVER_HOST"):
        if not values.get(name):
            raise ValueError("Missing required private setting: " + name)
    values.setdefault("SERVER_PORT", SERVER_PORT)
    return values
