import time
import unittest
from unittest import mock

from aiglasses import audio_stream
from aiglasses.audio_player import is_voice_playing


class MicMuteTests(unittest.TestCase):
    def setUp(self):
        self._saved_autostart = audio_stream._local_player_autostart
        self._thread_patch = mock.patch.object(audio_stream, "_ensure_local_player_thread")
        self._thread_patch.start()
        audio_stream._local_player_autostart = False
        audio_stream._stop_local_player_thread()
        audio_stream._close_local_player()
        audio_stream._last_audible_output_at = 0.0
        audio_stream._clear_local_play_schedule()
        audio_stream.stream_clients.clear()
        while True:
            try:
                audio_stream._local_pcm_queue.get_nowait()
            except Exception:
                break

    def tearDown(self):
        audio_stream._last_audible_output_at = 0.0
        audio_stream._clear_local_play_schedule()
        audio_stream.stream_clients.clear()
        audio_stream._local_player_autostart = False
        audio_stream._stop_local_player_thread()
        audio_stream._close_local_player()
        audio_stream._local_player_autostart = self._saved_autostart
        self._thread_patch.stop()

    def test_idle_is_not_playing(self):
        self.assertFalse(audio_stream.is_output_playing(hangover_sec=0.0))
        self.assertFalse(is_voice_playing())

    def test_queued_silence_and_empty_queue_do_not_mute(self):
        frame = audio_stream.BYTES_PER_20MS_16K
        audio_stream._local_pcm_queue.put_nowait(b"\x00" * frame)
        client = audio_stream.StreamClient(
            __import__("asyncio").Queue(),
            __import__("asyncio").Event(),
            __import__("asyncio").Event(),
        )
        client.q.put_nowait(b"\x00" * frame)
        audio_stream.stream_clients.add(client)
        self.assertFalse(audio_stream.is_output_playing(hangover_sec=0.0))
        timing = audio_stream.playback_timing()
        self.assertEqual(timing["device_audible_sec"], 0.0)
        self.assertGreater(timing["device_queued_chunks"], 0)

    def test_audible_local_schedule_mutes_only_for_its_duration(self):
        frame = bytes([1, 0]) * (audio_stream.BYTES_PER_20MS_16K // 2)
        audio_stream._schedule_local_play(frame)
        self.assertTrue(audio_stream.is_output_playing(hangover_sec=0.0))
        self.assertAlmostEqual(audio_stream.local_play_remaining_sec(), 0.02, delta=0.01)
        audio_stream._local_play_until = time.monotonic() - 0.01
        self.assertFalse(audio_stream.is_output_playing(hangover_sec=0.0))

    def test_hangover_keeps_mute_after_last_audible_chunk(self):
        audio_stream._last_audible_output_at = time.time()
        self.assertTrue(audio_stream.is_output_playing(hangover_sec=1.0))
        audio_stream._last_audible_output_at = time.time() - 2.0
        self.assertFalse(audio_stream.is_output_playing(hangover_sec=0.4))

    def test_silence_is_not_audible(self):
        self.assertFalse(audio_stream._pcm_has_signal(b""))
        self.assertFalse(audio_stream._pcm_has_signal(b"\x00\x00\x00"))
        self.assertTrue(audio_stream._pcm_has_signal(b"\x00\x01"))


if __name__ == "__main__":
    unittest.main()
