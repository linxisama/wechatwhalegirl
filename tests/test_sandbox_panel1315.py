"""隔离后台管理界面使用假异步 API，不启动沙盒或微信。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from src.gui.sandbox_panel import SandboxDialog
from src.runtime_identity import APP_VERSION

APP = None
SANDBOX = '7e2838d2-db90-448c-aced-96ecab3b5f8e'
CHANNEL = 'a' * 32


class SandboxPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        def request(*args, **kwargs):
            self.calls.append((args, kwargs))
        self.dialog = SandboxDialog(request)
        self.dialog._poll.stop()

    def tearDown(self):
        self.dialog.close()

    def deliver(self, data, index=0):
        self.calls.pop(index)[1]['on_done'](data)

    def ready(self):
        self.deliver({'success': True, 'environment': 'local',
            'sandboxes': [{'id': SANDBOX}], 'pairs': [{'channel_id': CHANNEL,
                'short_id': CHANNEL[:12], 'sandbox_id': SANDBOX, 'stage': 'agent_ready',
                'agent_online': True, 'reason': '代理已准备，微信未连接'}]})

    def mismatch(self, running=False, preparing=False):
        self.deliver({'success': True, 'environment': 'sandbox', 'sandboxes': [{'id': SANDBOX}],
            'pairs': [{'channel_id': CHANNEL, 'short_id': CHANNEL[:12], 'sandbox_id': SANDBOX,
                'stage': 'version_mismatch', 'agent_online': True, 'wechat_online': False,
                'app_version': '1.3.34',
                'reason': '隔离代理在线，版本 1.3.34 与控制台 1.3.35 不一致；可原地升级代理，升级不会启动机器人'}],
            'running': running, 'preparing': preparing})

    def test_in_place_upgrade_reuses_prepare_and_states_it_never_starts_the_robot(self):
        self.mismatch()
        self.assertEqual(self.dialog.upgrade_button.text(), '原地升级代理到 ' + APP_VERSION)
        self.assertTrue(self.dialog.upgrade_button.isEnabled())
        self.assertIn('不会启动机器人', self.dialog.upgrade_hint.text())
        self.dialog.upgrade()
        args, kwargs = self.calls[0]
        self.assertEqual(args[1:4], ('POST', 'sandbox/prepare', {'id': SANDBOX}))
        self.assertEqual(kwargs['timeout'], 180)
        self.assertIn('不会启动机器人', self.dialog.status.text())
        self.deliver({'success': True, 'stage': 'agent_ready',
                      'message': '隔离代理已安全更新；尚未自动启动机器人'})
        self.assertIn('已安全更新', self.dialog.status.text())
        self.assertEqual(self.calls[0][0][1:4], ('GET', 'sandbox/status', None))
        self.deliver({'success': True, 'environment': 'sandbox', 'sandboxes': [{'id': SANDBOX}],
            'pairs': [{'channel_id': CHANNEL, 'sandbox_id': SANDBOX, 'stage': 'agent_ready',
                       'agent_online': True}], 'running': False, 'preparing': False})
        self.assertFalse(self.dialog.upgrade_button.isEnabled())
        self.assertEqual(self.dialog.upgrade_hint.text(), '')
        self.assertTrue(self.dialog.activate_button.isEnabled())

    def test_in_place_upgrade_is_disabled_with_reason_while_running_or_preparing(self):
        self.mismatch(running=True)
        self.assertFalse(self.dialog.upgrade_button.isEnabled())
        self.assertIn('机器人运行中', self.dialog.upgrade_button.toolTip())
        self.dialog.upgrade()
        self.assertEqual(self.calls, [])
        self.dialog.refresh(explicit=True)
        self.deliver({'success': True, 'environment': 'sandbox', 'sandboxes': [{'id': SANDBOX}],
            'pairs': [{'channel_id': CHANNEL, 'sandbox_id': SANDBOX,
                       'stage': 'version_mismatch', 'agent_online': True}], 'preparing': True})
        self.assertFalse(self.dialog.upgrade_button.isEnabled())
        self.assertIn('正在配对', self.dialog.upgrade_button.toolTip())
        self.dialog.upgrade()
        self.assertEqual(self.calls, [])

    def test_prepare_and_activate_use_explicit_selected_id_without_starting_robot(self):
        self.ready()
        self.dialog.prepare()
        args, kwargs = self.calls[0]
        self.assertEqual(args[1:4], ('POST', 'sandbox/prepare', {'id': SANDBOX}))
        self.assertEqual(kwargs['timeout'], 180)
        self.dialog.prepare()
        self.assertEqual(len(self.calls), 1)
        self.deliver({'success': True, 'message': '准备完成，不自动启动机器人'})
        self.deliver({'success': True, 'sandboxes': [{'id': SANDBOX}],
            'pairs': [{'channel_id': CHANNEL, 'sandbox_id': SANDBOX, 'stage': 'agent_ready', 'agent_online': True}]})
        self.dialog.activate()
        self.assertEqual(self.calls[0][0][1:4], ('POST', 'sandbox/activate', {'channel_id': CHANNEL}))
        self.assertFalse(any('start' in row[0][2] for row in self.calls))

    def test_poll_refresh_keeps_operation_failure_visible_and_plain(self):
        self.ready()
        self.dialog.activate()
        self.deliver({'success': False, 'message': '配对失败：<module> 不可用'})
        self.assertEqual(self.dialog.status.text(), '配对失败：<module> 不可用')
        self.deliver({'success': True, 'environment': 'local', 'sandboxes': [], 'pairs': []})
        self.assertEqual(self.dialog.status.text(), '配对失败：<module> 不可用')
        self.assertEqual(self.dialog.status.textFormat(), Qt.TextFormat.PlainText)

    def test_late_status_before_operation_cannot_replace_current_selection(self):
        self.ready()
        self.dialog.refresh()
        self.dialog.activate()
        self.deliver({'success': True, 'environment': 'sandbox', 'sandboxes': [], 'pairs': []})
        self.assertEqual(self.dialog.sandboxes.currentData(), SANDBOX)
        self.assertEqual(self.dialog.status.text(), '正在启用隔离通道…')
        self.deliver({'success': True, 'message': '已启用，尚未启动机器人'})
        self.assertIn('已启用', self.dialog.status.text())

    def test_missing_sandbox_has_no_prepare_or_activate_request(self):
        self.deliver({'success': True, 'sandboxes': [], 'pairs': []})
        self.dialog.prepare()
        self.dialog.activate()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertFalse(self.dialog.activate_button.isEnabled())

    def test_close_ignores_late_response(self):
        self.dialog.close()
        self.deliver({'success': True, 'environment': 'sandbox', 'sandboxes': [], 'pairs': []})
        self.assertEqual(self.dialog.environment.text(), '当前运行环境：正在读取…')
        self.assertFalse(self.dialog._poll.isActive())

    def test_prepared_but_offline_agent_cannot_be_activated(self):
        self.deliver({'success': True, 'sandboxes': [{'id': SANDBOX}],
            'pairs': [{'channel_id': CHANNEL, 'sandbox_id': SANDBOX,
                       'stage': 'prepared', 'agent_online': False}]})
        self.assertFalse(self.dialog.activate_button.isEnabled())
        self.dialog.activate()
        self.assertEqual(self.calls, [])
        self.assertIn('尚未就绪', self.dialog.status.text())

    def test_failed_refresh_invalidates_previous_activate_candidate(self):
        self.ready()
        self.assertTrue(self.dialog.activate_button.isEnabled())
        self.dialog.refresh(explicit=True)
        self.deliver({'success': False, 'message': '读取失败'})
        self.assertFalse(self.dialog.activate_button.isEnabled())
        self.dialog.activate()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
