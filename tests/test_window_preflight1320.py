"""只读体检成功前须复核窗口和桌面；全部使用替身，不接触真实微信。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import wechat_window as windows


class WindowPreflightTests(unittest.TestCase):
    def setUp(self):
        self.pinned = dict(hwnd=10, pid=42, minimized=False, process_created=100.25)
        self.current = dict(self.pinned)
        self.access = Mock()
        self.access.candidates.side_effect = lambda: [dict(self.pinned if self.access.candidates.call_count == 1 else self.current)]
        self.access.matches_account.return_value = True
        self.field = SimpleNamespace(ClassName='mmui::ChatInputField', AutomationId='')
        self.root = SimpleNamespace(ClassName='Qt51514QWindowIcon')
        self.access.control.return_value = self.root
        self.lookup = dict(search_status='found', errors=0, classes=['mmui::ChatInputField'])
        self.desktop = Mock(return_value=(True, ''))

    def inspect(self, **kwargs):
        def search(root, predicate, timeout, diagnostic):
            diagnostic.update(self.lookup)
            return self.field
        with patch('src.wechat_send_worker.find_scoped', side_effect=search):
            return windows.inspect_window('C:/wx/bound', self.access, self.desktop, **kwargs)

    def test_strict_minimized_window_is_rejected_before_any_uia_read(self):
        self.pinned['minimized'] = True
        info = self.inspect(allow_restore=False)
        self.assertFalse(info['ready'])
        self.assertEqual(info['waiting_code'], 'minimized')
        self.access.control.assert_not_called()
        self.access.restore.assert_not_called()

    def test_control_query_failure_is_structured_and_has_no_exception_body(self):
        self.access.control.side_effect = PermissionError('PRIVATE CONTENT')
        info = self.inspect(allow_restore=False)
        self.assertFalse(info['ready'])
        self.assertEqual(info['waiting_code'], 'input_unavailable')
        self.assertEqual(info['error_type'], 'PermissionError')
        self.assertNotIn('PRIVATE', str(info))

    def test_root_class_query_failure_is_structured(self):
        class BrokenRoot:
            @property
            def ClassName(self):
                raise RuntimeError('PRIVATE CONTENT')
        self.access.control.return_value = BrokenRoot()
        info = self.inspect()
        self.assertFalse(info['ready'])
        self.assertEqual(info['waiting_code'], 'input_unavailable')
        self.assertEqual(info['error_type'], 'RuntimeError')
        self.assertNotIn('PRIVATE', str(info))

    def test_lookup_exception_is_structured(self):
        with patch('src.wechat_send_worker.find_scoped', side_effect=RuntimeError('PRIVATE CONTENT')):
            info = windows.inspect_window('bound', self.access, self.desktop)
        self.assertFalse(info['ready'])
        self.assertEqual(info['waiting_code'], 'input_unavailable')
        self.assertNotIn('PRIVATE', str(info))

    def test_partial_or_errored_chat_detail_tree_cannot_authorize_no_chat_ready(self):
        self.field = None
        for status, errors in (('budget', 0), ('exhausted', 1), ('error', 0)):
            with self.subTest(status=status, errors=errors):
                self.access.candidates.reset_mock()
                self.lookup = dict(search_status=status, errors=errors, classes=['mmui::ChatDetailView'])
                info = self.inspect(auto_open_chat=True)
                self.assertFalse(info['ready'])

    def test_clean_exhausted_no_chat_legacy_ready_still_rechecks_window(self):
        self.field = None
        self.lookup = dict(search_status='exhausted', errors=0, classes=['mmui::ChatDetailView'])
        info = self.inspect(auto_open_chat=True)
        self.assertTrue(info['ready'])
        self.assertEqual(self.access.candidates.call_count, 2)
        self.assertFalse(info['input_available'])
        self.access.restore.assert_not_called()

    def test_input_ready_is_cleared_when_desktop_locks_during_tree_read(self):
        self.desktop.side_effect = [(True, ''), (False, '桌面已锁定')]
        info = self.inspect()
        self.assertFalse(info['ready'])
        self.assertFalse(info['desktop_ready'])
        self.assertEqual(info['waiting_code'], 'desktop_unavailable')

    def test_input_ready_is_cleared_when_desktop_locks_during_account_recheck(self):
        self.desktop.side_effect = [(True, ''), (True, ''), (False, '桌面已锁定')]
        info = self.inspect()
        self.assertFalse(info['ready'])
        self.assertFalse(info['desktop_ready'])
        self.assertEqual(info['waiting_code'], 'desktop_unavailable')

    def test_changed_window_pid_or_process_generation_is_rejected(self):
        for change in (dict(hwnd=20), dict(pid=43), dict(process_created=200.5)):
            with self.subTest(change=change):
                self.access.candidates.reset_mock()
                self.current = dict(self.pinned, **change)
                info = self.inspect(allow_restore=False)
                self.assertFalse(info['ready'])
                self.assertEqual(info['waiting_code'], 'window_changed')

    def test_missing_generation_at_recheck_is_rejected(self):
        del self.current['process_created']
        info = self.inspect()
        self.assertFalse(info['ready'])
        self.assertEqual(info['waiting_code'], 'window_changed')

    def test_account_query_failure_at_recheck_is_not_hidden_by_old_input(self):
        self.access.matches_account.side_effect = [True, PermissionError('PRIVATE CONTENT')]
        info = self.inspect()
        self.assertFalse(info['ready'])
        self.assertEqual(info['waiting_code'], 'account_unverified')
        self.assertNotIn('PRIVATE', str(info))

    def test_strict_window_minimized_during_read_clears_ready(self):
        self.current['minimized'] = True
        info = self.inspect(allow_restore=False)
        self.assertFalse(info['ready'])
        self.assertTrue(info['minimized'])
        self.assertEqual(info['waiting_code'], 'minimized')

    def test_legacy_restore_allowed_reports_current_minimized_state_without_restoring(self):
        self.current['minimized'] = True
        info = self.inspect(allow_restore=True)
        self.assertTrue(info['ready'])
        self.assertTrue(info['minimized'])
        self.assertIn('最小化', info['reason'])
        self.access.restore.assert_not_called()


class WindowIdentityTests(unittest.TestCase):
    def test_same_generation_matches_and_changed_or_missing_generation_does_not(self):
        pinned = dict(hwnd=10, pid=42, process_created=100.25)
        self.assertTrue(windows.same_window_identity(dict(pinned), pinned))
        for value in (dict(pinned, process_created=200.5), dict(hwnd=10, pid=42),
                      dict(pinned, hwnd=20), dict(pinned, pid=43), None):
            with self.subTest(value=value):
                self.assertFalse(windows.same_window_identity(value, pinned))
        self.assertFalse(windows.same_window_identity(dict(hwnd=10, pid=42), dict(pinned, process_created=None)))

    def test_legacy_row_without_generation_keeps_hwnd_pid_comparison(self):
        pinned = dict(hwnd=10, pid=42)
        self.assertTrue(windows.same_window_identity(dict(pinned, process_created=100.25), pinned))
        self.assertFalse(windows.same_window_identity(dict(pinned, pid=43), pinned))


class WindowPrepareIdentityTests(unittest.TestCase):
    def test_changed_identity_or_minimized_after_lookup_never_activates(self):
        initial = dict(hwnd=10, pid=42, process_created=1.0, minimized=False)
        for changed in (dict(initial, hwnd=20), dict(initial, process_created=2.0),
                        dict(initial, minimized=True)):
            with self.subTest(changed=changed):
                access = Mock()
                access.candidates.side_effect = [[dict(initial)], [changed]]
                access.matches_account.return_value = True
                root = SimpleNamespace(ClassName='mmui::MainWindow', GetChildren=lambda: [])
                access.control.return_value = root
                uia = Mock()
                self.assertFalse(windows.prepare_window(uia, access, lambda: (True, ''), 'bound'))
                uia._activate.assert_not_called()
                access.restore.assert_not_called()


if __name__ == '__main__':
    unittest.main()
