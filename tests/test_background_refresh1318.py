"""后台写入可能替换 Qt 控件；只用假控件验证重新定位及单次提交。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.wechat_background import inspect_background, send_background
from tests import test_desktop_140 as fixtures


class BackgroundRefreshTests(unittest.TestCase):
    def setUp(self):
        fixtures.BackgroundTests.setUp(self)

    def send(self):
        return send_background(self.job, self.phase, self.access, self.desktop)

    def replacement(self, invoke):
        return SimpleNamespace(ClassName='mmui::XButton', AutomationId='', Name='发送',
            IsEnabled=True, ControlTypeName='ButtonControl', GetChildren=lambda: [],
            GetInvokePattern=lambda: invoke)

    def replace_after_write(self, button, *, field=None):
        def write(text):
            self.value.Value = text
            self.root.GetChildren = lambda: [field or self.field, button]
        self.value.SetValue.side_effect = write

    def test_replaced_button_uses_the_current_pattern_once_and_never_the_old_pattern(self):
        current = SimpleNamespace(Invoke=Mock(side_effect=lambda: setattr(self.value, 'Value', '')))
        self.replace_after_write(self.replacement(current))
        self.assertTrue(self.send()[0])
        self.invoke.Invoke.assert_not_called()
        current.Invoke.assert_called_once()
        self.value.SetValue.assert_called_once()
        self.assertFalse(self.job['_diagnostic']['delivery_verified'])

    def test_rebuilt_input_does_not_read_stale_field_or_value_after_single_write(self):
        stale = [False]
        current_value = SimpleNamespace(Value='', IsReadOnly=False)
        current_field = SimpleNamespace(ClassName='mmui::ChatInputField', AutomationId='',
            Name='测试群', ControlTypeName='EditControl', GetChildren=lambda: [],
            GetValuePattern=lambda: current_value)
        current_invoke = SimpleNamespace(Invoke=Mock(side_effect=lambda: setattr(current_value, 'Value', '')))
        current_button = self.replacement(current_invoke)
        class OldValue:
            IsReadOnly = False
            @property
            def Value(self):
                if stale[0]:
                    raise RuntimeError('stale value pattern')
                return ''
        old_value = OldValue()
        def write(text):
            current_value.Value = text
            self.root.GetChildren = lambda: [current_field, current_button]
            stale[0] = True
        old_value.SetValue = Mock(side_effect=write)
        class OldField:
            ClassName = 'mmui::ChatInputField'
            AutomationId = ''
            ControlTypeName = 'EditControl'
            @property
            def Name(self):
                if stale[0]:
                    raise RuntimeError('stale field')
                return '测试群'
            def GetChildren(self):
                return []
            def GetValuePattern(self):
                return old_value
        self.field = OldField()
        self.assertTrue(self.send()[0])
        old_value.SetValue.assert_called_once_with(self.job['text'])
        self.invoke.Invoke.assert_not_called()
        current_invoke.Invoke.assert_called_once()
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'confirmed')

    def test_replaced_button_without_invoke_keeps_draft_without_any_submission(self):
        self.replace_after_write(self.replacement(None))
        self.assertFalse(self.send()[0])
        self.invoke.Invoke.assert_not_called()
        self.assertEqual(self.value.Value, self.job['text'])
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'not_submitted')
        self.assertNotIn('send_key', [call.args[0] for call in self.phase.call_args_list])

    def test_account_window_change_after_write_cannot_invoke_the_previous_window(self):
        rows = [self.row]
        self.access.candidates = lambda: list(rows)
        def write(text):
            self.value.Value = text
            rows[:] = [dict(self.row, hwnd=101, pid=201)]
        self.value.SetValue.side_effect = write
        self.assertFalse(self.send()[0])
        self.invoke.Invoke.assert_not_called()
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'not_submitted')

    def test_replaced_input_with_other_title_or_body_cannot_invoke(self):
        for wrong in ('title', 'body'):
            with self.subTest(wrong=wrong):
                self.setUp()
                value = SimpleNamespace(Value=self.job['text'] if wrong == 'title' else '另一份草稿',
                                        IsReadOnly=False)
                field = SimpleNamespace(ClassName='mmui::ChatInputField', AutomationId='',
                    Name='另一个会话' if wrong == 'title' else '测试群', ControlTypeName='EditControl',
                    GetChildren=lambda: [], GetValuePattern=lambda: value)
                self.replace_after_write(self.button, field=field)
                self.assertFalse(self.send()[0])
                self.invoke.Invoke.assert_not_called()
                self.assertEqual(self.job['_diagnostic']['submission_state'], 'not_submitted')

    def test_false_invoke_return_with_real_clear_is_not_mistaken_for_not_submitted(self):
        def invoke():
            self.value.Value = ''
            return False
        self.invoke.Invoke.side_effect = invoke
        self.assertTrue(self.send()[0])
        self.assertIs(self.job['_diagnostic'].get('invoke_return'), False)
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'confirmed')
        self.assertFalse(self.job['_diagnostic']['delivery_verified'])
        self.invoke.Invoke.assert_called_once()

    def test_desktop_lost_during_fresh_control_lookup_never_invokes(self):
        ready = [True]
        self.desktop = lambda: (ready[0], '桌面不可用')
        reads = [0]
        def root(hwnd):
            reads[0] += 1
            # 首读和新增的写前复核保持可用；此用例仍专门模拟写后复核丢桌面。
            if reads[0] > 2:
                ready[0] = False
            return self.root
        self.access.control = root
        self.assertFalse(self.send()[0])
        self.assertEqual(self.value.Value, self.job['text'])
        self.invoke.Invoke.assert_not_called()
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'not_submitted')

    def test_false_return_without_clear_is_uncertain_without_claiming_submitted(self):
        self.invoke.Invoke.side_effect = lambda: False
        clock = [0.0]
        with patch('src.wechat_background.time.monotonic', side_effect=lambda: clock[0]), \
             patch('src.wechat_background.time.sleep', side_effect=lambda amount: clock.__setitem__(0, clock[0]+amount)):
            ok, reason = self.send()
        self.assertFalse(ok)
        self.assertIn('已尝试', reason)
        self.assertNotIn('已提交', reason)
        self.assertIs(self.job['_diagnostic'].get('invoke_return'), False)
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'uncertain')
        self.invoke.Invoke.assert_called_once()

    def test_invoke_exception_keeps_only_type_and_preserves_uncertain_state(self):
        self.invoke.Invoke.side_effect = RuntimeError('PRIVATE-CHAT-AND-TOKEN')
        ok, reason = self.send()
        self.assertFalse(ok)
        self.assertEqual(self.job['_diagnostic'].get('invoke_exception_type'), 'RuntimeError')
        self.assertNotIn('PRIVATE', str(self.job['_diagnostic']) + reason)
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'uncertain')
        self.invoke.Invoke.assert_called_once()

    def test_confirmation_read_error_is_not_mislabeled_as_an_invoke_error(self):
        confirmation = [False]
        body = ['']
        class Value:
            IsReadOnly = False
            @property
            def Value(self):
                if confirmation[0]:
                    raise RuntimeError('PRIVATE-CONFIRMATION-CONTENT')
                return body[0]
        value = Value()
        value.SetValue = Mock(side_effect=lambda text: body.__setitem__(0, text))
        self.field.GetValuePattern = lambda: value
        self.invoke.Invoke.side_effect = lambda: confirmation.__setitem__(0, True) or True
        ok, reason = self.send()
        self.assertFalse(ok)
        diagnostic = self.job['_diagnostic']
        self.assertIs(diagnostic.get('submit_call_returned'), True)
        self.assertEqual(diagnostic.get('confirmation_exception_type'), 'RuntimeError')
        self.assertNotIn('invoke_exception_type', diagnostic)
        self.assertNotIn('PRIVATE', str(diagnostic) + reason)
        self.assertEqual(diagnostic['submission_state'], 'uncertain')
        self.invoke.Invoke.assert_called_once()

    def test_readonly_preflight_explicitly_does_not_verify_submit(self):
        info, _ = inspect_background('test', '测试群', self.access, self.desktop)
        self.assertTrue(info['ready'])
        self.assertIs(info.get('submit_verified'), False)
        self.assertIn('未验证提交有效', info['reason'])
        self.value.SetValue.assert_not_called()
        self.invoke.Invoke.assert_not_called()
