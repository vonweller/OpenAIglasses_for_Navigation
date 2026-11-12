import ast
import asyncio
from pathlib import Path
import threading
import types
import unittest
import time
from unittest.mock import AsyncMock, Mock

from aiglasses import wake_gate
from aiglasses.performance import PROFILES


SOURCE = Path(__file__).resolve().parents[1] / 'aiglasses/app_main.py'


def load_function(name, namespace):
    tree = ast.parse(SOURCE.read_text('utf-8'))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace[name]


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_viewer_does_not_block_other_latest_slots(self):
        fast, slow = asyncio.Queue(maxsize=1), asyncio.Queue(maxsize=1)
        event = asyncio.Event()
        ns = {
            'asyncio': asyncio, 'time': time, 'PROFILES': PROFILES,
            'PERFORMANCE_PROFILE': 'k230_1_5k', '_output_event': event,
            '_latest_output_lock': threading.Lock(), '_latest_output_version': 1,
            '_latest_output_frame': b'original-jpeg-one', '_viewer_queues': {'fast': fast, 'slow': slow},
            'pipeline_metrics': types.SimpleNamespace(on_broadcast=lambda: None),
            '_viewer_send_ms': 0, '_viewer_loop_ms': 0,
        }
        run = load_function('_viewer_broadcast_loop', ns)
        task = asyncio.create_task(run())
        try:
            event.set()
            self.assertEqual(await asyncio.wait_for(fast.get(), .5), b'original-jpeg-one')
            ns['_latest_output_version'] = 2
            ns['_latest_output_frame'] = b'original-jpeg-two'
            event.set()
            self.assertEqual(await asyncio.wait_for(fast.get(), .5), b'original-jpeg-two')
            self.assertEqual(slow.qsize(), 1)
            self.assertEqual(slow.get_nowait(), b'original-jpeg-two')
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_explicit_sleep_never_reaches_model(self):
        reset, final, model = AsyncMock(), AsyncMock(), AsyncMock()
        stop = Mock()
        nav = types.SimpleNamespace(stop_navigation=Mock())
        ns = {
            'wake_gate': wake_gate, 'soft_reset_audio': reset, 'ui_broadcast_final': final,
            'stop_yolomedia': stop, 'orchestrator': nav, 'start_ai_with_text': model,
        }
        run = load_function('start_ai_with_text_custom', ns)
        wake_gate.activate()
        await run('进入休眠。')
        self.assertFalse(wake_gate.is_active())
        reset.assert_awaited_once()
        model.assert_not_awaited()
        self.assertIn('已进入休眠', final.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
