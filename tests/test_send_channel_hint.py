"""发送未就绪时的提示必须说清楚"下一步点哪里"。"""
import unittest

from src.wechat_app import send_channel_hint


class HintTests(unittest.TestCase):
    def test_missing_compat_mode_points_at_the_toggle(self):
        text = send_channel_hint({'waiting_code': 'accessibility_unavailable',
                                  'reason': '严格后台等待：微信只提供外层窗口'}, compat=False)
        self.assertIn('兼容模式', text)
        self.assertIn('严格后台等待', text)

    def test_with_compat_enabled_no_longer_blames_the_toggle(self):
        text = send_channel_hint({'waiting_code': 'accessibility_unavailable',
                                  'reason': '严格后台等待：没有开放聊天输入控件'}, compat=True)
        self.assertNotIn('勾选「兼容模式」', text)

    def test_minimized_says_send_restores_window_itself(self):
        text = send_channel_hint({'waiting_code': 'minimized', 'reason': '微信已最小化'})
        self.assertIn('自动恢复', text)
        self.assertIn('测试发送', text)

    def test_strict_background_minimized_keeps_manual_step(self):
        text = send_channel_hint({'waiting_code': 'minimized', 'reason': '严格后台等待：微信已最小化',
                                  'background_send': True})
        self.assertIn('不要最小化', text)

    def test_chat_not_open_tells_user_to_open_the_chat(self):
        text = send_channel_hint({'waiting_code': 'chat_not_open', 'reason': '严格后台等待：请手动打开目标会话'})
        self.assertIn('手动点开', text)

    def test_unknown_reason_still_returns_something_useful(self):
        self.assertEqual(send_channel_hint({}), '请检查发送环境')
        self.assertEqual(send_channel_hint(None), '请检查发送环境')

    def test_other_codes_keep_original_reason(self):
        text = send_channel_hint({'waiting_code': 'draft', 'reason': '严格后台等待：输入框已有草稿'})
        self.assertIn('草稿', text)


if __name__ == '__main__':
    unittest.main()
