import time
import unittest

from aiglasses.performance import DEFAULT_PROFILE, PipelineMetrics, normalize_profile, profile_payload


class PerformanceTests(unittest.TestCase):
    def test_performance_profile_defaults_and_values(self):
        self.assertEqual(normalize_profile("unknown"), DEFAULT_PROFILE)
        self.assertEqual(normalize_profile("SMOOTH"), "smooth")
        balanced = profile_payload("balanced")
        self.assertEqual(balanced["framesize"], "VGA")
        self.assertEqual(balanced["camera_fps"], 20)
        self.assertEqual(balanced["yolo_imgsz"], 416)

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
