import asyncio
import unittest

from aiglasses import audio_stream


class AudioStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        audio_stream.stream_clients.clear()
        with audio_stream._pcm_buffer_lock:
            audio_stream._pending_pcm16.clear()

    async def asyncTearDown(self):
        audio_stream.stream_clients.clear()
        with audio_stream._pcm_buffer_lock:
            audio_stream._pending_pcm16.clear()

    async def test_network_fragments_do_not_add_silence_between_chunks(self):
        queue = asyncio.Queue()
        client = audio_stream.StreamClient(queue, asyncio.Event(), asyncio.Event())
        audio_stream.stream_clients.add(client)
        frame_size = audio_stream.BYTES_PER_20MS_16K
        payload = bytes((index % 251) + 1 for index in range(frame_size * 2 + 17))

        await audio_stream.broadcast_pcm16_realtime(payload[:107])
        await audio_stream.broadcast_pcm16_realtime(payload[107:frame_size + 91])
        await audio_stream.broadcast_pcm16_realtime(payload[frame_size + 91:])

        self.assertEqual(queue.qsize(), 2)
        self.assertEqual(await queue.get(), payload[:frame_size])
        self.assertEqual(await queue.get(), payload[frame_size:frame_size * 2])

        await audio_stream.finish_pcm16_stream()
        tail = await queue.get()
        self.assertEqual(tail[:17], payload[-17:])
        self.assertEqual(tail[17:], b"\x00" * (frame_size - 17))
        self.assertTrue(client.flush_event.is_set())


if __name__ == "__main__":
    unittest.main()
