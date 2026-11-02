"""1.3.82：Windows 10 没有 wsb.exe 时，别再让用户去点「启用沙盒」再重启一次。"""
import unittest
from pathlib import Path
from unittest.mock import patch

from src import sandbox_provision as provision
from src.sandbox_provision_api import SandboxProvisionService


def probe(state, wsb_exe, edition='Windows 10 专业版'):
    return {'state': state, 'supported': True, 'edition': edition, 'feature': '',
            'wsb_exe': wsb_exe, 'needs_reboot': False, 'reason': state,
            'message': '功能状态：' + state, 'hint': '老提示', 'next_step': 'enable',
            'log_path': ''}


class StatusWordingTests(unittest.TestCase):
    def run_status(self, item):
        with patch.object(provision, 'detect', return_value=item), \
                patch.object(provision, '_resolve_log_dir', return_value=Path('.')), \
                patch.object(provision, '_read_record', return_value={}):
            return provision.status()

    def test_win10_without_wsb_points_at_local_mode(self):
        value = self.run_status(probe('disabled', False))
        self.assertEqual(value['next_step'], 'manual')
        self.assertIn('没有 wsb.exe', value['message'])
        self.assertIn('本机模式', value['message'])
        self.assertIn('WindowsSandbox.exe', value['message'])
        self.assertIn('wsb.exe', value['hint'])

    def test_machine_with_wsb_keeps_enable_step(self):
        value = self.run_status(probe('disabled', True))
        self.assertEqual(value['next_step'], 'enable')
        self.assertNotIn('本机模式', value['message'])
        self.assertEqual(value['hint'], '老提示')

    def test_enabled_machine_unchanged(self):
        value = self.run_status(probe('enabled', True))
        self.assertEqual(value['next_step'], 'create_channel')


class CreateChannelWordingTests(unittest.TestCase):
    def run_create(self, item):
        with patch.object(provision, 'status', return_value=item), \
                patch.object(provision, '_default_manager', return_value=object()):
            return provision.create_channel(None, manager=None)

    def test_without_wsb_reports_no_cli_not_needs_enable(self):
        item = {'state': 'disabled', 'wsb_exe': False, 'edition': 'Windows 10 专业版',
                'needs_reboot': False, 'message': '功能状态：disabled'}
        value = self.run_create(item)
        self.assertFalse(value['ok'])
        self.assertEqual(value['state'], 'no_cli')
        self.assertEqual(value['reason'], 'no_cli')
        self.assertIn('本机模式', value['message'])
        self.assertNotIn('请先启用', value['message'])

    def test_with_wsb_keeps_needs_enable(self):
        item = {'state': 'disabled', 'wsb_exe': True, 'edition': 'Windows 11 专业版',
                'needs_reboot': False, 'message': '功能状态：disabled'}
        value = self.run_create(item)
        self.assertEqual(value['state'], 'needs_enable')
        self.assertIn('请先启用 Windows 沙盒功能', value['message'])


class ApiVerdictWordingTests(unittest.TestCase):
    def verdict(self, item):
        service = SandboxProvisionService()
        with patch.object(provision, 'status', return_value=item):
            return service._probe_verdict('')

    def test_api_uses_status_message_and_step(self):
        item = {'state': 'disabled', 'needs_reboot': False, 'wsb_exe': False,
                'next_step': 'manual', 'message': '这台机器没有 wsb.exe 命令行，请直接用本机模式',
                'hint': ''}
        value = self.verdict(item)
        self.assertFalse(value['ok'])
        self.assertEqual(value['reason'], 'not_enabled')
        self.assertEqual(value['next_step'], 'manual')
        self.assertIn('本机模式', value['message'])

    def test_api_falls_back_to_old_text_when_status_is_silent(self):
        item = {'state': 'absent', 'needs_reboot': False, 'wsb_exe': True,
                'next_step': 'enable', 'message': '', 'hint': ''}
        value = self.verdict(item)
        self.assertEqual(value['next_step'], 'enable')
        self.assertIn('本机还没启用 Windows 沙盒', value['message'])


if __name__ == '__main__':
    unittest.main()
