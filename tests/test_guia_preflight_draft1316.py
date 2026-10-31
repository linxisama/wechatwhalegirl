"""只读体检不能将草稿占用冒充就绪；替身不会操作桌面。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.wechat_guia_send import GuiaSendResult, probe


class DraftPreflightTests(unittest.TestCase):
    def channel(self, *, draft=False, title='文件传输助手'):
        gui = SimpleNamespace(get_input_box=Mock(return_value=(10, 20, 200, 100)),
            _input_box_has_text=Mock(return_value=draft),
            wx_click=Mock(side_effect=AssertionError('只读检查不可点击')),
            set_clipboard=Mock(side_effect=AssertionError('只读检查不可修改剪贴板')))
        channel = SimpleNamespace(available=Mock(return_value=GuiaSendResult(True,
            diagnostic={'ready': True})), current_chat=Mock(return_value=title),
            title_matches=lambda actual, expected: actual == expected,
            _build=Mock(return_value=gui))
        return channel, gui

    def test_draft_occupancy_is_ready_because_send_clears_it_first(self):
        """1.3.58：草稿不再挡住发送卡——发送前自动清空，体检本身仍然只读。"""
        channel, gui = self.channel(draft=True)
        with patch('src.wechat_guia_send._channel', return_value=channel):
            result = probe(account_path='bound', who='文件传输助手')
        self.assertTrue(result.ok)
        self.assertTrue(result.diagnostic['draft_present'])
        self.assertIn('自动清空', result.diagnostic['draft_note'])
        gui.wx_click.assert_not_called()
        gui.set_clipboard.assert_not_called()

    def test_auto_clear_kill_switch_restores_the_waiting_state(self):
        channel, gui = self.channel(draft=True)
        with patch.dict('os.environ', {'WHALE_AUTO_CLEAR_DRAFT': '0'}), \
             patch('src.wechat_guia_send._channel', return_value=channel):
            result = probe(account_path='bound', who='文件传输助手')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'draft_not_empty')
        self.assertFalse(result.diagnostic['ready'])

    def test_empty_draft_is_read_after_exact_title_match(self):
        channel, gui = self.channel()
        with patch('src.wechat_guia_send._channel', return_value=channel):
            self.assertTrue(probe(account_path='bound', who='文件传输助手').ok)
        gui._input_box_has_text.assert_called_once_with((10, 20, 200, 100))

    def test_other_chat_does_not_even_inspect_its_draft(self):
        channel, gui = self.channel(title='另一会话')
        with patch('src.wechat_guia_send._channel', return_value=channel):
            result = probe(account_path='bound', who='文件传输助手')
        self.assertEqual(result.reason_code, 'chat_not_open')
        gui.get_input_box.assert_not_called()

    def test_layout_change_or_unreadable_input_cannot_report_ready(self):
        channel, gui = self.channel()
        gui._input_box_has_text.side_effect = RuntimeError('layout changed')
        with patch('src.wechat_guia_send._channel', return_value=channel):
            result = probe(account_path='bound', who='文件传输助手')
        self.assertFalse(result.ok)
        self.assertFalse(result.diagnostic['ready'])
