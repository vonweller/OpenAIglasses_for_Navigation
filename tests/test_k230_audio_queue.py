import importlib.util
from pathlib import Path
import unittest


path = Path(__file__).resolve().parents[1] / 'firmware/k230/ws_client.py'
spec = importlib.util.spec_from_file_location('audio_queue_ws', path)
ws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ws)


class AudioQueueTests(unittest.TestCase):
    def client(self, size=3):
        client = ws.WSClient('localhost', 1, '/test', max_queue=size)
        client._connected = True
        return client

    def test_audio_preserves_all_frames_until_bounded_capacity(self):
        client = self.client()
        for value in (b'a', b'b', b'c'):
            self.assertTrue(client.queue_audio(value * 640))
        self.assertEqual([f.data[:1] for f in client._queue], [b'a', b'b', b'c'])
        client.queue_audio(b'd' * 640)
        self.assertEqual([f.data[:1] for f in client._queue], [b'b', b'c', b'd'])
        self.assertEqual(client.stats['dropped'], 1)

    def test_start_control_is_not_dropped_for_audio(self):
        client = self.client(2)
        client.queue_text('START')
        client.queue_audio(b'a' * 640)
        client.queue_audio(b'b' * 640)
        self.assertEqual(client._queue[0].data, b'START')
        self.assertEqual(client._queue[1].data, b'b' * 640)

    def test_partial_frame_stays_ahead_of_pong(self):
        client = self.client()
        client.queue_binary(b'x' * 100, latest=True)
        client._queue[0].sent = 6
        client._queue_front(ws.OP_PONG, b'ok')
        self.assertEqual(client._queue[0].opcode, ws.OP_BIN)
        self.assertEqual(client._queue[1].opcode, ws.OP_PONG)


if __name__ == '__main__':
    unittest.main()
