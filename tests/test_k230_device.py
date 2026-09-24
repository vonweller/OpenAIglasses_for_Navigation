import importlib.util
from pathlib import Path
import struct
import unittest


ROOT = Path(__file__).resolve().parents[1] / "firmware" / "k230"


def load(name):
    spec = importlib.util.spec_from_file_location("k230_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


protocol = load("device_protocol")
audio = load("audio_io")


class CameraSettingsTests(unittest.TestCase):
    def test_frame_sizes(self):
        settings = protocol.CameraSettings()
        for name, expected in protocol.FRAME_SIZES.items():
            self.assertTrue(settings.apply("SET:FRAMESIZE=" + name.lower()))
            self.assertEqual(settings.dimensions, expected)
        self.assertFalse(settings.apply("SET:FRAMESIZE=UNKNOWN"))
        self.assertEqual(settings.framesize, "K230_FHD")

    def test_quality_scale_and_limits(self):
        settings = protocol.CameraSettings()
        settings.apply("SET:QUALITY=5")
        high = settings.jpeg_quality
        settings.apply("SET:QUALITY=99")
        self.assertEqual(settings.quality, 40)
        self.assertLess(settings.jpeg_quality, high)
        settings.apply("SET:QUALITY=-1")
        self.assertEqual(settings.quality, 5)

    def test_fps_and_malformed_commands(self):
        settings = protocol.CameraSettings()
        for command, expected in (("0", 0), ("-2", 0), ("2", 5), ("100", 60)):
            self.assertTrue(settings.apply("SET:FPS=" + command))
            self.assertEqual(settings.fps, expected)
        for command in ("SET:FPS=nan", "SET:FPS", "SET:OTHER=2", "RESET", "set:FPS=2"):
            self.assertFalse(settings.apply(command))
        self.assertEqual(settings.fps, 60)


class AudioTests(unittest.TestCase):
    def test_right_channel_preserves_signed_samples(self):
        pcm = struct.pack("<8h", 1, -32768, 2, -1, 3, 0, 4, 32767)
        self.assertEqual(audio.extract_channel(pcm), struct.pack("<4h", -32768, -1, 0, 32767))
        self.assertEqual(audio.extract_channel(pcm, 0), struct.pack("<4h", 1, 2, 3, 4))

    def test_rejects_misaligned_audio(self):
        for pcm, channel in ((b"x", 1), (b"xx", 1), (b"xxxx", 2)):
            with self.assertRaises(ValueError):
                audio.extract_channel(pcm, channel)

    def test_twenty_millisecond_frame(self):
        pcm = struct.pack("<640h", *range(640))
        mono = audio.extract_channel(pcm)
        self.assertEqual(len(mono), 640)
        self.assertEqual(struct.unpack("<320h", mono), tuple(range(1, 640, 2)))

    def test_resampling_preserves_duration_and_polarity(self):
        pcm = struct.pack("<4h", -32768, -1, 0, 32767)
        self.assertEqual(
            audio.upsample_8k_to_16k(pcm),
            struct.pack("<8h", -32768, -32768, -1, -1, 0, 0, 32767, 32767),
        )

    def test_playback_padding(self):
        class Stream:
            data = None
            def write(self, data):
                self.data = data
        io = audio.AudioIO(None)
        io.output = Stream()
        io.play(b"\x01\x00")
        self.assertEqual(io.output.data, b"\x01\x00\x01\x00" + bytes(636))
        with self.assertRaises(ValueError):
            io.play(b"\x01")


if __name__ == "__main__":
    unittest.main()
