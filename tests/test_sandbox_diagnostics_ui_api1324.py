"""配对首错与续接状态必须可见；全部使用假 API，不操作真实沙盒。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_sandbox_api1315 as api_fixtures
import test_sandbox_panel1315 as panel_fixtures

from PyQt6.QtCore import Qt

SANDBOX = api_fixtures.SANDBOX
CHANNEL = api_fixtures.CHANNEL


class DiagnosticApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = api_fixtures.SandboxApiTests.asyncSetUp
    asyncTearDown = api_fixtures.SandboxApiTests.asyncTearDown

    async def test_status_exposes_finite_recovery_evidence_without_raw_records(self):
        pair = self.pair_status['pairs'][0]
        pair.update(stage='failed', recovery_state='resumable', recovery_stage='failed',
            recovery_failed_operation='process_probe', recovery_hresult='0x800711C7',
            recovery_returncode=-1,
            recovery_reason='原映射已确认，可在重新核验后继续恢复；不会重复映射通道',
            initial_failed_operation='launch', initial_hresult='0x80070005', initial_returncode=5,
            recovery={'secret': 'do-not-export'}, original_session={'secret': 'do-not-export'},
            command='do-not-export', failure={'text': r'C:\private\do-not-export'})
        result = await self.client.get('/api/sandbox/status')
        self.assertEqual(result.status_code, 200)
        actual = result.json()['pairs'][0]
        for key in ('recovery_state', 'recovery_stage', 'recovery_failed_operation',
                    'recovery_hresult', 'recovery_returncode', 'recovery_reason',
                    'initial_failed_operation', 'initial_hresult', 'initial_returncode'):
            self.assertEqual(actual[key], pair[key])
        self.assertNotIn('do-not-export', result.text)
        self.assertNotIn('private', result.text)
        self.assertEqual(self.switches, [])
        self.prepare_mock.assert_not_called()
        self.assertFalse(self.bot.running)

    async def test_unregistered_strings_nested_values_and_bool_returncodes_are_omitted(self):
        pair = self.pair_status['pairs'][0]
        pair.update(recovery_state='do-not-export', recovery_stage='do-not-export',
            recovery_failed_operation='do-not-export', recovery_hresult='0x800711C7 do-not-export',
            recovery_returncode=True, recovery_reason=r'C:\private\do-not-export',
            initial_failed_operation={'secret': 'do-not-export'},
            initial_hresult={'value': 'do-not-export'}, initial_returncode=True)
        result = await self.client.get('/api/sandbox/status')
        self.assertEqual(result.status_code, 200)
        actual = result.json()['pairs'][0]
        self.assertEqual({key: item for key, item in actual.items()
                          if key.startswith(('recovery_', 'initial_'))}, {'recovery_state': 'blocked'})
        self.assertNotIn('do-not-export', result.text)

    async def test_malformed_state_filtered_by_api_does_not_become_first_recovery_in_ui(self):
        from src.gui.sandbox_panel import _recovery_allowed
        for invalid in (None, True, {'value': 'do-not-export'}, ['do-not-export'], 'do-not-export'):
            with self.subTest(invalid=invalid):
                self.pair_status['pairs'][0].update(stage='failed', recovery_state=invalid)
                result = await self.client.get('/api/sandbox/status')
                self.assertEqual(result.status_code, 200)
                pair = result.json()['pairs'][0]
                self.assertEqual(pair['recovery_state'], 'blocked')
                self.assertFalse(_recovery_allowed(pair))
                self.assertNotIn('do-not-export', result.text)
        self.assertFalse(self.bot.running)
        self.assertEqual(self.switches, [])


class DiagnosticPanelTests(unittest.TestCase):
    setUpClass = classmethod(panel_fixtures.SandboxPanelTests.setUpClass.__func__)
    setUp = panel_fixtures.SandboxPanelTests.setUp
    tearDown = panel_fixtures.SandboxPanelTests.tearDown
    deliver = panel_fixtures.SandboxPanelTests.deliver

    def failed(self, **kwargs):
        pair = {'channel_id': CHANNEL, 'sandbox_id': SANDBOX, 'stage': 'failed',
                'agent_online': False, 'reason': '上次配对未完成', **kwargs}
        self.deliver({'success': True, 'sandboxes': [{'id': SANDBOX}], 'pairs': [pair]})

    def test_initial_and_recovery_failure_remain_visible_after_operation_and_refresh(self):
        fields = dict(recovery_state='resumable', recovery_stage='failed',
            recovery_failed_operation='process_probe', recovery_hresult='0x800711C7',
            recovery_returncode=-1, initial_failed_operation='launch',
            initial_hresult='0x80070005', initial_returncode=5)
        self.failed(**fields)
        text = self.dialog.diagnostics.text()
        for part in ('首次准备失败', '启动代理', '只读进程核验', '0x800711C7', '0x80070005', '-1'):
            self.assertIn(part, text)
        self.assertEqual(self.dialog.diagnostics.textFormat(), Qt.TextFormat.PlainText)
        self.dialog.recover()
        self.deliver({'success': False, 'message': '续接核验未完成'})
        self.failed(**fields)
        self.assertEqual(self.dialog.status.text(), '续接核验未完成')
        self.assertIn('0x80070005', self.dialog.diagnostics.text())
        self.assertIn('0x800711C7', self.dialog.diagnostics.text())

    def test_confirmed_mapping_exposes_explicit_continue_without_auto_request(self):
        self.failed(recovery_state='resumable', recovery_stage='failed',
                    recovery_failed_operation='process_probe')
        self.assertEqual(self.dialog.recover_button.text(), '继续恢复配对')
        self.assertTrue(self.dialog.recover_button.isEnabled())
        self.assertFalse(self.dialog.activate_button.isEnabled())
        self.assertEqual(self.calls, [])
        self.dialog.recover()
        self.assertEqual(self.calls[0][0][1:4], ('POST', 'sandbox/recover', {'id': SANDBOX}))
        self.assertFalse(self.dialog.recover_button.isEnabled())
        self.assertFalse(any('start' in row[0][2] or 'activate' in row[0][2] for row in self.calls))

    def test_unknown_mapping_blocks_recovery_and_prepare_without_hiding_first_failure(self):
        self.failed(recovery_state='blocked', recovery_stage='failed',
                    recovery_failed_operation='share_channel', initial_failed_operation='launch',
                    initial_hresult='0x800711C7')
        self.assertFalse(self.dialog.recover_button.isEnabled())
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertIn('保留', self.dialog.diagnostics.text())
        self.assertIn('0x800711C7', self.dialog.diagnostics.text())
        self.dialog.recover()
        self.dialog.prepare()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.dialog.activate_button.isEnabled())

    def test_malformed_diagnostics_never_render_raw_paths_or_enable_continue(self):
        self.failed(recovery_state='do-not-export', recovery_stage='do-not-export',
                    recovery_failed_operation='do-not-export',
                    recovery_hresult='0x800711C7 do-not-export', recovery_returncode={'secret': 'do-not-export'},
                    recovery_reason=r'C:\private\do-not-export')
        self.assertNotIn('do-not-export', self.dialog.diagnostics.text())
        self.assertNotIn('private', self.dialog.diagnostics.text())
        self.assertFalse(self.dialog.recover_button.isEnabled())

    def test_refresh_failure_clears_old_diagnostic_candidate(self):
        self.failed(recovery_state='resumable', recovery_failed_operation='process_probe')
        self.dialog.refresh(explicit=True)
        self.deliver({'success': False, 'message': '读取失败'})
        self.assertEqual(self.dialog.diagnostics.text(), '')
        self.assertFalse(self.dialog.recover_button.isEnabled())
        self.assertIn('暂时无法核验', self.dialog.pair_info.text())
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.dialog.prepare()
        self.assertEqual(self.calls, [])

    def test_no_existing_login_session_has_specific_guidance_without_security_workarounds(self):
        self.failed(recovery_state='blocked', recovery_stage='failed',
                    recovery_failed_operation='preflight', recovery_hresult='0x80070520')
        text = self.dialog.diagnostics.text()
        self.assertIn('0x80070520', text)
        self.assertIn('沙盒交互会话尚未就绪', text)
        self.assertIn('连接并打开沙盒桌面', text)
        self.assertNotIn('System', text)
        self.assertNotIn('安全设置', text)
        self.assertNotIn('微信登录失败', text)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
