"""后台识别返回及写入之前的窗口边界；全是假控件，不操作真实微信。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.wechat_background import inspect_background, send_background
from tests import test_desktop_140 as fixtures


class BackgroundWindowGuardTests(unittest.TestCase):
    def setUp(self):
        fixtures.BackgroundTests.setUp(self)
        self.rows = [self.row]
        self.access.candidates = lambda: list(self.rows)
        self.desktop_ready = True
        self.desktop = lambda: (self.desktop_ready, '桌面暂不可用')

    def assert_untouched(self):
        self.value.SetValue.assert_not_called()
        self.invoke.Invoke.assert_not_called()

    def test_normal_inspection_rejects_window_replaced_while_reading_controls(self):
        children = self.root.GetChildren
        def changed():
            self.rows[:] = [dict(self.row, hwnd=999, pid=999)]
            return children()
        self.root.GetChildren = changed
        info, controls = inspect_background('test', '测试群', self.access, self.desktop)
        self.assertFalse(info['ready'])
        self.assertEqual(info['waiting_code'], 'account_unverified')
        self.assertIsNone(controls)
        self.assert_untouched()

    def test_normal_inspection_rejects_minimized_or_locked_during_tree_read(self):
        for change in ('minimized', 'locked'):
            with self.subTest(change=change):
                self.setUp()
                children = self.root.GetChildren
                def changed():
                    if change == 'minimized':
                        self.rows[:] = [dict(self.row, minimized=True)]
                    else:
                        self.desktop_ready = False
                    return children()
                self.root.GetChildren = changed
                info, controls = inspect_background('test', '测试群', self.access, self.desktop)
                self.assertFalse(info['ready'])
                self.assertEqual(info['waiting_code'], 'minimized' if change == 'minimized' else 'desktop_unavailable')
                self.assertIsNone(controls)
                self.assert_untouched()

    def test_input_phase_window_change_is_rejected_before_any_write(self):
        for change in ('hwnd', 'pid', 'process_created', 'minimized', 'locked', 'account'):
            with self.subTest(change=change):
                self.setUp()
                self.row['process_created'] = 1.0
                def phase(stage):
                    if stage != 'input':
                        return
                    if change in ('hwnd', 'pid', 'process_created'):
                        self.rows[:] = [dict(self.row, **{change: 999})]
                    elif change == 'minimized':
                        self.rows[:] = [dict(self.row, minimized=True)]
                    elif change == 'locked':
                        self.desktop_ready = False
                    else:
                        self.access.matches_account = lambda *args: False
                ok, _ = send_background(self.job, phase, self.access, self.desktop)
                self.assertFalse(ok)
                self.assertEqual(self.job['_diagnostic']['submission_state'], 'not_submitted')
                self.assert_untouched()

    def test_new_input_tree_with_other_chat_or_user_draft_is_never_overwritten(self):
        for change in ('title', 'draft', 'readonly'):
            with self.subTest(change=change):
                self.setUp()
                value = SimpleNamespace(Value='用户草稿' if change == 'draft' else '',
                    IsReadOnly=change == 'readonly', SetValue=Mock())
                field = SimpleNamespace(ClassName='mmui::ChatInputField', AutomationId='',
                    Name='另一个会话' if change == 'title' else '测试群',
                    ControlTypeName='EditControl', GetChildren=lambda: [], GetValuePattern=lambda: value)
                def phase(stage):
                    if stage == 'input':
                        self.root.GetChildren = lambda: [field, self.button]
                ok, _ = send_background(self.job, phase, self.access, self.desktop)
                self.assertFalse(ok)
                self.assert_untouched()
                value.SetValue.assert_not_called()
                self.assertEqual(value.Value, '用户草稿' if change == 'draft' else '')

    def test_submit_phase_replaced_window_preserves_draft_without_invoking(self):
        self.row['process_created'] = 1.0
        def phase(stage):
            if stage == 'send_key':
                self.rows[:] = [dict(self.row, process_created=2.0)]
        ok, _ = send_background(self.job, phase, self.access, self.desktop)
        self.assertFalse(ok)
        self.value.SetValue.assert_called_once()
        self.invoke.Invoke.assert_not_called()
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'not_submitted')

    def test_rebuilt_empty_input_uses_fresh_value_once_instead_of_old_pattern(self):
        value = SimpleNamespace(Value='', IsReadOnly=False)
        value.SetValue = Mock(side_effect=lambda body: setattr(value, 'Value', body))
        field = SimpleNamespace(ClassName='mmui::ChatInputField', AutomationId='', Name='测试群',
            ControlTypeName='EditControl', GetChildren=lambda: [], GetValuePattern=lambda: value)
        self.invoke.Invoke.side_effect = lambda: setattr(value, 'Value', '')
        def phase(stage):
            if stage == 'input':
                self.root.GetChildren = lambda: [field, self.button]
        self.assertTrue(send_background(self.job, phase, self.access, self.desktop)[0])
        self.value.SetValue.assert_not_called()
        value.SetValue.assert_called_once_with(self.job['text'])
        self.invoke.Invoke.assert_called_once()

    def test_desktop_or_window_change_during_fresh_input_lookup_never_writes(self):
        for change in ('locked', 'window'):
            with self.subTest(change=change):
                self.setUp()
                original = self.access.control
                reads = 0
                def control(hwnd):
                    nonlocal reads
                    reads += 1
                    if reads == 2:
                        if change == 'locked':
                            self.desktop_ready = False
                        else:
                            self.rows[:] = [dict(self.row, hwnd=999, pid=999)]
                    return original(hwnd)
                self.access.control = control
                self.assertFalse(send_background(self.job, self.phase, self.access, self.desktop)[0])
                self.assert_untouched()

    def test_fresh_input_lookup_error_preserves_empty_or_user_draft_and_hides_content(self):
        self.access.control = Mock(side_effect=[self.root, RuntimeError('PRIVATE-CONTENT')])
        ok, reason = send_background(self.job, self.phase, self.access, self.desktop)
        self.assertFalse(ok)
        self.assert_untouched()
        self.assertNotIn('PRIVATE', reason + str(self.job['_diagnostic']))
        self.assertEqual(self.job['_diagnostic']['input_refresh_exception_type'], 'RuntimeError')


if __name__ == '__main__':
    unittest.main()
