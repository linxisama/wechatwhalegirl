"""候选 Hook 设置窗口：假异步 HTTP，不连接微信或网络。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel
from src.gui.hook_panel import HookDialog


APP = None
SETTINGS = {'api_url': 'http://127.0.0.1:30001', 'account': 'wxid_test_owner',
            'account_path': 'C:/test/account', 'target': 'filehelper'}


class HookPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        self.dialog = HookDialog(lambda *a, **k: self.calls.append((a, k)))

    def tearDown(self):
        self.dialog.close()

    def deliver(self, value):
        self.calls.pop(0)[1]['on_done'](value)

    def load(self):
        self.deliver({'success': True, 'settings': SETTINGS, 'environment': 'local',
                      'status': {'ready': False, 'reason': '候选通道默认停用'}})

    def test_initial_load_is_read_only_and_does_not_enable(self):
        self.assertEqual(self.calls[0][0][1:3], ('GET', 'wechat/hook'))
        self.load()
        self.assertEqual(self.dialog.collect(), SETTINGS)
        self.assertFalse(self.dialog.activate_button.isEnabled())
        self.assertEqual(self.calls, [])

    def test_unverified_candidate_boundary_remains_visible(self):
        self.load()
        texts = '\n'.join(label.text() for label in self.dialog.findChildren(QLabel))
        self.assertIn('尚未通过正式通道的真机收发验收', texts)
        self.assertIn('现有原版 DLL 不能直接使用', texts)
        self.assertFalse(self.dialog.activate_button.isEnabled())

    def test_probe_and_activate_need_current_saved_settings_and_success(self):
        self.load()
        self.dialog.probe()
        self.assertEqual(self.calls[0][0][1:4], ('POST', 'wechat/hook/probe', SETTINGS))
        self.assertFalse(self.dialog.probe_button.isEnabled())
        self.deliver({'success': True, 'status': {'ready': True, 'reason': '核验通过'}})
        self.assertTrue(self.dialog.activate_button.isEnabled())
        self.dialog.activate()
        self.assertEqual(self.calls[0][0][1:4], ('POST', 'wechat/hook/activate', {}))
        self.deliver({'success': True, 'message': '已启用；机器人保持停止'})
        self.assertIn('保持停止', self.dialog.environment.text())
        self.assertFalse(any('/start' in row[0][2] for row in self.calls))

    def test_edit_invalidates_prior_probe_and_unsaved_probe_cannot_activate(self):
        self.load()
        self.dialog.target.setText('wxid_other')
        self.dialog.probe()
        self.deliver({'success': True, 'status': {'ready': True}})
        self.assertFalse(self.dialog.activate_button.isEnabled())
        self.dialog.activate()
        self.assertEqual(self.calls, [])
        self.assertIn('保存', self.dialog.status.text())

    def test_save_does_not_enable_without_another_probe(self):
        self.load()
        self.dialog.save()
        self.assertEqual(self.calls[0][0][2], 'wechat/hook/config')
        self.deliver({'success': True, 'message': '已保存停用配置', 'status': {'ready': True}})
        self.assertFalse(self.dialog.activate_button.isEnabled())

    def test_error_without_success_flag_does_not_mark_draft_saved(self):
        self.load()
        self.dialog.target.setText('wxid_unsaved')
        self.dialog.save()
        self.deliver({'detail': '配置未通过校验'})
        self.assertEqual(self.dialog._saved, SETTINGS)
        self.assertFalse(self.dialog.activate_button.isEnabled())

    def test_edit_while_initial_load_pending_preserves_new_draft(self):
        self.dialog.target.setText('wxid_new_target')
        self.load()
        self.assertEqual(self.dialog.target.text(), 'wxid_new_target')

    def test_late_probe_after_programmatic_edit_cannot_enable_new_settings(self):
        self.load()
        self.dialog.probe()
        self.dialog.target.setText('wxid_late_target')
        self.deliver({'success': True, 'status': {'ready': True}})
        self.assertFalse(self.dialog.activate_button.isEnabled())
        self.assertTrue(self.dialog.probe_button.isEnabled())

    def test_failure_diagnostic_is_plain_text_and_never_enables(self):
        self.load()
        self.dialog.probe()
        self.deliver({'success': False, 'status': {'ready': False,
            'reason': '<module> 全网无鉴权监听被拒绝', 'waiting_code': 'unsafe_listener'}})
        self.assertIn('<module>', self.dialog.status.text())
        self.assertEqual(self.dialog.status.textFormat(), Qt.TextFormat.PlainText)
        self.assertFalse(self.dialog.activate_button.isEnabled())

    def test_close_ignores_late_reply_and_busy_rejects_duplicate_calls(self):
        self.load()
        self.dialog.save()
        self.dialog.save()
        self.assertEqual(len(self.calls), 1)
        self.dialog.close()
        self.deliver({'success': True, 'message': '迟到响应'})
        self.assertNotEqual(self.dialog.status.text(), '迟到响应')

    def test_deactivate_never_submits_form_or_starts_robot(self):
        self.load()
        self.dialog.deactivate()
        self.assertEqual(self.calls[0][0][1:4], ('POST', 'wechat/hook/deactivate', {}))
        self.deliver({'success': True, 'message': '已切回本机'})
        self.assertIn('本机', self.dialog.environment.text())
