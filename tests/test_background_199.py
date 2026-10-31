"""只在假控件验证后台不回退、不重复提交；不操作真实微信。"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src.wechat_app import send_channel_hint
from src.wechat_send_worker import execute
from tests import test_desktop_140 as fixtures


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        fixtures.BackgroundTests.setUp(self)

    def send(self):
        from src.wechat_background import send_background
        return send_background(self.job, self.phase, self.access, self.desktop)

    def test_slow_clear_is_confirmed_without_second_invoke(self):
        clock = [0.0]
        self.invoke.Invoke.side_effect = None
        def wait(seconds):
            clock[0] += seconds
            if clock[0] >= .8:
                self.value.Value = ''
        with patch('src.wechat_background.time.monotonic', side_effect=lambda: clock[0]), \
             patch('src.wechat_background.time.sleep', side_effect=wait):
            self.assertTrue(self.send()[0])
        self.assertGreaterEqual(clock[0], .8)
        self.invoke.Invoke.assert_called_once()
        self.value.SetValue.assert_called_once()
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'confirmed')

    def test_changed_chat_cannot_confirm_another_empty_input(self):
        def invoke():
            self.field.Name = '另一个群'
            self.value.Value = ''
        self.invoke.Invoke.side_effect = invoke
        self.assertFalse(self.send()[0])
        self.assertEqual(self.job['_diagnostic']['waiting_code'], 'submission_uncertain')
        self.assertFalse(self.job['_diagnostic']['ready'])
        self.invoke.Invoke.assert_called_once()

    def test_invoke_exception_is_uncertain_and_preserves_draft(self):
        self.invoke.Invoke.side_effect = RuntimeError('provider disconnected')
        self.assertIn('不重发', self.send()[1])
        self.assertEqual(self.value.Value, self.job['text'])
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'uncertain')
        self.invoke.Invoke.assert_called_once()


class DispatchTests(unittest.TestCase):
    def test_compat_background_failure_or_exception_never_enters_foreground(self):
        for result in ((False, '后台已提交但未确认清空'), (False, '回读不一致'), RuntimeError('COM failed')):
            with self.subTest(result=str(result)), \
                 patch('src.interactive_desktop.interactive_desktop_status', return_value=(True, '')), \
                 patch('wechatauto.uia_driver.WeChatUIA', return_value=Mock()), \
                 patch('src.wechat_send_worker.enable_send_compat', return_value=True), \
                 patch('src.wechat_background.inspect_background', return_value=({'ready': True}, (1,))), \
                 patch('src.wechat_background.send_background') as quiet, \
                 patch('src.wechat_send_worker.send_text', side_effect=AssertionError('前台退路')), \
                 patch('src.wechat_send_worker._restore_foreground', side_effect=AssertionError('前台还原')):
                if isinstance(result, Exception):
                    quiet.side_effect = result
                else:
                    quiet.return_value = result
                ok, _ = execute({'action': 'text', 'who': '测试群', 'text': '你好', 'send_compat': True})
                self.assertFalse(ok)
                quiet.assert_called_once()

    def test_strict_branch_never_loads_driver_or_uses_compat_even_when_both_flags_set(self):
        with patch('wechatauto.uia_driver.WeChatUIA', side_effect=AssertionError('driver loaded')), \
             patch('src.wechat_send_worker.enable_send_compat', side_effect=AssertionError('compat activated')), \
             patch('src.wechat_background.send_background', return_value=(False, 'waiting')) as quiet:
            self.assertEqual(execute({'action': 'text', 'background_send': True, 'send_compat': True}),
                             (False, 'waiting'))
            quiet.assert_called_once()

    def test_strict_hint_does_not_instruct_incompatible_toggle(self):
        text = send_channel_hint({'background_send': True, 'waiting_code': 'accessibility_unavailable',
                                  'reason': '没有输入控件'}, False)
        self.assertNotIn('勾选「兼容模式」', text)
        self.assertIn('不支持', text)


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_schedule_and_binding_have_opaque_background_under_transparent_application_style(self):
        import wechat_gui as gui
        from PyQt6.QtCore import Qt
        from src.gui.agent_binding import AgentBindingDialog
        with patch.object(gui, 'call_api'):
            dialogs = [gui.ScheduleDialog(), AgentBindingDialog(Mock())]
        before = self.app.styleSheet()
        try:
            self.app.setStyleSheet(gui.STYLESHEET)
            for dialog in dialogs:
                dialog.show()
                self.app.processEvents()
                self.assertTrue(dialog.testAttribute(Qt.WidgetAttribute.WA_StyledBackground))
                color = dialog.grab().toImage().pixelColor(4, 4)
                self.assertGreater(color.lightness(), 150)
                self.assertEqual(color.alpha(), 255)
        finally:
            self.app.setStyleSheet(before)
            for dialog in dialogs:
                dialog.close()

