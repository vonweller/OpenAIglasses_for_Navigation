import unittest
from unittest import mock

from aiglasses import audio_stream


class PlaybackTargetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved_target = audio_stream.get_playback_target()
        self._saved_autostart = audio_stream._local_player_autostart
        self._saved_total = audio_stream.total_broadcast_bytes
        # 全套里前序用例可能已经把目标还原成 server 并留下真线程。
        self._thread_patch = mock.patch.object(audio_stream, "_ensure_local_player_thread")
        self._thread_patch.start()
        audio_stream._local_player_autostart = False
        audio_stream._stop_local_player_thread()
        audio_stream._close_local_player()
        audio_stream.stream_clients.clear()
        audio_stream.total_broadcast_bytes = 0
        audio_stream.last_broadcast_bytes = 0
        audio_stream._last_audible_output_at = 0.0
        audio_stream._clear_local_play_schedule()
        self._drain_local()
        with audio_stream._pcm_buffer_lock:
            audio_stream._pending_pcm16.clear()

    async def asyncTearDown(self):
        audio_stream.stream_clients.clear()
        audio_stream._clear_local_play_schedule()
        audio_stream._last_audible_output_at = 0.0
        self._drain_local()
        with audio_stream._pcm_buffer_lock:
            audio_stream._pending_pcm16.clear()
        audio_stream.total_broadcast_bytes = self._saved_total
        audio_stream._local_player_autostart = False
        audio_stream.set_playback_target(self._saved_target)
        audio_stream._stop_local_player_thread()
        audio_stream._close_local_player()
        audio_stream._local_player_autostart = self._saved_autostart
        self._thread_patch.stop()

    def _drain_local(self):
        while True:
            try:
                audio_stream._local_pcm_queue.get_nowait()
            except Exception:
                break

    def test_aliases_keep_server_and_device(self):
        self.assertEqual(audio_stream.normalize_playback_target(None), "server")
        self.assertEqual(audio_stream.normalize_playback_target("server"), "server")
        self.assertEqual(audio_stream.normalize_playback_target("ESP32"), "esp32")
        self.assertEqual(audio_stream.normalize_playback_target("k230"), "esp32")
        self.assertEqual(audio_stream.normalize_playback_target("both"), "both")
        self.assertEqual(audio_stream.normalize_playback_target("dual"), "both")
        self.assertEqual(audio_stream.normalize_playback_target("nope"), "server")

    def test_device_does_not_keep_local_player(self):
        audio_stream.set_playback_target("both")
        audio_stream.set_playback_target("device")
        self.assertEqual(audio_stream.get_playback_target(), "esp32")
        self.assertFalse(audio_stream._targets_server())
        self.assertIsNone(audio_stream._local_player_thread)

    async def test_both_fans_out_equal_frames_without_doubling_bytes(self):
        audio_stream.set_playback_target("both")
        queue = __import__("asyncio").Queue()
        client = audio_stream.StreamClient(queue, __import__("asyncio").Event(), __import__("asyncio").Event())
        audio_stream.stream_clients.add(client)
        frame = audio_stream.BYTES_PER_20MS_16K
        payload = bytes((index % 251) + 1 for index in range(frame))

        await audio_stream.broadcast_pcm16_realtime(payload)

        self.assertEqual(audio_stream.total_broadcast_bytes, frame)
        self.assertEqual(audio_stream.last_broadcast_bytes, frame)
        self.assertEqual(audio_stream._local_pcm_queue.qsize(), 1)
        timing = audio_stream.playback_timing()
        self.assertEqual(timing["local_queued_chunks"], 1)
        self.assertEqual(timing["device_queued_chunks"], 1)
        self.assertAlmostEqual(timing["local_remaining_sec"], 0.02, delta=0.01)
        self.assertAlmostEqual(timing["device_audible_sec"], 0.02, delta=0.001)
        local = audio_stream._local_pcm_queue.get_nowait()
        device = await queue.get()
        self.assertEqual(local, payload)
        self.assertEqual(device, payload)
        self.assertEqual(len(local), len(device))
        thread = audio_stream._local_player_thread
        self.assertTrue(thread is None or not thread.is_alive())
        audio_stream._ensure_local_player_thread.assert_not_called()

    async def test_both_partial_fragment_adds_no_silence_until_finish(self):
        audio_stream.set_playback_target("both")
        queue = __import__("asyncio").Queue()
        client = audio_stream.StreamClient(queue, __import__("asyncio").Event(), __import__("asyncio").Event())
        audio_stream.stream_clients.add(client)
        frame = audio_stream.BYTES_PER_20MS_16K
        partial = bytes((index % 200) + 1 for index in range(17))

        await audio_stream.broadcast_pcm16_realtime(partial)
        self.assertEqual(audio_stream._local_pcm_queue.qsize(), 0)
        self.assertEqual(queue.qsize(), 0)
        self.assertEqual(audio_stream.total_broadcast_bytes, 17)

        await audio_stream.finish_pcm16_stream()
        local = audio_stream._local_pcm_queue.get_nowait()
        device = await queue.get()
        self.assertEqual(local, device)
        self.assertEqual(local[:17], partial)
        self.assertEqual(local[17:], b"\x00" * (frame - 17))
        self.assertEqual(audio_stream.total_broadcast_bytes, 17)

    async def test_silence_frame_does_not_count_as_device_audio(self):
        audio_stream.set_playback_target("esp32")
        queue = __import__("asyncio").Queue()
        client = audio_stream.StreamClient(queue, __import__("asyncio").Event(), __import__("asyncio").Event())
        audio_stream.stream_clients.add(client)
        silence = audio_stream.STREAM_IDLE_SILENCE
        await audio_stream.broadcast_pcm16_realtime(silence)
        self.assertEqual(queue.qsize(), 1)
        timing = audio_stream.playback_timing()
        self.assertEqual(timing["device_queued_chunks"], 1)
        self.assertEqual(timing["device_audible_sec"], 0.0)
        self.assertFalse(audio_stream.is_output_playing(hangover_sec=0.0))
        self.assertEqual(audio_stream._local_pcm_queue.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
