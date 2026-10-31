"""回复节奏：开口前等一小会儿，范围可设，长提问再多等一点。"""
import tempfile
import unittest
from pathlib import Path

from src.plugins.manager import PluginManager


class ReplyPaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.plugin = self.manager._plugins['reply_pace']

    def tearDown(self):
        self.tmp.cleanup()

    def test_registered_and_off_by_default(self):
        self.assertIn('reply_pace', self.manager._plugins)
        self.assertFalse(self.plugin.get('enabled'))
        self.assertIsNone(self.plugin.on_pre_ai({'text': '你好'}, [], None))

    def test_delay_within_configured_range(self):
        self.manager.update_settings('reply_pace', {'enabled': True, 'minDelay': 3, 'maxDelay': 5,
                                                    'longTextExtra': 0})
        for _ in range(20):
            hint = self.plugin.on_pre_ai({'text': '你好'}, [], None)
            self.assertGreaterEqual(hint['delay'], 3)
            self.assertLessEqual(hint['delay'], 5)

    def test_long_question_waits_a_bit_more(self):
        self.manager.update_settings('reply_pace', {'enabled': True, 'minDelay': 2, 'maxDelay': 2,
                                                    'longTextExtra': 2})
        short = self.plugin.on_pre_ai({'text': '你好'}, [], None)['delay']
        long = self.plugin.on_pre_ai({'text': '这是一句明显超过三十个字的提问，用来触发额外的思考等待时间'}, [], None)['delay']
        self.assertEqual(short, 2)
        self.assertGreaterEqual(long, 2)
        self.assertLessEqual(long, 4)

    def test_zero_delay_returns_nothing(self):
        self.manager.update_settings('reply_pace', {'enabled': True, 'minDelay': 0, 'maxDelay': 0,
                                                    'longTextExtra': 0})
        self.assertIsNone(self.plugin.on_pre_ai({'text': '你好'}, [], None))

    def test_bad_numbers_do_not_raise(self):
        self.manager.update_settings('reply_pace', {'enabled': True, 'minDelay': 9, 'maxDelay': 1,
                                                    'longTextExtra': 0})
        hint = self.plugin.on_pre_ai({'text': '你好'}, [], None)
        self.assertEqual(hint['delay'], 9)


if __name__ == '__main__':
    unittest.main()
