# -*- coding: utf-8 -*-
r"""1.3.78：wsb.exe 解析器不能再被改写过的环境变量带偏。

现场：用「干净数据目录」模拟新电脑时把 LOCALAPPDATA 指到临时目录，1.3.77 的自检
立刻报「System32 与 WindowsApps 里都没有 wsb.exe 命令行」，可命令行明明能用。
真实世界里同样会发生：控制台被计划任务/服务拉起时 LOCALAPPDATA 指向 systemprofile。
所以解析器要问 Known Folder API，并在 PATH 里兜底。
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


class WsbResolverTests(unittest.TestCase):
    def _layout(self, root):
        system_root = Path(root) / 'Windows'
        (system_root / 'System32').mkdir(parents=True, exist_ok=True)
        (system_root / 'System32' / 'WindowsSandbox.exe').write_bytes(b'MZ')
        return system_root

    def _alias(self, local_appdata):
        """在给定的 LocalAppData 目录下造出 wsb.exe 的应用执行别名。"""
        directory = Path(local_appdata) / 'Microsoft' / 'WindowsApps'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'wsb.exe').write_bytes(b'MZ')
        return directory

    def test_known_folder_wins_over_redirected_env(self):
        """环境变量被指到空目录时，仍要按真实 LocalAppData 找到别名。"""
        with tempfile.TemporaryDirectory() as tmp:
            system_root = self._layout(tmp)
            real_local = Path(tmp) / 'real-local'
            self._alias(real_local)
            with mock.patch.dict(os.environ, {'SystemRoot': str(system_root),
                                              'LOCALAPPDATA': str(Path(tmp) / 'empty'),
                                              'PATH': ''}, clear=False), \
                    mock.patch.object(site_profile, '_known_local_appdata',
                                      return_value=str(real_local)):
                path, exists, source = site_profile._resolve_wsb_exe()
                self.assertTrue(exists)
                self.assertEqual(source, 'windowsapps')
                self.assertEqual(Path(path).name, 'wsb.exe')
                item = site_profile._probe_windows_sandbox()
        self.assertEqual(item['status'], 'ok')
        self.assertTrue(item['data']['sandbox_available'])
        self.assertEqual(item['data']['wsb_exe_source'], 'windowsapps')

    def test_path_fallback_finds_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            system_root = self._layout(tmp)
            path_dir = self._alias(Path(tmp) / 'path-alias')
            path_env = str(path_dir)
            with mock.patch.dict(os.environ, {'SystemRoot': str(system_root),
                                              'LOCALAPPDATA': str(Path(tmp) / 'empty')},
                                 clear=False), \
                    mock.patch.object(site_profile, '_known_local_appdata',
                                      return_value=str(Path(tmp) / 'empty')):
                _path, exists, source = site_profile._resolve_wsb_exe(path_env=path_env)
        self.assertTrue(exists)
        self.assertEqual(source, 'path')

    def test_nothing_anywhere_reports_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            system_root = self._layout(tmp)
            with mock.patch.dict(os.environ, {'SystemRoot': str(system_root),
                                              'LOCALAPPDATA': str(Path(tmp) / 'empty'),
                                              'PATH': ''}, clear=False), \
                    mock.patch.object(site_profile, '_known_local_appdata',
                                      return_value=str(Path(tmp) / 'empty')):
                _path, exists, source = site_profile._resolve_wsb_exe()
        self.assertFalse(exists)
        self.assertEqual(source, 'none')

    def test_system32_still_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            system_root = self._layout(tmp)
            (system_root / 'System32' / 'wsb.exe').write_bytes(b'MZ')
            alias_dir = self._alias(Path(tmp) / 'alias')
            with mock.patch.dict(os.environ, {'SystemRoot': str(system_root),
                                              'PATH': ''}, clear=False):
                _path, exists, source = site_profile._resolve_wsb_exe(
                    str(system_root), local_appdata=str(alias_dir.parent.parent))
        self.assertTrue(exists)
        self.assertEqual(source, 'system32')

    def test_known_folder_falls_back_to_env_when_api_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(Path(tmp) / 'from-env')},
                                 clear=False), \
                    mock.patch.object(site_profile.ctypes, 'WinDLL',
                                      side_effect=OSError('no shell32')):
                value = site_profile._known_local_appdata()
        self.assertEqual(Path(value), Path(tmp) / 'from-env')


if __name__ == '__main__':
    unittest.main()
