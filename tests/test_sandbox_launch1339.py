"""1.3.39 由控制台打开 Windows 沙盒：幂等、超时与配置内容都用替身验证。"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import sandbox_manager as manager

SANDBOX = 'd4254f07-930a-4602-ac01-96d6adffbf4a'
EXE = Path(r'C:\Windows\System32\WindowsSandbox.exe')


class LaunchSandboxTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def launch(self, listing):
        """list_sandboxes 与 Popen 全部替身：任何用例都不会启动真沙盒。"""
        with patch.object(manager, 'list_sandboxes', side_effect=listing) as listed, \
             patch.object(manager, 'find_sandbox_executable', return_value=EXE), \
             patch.object(manager.subprocess, 'Popen') as popen, \
             patch('src.sandbox_manager.time.sleep'):
            result = manager.launch_sandbox(self.root)
        return result, popen, listed

    def test_no_instance_launches_and_returns_new_id(self):
        result, popen, _listed = self.launch([[], [{'id': SANDBOX}]])
        self.assertTrue(result['success'])
        self.assertEqual(result['sandbox_id'], SANDBOX)
        self.assertTrue(result['created'])
        popen.assert_called_once()
        command = popen.call_args[0][0]
        self.assertEqual(command[0], str(EXE))
        self.assertTrue(command[1].endswith('.wsb'))

    def test_existing_instance_is_reused_without_launching(self):
        result, popen, listed = self.launch([[{'id': SANDBOX}]])
        self.assertTrue(result['success'])
        self.assertEqual(result['sandbox_id'], SANDBOX)
        self.assertFalse(result['created'])
        popen.assert_not_called()
        self.assertEqual(listed.call_count, 1)

    def test_timeout_reports_reason(self):
        with patch.object(manager, 'LAUNCH_TIMEOUT_SECONDS', 0.0), \
             patch.object(manager, 'LAUNCH_POLL_SECONDS', 0.0):
            with self.assertRaises(manager.SandboxLaunchError) as caught:
                self.launch([[], []])
        self.assertEqual(caught.exception.reason, 'launch_timeout')
        self.assertIn('秒内没有出现新的 Windows 沙盒', str(caught.exception))

    def test_missing_executable_reports_reason(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        with patch.object(manager, 'list_sandboxes', return_value=[]), \
             patch.dict(os.environ, {'SystemRoot': empty}), \
             patch.object(manager.subprocess, 'Popen') as popen:
            with self.assertRaises(manager.SandboxLaunchError) as caught:
                manager.launch_sandbox(self.root)
        self.assertEqual(caught.exception.reason, 'sandbox_exe_missing')
        self.assertIn('Windows Sandbox', str(caught.exception))
        popen.assert_not_called()

    def test_config_text_has_required_items(self):
        result, popen, _listed = self.launch([[], [{'id': SANDBOX}]])
        self.assertTrue(result['created'])
        config = Path(popen.call_args[0][0][1])
        text = config.read_text('utf-8')
        for item in ('<MemoryInMB>4096</MemoryInMB>', '<vGPU>Disable</vGPU>',
                     '<Networking>Enable</Networking>',
                     '<ClipboardRedirection>Disable</ClipboardRedirection>',
                     '<AudioInput>Disable</AudioInput>', '<VideoInput>Disable</VideoInput>',
                     '<PrinterRedirection>Disable</PrinterRedirection>'):
            self.assertIn(item, text)
        self.assertEqual(config.parent, Path(self.root).resolve() / 'data' / 'sandbox_launch')


if __name__ == '__main__':
    unittest.main()
