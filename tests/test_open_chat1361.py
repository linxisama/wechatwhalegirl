"""1.3.61 自动打开目标会话：只在真发送路径、按标题核对，fail-closed。"""
import os
import unittest
from unittest.mock import Mock, patch

from src import wechat_guia_send as mod
from src.wechat_guia_send import GuiaChannel, _looks_like_chat_missing, _fail


class FakeClipboard:
    def __init__(self):
        self.saved = []
        self.restored = 0

    def snapshot(self):
        self.saved.append('snap')
        return {'token': 1}

    def copy(self, text):
        self.saved.append(('copy', text))

    def restore(self, snap):
        self.restored += 1


class FakeGUI:
    def __init__(self, has_text=False):
        self.has_text = has_text
        self.main_hwnd = 100
        self.pid = 200

    def get_input_box(self):
        return (10, 10, 200, 60)

    def _input_box_has_text(self, box):
        return self.has_text


def make_channel(titles, has_text=False):
    gui = FakeGUI(has_text)
    channel = GuiaChannel(gui_factory=lambda: gui, clock=lambda: 100.0, sleep=lambda _s: None,
                          user32=Mock(), clipboard=FakeClipboard())
    channel._gui = gui
    channel._foreground = lambda _gui: True
    seq = list(titles)
    channel._verified_title = lambda _gui, _deadline, signature=None: (
        seq.pop(0) if seq else '')
    return channel, gui


class OpenTargetChatTests(unittest.TestCase):
    def test_search_opens_target_chat_and_returns_none(self):
        channel, _gui = make_channel(['西莲寺紫'])
        keys = []
        with patch.object(mod, '_paste_keys', side_effect=lambda _u, k=0x56: keys.append(('paste', k))), \
                patch.object(mod, '_tap_key', side_effect=lambda _u, k: keys.append(('tap', k))):
            result = channel._open_target_chat(FakeGUI(), '西莲寺紫', 200.0)
        self.assertIsNone(result)
        self.assertEqual(keys, [('paste', 0x46), ('paste', 0x56), ('tap', 0x0D)])
        self.assertEqual(channel._clipboard.restored, 1)

    def test_search_focus_lost_clears_and_never_presses_enter(self):
        channel, _gui = make_channel([])
        keys = []
        with patch.object(mod, '_paste_keys', side_effect=lambda _u, k=0x56: keys.append(('paste', k))), \
                patch.object(mod, '_tap_key', side_effect=lambda _u, k: keys.append(('tap', k))):
            result = channel._open_target_chat(FakeGUI(has_text=True), '西莲寺紫', 200.0)
        self.assertFalse(result.ok)
        self.assertIn('搜索框没有拿到焦点', result.reason)
        self.assertNotIn(('tap', 0x0D), keys)
        self.assertIn(('tap', 0x1B), keys)

    def test_title_mismatch_escapes_without_input(self):
        channel, _gui = make_channel(['别的人'])
        keys = []
        with patch.object(mod, '_paste_keys', side_effect=lambda _u, k=0x56: keys.append(('paste', k))), \
                patch.object(mod, '_tap_key', side_effect=lambda _u, k: keys.append(('tap', k))):
            result = channel._open_target_chat(FakeGUI(), '西莲寺紫', 200.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'target_not_open')
        self.assertEqual(keys[-1], ('tap', 0x1B))

    def test_env_flag_disables_auto_open(self):
        channel, _gui = make_channel(['西莲寺紫'])
        keys = []
        with patch.dict(os.environ, {'WHALE_AUTO_OPEN_CHAT': '0'}), \
                patch.object(mod, '_paste_keys', side_effect=lambda _u, k=0x56: keys.append(k)), \
                patch.object(mod, '_tap_key', side_effect=lambda _u, k: keys.append(k)):
            result = channel._open_target_chat(FakeGUI(), '西莲寺紫', 200.0)
        self.assertFalse(result.ok)
        self.assertEqual(keys, [])
        self.assertIn('WHALE_AUTO_OPEN_CHAT', result.reason)

    def test_no_foreground_types_nothing(self):
        channel, _gui = make_channel(['西莲寺紫'])
        channel._foreground = lambda _gui: False
        keys = []
        with patch.object(mod, '_paste_keys', side_effect=lambda _u, k=0x56: keys.append(k)), \
                patch.object(mod, '_tap_key', side_effect=lambda _u, k: keys.append(k)):
            result = channel._open_target_chat(FakeGUI(), '西莲寺紫', 200.0)
        self.assertFalse(result.ok)
        self.assertEqual(keys, [])
        self.assertEqual(channel._clipboard.restored, 0)


class ChatMissingSignalTests(unittest.TestCase):
    def test_layout_error_counts_as_missing_chat(self):
        result = _fail('guia_unavailable', '只读布局检查失败：RuntimeError: 未确认标题栏与正文之间的分隔线')
        self.assertTrue(_looks_like_chat_missing(result))

    def test_target_not_open_code_counts(self):
        self.assertTrue(_looks_like_chat_missing(_fail('target_not_open', '标题栏读到别的人')))

    def test_unrelated_failure_is_not(self):
        self.assertFalse(_looks_like_chat_missing(_fail('guia_unavailable', '微信窗口已关闭，请从托盘打开微信并登录')))




class WorkerWiringTest(unittest.TestCase):
    # 设置页「自动打开会话」必须真的管到发送子进程：一次发送一个进程，改完下次生效。

    def test_setting_is_turned_into_env_for_the_worker(self):
        import os

        import src.wechat_send_worker as worker
        calls = []
        with patch.dict(os.environ, {}, clear=False):
            patcher = patch('src.wechat_guia_send.send_text_via_guia',
                            new=lambda *a, **k: calls.append(k) or 'sent')
            with patcher:
                worker.send_text_via_guia('测试群', '正文', auto_open=False)
                self.assertEqual(os.environ.get('WHALE_AUTO_OPEN_CHAT'), '0')
                worker.send_text_via_guia('测试群', '正文', auto_open=True)
                self.assertEqual(os.environ.get('WHALE_AUTO_OPEN_CHAT'), '1')
                worker.send_text_via_guia('测试群', '正文')
                self.assertEqual(os.environ.get('WHALE_AUTO_OPEN_CHAT'), '1')
        self.assertEqual(len(calls), 3)


if __name__ == '__main__':
    unittest.main()
