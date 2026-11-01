# -*- coding: utf-8 -*-
r"""1.3.77：沙盒自检与沙盒面板对 wsb.exe 的判定必须一致。

背景：较新的 Windows 把 wsb.exe 装成
%LOCALAPPDATA%\Microsoft\WindowsApps\wsb.exe 这个应用执行别名，
而 src/site_profile.py 原来只认 %SystemRoot%\System32\wsb.exe，
于是「兼容性自检」报「没有 wsb.exe 命令行」，可命令行其实能用——
两个面板口径不一致，正是用户问题日志里抱怨的那类误报。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import site_profile


PRO_EDITION = {
    'EditionID': 'Professional',
    'ProductName': 'Windows 11 Pro',
    'DisplayVersion': '25H2',
    'CurrentBuildNumber': '26200',
}


class SandboxPathTests(unittest.TestCase):
    def _env(self, root, *, system32_wsb=False, windowsapps_wsb=False, sandbox_exe=True):
        system_root = Path(root) / 'Windows'
        (system_root / 'System32').mkdir(parents=True, exist_ok=True)
        if system32_wsb:
            (system_root / 'System32' / 'wsb.exe').write_bytes(b'MZ')
        if sandbox_exe:
            (system_root / 'System32' / 'WindowsSandbox.exe').write_bytes(b'MZ')
        local = Path(root) / 'LocalAppData'
        if windowsapps_wsb:
            alias_dir = local / 'Microsoft' / 'WindowsApps'
            alias_dir.mkdir(parents=True, exist_ok=True)
            (alias_dir / 'wsb.exe').write_bytes(b'MZ')
        return {'SystemRoot': str(system_root), 'LOCALAPPDATA': str(local)}

    def _probe(self, env, edition=None):
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(site_profile, '_registry_edition',
                                  return_value=dict(edition or PRO_EDITION)):
            return site_profile._probe_windows_sandbox()

    def test_system32_wsb_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = self._probe(self._env(tmp, system32_wsb=True))
        self.assertEqual(item['status'], 'ok')
        self.assertTrue(item['data']['sandbox_available'])
        self.assertEqual(item['data']['wsb_exe_source'], 'system32')

    def test_windowsapps_alias_counts_as_cli(self):
        """回归：只有 WindowsApps 别名时以前被判 warn，现在必须 ok。"""
        with tempfile.TemporaryDirectory() as tmp:
            item = self._probe(self._env(tmp, windowsapps_wsb=True))
        self.assertEqual(item['status'], 'ok')
        self.assertTrue(item['data']['wsb_exe_exists'])
        self.assertTrue(item['data']['sandbox_available'])
        self.assertEqual(item['data']['wsb_exe_source'], 'windowsapps')
        self.assertIn('WindowsApps', item['data']['wsb_exe'])

    def test_no_wsb_anywhere_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = self._probe(self._env(tmp))
        self.assertEqual(item['status'], 'warn')
        self.assertFalse(item['data']['wsb_exe_exists'])
        self.assertFalse(item['data']['sandbox_available'])
        self.assertEqual(item['data']['wsb_exe_source'], 'none')
        self.assertIn('wsb.exe', item['detail'])

    def test_missing_sandbox_exe_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = self._probe(self._env(tmp, system32_wsb=True, sandbox_exe=False))
        self.assertEqual(item['status'], 'warn')
        self.assertFalse(item['data']['sandbox_exe_exists'])

    def test_home_edition_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = self._probe(self._env(tmp, system32_wsb=True),
                               edition={'EditionID': 'Core', 'ProductName': 'Windows 11 Home',
                                        'DisplayVersion': '25H2', 'CurrentBuildNumber': '26200'})
        self.assertEqual(item['status'], 'warn')
        self.assertFalse(item['data']['sandbox_available'])

    def test_resolver_prefers_system32(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp, system32_wsb=True, windowsapps_wsb=True)
            path, exists, source = site_profile._resolve_wsb_exe(env['SystemRoot'])
        self.assertTrue(exists)
        self.assertEqual(source, 'system32')
        self.assertEqual(Path(path).name, 'wsb.exe')


if __name__ == '__main__':
    unittest.main()
