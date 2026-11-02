"""1.3.82：Ctrl+F 抢不到搜索框时的兜底（再核一次标题 + 进程内停用自动打开）。"""
import unittest
from unittest.mock import patch

from src import wechat_guia_send as mod

from test_open_chat1361 import make_channel


def make(titles, has_text=True):
    channel, gui = make_channel(titles, has_text=has_text)
    return channel, gui


def run_open(channel, gui, target='西莲寺紫', raise_title=None):
    """跑一次 _open_target_chat，返回 (结果, 按键序列)。"""
    if raise_title is not None:
        def boom(_gui, _deadline, signature=None):
            raise raise_title
        channel._verified_title = boom
    keys = []
    def paste(_u, k=0x56):
        keys.append(('paste', k))
    def tap(_u, k):
        keys.append(('tap', k))
    with patch.object(mod, '_paste_keys', side_effect=paste), \
            patch.object(mod, '_tap_key', side_effect=tap):
        result = channel._open_target_chat(gui, target, 200.0)
    return result, keys


class OpenChatFallbackTests(unittest.TestCase):
    def test_falls_back_to_title_when_user_opened_chat(self):
        # 搜索框没抢到焦点 -> 清框；此时标题已是目标会话 -> 当作打开成功，绝不再按 Enter。
        channel, gui = make(['西莲寺紫'])
        result, keys = run_open(channel, gui)
        self.assertIsNone(result)
        self.assertEqual(channel._title, '西莲寺紫')
        self.assertNotIn(('tap', 0x0D), keys)
        self.assertIn(('tap', 0x1B), keys)
        self.assertFalse(channel._auto_open_disabled)
        self.assertEqual(channel._clipboard.restored, 1)

    def test_still_mismatched_disables_auto_open(self):
        channel, gui = make([])
        result, keys = run_open(channel, gui)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'target_not_open')
        self.assertIn('4.1.15', result.reason)
        self.assertIn('西莲寺紫', result.reason)
        self.assertNotIn(('tap', 0x0D), keys)
        self.assertTrue(channel._auto_open_disabled)

    def test_disabled_channel_sends_no_keys_at_all(self):
        channel, gui = make([])
        run_open(channel, gui)
        result, keys = run_open(channel, gui)
        self.assertFalse(result.ok)
        self.assertEqual(keys, [])
        self.assertIn('已停用自动打开', result.reason)
        self.assertEqual(channel._clipboard.restored, 1)   # 第二次连快照都没做

    def test_title_read_error_never_presses_enter(self):
        channel, gui = make([])
        result, keys = run_open(channel, gui, raise_title=RuntimeError('没帧'))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'target_not_open')
        self.assertNotIn(('tap', 0x0D), keys)
        self.assertTrue(channel._auto_open_disabled)

    def test_disable_flag_is_per_process_not_saved(self):
        channel, gui = make([])
        run_open(channel, gui)
        fresh, gui2 = make(['西莲寺紫'])
        self.assertFalse(fresh._auto_open_disabled)
        self.assertIsNone(run_open(fresh, gui2)[0])

    def test_phase_log_records_auto_open_disabled(self):
        channel, gui = make([])
        seen = []
        with patch.object(mod, 'log_send_phase', side_effect=lambda *a, **k: seen.append((a, k))):
            run_open(channel, gui)
        reasons = [k.get('reason') for _a, k in seen]
        self.assertIn('search_unavailable', reasons)
        self.assertIn('auto_open_disabled', reasons)


if __name__ == '__main__':
    unittest.main()
