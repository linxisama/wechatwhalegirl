"""窗口发现只读替身：附属窗、枚举缺口与进程代次不能误作主窗。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.wechat_window import NativeWindowAccess, select_window


class NativeDiscoveryFixture:
    def __init__(self):
        self.windows = {10: dict(pid=42, visible=True, minimized=False, owner=0,
            style=0, window_style=0x96c70000, title='微信', native_class='Qt51514QWindowIcon')}
        self.processes = {42: dict(name='Weixin.exe', created=100.25)}
        self.fail_owner = set()
        self.account_hook = None
        self.enum_failure = False
        self.owner_calls = []
        self.access = object.__new__(NativeWindowAccess)
        self.access.gui = SimpleNamespace(EnumWindows=self.enumerate,
            IsIconic=lambda hwnd: self.windows[hwnd]['minimized'],
            IsWindowVisible=lambda hwnd: self.windows[hwnd]['visible'],
            IsWindow=lambda hwnd: hwnd in self.windows,
            GetWindowText=lambda hwnd: self.windows[hwnd]['title'],
            GetClassName=lambda hwnd: self.windows[hwnd]['native_class'],
            GetWindow=self.owner, GetWindowLong=self.window_long)
        self.access.process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, self.windows[hwnd]['pid']))
        self.access.psutil = SimpleNamespace(Process=self.process)
        self.access.matches_account = Mock(side_effect=self.matches)

    def enumerate(self, callback, data):
        for hwnd in tuple(self.windows):
            callback(hwnd, data)
        if self.enum_failure:
            raise OSError('private enumeration details')

    def owner(self, hwnd, _):
        self.owner_calls.append(hwnd)
        if hwnd in self.fail_owner:
            raise PermissionError('private window details')
        return self.windows[hwnd]['owner']

    def window_long(self, hwnd, index):
        assert index in (-16, -20)
        value = self.windows[hwnd]['window_style' if index == -16 else 'style']
        if isinstance(value, Exception):
            raise value
        return value

    def process(self, pid):
        def created():
            value = self.processes[pid]['created']
            if isinstance(value, Exception):
                raise value
            return value
        return SimpleNamespace(name=lambda: self.processes[pid]['name'], create_time=created)

    def matches(self, pid, path):
        if self.account_hook:
            self.account_hook(pid)
        return pid == 42

    def add(self, hwnd, **values):
        self.windows[hwnd] = dict(self.windows[10], **values)

    def selected(self):
        return select_window(self.access, 'C:/wx/bound')


class NativeWindowDiscoveryTests(unittest.TestCase):
    def test_owned_and_tool_windows_do_not_make_same_account_main_ambiguous(self):
        fixture = NativeDiscoveryFixture()
        fixture.add(20, owner=10)
        fixture.add(30, style=0x80)
        row, info = fixture.selected()
        self.assertEqual(row['hwnd'], 10)
        self.assertEqual(info['candidate_count'], 1)
        self.assertEqual(info['window_discovery']['owned_windows'], 1)
        self.assertEqual(info['window_discovery']['tool_windows'], 1)

    def test_other_process_is_filtered_before_owner_metadata_query(self):
        fixture = NativeDiscoveryFixture()
        fixture.processes[43] = dict(name='Other.exe', created=20.)
        fixture.add(20, pid=43)
        fixture.fail_owner.add(20)
        self.assertEqual(fixture.selected()[0]['hwnd'], 10)
        self.assertNotIn(20, fixture.owner_calls)

    def test_hidden_main_is_ignored_but_minimized_main_keeps_its_identity(self):
        fixture = NativeDiscoveryFixture()
        fixture.windows[10]['visible'] = False
        self.assertIsNone(fixture.selected()[0])
        fixture.windows[10]['minimized'] = True
        row, info = fixture.selected()
        self.assertEqual(row['hwnd'], 10)
        self.assertTrue(info['minimized'])
        self.assertEqual(row['process_created'], 100.25)

    def test_two_independent_main_windows_of_same_account_are_still_ambiguous(self):
        fixture = NativeDiscoveryFixture()
        fixture.add(20)
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['matched_count'], 2)
        self.assertEqual(info['waiting_code'], 'account_unverified')

    def test_owner_query_failure_blocks_partial_enumeration_and_does_not_leak_text(self):
        fixture = NativeDiscoveryFixture()
        fixture.add(20)
        fixture.fail_owner.add(20)
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'window_query_failed')
        self.assertEqual(info['window_discovery']['query_errors'], 1)
        self.assertNotIn('private', str(info))
        fixture.access.matches_account.assert_not_called()

    def test_enumeration_failure_after_a_good_row_is_not_success(self):
        fixture = NativeDiscoveryFixture()
        fixture.enum_failure = True
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'window_query_failed')
        self.assertFalse(info['window_discovery']['enumeration_complete'])

    def test_process_creation_query_failure_is_not_absence(self):
        fixture = NativeDiscoveryFixture()
        fixture.processes[42]['created'] = PermissionError('private executable path')
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'window_query_failed')
        self.assertEqual(info['window_discovery']['query_errors'], 1)
        self.assertNotIn('private', str(info))

    def test_unknown_native_metadata_is_not_coerced_into_an_unowned_main_window(self):
        for field, value in (('owner', None), ('owner', Mock()), ('style', None), ('style', Mock())):
            with self.subTest(field=field, value=value):
                fixture = NativeDiscoveryFixture()
                fixture.windows[10][field] = value
                row, info = fixture.selected()
                self.assertIsNone(row)
                self.assertEqual(info['waiting_code'], 'window_query_failed')

    def test_invalid_process_creation_metadata_cannot_authorize_a_window(self):
        for created in (True, None, 0, float('nan')):
            with self.subTest(created=created):
                fixture = NativeDiscoveryFixture()
                fixture.processes[42]['created'] = created
                row, info = fixture.selected()
                self.assertIsNone(row)
                self.assertEqual(info['waiting_code'], 'window_query_failed')

    def test_handle_pid_change_during_account_lookup_is_rejected(self):
        fixture = NativeDiscoveryFixture()
        fixture.processes[43] = dict(name='Weixin.exe', created=100.5)
        fixture.account_hook = lambda pid: fixture.windows[10].update(pid=43)
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'window_changed')

    def test_reused_pid_with_different_creation_time_is_rejected(self):
        fixture = NativeDiscoveryFixture()
        fixture.account_hook = lambda pid: fixture.processes[42].update(created=200.5)
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'window_changed')

    def test_window_becoming_owned_during_account_lookup_is_rejected(self):
        fixture = NativeDiscoveryFixture()
        fixture.account_hook = lambda pid: fixture.windows[10].update(owner=77)
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'window_changed')

    def test_minimized_state_is_refreshed_after_account_lookup(self):
        fixture = NativeDiscoveryFixture()
        fixture.account_hook = lambda pid: fixture.windows[10].update(minimized=True)
        row, info = fixture.selected()
        self.assertTrue(row['minimized'])
        self.assertTrue(info['minimized'])

    def test_closed_handle_during_account_lookup_is_rejected(self):
        fixture = NativeDiscoveryFixture()
        fixture.account_hook = lambda pid: fixture.windows.pop(10)
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'window_changed')

    def test_plain_mock_does_not_gain_a_magic_validation_method(self):
        access = Mock()
        access.candidates.return_value = [{'hwnd': 10, 'pid': 42, 'minimized': False}]
        access.matches_account.return_value = True
        self.assertEqual(select_window(access, 'bound')[0]['hwnd'], 10)
        access.validate_candidate.assert_not_called()

    def test_unknown_second_account_candidate_prevents_false_uniqueness(self):
        access = Mock()
        access.candidates.return_value = [{'hwnd': 10, 'pid': 42, 'minimized': False},
                                         {'hwnd': 20, 'pid': 43, 'minimized': False}]
        access.matches_account.side_effect = [True, PermissionError('private')]
        row, info = select_window(access, 'bound')
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'account_unverified')
        self.assertEqual(info['account_checks_failed'], 1)

    def test_complete_empty_enumeration_has_distinct_not_found_code(self):
        fixture = NativeDiscoveryFixture()
        fixture.windows.clear()
        row, info = fixture.selected()
        self.assertIsNone(row)
        self.assertEqual(info['waiting_code'], 'window_not_found')
        self.assertTrue(info['window_discovery']['enumeration_complete'])


if __name__ == '__main__':
    unittest.main()
