"""只读冷树和读取错误不能被误当成稳定客户端能力；全部假控件。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.wechat_background import inspect_background, send_background
from tests import test_desktop_140 as fixtures


class BackgroundReadinessTests(unittest.TestCase):
    def setUp(self):
        fixtures.BackgroundTests.setUp(self)
        self.shell = SimpleNamespace(ClassName='Qt51514QWindowIcon', AutomationId='',
            ControlTypeName='WindowControl', GetChildren=lambda: [])
        # 冷树刷新后还须写前重新定位和写后重新定位，各自取得当前树。
        self.access.control = Mock(side_effect=[self.shell, self.root, self.root, self.root])
        self.clock = [0.0]

    def inspect(self):
        return inspect_background('test', '测试群', self.access, self.desktop)

    def clocked(self):
        return patch.multiple('src.wechat_background.time',
            monotonic=lambda: self.clock[0],
            sleep=lambda duration: self.clock.__setitem__(0, self.clock[0] + duration))

    def assert_no_input(self):
        self.value.SetValue.assert_not_called()
        self.invoke.Invoke.assert_not_called()

    def test_cold_shell_reacquires_same_window_once_without_input(self):
        with self.clocked():
            info, controls = self.inspect()
        self.assertTrue(info['ready'])
        self.assertIsNotNone(controls)
        self.assertEqual(self.access.control.call_count, 2)
        self.assertEqual(info['input_recheck_count'], 1)
        self.assertEqual(len(info['input_search_attempts']), 2)
        self.assertIs(info['submit_verified'], False)
        self.assert_no_input()

    def test_read_error_is_not_reported_as_provider_unavailable_or_retried(self):
        self.shell.GetChildren = Mock(side_effect=RuntimeError('PRIVATE PROVIDER CONTENT'))
        with self.clocked():
            info, controls = self.inspect()
        self.assertFalse(info['ready'])
        self.assertIsNone(controls)
        self.assertEqual(info['waiting_code'], 'input_unavailable')
        self.assertEqual(info['input_search']['errors'], 1)
        self.assertIn('读取失败', info['reason'])
        self.assertNotIn('PRIVATE', str(info))
        self.assertNotIn('当前客户端暂不支持', info['reason'])
        self.assertEqual(self.access.control.call_count, 1)
        self.assert_no_input()

    def test_persistent_shell_gets_one_short_read_recheck_only(self):
        self.access.control.side_effect = [self.shell, self.shell]
        with self.clocked():
            info, controls = self.inspect()
        self.assertFalse(info['ready'])
        self.assertIsNone(controls)
        self.assertEqual(self.access.control.call_count, 2)
        self.assertLessEqual(self.clock[0], .3)
        self.assertIn('证据不足', info['reason'])
        self.assertNotIn('当前客户端暂不支持', info['reason'])
        self.assert_no_input()

    def test_account_window_change_before_recheck_does_not_read_new_window(self):
        self.access.candidates = Mock(side_effect=[[self.row], [dict(self.row, hwnd=101)]])
        with self.clocked():
            info, controls = self.inspect()
        self.assertFalse(info['ready'])
        self.assertIsNone(controls)
        self.assertEqual(info['waiting_code'], 'account_unverified')
        self.assertEqual(self.access.control.call_count, 1)
        self.assert_no_input()

    def test_minimized_window_during_read_wait_is_never_restored(self):
        self.access.candidates = Mock(side_effect=[[self.row], [dict(self.row, minimized=True)]])
        self.access.restore = Mock(side_effect=AssertionError('must not restore'))
        with self.clocked():
            info, controls = self.inspect()
        self.assertFalse(info['ready'])
        self.assertIsNone(controls)
        self.assertEqual(info['waiting_code'], 'minimized')
        self.access.restore.assert_not_called()
        self.assertEqual(self.access.control.call_count, 1)
        self.assert_no_input()

    def test_desktop_lost_while_waiting_prevents_recheck(self):
        self.desktop = lambda: (self.clock[0] == 0, '桌面不可用')
        with self.clocked():
            info, controls = self.inspect()
        self.assertFalse(info['ready'])
        self.assertIsNone(controls)
        self.assertIs(info['desktop_ready'], False)
        self.assertEqual(self.access.control.call_count, 1)
        self.assert_no_input()

    def test_initial_read_consuming_budget_does_not_extend_by_sleeping(self):
        def late_children():
            self.clock[0] = 2.1
            return []
        self.shell.GetChildren = late_children
        with self.clocked():
            info, controls = self.inspect()
        self.assertFalse(info['ready'])
        self.assertIsNone(controls)
        self.assertEqual(self.access.control.call_count, 1)
        self.assertEqual(info['input_recheck_skipped'], 'budget')
        self.assertEqual(self.clock[0], 2.1)
        self.assert_no_input()

    def test_non_shell_empty_chat_tree_is_not_retried(self):
        self.shell.ClassName = 'mmui::MainWindow'
        with self.clocked():
            info, controls = self.inspect()
        self.assertFalse(info['ready'])
        self.assertIsNone(controls)
        self.assertEqual(self.access.control.call_count, 1)
        self.assert_no_input()

    def test_refreshed_chat_still_requires_exact_title_and_empty_draft(self):
        for changed in ('title', 'draft'):
            with self.subTest(changed=changed):
                self.setUp()
                if changed == 'title':
                    self.field.Name = '别的群'
                else:
                    self.value.Value = '保留用户草稿'
                with self.clocked():
                    info, controls = self.inspect()
                self.assertFalse(info['ready'])
                self.assertIsNone(controls)
                self.assertEqual(info['waiting_code'], 'chat_not_open' if changed == 'title' else 'draft')
                self.assert_no_input()

    def test_refreshed_tree_read_error_does_not_turn_into_unsupported_claim(self):
        self.root.GetChildren = Mock(side_effect=RuntimeError('PRIVATE'))
        with self.clocked():
            info, controls = self.inspect()
        self.assertFalse(info['ready'])
        self.assertIsNone(controls)
        self.assertEqual(info['waiting_code'], 'input_unavailable')
        self.assertEqual(info['input_search']['errors'], 1)
        self.assertNotIn('PRIVATE', str(info))
        self.assert_no_input()

    def test_initial_root_read_error_is_a_read_failure(self):
        self.access.control.side_effect = RuntimeError('PRIVATE')
        with self.clocked():
            info, controls = self.inspect()
        self.assertFalse(info['ready'])
        self.assertIsNone(controls)
        self.assertEqual(info['waiting_code'], 'input_unavailable')
        self.assertNotIn('PRIVATE', str(info))
        self.assertEqual(self.access.control.call_count, 1)
        self.assert_no_input()

    def test_read_recheck_does_not_duplicate_eventual_single_submission(self):
        with self.clocked():
            ok, _ = send_background(self.job, self.phase, self.access, self.desktop)
        self.assertTrue(ok)
        self.value.SetValue.assert_called_once_with(self.job['text'])
        self.invoke.Invoke.assert_called_once()
        self.assertEqual(self.access.control.call_count, 4)
        self.assertFalse(self.job['_diagnostic']['delivery_verified'])
