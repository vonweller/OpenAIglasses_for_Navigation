import asyncio
import queue
import struct
import unittest
from unittest.mock import patch

from aiglasses import audio_stream
from aiglasses.playback_clock import PlaybackClock


class ClockTests(unittest.TestCase):
    def test_burst_is_paced_by_sample_count(self):
        clock = PlaybackClock(clock=lambda: 100.0)
        deadlines = [clock.reserve(320) for _ in range(500)]
        self.assertAlmostEqual(deadlines[0], 100.0)
        self.assertAlmostEqual(deadlines[-1], 109.82, places=6)
        self.assertAlmostEqual(clock.end_at, 110.0, places=6)
        clock.reset()
        self.assertEqual(clock.reserve(320), 100.0)


class PlaybackPacingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = [
            patch.object(audio_stream, '_local_player_autostart', False),
            patch.object(audio_stream, '_playback_target', 'both'),
            patch.object(audio_stream, '_stream_loop', None),
            patch.object(audio_stream, '_local_pcm_queue', queue.Queue(maxsize=240)),
            patch.object(audio_stream, 'stream_clients', set()),
            patch.object(audio_stream, '_playback_clock', PlaybackClock(ahead_seconds=0.04)),
        ]
        for p in self.patches:
            p.start()
        audio_stream._invalidate_pcm()
        self.device = asyncio.Queue(maxsize=240)
        self.client = audio_stream.StreamClient(self.device, asyncio.Event(), asyncio.Event())
        audio_stream.stream_clients.add(self.client)

    async def asyncTearDown(self):
        audio_stream._invalidate_pcm()
        for p in reversed(self.patches):
            p.stop()

    async def test_ten_second_cloud_burst_preserves_every_frame(self):
        now = [100.0]
        audio_stream._playback_clock = PlaybackClock(clock=lambda: now[0], ahead_seconds=.16)
        original_sleep = asyncio.sleep
        local, device = [], []
        high_water = [0]

        def drain():
            high_water[0] = max(high_water[0], audio_stream._local_pcm_queue.qsize())
            while not audio_stream._local_pcm_queue.empty():
                local.append(audio_stream._local_pcm_queue.get_nowait())
            while not self.device.empty():
                device.append(self.device.get_nowait())

        async def sleep(delay):
            now[0] += max(delay, .001)
            drain()
            await original_sleep(0)

        payload = b''.join(struct.pack('<h', i) * 160 for i in range(1, 501))
        with patch.object(audio_stream.asyncio, 'sleep', sleep):
            await audio_stream.broadcast_pcm16_realtime(payload)
            await audio_stream.finish_pcm16_stream()
        drain()
        self.assertEqual(b''.join(local), payload)
        self.assertEqual(b''.join(device), payload)
        self.assertLessEqual(high_water[0], 10)
        self.assertGreaterEqual(now[0] - 100, 9.8)

    async def test_full_local_queue_waits_without_dropping_head(self):
        audio_stream._local_pcm_queue = queue.Queue(maxsize=1)
        audio_stream._local_pcm_queue.put_nowait(b'old' * 100)
        producer = asyncio.create_task(audio_stream.broadcast_pcm16_realtime(b'x' * 320))
        await asyncio.sleep(.025)
        self.assertFalse(producer.done())
        self.assertEqual(audio_stream._local_pcm_queue.get_nowait(), b'old' * 100)
        await asyncio.wait_for(producer, .5)
        self.assertEqual(audio_stream._local_pcm_queue.get_nowait(), b'x' * 320)

    async def test_reset_cancels_pending_burst(self):
        producer = asyncio.create_task(audio_stream.broadcast_pcm16_realtime(b'\x01\x00' * 80000))
        await asyncio.sleep(.03)
        await audio_stream.soft_reset_audio('test')
        await asyncio.wait_for(producer, .5)
        await asyncio.sleep(.03)
        self.assertTrue(audio_stream._local_pcm_queue.empty())
        self.assertTrue(self.device.empty())
        self.assertEqual(audio_stream._playback_clock.end_at, 0)

    async def test_background_audio_producer_uses_the_server_loop(self):
        loop = asyncio.get_running_loop()
        audio_stream._stream_loop = loop
        seen = []
        original = audio_stream._broadcast_pcm

        async def observe(pcm):
            seen.append(asyncio.get_running_loop())
            await original(pcm)

        def worker():
            asyncio.run(audio_stream.broadcast_pcm16_realtime(b'x' * 320))

        with patch.object(audio_stream, '_broadcast_pcm', observe):
            await asyncio.to_thread(worker)
        self.assertEqual(seen, [loop])
        self.assertEqual(self.device.get_nowait(), b'x' * 320)

    async def test_two_producers_never_interleave_within_a_burst(self):
        a, b = b'a' * 3200, b'b' * 3200
        await asyncio.gather(audio_stream.broadcast_pcm16_realtime(a), audio_stream.broadcast_pcm16_realtime(b))
        output = b''.join(audio_stream._local_pcm_queue.get_nowait() for _ in range(20))
        self.assertEqual(output, a + b)


if __name__ == '__main__':
    unittest.main()
