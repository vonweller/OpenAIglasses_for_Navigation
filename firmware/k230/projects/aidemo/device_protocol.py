"""ESP32-compatible camera commands, independent of board hardware."""

FRAME_SIZES = {
    "QQVGA": (160, 120), "HQVGA": (240, 176), "QVGA": (320, 240),
    "CIF": (400, 296), "VGA": (640, 480), "SVGA": (800, 600),
    "XGA": (1024, 768), "K230_HD": (1280, 720),
    "K230_1K": (1280, 960), "K230_1_5K": (1536, 864),
    "K230_FHD": (1920, 1080),
}


class CameraSettings:
    def __init__(self, framesize="VGA", quality=14, fps=24):
        self.framesize = framesize
        self.quality = quality
        self.fps = fps

    @property
    def dimensions(self):
        return FRAME_SIZES[self.framesize]

    @property
    def jpeg_quality(self):
        # ESP32 uses lower values for higher quality; VENC uses the opposite scale.
        return max(40, min(95, 100 - self.quality))

    def apply(self, command):
        if not command.startswith("SET:") or "=" not in command:
            return False
        key, value = command[4:].split("=", 1)
        if key == "FRAMESIZE":
            value = value.upper()
            if value not in FRAME_SIZES:
                return False
            self.framesize = value
        elif key in ("QUALITY", "FPS"):
            try:
                value = int(value)
            except (ValueError, TypeError):
                return False
            if key == "QUALITY":
                self.quality = max(5, min(40, value))
            else:
                self.fps = 0 if value <= 0 else max(5, min(60, value))
        else:
            return False
        return True
