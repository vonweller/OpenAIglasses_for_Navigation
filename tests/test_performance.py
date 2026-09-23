import time
import unittest

from aiglasses.performance import (
    DEFAULT_OVERLAY_JPEG_QUALITY,
    DEFAULT_PROFILE,
    PROFILES,
    PipelineMetrics,
    normalize_profile,
    overlay_jpeg_quality_for,
    preview_fps_for,
    profile_payload,
)


class PerformanceTests(unittest.TestCase):
    def test_performance_profile_defaults_and_values(self):
        self.assertEqual(normalize_profile("unknown"), DEFAULT_PROFILE)
        self.assertEqual(normalize_profile("SMOOTH"), "smooth")
        balanced = profile_payload("balanced")
        self.assertEqual(balanced["framesize"], "VGA")
        self.assertEqual(balanced["width"], 640)
        self.assertEqual(balanced["height"], 480)
        self.assertEqual(balanced["camera_fps"], 20)
        self.assertEqual(balanced["jpeg_quality"], 12)
        self.assertEqual(balanced["yolo_imgsz"], 512)
        self.assertEqual(balanced["inference_hz"], 10.0)
        self.assertEqual(balanced["preview_fps"], 30)
        self.assertEqual(balanced["device_family"], "esp32")
        self.assertNotEqual(balanced["preview_fps"], balanced["inference_hz"])

        smooth = profile_payload("smooth")
        self.assertEqual((smooth["framesize"], smooth["camera_fps"], smooth["jpeg_quality"]), ("VGA", 24, 14))
        quality = profile_payload("quality")
        self.assertEqual((quality["framesize"], quality["width"], quality["height"]), ("SVGA", 800, 600))
        self.assertEqual((quality["camera_fps"], quality["jpeg_quality"]), (15, 10))

    def test_k230_profiles_keep_preview_independent_of_inference(self):
        expected = {
            "k230_hd": ("K230_HD", 1280, 720, 30, 24, 30),
            "k230_1k": ("K230_1K", 1280, 960, 25, 24, 25),
            "k230_1_5k": ("K230_1_5K", 1536, 864, 25, 24, 25),
            "k230_fhd": ("K230_FHD", 1920, 1080, 20, 26, 20),
        }
        for key, spec in expected.items():
            payload = profile_payload(key)
            self.assertEqual(payload["device_family"], "k230")
            self.assertEqual(
                (
                    payload["framesize"],
                    payload["width"],
                    payload["height"],
                    payload["camera_fps"],
                    payload["jpeg_quality"],
                    payload["preview_fps"],
                ),
                spec,
            )
            self.assertNotEqual(payload["preview_fps"], payload["inference_hz"])
            self.assertIn(key, PROFILES)

        self.assertEqual(preview_fps_for("k230_1k"), 25)
        self.assertEqual(preview_fps_for("balanced"), 30)
        self.assertEqual(preview_fps_for("missing"), preview_fps_for(DEFAULT_PROFILE))
        self.assertEqual(overlay_jpeg_quality_for("k230_fhd"), DEFAULT_OVERLAY_JPEG_QUALITY)
        self.assertGreater(overlay_jpeg_quality_for("k230_hd"), 24)

    def test_pipeline_metrics_snapshot(self):
        metrics = PipelineMetrics()
        now = time.time()
        metrics.on_capture(1024, now)
        metrics.on_capture(2048, now + 0.1)
        metrics.on_processed(captured_at=now, inference_ms=12.5, now=now + 0.12)
        metrics.on_broadcast(now + 0.13)
        metrics.on_output_drop()
        metrics.update_esp32_camera({"rssi": -45})
        snapshot = metrics.snapshot()
        self.assertGreater(snapshot["capture_fps"], 0)
        self.assertEqual(snapshot["inference_ms"], 12.5)
        self.assertEqual(snapshot["latency_ms"], 120.0)
        self.assertEqual(snapshot["output_dropped"], 1)
        self.assertEqual(snapshot["esp32_camera"]["rssi"], -45)


if __name__ == "__main__":
    unittest.main()
