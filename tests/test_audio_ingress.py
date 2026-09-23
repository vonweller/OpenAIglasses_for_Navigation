import asyncio
import struct
import time
import unittest

from aiglasses.audio_ingress import OrderedAudioSender, PcmFramer


class PcmFramerTests(unittest.TestCase):
    def test_fragmented_samples_keep_order(self):
        pcm = struct.pack('<640h', *range(640))
        framer = PcmFramer()
        self.assertEqual(framer.feed(pcm[:101], now=1), [])
        frames = framer.feed(pcm[101:], now=1.02)
        self.assertEqual(b''.join(frames), pcm)
        self.assertEqual(len(frames), 2)
        self.assertEqual(framer.snapshot()['pending_bytes'], 0)

    def test_muting_preserves_duration_and_input_levels(self):
        framer = PcmFramer()
        pcm = struct.pack('<320h', *([1000] * 320))
        self.assertEqual(framer.feed(pcm, muted=True), [bytes(640)])
        self.assertEqual(framer.snapshot()['input_rms'], 1000)
        self.assertEqual(framer.snapshot()['muted_frames'], 1)

    def test_remainder_and_gap_metrics(self):
        framer = PcmFramer()
        framer.feed(bytes(1300), now=1)
        framer.feed(bytes(620), now=1.12)
        self.assertEqual(framer.frames, 3)
        self.assertAlmostEqual(framer.max_gap_ms, 120)


class AudioSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_and_bounded_oldest_drop(self):
        got = []
        sender = OrderedAudioSender(got.append, lambda _: None, max_frames=2)
        sender.offer(b'a' * 640)
        sender.offer(b'b' * 640)
        sender.offer(b'c' * 640)
        sender.start()
        await asyncio.sleep(.05)
        await sender.close()
        self.assertEqual(got, [b'b' * 640, b'c' * 640])
        self.assertEqual(sender.dropped_frames, 1)

    async def test_keepalive_sends_one_frame_not_a_burst(self):
        got = []
        sender = OrderedAudioSender(got.append, lambda _: None, idle_seconds=.04)
        sender.start()
        for _ in range(100):
            if sender.keepalive_frames:
                break
            await asyncio.sleep(.005)
        await sender.close()
        self.assertEqual(got, [bytes(640)])
        self.assertEqual(sender.keepalive_frames, 1)

    async def test_slow_sdk_does_not_block_event_loop(self):
        def send(_):
            time.sleep(.08)
        sender = OrderedAudioSender(send, lambda _: None)
        sender.offer(bytes(640))
        sender.start()
        start = time.monotonic()
        await asyncio.sleep(.01)
        self.assertLess(time.monotonic() - start, .06)
        await asyncio.sleep(.09)
        await sender.close()


if __name__ == '__main__':
    unittest.main()
