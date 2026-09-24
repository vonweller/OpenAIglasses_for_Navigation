import unittest
from aiglasses import wake_gate


class WakeGateTests(unittest.TestCase):
    def test_sleep_command_is_local_and_punctuation_tolerant(self):
        for text in ('进入休眠。', '进入 休眠 模式！', '助手休眠', '停止对话'):
            self.assertTrue(wake_gate.is_sleep_command(text))
            self.assertTrue(wake_gate.is_always_on_command(text))
        for text in ('手机为什么进入休眠', '怎么让电脑休眠', '不要进入休眠'):
            self.assertFalse(wake_gate.is_sleep_command(text))

    def test_punctuation_does_not_break_wake_phrase(self):
        self.assertTrue(wake_gate.is_wake_phrase('你好，智能助手。'))
        self.assertEqual(wake_gate.extract_command_after_wake('你好，智能助手。帮我找手机。'), '帮我找手机。')


if __name__ == '__main__':
    unittest.main()
