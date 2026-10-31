"""发送身份、正文与提交证据回归；全部假窗口，不触碰微信。"""
import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.wechat_guia_send import GuiaChannel, _channel
from src.wechat_send_worker import send_text
from tests.test_wechat_guia_send import FakeGUI, FakeClipboard, channel_for, GuiaWorkerFallbackTest


class ExactRecipientTests(unittest.TestCase):
    def test_nearby_names_and_single_character_are_rejected(self):
        for title in ('群', '测试', '测试群二', '新测试群', '测 试群'):
            with self.subTest(title=title):
                self.assertFalse(GuiaChannel.title_matches(title, '测试群'))
        self.assertTrue(GuiaChannel.title_matches(' 测试群 ', '测试群'))

    def test_no_account_or_unmatched_account_never_constructs_gui(self):
        for account, row, diagnostic in (
                ('', {'hwnd': 1, 'pid': 2}, {'matched_count': 1}),
                ('bound', None, {'matched_count': 0}),
                ('bound', {'hwnd': 1, 'pid': 2}, {'matched_count': 0, 'fallback': 'single_window'})):
            with self.subTest(account=account, diagnostic=diagnostic), \
                 patch('src.wechat_window.NativeWindowAccess'), \
                 patch('src.wechat_window.select_window', return_value=(row, diagnostic)), \
                 patch('src.wechat_guia_send._default_factory') as factory:
                with self.assertRaises(RuntimeError):
                    _channel(account_path=account)
                factory.assert_not_called()

    def test_bound_handle_change_is_rejected_before_foreground_or_input(self):
        gui = FakeGUI()
        channel = channel_for(gui)
        channel._account_path = 'bound'
        channel._expected_window = {'hwnd': gui.main_hwnd, 'pid': gui.pid, 'minimized': False}
        channel._window_access = Mock()
        changed = dict(channel._expected_window, hwnd=999)
        with patch('src.wechat_window.select_window', return_value=(changed, {'matched_count': 1})):
            result = channel.send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertNotIn('front', gui.calls)
        self.assertNotIn('input', gui.calls)


class FullBodyReadbackTests(unittest.TestCase):
    def test_partial_or_wrong_body_never_submits_and_preserves_clipboard(self):
        for actual in ('正', '另一个正文', '', '正 文'):
            with self.subTest(actual=actual):
                gui = FakeGUI()
                clipboard = FakeClipboard(gui)
                clipboard.text = lambda: actual
                result = channel_for(gui, clipboard=clipboard).send('测试群', '正文')
                self.assertFalse(result.ok)
                self.assertNotIn('click_send', gui.calls)
                self.assertEqual(clipboard.value, '原剪贴板')
                self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')
                self.assertTrue(result.diagnostic['input_touched'])

    def test_no_copy_cannot_validate_from_original_clipboard(self):
        gui = FakeGUI()
        clipboard = FakeClipboard(gui)
        clipboard.value = '正文'
        channel = channel_for(gui, clipboard=clipboard)
        original = channel._win32().SendInput.side_effect
        def ignore_copy(n, events, size):
            return 4 if events[1].payload.keyboard.vk == 0x43 else original(n, events, size)
        channel._win32().SendInput.side_effect = ignore_copy
        result = channel.send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertNotIn('click_send', gui.calls)
        self.assertEqual(clipboard.value, '正文')

    def test_body_changed_while_waiting_is_not_sent(self):
        gui = FakeGUI()
        channel = channel_for(gui)
        channel._send_gate = lambda: setattr(gui, 'draft_body', 'changed')
        result = channel.send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertNotIn('click_send', gui.calls)
        self.assertEqual(gui.calls.count('input'), 1)

    def test_phase_precedes_send_click_and_result_keeps_submission_evidence(self):
        gui = FakeGUI()
        channel = channel_for(gui)
        phases = []
        channel._phase = phases.append
        original = gui.wx_click
        def click(x, y):
            if y >= gui.origin_y + 850:
                self.assertEqual(phases[-1], 'send_key')
            original(x, y)
        gui.wx_click = click
        result = channel.send('测试群', '正文')
        self.assertTrue(result.ok)
        self.assertTrue(result.diagnostic['send_key'])
        self.assertEqual(result.diagnostic['submission_state'], 'confirmed')
        self.assertFalse(result.diagnostic['delivery_verified'])
        self.assertEqual(phases.count('send_key'), 1)


class UiaDraftTests(unittest.TestCase):
    def driver(self, reads):
        uia = Mock()
        uia.ensure_window.return_value = True
        uia.current_chat.return_value = '测试群'
        uia._read_edit_value.side_effect = reads
        control = uia._chat_input.return_value
        control.HasKeyboardFocus = True
        control.GetValuePattern.return_value.IsReadOnly = False
        return uia, control

    def test_existing_draft_is_not_overwritten(self):
        for draft in ('用户草稿', ' ', None):
            uia, control = self.driver([draft])
            self.assertFalse(send_text(uia, '测试群', '正文')[0])
            control.GetValuePattern.return_value.SetValue.assert_not_called()
            control.SendKeys.assert_not_called()

    def test_draft_changed_after_phase_is_not_overwritten(self):
        uia, control = self.driver(['', '新草稿'])
        self.assertFalse(send_text(uia, '测试群', '正文')[0])
        control.GetValuePattern.return_value.SetValue.assert_not_called()
        control.SendKeys.assert_not_called()

    def test_setvalue_exception_does_not_fall_back_to_typing(self):
        uia, control = self.driver(['', ''])
        control.GetValuePattern.return_value.SetValue.side_effect = RuntimeError('partial write')
        self.assertFalse(send_text(uia, '测试群', '正文')[0])
        control.SendKeys.assert_not_called()

    def test_whitespace_difference_is_not_complete_readback(self):
        uia, control = self.driver(['', ''])
        uia._read_edit_value.side_effect = None
        uia._read_edit_value.return_value = ''
        def written(text):
            uia._read_edit_value.return_value = '正 文'
        control.GetValuePattern.return_value.SetValue.side_effect = written
        self.assertFalse(send_text(uia, '测试群', '正文')[0])
        control.SendKeys.assert_not_called()

    def test_started_input_never_switches_channel(self):
        fixture = GuiaWorkerFallbackTest()
        for stage in ('focus', 'input', 'readback', 'send_key'):
            def failure(driver, who, text, phase, **kwargs):
                phase(stage)
                return False, 'failed'
            result, _ = fixture.run_execute(fixture.job(), failure)
            self.assertFalse(result[0])


class WorkerEvidenceTests(unittest.TestCase):
    def test_submit_state_is_persisted_before_callback_continues(self):
        from src.wechat_send_worker import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = root / 'jobs' / 'job.json'
            request.parent.mkdir()
            request.write_text(json.dumps({'action': 'text', 'who': 'target', 'text': 'body'}), encoding='utf-8')
            def execute(job, phase):
                phase('send_key')
                persisted = json.loads(request.with_suffix('.status.json').read_text(encoding='utf-8'))
                self.assertTrue(persisted['send_key'])
                self.assertEqual(persisted['submission_state'], 'uncertain')
                job['_diagnostic'] = {'submission_state': 'confirmed'}
                return True, 'submitted'
            with patch('src.wechat_send_worker.execute', side_effect=execute), \
                 patch('src.wechat_send_worker.setup_logging'), \
                 patch('src.interactive_desktop.interactive_desktop_status', return_value=(True, '')):
                self.assertEqual(main(str(request)), 0)
            result = json.loads(request.with_suffix('.result.json').read_text(encoding='utf-8'))
            self.assertEqual(result['submission_state'], 'confirmed')
            self.assertTrue(result['diagnostic']['send_key'])
            self.assertFalse(result['diagnostic']['delivery_verified'])

    def test_failed_check_never_logs_send_failure(self):
        from src.wechat_send_worker import main
        for effect in ((False, 'not ready'), RuntimeError('check unavailable')):
            with self.subTest(effect=type(effect)), tempfile.TemporaryDirectory() as folder:
                request = Path(folder) / 'jobs' / 'job.json'
                request.parent.mkdir()
                request.write_text(json.dumps({'action': 'window-check'}), encoding='utf-8')
                kwargs = {'side_effect': effect} if isinstance(effect, Exception) else {'return_value': effect}
                with patch('src.wechat_send_worker.execute', **kwargs), \
                     patch('src.wechat_send_worker.setup_logging'), \
                     patch('src.wechat_send_worker.log_send_failure') as failure, \
                     patch('src.wechat_send_worker.record_exception') as exception:
                    main(str(request))
                failure.assert_not_called()
                exception.assert_not_called()
                result = json.loads(request.with_suffix('.result.json').read_text(encoding='utf-8'))
                self.assertFalse(result['send_key'])
                self.assertEqual(result['diagnostic']['submission_state'], 'not_submitted')


if __name__ == '__main__':
    unittest.main()
