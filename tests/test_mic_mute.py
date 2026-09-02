import time
import unittest

from aiglasses import audio_stream
from aiglasses.audio_player import is_voice_playing


class MicMuteTests(unittest.TestCase):
    def setUp(self):
        audio_stream._last_audible_output_at = 0.0
        audio_stream.stream_clients.clear()
        while True:
            try:
                audio_stream._local_pcm_queue.get_nowait()
            except Exception:
                break

    def tearDown(self):
        audio_stream._last_audible_output_at = 0.0
        audio_stream.stream_clients.clear()
        while True:
            try:
                audio_stream._local_pcm_queue.get_nowait()
            except Exception:
                break

    def test_idle_is_not_playing(self):
        self.assertFalse(audio_stream.is_output_playing(hangover_sec=0.0))
        self.assertFalse(is_voice_playing())

    def test_local_queue_counts_as_playing(self):
        audio_stream._local_pcm_queue.put_nowait(b"\x01\x00")
        self.assertTrue(audio_stream.is_output_playing(hangover_sec=0.0))

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
