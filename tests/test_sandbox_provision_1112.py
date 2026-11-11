# -*- coding: utf-8 -*-
"""1.3.112：DISM 读不到状态时，用 WMI 的 Win32_OptionalFeature 兜底。

背景：dism /get-featureinfo 需要管理员权限，普通用户的微信机器人进程只会拿到 740，
于是「沙盒是否已启用」永远显示状态未知，一键安装在大多数机器上根本走不下去。
WMI 读的是同一份状态，普通权限就能读，所以它才是那条默认路。
"""

import subprocess
import unittest
from unittest import mock

from src import sandbox_provision as provision

PRO = {'EditionID': 'Professional'}
ENABLED_OUT = 'State : Enabled' + chr(10)
DISABLED_OUT = 'State : Disabled' + chr(10)
ABSENT_OUT = 'Error: 0x800f0805' + chr(10) + 'Feature name is unknown.' + chr(10)


def two_way_runner(dism_out='', dism_rc=740, cim_out='', cim_rc=0, cim_err='', sink=None):
    """按子命令分流：dism 一条、WMI 一条，其余都当意外调用记下来。"""
    def run(cmd, timeout=None):
        joined = ' '.join(str(part) for part in cmd)
        if sink is not None:
            sink.append(joined)
        if 'get-featureinfo' in joined:
            return {'returncode': dism_rc, 'stdout': dism_out, 'stderr': ''}
        if 'Win32_OptionalFeature' in joined:
            return {'returncode': cim_rc, 'stdout': cim_out, 'stderr': cim_err}
        return {'returncode': -1, 'stdout': '', 'stderr': 'unexpected: ' + joined}
    return run


class InstallStateLabelTests(unittest.TestCase):
    def test_known_codes(self):
        self.assertEqual(provision._install_state_label(1), 'Enabled')
        self.assertEqual(provision._install_state_label(2), 'Disabled')
        self.assertEqual(provision._install_state_label(3), 'Absent')

    def test_accepts_strings(self):
        self.assertEqual(provision._install_state_label('1'), 'Enabled')
        self.assertEqual(provision._install_state_label(' 2 ' + chr(13)), 'Disabled')

    def test_unknown_and_garbage_are_empty(self):
        for value in (4, 0, -1, '', None, 'abc', True, [], {}):
            self.assertEqual(provision._install_state_label(value), '', 'value=%r' % (value,))


class ParseInstallStateTests(unittest.TestCase):
    def test_bare_number(self):
        self.assertEqual(provision._parse_install_state('1' + chr(13) + chr(10)), 'Enabled')
        self.assertEqual(provision._parse_install_state('2'), 'Disabled')
        self.assertEqual(provision._parse_install_state('3'), 'Absent')

    def test_json_object(self):
        text = '{"Name":"' + provision.FEATURE_NAME + '","InstallState":1}'
        self.assertEqual(provision._parse_install_state(text), 'Enabled')
        text = '{"Name":"' + provision.FEATURE_NAME + '","InstallState":2}'
        self.assertEqual(provision._parse_install_state(text), 'Disabled')

    def test_json_object_other_feature_is_ignored(self):
        text = '{"Name":"Containers","InstallState":1}'
        self.assertEqual(provision._parse_install_state(text), '')

    def test_json_list(self):
        text = ('[{"Name":"Other","InstallState":2},'
                '{"Name":"' + provision.FEATURE_NAME + '","InstallState":1}]')
        self.assertEqual(provision._parse_install_state(text), 'Enabled')

    def test_broken_json_is_empty(self):
        self.assertEqual(provision._parse_install_state('{not json'), '')

    def test_empty_and_noise(self):
        self.assertEqual(provision._parse_install_state(''), '')
        self.assertEqual(provision._parse_install_state(None), '')
        self.assertEqual(provision._parse_install_state('Get-CimInstance : 拒绝访问'), '')


class CimFeatureStateTests(unittest.TestCase):
    def test_reads_install_state(self):
        result = provision._cim_feature_state(two_way_runner(cim_out='1' + chr(13) + chr(10)))
        self.assertEqual(result['state'], 'Enabled')
        self.assertEqual(result['returncode'], 0)

    def test_disabled(self):
        result = provision._cim_feature_state(two_way_runner(cim_out='2'))
        self.assertEqual(result['state'], 'Disabled')

    def test_command_targets_the_feature(self):
        sink = []
        provision._cim_feature_state(two_way_runner(cim_out='1', sink=sink))
        self.assertEqual(len(sink), 1)
        joined = sink[0]
        self.assertIn('Win32_OptionalFeature', joined)
        self.assertIn(provision.FEATURE_NAME, joined)
        self.assertIn('-NoProfile', joined)

    def test_command_uses_powershell(self):
        sink = []
        provision._cim_feature_state(two_way_runner(cim_out='1', sink=sink))
        self.assertTrue(sink[0].lower().startswith('powershell') or
                        'powershell.exe' in sink[0].lower(), sink[0])

    def test_nonzero_exit_is_not_absent(self):
        result = provision._cim_feature_state(
            two_way_runner(cim_out='', cim_rc=740, cim_err='需要提升权限'))
        self.assertEqual(result['state'], '')
        self.assertEqual(result['returncode'], 740)
        self.assertIn('需要提升权限', result['text'])

    def test_empty_output_with_zero_exit_is_absent(self):
        result = provision._cim_feature_state(two_way_runner(cim_out='', cim_rc=0))
        self.assertEqual(result['state'], 'Absent')

    def test_empty_output_with_stderr_is_not_absent(self):
        result = provision._cim_feature_state(
            two_way_runner(cim_out='', cim_rc=0, cim_err='Get-CimInstance : 访问被拒绝'))
        self.assertEqual(result['state'], '')

    def test_runner_crash_is_swallowed(self):
        def boom(cmd, timeout=None):
            raise OSError('spawn failed')
        result = provision._cim_feature_state(boom)
        self.assertEqual(result['state'], '')
        self.assertEqual(result['returncode'], -1)

    def test_powershell_path_is_absolute_when_present(self):
        path = provision._powershell_path()
        self.assertTrue(path.lower().endswith('powershell.exe'), path)
        if path != 'powershell.exe':
            self.assertTrue(path.startswith('\\\\') or ':' in path, path)


class ProbeFeatureTests(unittest.TestCase):
    def test_dism_answer_wins_and_cim_is_not_called(self):
        sink = []
        probe = provision._probe_feature(two_way_runner(dism_out=ENABLED_OUT, dism_rc=0, sink=sink))
        self.assertEqual(probe['state'], 'Enabled')
        self.assertEqual(probe['source'], 'dism')
        self.assertEqual(len(sink), 1, sink)

    def test_dism_absent_skips_cim(self):
        sink = []
        probe = provision._probe_feature(
            two_way_runner(dism_out=ABSENT_OUT, dism_rc=1, sink=sink))
        self.assertEqual(probe['state'], 'Absent')
        self.assertEqual(probe['source'], 'dism')
        self.assertEqual(len(sink), 1, sink)

    def test_falls_back_to_cim_without_admin(self):
        probe = provision._probe_feature(
            two_way_runner(dism_out='', dism_rc=740, cim_out='1'))
        self.assertEqual(probe['state'], 'Enabled')
        self.assertEqual(probe['source'], 'cim')
        self.assertIn('1', probe['text'])

    def test_falls_back_to_cim_when_dism_says_nothing(self):
        probe = provision._probe_feature(
            two_way_runner(dism_out='', dism_rc=0, cim_out='2'))
        self.assertEqual(probe['state'], 'Disabled')
        self.assertEqual(probe['source'], 'cim')

    def test_both_unreadable_leaves_empty_state(self):
        probe = provision._probe_feature(
            two_way_runner(dism_out='', dism_rc=740, cim_out='', cim_rc=740))
        self.assertEqual(probe['state'], '')
        self.assertEqual(probe['source'], '')

    def test_keeps_dism_returncode(self):
        probe = provision._probe_feature(
            two_way_runner(dism_out='', dism_rc=740, cim_out='1'))
        self.assertEqual(probe['returncode'], 740)


class DetectWithCimTests(unittest.TestCase):
    def detect(self, runner, last_record=None, wsb=True, broken=False):
        with mock.patch.object(provision, '_wsb_exe_exists', return_value=wsb), \
                mock.patch.object(provision, '_sandbox_app_exists', return_value=True), \
                mock.patch.object(provision, '_container_broken', return_value=broken):
            return provision.detect(runner=runner, registry_reader=PRO, last_record=last_record)

    def test_cim_enabled_reaches_launch(self):
        status = self.detect(two_way_runner(dism_out='', dism_rc=740, cim_out='1'))
        self.assertEqual(status['state'], 'enabled')
        self.assertEqual(status['reason'], 'enabled')
        self.assertEqual(status['next_step'], 'launch')
        self.assertEqual(status['feature'], 'Enabled')

    def test_cim_disabled_asks_to_enable(self):
        status = self.detect(two_way_runner(dism_out='', dism_rc=740, cim_out='2'))
        self.assertEqual(status['state'], 'disabled')
        self.assertEqual(status['next_step'], 'enable')

    def test_cim_absent_reports_absent(self):
        status = self.detect(two_way_runner(dism_out='', dism_rc=740, cim_out='', cim_rc=0))
        self.assertEqual(status['state'], 'absent')
        self.assertEqual(status['reason'], 'feature_absent')

    def test_cim_enabled_without_wsb_is_no_cli(self):
        status = self.detect(two_way_runner(dism_out='', dism_rc=740, cim_out='1'), wsb=False)
        self.assertEqual(status['state'], 'no_cli')
        self.assertEqual(status['next_step'], 'manual')

    def test_cim_enabled_with_broken_container(self):
        status = self.detect(two_way_runner(dism_out='', dism_rc=740, cim_out='1'), broken=True)
        self.assertEqual(status['state'], 'broken')

    def test_both_unreadable_still_actionable(self):
        status = self.detect(two_way_runner(dism_out='', dism_rc=740, cim_out='', cim_rc=740))
        self.assertEqual(status['state'], 'unknown')
        self.assertEqual(status['reason'], 'dism_unavailable')
        self.assertEqual(status['next_step'], 'enable')
        self.assertIn('WMI', status['message'])
        self.assertTrue(status['hint'])


class PendingRebootTests(unittest.TestCase):
    def detect(self, rebooted, runner=None):
        runner = runner or two_way_runner(dism_out='', dism_rc=740, cim_out='', cim_rc=740)
        with mock.patch.object(provision, '_wsb_exe_exists', return_value=True), \
                mock.patch.object(provision, '_sandbox_app_exists', return_value=True), \
                mock.patch.object(provision, '_rebooted_since', return_value=rebooted):
            return provision.detect(runner=runner, registry_reader=PRO,
                                    last_record={'state': 'pending_reboot'})

    def test_waiting_for_reboot_says_so(self):
        status = self.detect(False)
        self.assertEqual(status['state'], 'unknown')
        self.assertTrue(status['needs_reboot'])
        self.assertEqual(status['next_step'], 'enable')
        self.assertIn('重启', status['message'])
        self.assertIn('我已重启', status['hint'])

    def test_after_reboot_falls_back_to_plain_unknown(self):
        status = self.detect(True)
        self.assertEqual(status['state'], 'unknown')
        self.assertFalse(status['needs_reboot'])
        self.assertIn('WMI', status['message'])

    def test_reboot_pending_but_cim_says_enabled(self):
        status = self.detect(False, runner=two_way_runner(dism_out='', dism_rc=740, cim_out='1'))
        self.assertEqual(status['state'], 'enabled')
        self.assertTrue(status['needs_reboot'])


class DefaultRunnerTests(unittest.TestCase):
    def test_hides_console_window(self):
        completed = mock.Mock(returncode=0, stdout=b'ok', stderr=b'')
        with mock.patch.object(provision.subprocess, 'run', return_value=completed) as run:
            result = provision._default_runner(['whoami'], 5.0)
        self.assertEqual(result['stdout'], 'ok')
        kwargs = run.call_args[1]
        self.assertEqual(kwargs['creationflags'],
                         getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(kwargs['timeout'], 5.0)

    def test_missing_executable_is_a_result_not_an_exception(self):
        with mock.patch.object(provision.subprocess, 'run', side_effect=OSError('nope')):
            result = provision._default_runner(['nope.exe'], 5.0)
        self.assertEqual(result['returncode'], -1)
        self.assertIn('nope', result['stderr'])


if __name__ == '__main__':
    unittest.main()
