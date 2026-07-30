import cv2
import numpy as np
import unittest

from aiglasses import bridge_io


def _jpeg(value: int) -> bytes:
    image = np.full((24, 32, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


class BridgeIoTests(unittest.TestCase):
    def test_wait_next_frame_does_not_repeat_stale_frame(self):
        bridge_io.clear_raw_frames()
        bridge_io.push_raw_jpeg(_jpeg(10))
        first = bridge_io.wait_next_raw_bgr(0, timeout_sec=0.1)
        self.assertIsNotNone(first)
        self.assertIsNone(bridge_io.wait_next_raw_bgr(first.seq, timeout_sec=0.02))

        bridge_io.push_raw_jpeg(_jpeg(20))
        second = bridge_io.wait_next_raw_bgr(first.seq, timeout_sec=0.1)
        self.assertIsNotNone(second)
        self.assertGreater(second.seq, first.seq)

    def test_latest_frame_buffer_drops_older_frame(self):
        bridge_io.clear_raw_frames()
        before = bridge_io.get_frame_stats()["dropped"]
        bridge_io.push_raw_jpeg(_jpeg(30))
        bridge_io.push_raw_jpeg(_jpeg(40))
        stats = bridge_io.get_frame_stats()
        self.assertEqual(stats["buffered"], 1)
        self.assertGreaterEqual(stats["dropped"], before + 1)


if __name__ == "__main__":
    unittest.main()
