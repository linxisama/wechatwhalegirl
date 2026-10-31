"""1.3.60：管理员权限判断与一键提权重启（不真的弹 UAC，全部替身）。"""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src import elevation

ROOT = Path(__file__).resolve().parent.parent


class IsElevatedTests(unittest.TestCase):
    def test_admin_process_is_reported_as_elevated(self):
        with patch.object(elevation.ctypes.windll.shell32, 'IsUserAnAdmin', return_value=1):
            self.assertTrue(elevation.is_elevated())

    def test_non_admin_process_is_reported_as_not_elevated(self):
        with patch.object(elevation.ctypes.windll.shell32, 'IsUserAnAdmin', return_value=0):
            self.assertFalse(elevation.is_elevated())

    def test_broken_check_counts_as_not_elevated(self):
        with patch.object(elevation.ctypes.windll.shell32, 'IsUserAnAdmin', side_effect=OSError('nope')):
            self.assertFalse(elevation.is_elevated())


class LaunchCommandTests(unittest.TestCase):
    def test_frozen_build_restarts_the_exe_itself(self):
        exe, params = elevation.launch_command(['WhaleGirlWeChat.exe', '--x'], executable='C:/app/WhaleGirlWeChat.exe', frozen=True)
        self.assertEqual(exe, 'C:/app/WhaleGirlWeChat.exe')
        self.assertEqual(params, '')

    def test_source_run_restarts_python_with_absolute_script(self):
        exe, params = elevation.launch_command(['wechat_gui.py'], executable='C:/py/python.exe', frozen=False)
        self.assertEqual(exe, 'C:/py/python.exe')
        self.assertEqual(params, '"' + os.path.join(os.getcwd(), 'wechat_gui.py') + '"')

    def test_arguments_with_spaces_stay_quoted(self):
        _, params = elevation.launch_command(['wechat_gui.py', 'a b'], executable='py.exe', frozen=False)
        self.assertTrue(params.endswith('"a b"'), params)


class RelaunchTests(unittest.TestCase):
    def test_already_elevated_does_not_launch_anything(self):
        calls = []
        with patch.object(elevation, 'is_elevated', return_value=True):
            ok, message = elevation.relaunch_elevated(result_hook=lambda *a: calls.append(a) or 33)
        self.assertFalse(ok)
        self.assertIn('不需要重启', message)
        self.assertEqual(calls, [])

    def test_success_reports_that_the_prompt_is_waiting(self):
        seen = {}

        def hook(hwnd, verb, exe, params, cwd, show):
            seen.update(verb=verb, exe=exe)
            return 33

        with patch.object(elevation, 'is_elevated', return_value=False):
            ok, message = elevation.relaunch_elevated(executable='C:/app/x.exe', frozen=True, result_hook=hook)
        self.assertTrue(ok)
        self.assertEqual(seen['verb'], 'runas')
        self.assertIn('点「是」', message)

    def test_failed_shell_execute_points_to_the_manual_way(self):
        with patch.object(elevation, 'is_elevated', return_value=False):
            ok, message = elevation.relaunch_elevated(executable='C:/app/x.exe', frozen=True,
                                                     result_hook=lambda *a: 5)
        self.assertFalse(ok)
        self.assertIn('返回码 5', message)
        self.assertIn('以管理员身份运行', message)

    def test_exception_never_escapes(self):
        def hook(*_a):
            raise OSError('shell32 missing')

        with patch.object(elevation, 'is_elevated', return_value=False):
            ok, message = elevation.relaunch_elevated(executable='C:/app/x.exe', frozen=True, result_hook=hook)
        self.assertFalse(ok)
        self.assertIn('提权失败', message)


class GuiWiringTests(unittest.TestCase):
    def test_console_exposes_the_elevate_button_and_startup_notice(self):
        source = (ROOT / 'wechat_gui.py').read_text('utf-8')
        self.assertIn('以管理员身份重启', source)
        self.assertIn('def _relaunch_elevated', source)
        self.assertIn('def _warn_if_not_elevated', source)
        self.assertIn('QTimer.singleShot(1500, self._warn_if_not_elevated)', source)


if __name__ == '__main__':
    unittest.main()
