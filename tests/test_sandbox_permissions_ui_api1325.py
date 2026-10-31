"""目录修复候选必须显式执行，读取状态不能启动、启用或发送。"""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_sandbox_api1315 as api_fixtures
import test_sandbox_panel1315 as panel_fixtures
from src.gui.sandbox_panel import _diagnostic_text

SANDBOX = api_fixtures.SANDBOX
CHANNEL = api_fixtures.CHANNEL

class DirectoryRepairPanelTests(unittest.TestCase):
    setUpClass = classmethod(panel_fixtures.SandboxPanelTests.setUpClass.__func__)
    setUp = panel_fixtures.SandboxPanelTests.setUp
    tearDown = panel_fixtures.SandboxPanelTests.tearDown
    deliver = panel_fixtures.SandboxPanelTests.deliver

    def failed(self, state='repairable', **fields):
        self.deliver({'success': True, 'sandboxes': [{'id': SANDBOX}], 'pairs': [{
            'channel_id': CHANNEL, 'sandbox_id': SANDBOX, 'stage': 'failed',
            'recovery_state': state, 'agent_online': False,
            'initial_failed_operation': 'share_program', 'initial_hresult': '0x80070005',
            'recovery_failed_operation': 'share_program', 'recovery_hresult': '0x80070005',
            **fields}]})

    def test_directory_repair_is_explicit_and_does_not_activate_or_prepare(self):
        self.failed()
        self.assertEqual(self.dialog.recover_button.text(), '修复目录并恢复配对')
        self.assertTrue(self.dialog.recover_button.isEnabled())
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertFalse(self.dialog.activate_button.isEnabled())
        self.assertEqual(self.calls, [])
        self.assertIn('微信登录可保持', self.dialog.status.text())
        self.dialog.recover()
        self.assertEqual(self.calls[0][0][1:4], ('POST', 'sandbox/recover', {'id': SANDBOX}))
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.dialog.recover_button.isEnabled())

    def test_access_denied_is_not_reported_as_missing_wechat_login(self):
        self.failed()
        text = self.dialog.diagnostics.text()
        for required in ('访问被拒绝', '不表示微信没有登录', '保留原配对'):
            self.assertIn(required, text)
        self.assertEqual(self.calls, [])

    def test_partial_repair_remains_blocked_and_preserves_stage(self):
        self.failed('blocked', recovery_stage='repair_installing',
                    recovery_failed_operation='install_channel')
        self.assertFalse(self.dialog.recover_button.isEnabled())
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertIn('交接修复目录', self.dialog.diagnostics.text())
        self.dialog.recover()
        self.dialog.prepare()
        self.assertEqual(self.calls, [])

    def test_unavailable_global_guard_blocks_stale_repairable_candidate(self):
        self.deliver({'success': True, 'available': False,
            'message': '目录修复日志无法核验', 'sandboxes': [{'id': SANDBOX}],
            'pairs': [{'channel_id': CHANNEL, 'sandbox_id': SANDBOX,
                'stage': 'failed', 'recovery_state': 'repairable'}]})
        for button in (self.dialog.prepare_button, self.dialog.recover_button,
                       self.dialog.activate_button):
            self.assertFalse(button.isEnabled())
        self.assertTrue(self.dialog.refresh_button.isEnabled())
        self.assertIn('无法核验', self.dialog.status.text())
        self.dialog.prepare()
        self.dialog.recover()
        self.dialog.activate()
        self.assertEqual(self.calls, [])

class DirectoryRepairApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = api_fixtures.SandboxApiTests.asyncSetUp
    asyncTearDown = api_fixtures.SandboxApiTests.asyncTearDown

    async def test_recognized_directory_repair_status_survives_public_filter(self):
        self.pair_status['pairs'][0].update(stage='failed', recovery_state='repairable',
            recovery_reason='共享程序在首次通道映射前被拒绝，可显式修复目录并保留原配对',
            recovery_stage='repair_staging', recovery_failed_operation='stage_channel',
            archive_path='do-not-export', secret='do-not-export')
        response = await self.client.get('/api/sandbox/status')
        self.assertEqual(response.status_code, 200)
        pair = response.json()['pairs'][0]
        self.assertEqual(pair['recovery_state'], 'repairable')
        self.assertEqual(pair['recovery_stage'], 'repair_staging')
        self.assertEqual(pair['recovery_failed_operation'], 'stage_channel')
        self.assertNotIn('do-not-export', response.text)
        self.assertFalse(self.bot.running)
        self.assertEqual(self.switches, [])
        self.prepare_mock.assert_not_called()

if __name__ == '__main__':
    unittest.main()
