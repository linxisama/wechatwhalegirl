"""实测 Qt 说明窗不能冒充同账号主窗；保留多主窗歧义及查询失败保护。"""
import unittest
from unittest.mock import Mock

from tests.test_window_discovery1320 import NativeDiscoveryFixture


class QtWindowRole1329Tests(unittest.TestCase):
    def actual_pair(self):
        fixture = NativeDiscoveryFixture()
        fixture.windows[10].update(window_style=2529624064, style=256)
        fixture.add(20, window_style=2529427456)
        return fixture

    def test_observed_same_pid_same_caption_unowned_intro_is_excluded(self):
        fixture = self.actual_pair()
        row, info = fixture.selected()
        self.assertEqual(row['hwnd'], 10)
        self.assertEqual(info['candidate_count'], 1)
        self.assertEqual(info['matched_count'], 1)
        self.assertEqual(info['window_discovery']['auxiliary_windows'], 1)
        self.assertEqual(info['window_discovery']['query_errors'], 0)
        self.assertEqual(info['window_discovery']['owned_windows'], 0)
        self.assertEqual(info['window_discovery']['tool_windows'], 0)

    def test_intro_alone_is_not_a_chat_main_window(self):
        fixture = self.actual_pair()
        fixture.windows.pop(10)
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['candidate_count'], 0)
        self.assertEqual(info['waiting_code'], 'window_not_found')
        self.assertEqual(info['window_discovery']['auxiliary_windows'], 1)
        fixture.access.matches_account.assert_not_called()

    def test_two_legitimate_main_windows_remain_ambiguous_with_intro(self):
        fixture = self.actual_pair()
        fixture.add(30)
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['matched_count'], 2)
        self.assertEqual(info['waiting_code'], 'account_unverified')
        self.assertEqual(info['window_discovery']['auxiliary_windows'], 1)

    def test_either_minimize_or_maximize_role_is_sufficient(self):
        for buttons in (0x10000, 0x20000, 0x30000):
            with self.subTest(buttons=buttons):
                fixture = NativeDiscoveryFixture()
                fixture.windows[10]['window_style'] = 0x96c40000 | buttons
                row, info = fixture.selected()
                self.assertEqual(row['hwnd'], 10)
                self.assertEqual(info['window_discovery']['auxiliary_windows'], 0)

    def test_signed_native_style_preserves_role_bits(self):
        fixture = self.actual_pair()
        for row in fixture.windows.values():
            row['window_style'] -= 1 << 32
        selected, info = fixture.selected()
        self.assertEqual(selected['hwnd'], 10)
        self.assertEqual(info['window_discovery']['auxiliary_windows'], 1)

    def test_mmui_main_window_does_not_require_qt_role_style(self):
        fixture = NativeDiscoveryFixture()
        fixture.windows[10].update(native_class='mmui::MainWindow', window_style=0)
        fixture.access.gui.GetWindowLong = Mock(wraps=fixture.window_long)
        self.assertEqual(fixture.selected()[0]['hwnd'], 10)
        self.assertTrue(fixture.access.gui.GetWindowLong.call_args_list)
        self.assertTrue(all(call.args[1] == -20 for call in fixture.access.gui.GetWindowLong.call_args_list))

    def test_unknown_qt_role_metadata_blocks_partial_enumeration(self):
        for value in (None, True, '2529624064', Mock()):
            with self.subTest(value_type=type(value).__name__):
                fixture = NativeDiscoveryFixture()
                fixture.add(20, window_style=value)
                row, info = fixture.selected()
                self.assertIsNone(row)
                self.assertEqual(info['waiting_code'], 'window_query_failed')
                self.assertEqual(info['window_discovery']['query_errors'], 1)
                self.assertEqual(info['window_discovery']['auxiliary_windows'], 0)
                fixture.access.matches_account.assert_not_called()

    def test_native_role_query_exception_is_not_window_absence(self):
        fixture = NativeDiscoveryFixture()
        fixture.add(20, window_style=PermissionError('private role details'))
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'window_query_failed')
        self.assertEqual(info['window_discovery']['error_types'], ['PermissionError'])
        self.assertNotIn('private', str(info))
        fixture.access.matches_account.assert_not_called()

    def test_role_removed_during_account_lookup_rejects_stale_main(self):
        fixture = NativeDiscoveryFixture()
        fixture.account_hook = lambda pid: fixture.windows[10].update(window_style=0x96c40000)
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'window_changed')

    def test_role_query_failure_during_validation_rejects_stale_main(self):
        for value in (None, False, PermissionError('private role changed')):
            with self.subTest(value_type=type(value).__name__):
                fixture = NativeDiscoveryFixture()
                fixture.account_hook = lambda pid: fixture.windows[10].update(window_style=value)
                row, info = fixture.selected()
                self.assertIsNone(row)
                self.assertEqual(info['waiting_code'], 'window_changed')
                self.assertNotIn('private', str(info))

    def test_zero_role_style_is_explicit_auxiliary_not_query_failure(self):
        fixture = NativeDiscoveryFixture()
        fixture.windows[10]['window_style'] = 0
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['window_discovery']['auxiliary_windows'], 1)
        self.assertEqual(info['window_discovery']['query_errors'], 0)

    def test_hidden_intro_and_minimized_main_keep_existing_visibility_rules(self):
        fixture = self.actual_pair()
        fixture.windows[10].update(visible=False, minimized=True)
        fixture.windows[20]['visible'] = False
        row, info = fixture.selected()
        self.assertEqual(row['hwnd'], 10)
        self.assertTrue(row['minimized'])
        self.assertEqual(info['window_discovery']['auxiliary_windows'], 0)


if __name__ == '__main__':
    unittest.main()
