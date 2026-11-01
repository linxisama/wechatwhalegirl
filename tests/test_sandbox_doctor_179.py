# -*- coding: utf-8 -*-
"""1.3.79：沙盒环境诊断（--sandbox-doctor）的结论判定与报告格式。

用户那台 Win10 起不来沙盒，我们看不到它的屏幕，只能让它把事实采集回来。
这个用例锁定三件事：结论怎么判、报告长什么样、探测失败会不会把进程留在桌面。
"""
import unittest
from pathlib import Path
from unittest import mock

from src import sandbox_doctor


def _runner_factory(mapping):
    def runner(command, timeout=None):
        key = " ".join(command).lower()
        for name, value in mapping.items():
            if name.lower() in key:
                return value
        return -1, ""
    return runner


SC_RUNNING = (0, "STATE              : 4  RUNNING")
SC_STOPPED = (0, "STATE              : 1  STOPPED")


class VerdictTests(unittest.TestCase):
    def _report(self, *, app=True, wsb=True, cm=SC_RUNNING[1], probe=None):
        report = {
            'binaries': {
                'sandbox_app': {'exists': app},
                'wsb': {'exists': wsb},
            },
            'services': {'CmService': {'state': sandbox_doctor._sc_state(cm)}},
        }
        if probe is not None:
            report['probe'] = probe
        return report

    def test_missing_sandbox_app(self):
        code, hint = sandbox_doctor.verdict(self._report(app=False))
        self.assertEqual(code, 'no_sandbox_app')
        self.assertIn('Windows 沙盒', hint)

    def test_healthy_machine_is_ok(self):
        code, _ = sandbox_doctor.verdict(self._report())
        self.assertEqual(code, 'ok')

    def test_stopped_container_service(self):
        code, hint = sandbox_doctor.verdict(self._report(cm=SC_STOPPED[1]))
        self.assertEqual(code, 'container_service_down')
        self.assertIn('STOPPED', hint)

    def test_missing_cli_is_not_fatal(self):
        code, hint = sandbox_doctor.verdict(self._report(wsb=False))
        self.assertEqual(code, 'no_cli_but_app')
        self.assertIn('本机模式', hint)

    def test_inconclusive_probe_reports_inconclusive(self):
        report = self._report(probe={'started': True, 'marker': False, 'window': False})
        code, hint = sandbox_doctor.verdict(report)
        self.assertEqual(code, 'sandbox_probe_inconclusive')
        self.assertIn('组件存储', hint)

    def test_successful_probe_wins(self):
        report = self._report(cm=SC_STOPPED[1], probe={'started': True, 'marker': True, 'window': True})
        code, _ = sandbox_doctor.verdict(report)
        self.assertEqual(code, 'ok')


class ProbeTests(unittest.TestCase):
    def test_no_exe_never_spawns(self):
        spawned = []
        result = sandbox_doctor.probe_launch(
            system_root='.tmp-no-such-root',
            spawn=lambda command: spawned.append(command))
        self.assertFalse(result['started'])
        self.assertEqual(result['reason'], 'no_sandbox_app')
        self.assertEqual(spawned, [])

    def test_marker_seen_and_process_killed(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        (root / 'System32').mkdir()
        (root / 'System32' / 'WindowsSandbox.exe').write_bytes(b'MZ')
        killed = []

        class FakeProc:
            pid = 4242

        def spawn(command):
            # 模拟沙盒起来后写入标记文件
            wsb = Path(command[1])
            host = wsb.read_text(encoding='utf-8').split('<HostFolder>')[1].split('<')[0]
            (Path(host) / 'marker.txt').write_text('ok', encoding='utf-8')
            return FakeProc()

        with mock.patch.object(sandbox_doctor, '_terminate',
                               lambda proc: killed.append(proc.pid)):
            result = sandbox_doctor.probe_launch(
                system_root=root, spawn=spawn, timeout=5.0, poll=0.0,
                sleep=lambda seconds: None)
        self.assertTrue(result['started'])
        self.assertTrue(result['marker'])
        self.assertEqual(killed, [4242])

    def test_timeout_reports_no_marker_and_still_kills(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        (root / 'System32').mkdir()
        (root / 'System32' / 'WindowsSandbox.exe').write_bytes(b'MZ')
        killed = []
        ticks = {'now': 0.0}

        class FakeProc:
            pid = 7

        def clock():
            ticks['now'] += 10.0
            return ticks['now']

        with mock.patch.object(sandbox_doctor, '_terminate',
                               lambda proc: killed.append(proc.pid)):
            result = sandbox_doctor.probe_launch(
                system_root=root, spawn=lambda command: FakeProc(),
                timeout=5.0, poll=0.0, clock=clock, sleep=lambda seconds: None)
        self.assertFalse(result['marker'])
        self.assertEqual(result['reason'], 'no_marker')
        self.assertEqual(killed, [7])

    def test_wsb_payload_has_mapping_and_logon_command(self):
        xml = sandbox_doctor._probe_wsb(r'D:\tmp\out')
        self.assertIn('<HostFolder>D:\\tmp\\out</HostFolder>', xml)
        self.assertIn('<SandboxFolder>C:\\WhaleDoctor</SandboxFolder>', xml)
        self.assertIn('&gt;C:\\WhaleDoctor\\marker.txt', xml)


class CollectTests(unittest.TestCase):
    def test_collect_aggregates_sections(self):
        runner = _runner_factory({
            'get-featureinfo': (0, 'State : Enabled'),
            'cmService': SC_RUNNING,
            'vmcompute': SC_RUNNING,
            'hns': SC_RUNNING,
            'hcsdiag': (0, ''),
        })
        report = sandbox_doctor.collect(
            runner=runner, resolver=lambda: (r'C:\x\wsb.exe', True, 'path'),
            version='1.3.79')
        self.assertEqual(report['version'], '1.3.79')
        self.assertEqual(report['feature']['state'], 'Enabled')
        self.assertEqual(report['services']['CmService']['state'], 'RUNNING')
        self.assertEqual(report['hcsdiag']['lines'], 0)
        self.assertEqual(report['verdict'], 'ok')

    def test_render_lists_verdict_first(self):
        report = sandbox_doctor.collect(
            runner=_runner_factory({'cmService': SC_STOPPED}),
            resolver=lambda: ('', False, 'none'), version='1.3.79')
        text = sandbox_doctor.render(report)
        lines = text.splitlines()
        self.assertIn('结论：', lines[3])
        self.assertIn('建议：', lines[4])
        for section in ('[系统]', '[可执行文件]', '[服务]', '[原始输出]'):
            self.assertIn(section, lines)

    def test_write_report_is_bom_crlf(self):
        import tempfile
        target = Path(tempfile.mkdtemp()) / 'report.txt'
        report = {'verdict': 'ok', 'hint': 'h', 'generated': 'now', 'version': '1.3.79'}
        path = sandbox_doctor.write_report(report, target)
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        self.assertGreater(raw.count(b'\r\n'), 3)
        self.assertNotIn(b'\n\n', raw.replace(b'\r\n', b''))


if __name__ == '__main__':
    unittest.main()
