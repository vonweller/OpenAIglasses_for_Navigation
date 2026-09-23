import asyncio
import unittest
from unittest import mock

from aiglasses import audio_stream


class AudioStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved_target = audio_stream.get_playback_target()
        self._saved_autostart = audio_stream._local_player_autostart
        # 挡住 set/restore 期间对真声卡线程的启动，并清掉其它用例留下的线程。
        self._thread_patch = mock.patch.object(audio_stream, "_ensure_local_player_thread")
        self._thread_patch.start()
        audio_stream._local_player_autostart = False
        audio_stream._stop_local_player_thread()
        audio_stream._close_local_player()
        audio_stream.set_playback_target(audio_stream.PLAYBACK_ESP32)
        audio_stream.stream_clients.clear()
        audio_stream._last_audible_output_at = 0.0
        audio_stream._clear_local_play_schedule()
        with audio_stream._pcm_buffer_lock:
            audio_stream._pending_pcm16.clear()

    async def asyncTearDown(self):
        audio_stream.stream_clients.clear()
        audio_stream._last_audible_output_at = 0.0
        audio_stream._clear_local_play_schedule()
        with audio_stream._pcm_buffer_lock:
            audio_stream._pending_pcm16.clear()
        audio_stream._local_player_autostart = False
        audio_stream.set_playback_target(self._saved_target)
        audio_stream._stop_local_player_thread()
        audio_stream._close_local_player()
        audio_stream._local_player_autostart = self._saved_autostart
        self._thread_patch.stop()

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

    async def test_silence_keepalive_does_not_mark_audible(self):
        audio_stream._mark_audible_output(audio_stream.STREAM_IDLE_SILENCE)
        audio_stream._mark_audible_output(b"")
        self.assertEqual(audio_stream._last_audible_output_at, 0.0)
        self.assertFalse(audio_stream.is_output_playing(hangover_sec=1.0))

        audio_stream._mark_audible_output(b"\x00\x01" + b"\x00" * 10)
        self.assertGreater(audio_stream._last_audible_output_at, 0.0)


if __name__ == "__main__":
    unittest.main()
