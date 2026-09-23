import cv2
import numpy as np
import unittest

from aiglasses import bridge_io
from aiglasses.performance import DEFAULT_OVERLAY_JPEG_QUALITY


def _jpeg(value: int, quality: int = 80) -> bytes:
    image = np.full((48, 64, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    assert ok
    return encoded.tobytes()


def _jpeg_dqt(data: bytes) -> bytes:
    start = data.find(b"\xff\xdb")
    assert start >= 0
    length = int.from_bytes(data[start + 2:start + 4], "big")
    return data[start:start + 2 + length]


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

    def test_raw_jpeg_is_not_reencoded_and_overlay_uses_profile_quality(self):
        bridge_io.clear_raw_frames()
        original = _jpeg(80)
        bridge_io.push_raw_jpeg(original)
        packet = bridge_io.wait_next_raw_bgr(0, timeout_sec=0.1)
        self.assertIsNotNone(packet)
        stored = bridge_io._frames[-1][2]
        self.assertEqual(stored, original)

        sent = []
        bridge_io.set_sender(sent.append)
        try:
            bridge_io.send_vis_bgr(packet.bgr, profile_key="k230_hd")
            bridge_io.send_vis_bgr(packet.bgr, quality=50, profile_key="k230_hd")
        finally:
            bridge_io.set_sender(None)

        self.assertEqual(len(sent), 2)
        self.assertEqual(bridge_io._frames[-1][2], original)
        self.assertNotEqual(sent[0], original)
        reference = _jpeg(80, DEFAULT_OVERLAY_JPEG_QUALITY)
        explicit = _jpeg(80, 50)
        self.assertEqual(_jpeg_dqt(sent[0]), _jpeg_dqt(reference))
        self.assertEqual(_jpeg_dqt(sent[1]), _jpeg_dqt(explicit))
        self.assertNotEqual(_jpeg_dqt(sent[0]), _jpeg_dqt(sent[1]))
        decoded = cv2.imdecode(np.frombuffer(sent[0], dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape[:2], packet.bgr.shape[:2])


if __name__ == "__main__":
    unittest.main()
