"""1.3.76：Windows 10 只有 WindowsSandbox.exe、没有 wsb.exe 命令行，不能再说「沙盒已就绪」。

用户 2026-11-01 反馈：/api/sandbox/status 说没找到命令行，/api/sandbox/provision/status 却说
wsb_exe=true、可以去建通道。根因是 provision 探的其实是 WindowsSandbox.exe。这里钉住口径。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import sandbox_provision as provision
from src import site_profile
from src.sandbox_provision_api import SandboxProvisionService

ENABLED_OUT = ('部署映像服务和管理工具' + chr(13) + chr(10) + '功能信息:' + chr(13) + chr(10) +
               '功能名称 : Containers-DisposableClientVM' + chr(13) + chr(10) + '状态 : 已启用')
PRO = {'EditionID': 'Professional'}


def enabled_runner(cmd, timeout):
    return {'returncode': 0, 'stdout': ENABLED_OUT, 'stderr': ''}


class _SystemRoot:
    """伪造 C:\Windows：只放调用方点名的 exe，其他文件一律不存在。"""

    def __init__(self, *names):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'System32').mkdir()
        for name in names:
            (self.root / 'System32' / name).write_bytes(b'x')

    def __enter__(self):
        self._patch = mock.patch.dict(os.environ, {
            'SystemRoot': str(self.root),
            'LOCALAPPDATA': str(self.root / 'no-local-appdata'),
            'PATH': ''})
        self._patch.start()
        # 1.3.78 起 wsb 解析器还会问 Known Folder API，这里一并钉到临时目录，
        # 免得本机真实安装的 wsb.exe 让「没有命令行」的用例失效。
        from src import site_profile
        self._known = mock.patch.object(site_profile, '_known_local_appdata',
                                        return_value=str(self.root / 'no-local-appdata'))
        self._known.start()
        return self

    def __exit__(self, *exc):
        self._known.stop()
        self._patch.stop()
        self._tmp.cleanup()
        return False


class WsbCliProbeTests(unittest.TestCase):
    def test_only_sandbox_app_counts_as_no_cli(self):
        with _SystemRoot('WindowsSandbox.exe'):
            self.assertTrue(provision._sandbox_app_exists())
            self.assertFalse(provision._wsb_exe_exists())

    def test_wsb_exe_in_system32_is_found(self):
        with _SystemRoot('wsb.exe', 'WindowsSandbox.exe'):
            self.assertTrue(provision._wsb_exe_exists())

    def test_wsb_exe_from_windowsapps_fallback(self):
        with _SystemRoot('WindowsSandbox.exe') as fake:
            alias = fake.root / 'local' / 'Microsoft' / 'WindowsApps'
            alias.mkdir(parents=True)
            (alias / 'wsb.exe').write_bytes(b'x')
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(fake.root / 'local')}):
                self.assertTrue(provision._wsb_exe_exists())

    def test_enabled_without_cli_says_no_cli_instead_of_ready(self):
        with _SystemRoot('WindowsSandbox.exe'):
            status = provision.detect(runner=enabled_runner, registry_reader=PRO)
        self.assertEqual(status['state'], 'no_cli')
        self.assertEqual(status['next_step'], 'manual')
        self.assertFalse(status['wsb_exe'])
        self.assertTrue(status['sandbox_app'])
        self.assertIn('wsb.exe', status['message'])
        self.assertIn('本机模式', status['hint'])

    def test_enabled_with_cli_keeps_launch_step(self):
        with _SystemRoot('wsb.exe', 'WindowsSandbox.exe'):
            status = provision.detect(runner=enabled_runner, registry_reader=PRO)
        self.assertEqual(status['state'], 'enabled')
        self.assertEqual(status['next_step'], 'launch')


class ProvisionVerdictTests(unittest.TestCase):
    def _service(self):
        return SandboxProvisionService(runner=enabled_runner)

    def test_no_cli_verdict_is_not_ok_and_points_to_manual(self):
        service = self._service()
        with _SystemRoot('WindowsSandbox.exe'):
            verdict = service._probe_verdict('')
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['reason'], 'no_cli')
        self.assertEqual(verdict['next_step'], 'manual')
        self.assertIn('wsb.exe', verdict['message'])


class _FakePath:
    def __init__(self, text, exists_for):
        self._text = str(text)
        self._exists_for = exists_for

    def __truediv__(self, part):
        return _FakePath(self._text + '\\' + str(part), self._exists_for)

    def is_file(self):
        return str(self._text).rsplit('\\', 1)[-1] in self._exists_for

    def __str__(self):
        return self._text


class SiteProfileSandboxTests(unittest.TestCase):
    def test_self_check_warns_when_wsb_cli_missing(self):
        edition = {'EditionID': 'Professional', 'ProductName': 'Windows 10 Pro',
                   'DisplayVersion': '22H2', 'CurrentBuildNumber': '19045'}
        with mock.patch.object(site_profile, '_registry_edition', return_value=edition), \
                mock.patch.object(site_profile, 'Path',
                                  lambda value: _FakePath(value, {'WindowsSandbox.exe'})):
            item = site_profile._probe_windows_sandbox()
        self.assertEqual(item['status'], 'warn')
        self.assertIn('wsb.exe', item['detail'])
        self.assertFalse(item['data']['sandbox_available'])

    def test_self_check_ok_when_both_exist(self):
        edition = {'EditionID': 'Professional', 'ProductName': 'Windows 10 Pro',
                   'DisplayVersion': '25H2', 'CurrentBuildNumber': '26200'}
        with mock.patch.object(site_profile, '_registry_edition', return_value=edition), \
                mock.patch.object(site_profile, 'Path',
                                  lambda value: _FakePath(value, {'WindowsSandbox.exe', 'wsb.exe'})):
            item = site_profile._probe_windows_sandbox()
        self.assertEqual(item['status'], 'ok')
        self.assertTrue(item['data']['sandbox_available'])


if __name__ == '__main__':
    unittest.main()
