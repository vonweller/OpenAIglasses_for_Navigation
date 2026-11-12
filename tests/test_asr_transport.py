import threading
import time
import unittest
from unittest.mock import patch

from aiglasses.asr_transport import QueuedRecognition


class AsrTransportTests(unittest.TestCase):
    def recognition(self):
        rec = QueuedRecognition(model='test', format='pcm', sample_rate=16000, callback=None)
        rec._running = True
        rec._pcm_ready.set()
        return rec

    def test_append_during_yield_is_not_cleared(self):
        rec = self.recognition()
        rec.send_audio_frame(b'a' * 640)
        stream = rec._input_stream_cycle()
        self.assertEqual(next(stream), b'a' * 640)
        rec.send_audio_frame(b'b' * 640)
        self.assertEqual(next(stream), b'b' * 640)
        rec._pcm_stop.set()
        with self.assertRaises(StopIteration):
            next(stream)

    def test_empty_input_waits_in_queue_and_stop_wakes_it(self):
        rec = self.recognition()
        result = []
        def consume():
            result.extend(rec._input_stream_cycle())
        worker = threading.Thread(target=consume)
        worker.start()
        time.sleep(.02)
        self.assertTrue(worker.is_alive())
        rec._running = False
        rec.stop()
        worker.join(timeout=.3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [])

    def test_input_capacity_is_bounded(self):
        rec = self.recognition()
        for i in range(105):
            rec.send_audio_frame(bytes([i]) * 640)
        self.assertEqual(rec._pcm_queue.qsize(), 100)
        self.assertEqual(rec.dropped_frames, 5)
        self.assertEqual(rec._pcm_queue.get_nowait(), bytes([5]) * 640)


if __name__ == '__main__':
    unittest.main()
