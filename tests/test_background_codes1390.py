"""1.3.90 后台通道的每个失败出口都必须带码，且码能在控制台查到中文。

裸 not_ready 在三张码表里都查不到，用户只会看到原始英文；send_failed 又会把
"根本没写入"说成"发送失败"。这两条是本文件要钉住的边界。
"""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src import send_history as sh
from src.wechat_background import inspect_background, send_background
from tests import test_desktop_140 as fixtures

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'src' / 'wechat_background.py'


def _literal_codes():
    """静态收集后台模块能落下的码：_stop/unavailable 的第二参 + _current_bound_window 的第四元。"""
    tree = ast.parse(SOURCE.read_text('utf-8'))
    codes, dynamic = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_current_bound_window':
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Tuple):
                    last = sub.value.elts[-1] if sub.value.elts else None
                    # 成功出口返回的码是空串，不算失败码。
                    if isinstance(last, ast.Constant) and isinstance(last.value, str) and last.value:
                        codes.add(last.value)
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ('_stop', 'unavailable'):
            continue
        argument = node.args[1] if len(node.args) > 1 else None
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            codes.add(argument.value)
        else:
            dynamic.append(node.func.id)
    return codes, dynamic


class ExitCodeTests(unittest.TestCase):
    def test_every_failure_exit_passes_a_literal_code(self):
        codes, dynamic = _literal_codes()
        # 唯一允许的动态码是 _current_bound_window 算出来的那个，它自己的出口也全是字面量。
        self.assertTrue(dynamic)
        self.assertTrue(set(dynamic) <= {'_stop', 'unavailable'})
        self.assertNotIn('not_ready', codes)
        for code in sorted(codes):
            self.assertIn(code, sh.CODE_TEXT, code)

    def test_every_documented_code_has_chinese_text(self):
        for code in sorted(_literal_codes()[0]):
            self.assertNotEqual(sh.describe(code), code, code)
            self.assertNotEqual(sh.hint_for(code), '', code)


class FixtureExitCodeTests(unittest.TestCase):
    def setUp(self):
        fixtures.BackgroundTests.setUp(self)

    def assert_coded(self, info, expected):
        code = info['waiting_code']
        self.assertEqual(code, expected)
        self.assertNotEqual(sh.describe(code), code, code)
        self.assertFalse(info['ready'])

    def test_invalid_target_is_not_reported_as_send_failed(self):
        self.job['who'] = '  '
        ok, _ = send_background(self.job, self.phase, self.access, self.desktop)
        self.assertFalse(ok)
        self.assert_coded(self.job['_diagnostic'], 'invalid_target')
        self.value.SetValue.assert_not_called()

    def test_locked_desktop_is_reported_before_any_input(self):
        info, controls = inspect_background('test', '测试群', self.access, lambda: (False, '桌面已锁屏'))
        self.assertIsNone(controls)
        self.assert_coded(info, 'desktop_unavailable')

    def test_unknown_submit_method_is_reported(self):
        info, controls = inspect_background('test', '测试群', self.access, self.desktop, 'paste')
        self.assertIsNone(controls)
        self.assert_coded(info, 'unsupported_submit')

    def test_readonly_input_is_reported_as_input_unavailable(self):
        self.value.IsReadOnly = True
        info, controls = inspect_background('test', '测试群', self.access, self.desktop)
        self.assertIsNone(controls)
        self.assert_coded(info, 'input_unavailable')

    def test_other_chat_is_reported_as_chat_not_open(self):
        self.field.Name = '其他群'
        info, controls = inspect_background('test', '测试群', self.access, self.desktop)
        self.assertIsNone(controls)
        self.assert_coded(info, 'chat_not_open')

    def test_existing_draft_is_reported_as_draft(self):
        self.value.Value = '用户草稿'
        info, controls = inspect_background('test', '测试群', self.access, self.desktop)
        self.assertIsNone(controls)
        self.assert_coded(info, 'draft')

    def test_desktop_lost_right_before_write_keeps_its_own_code(self):
        calls = []
        def desktop():
            calls.append(1)
            # 前三次是只读复查（进检查 + 两次出口复核），第四次是写入前的门。
            return (len(calls) <= 3, '桌面已锁屏')
        ok, reason = send_background(self.job, self.phase, self.access, desktop)
        self.assertFalse(ok)
        self.assertIn('未写入正文', reason)
        self.assert_coded(self.job['_diagnostic'], 'desktop_unavailable')
        self.value.SetValue.assert_not_called()

    def test_chat_switched_right_before_write_keeps_its_own_code(self):
        def phase(stage):
            if stage == 'input':
                self.field.Name = '另外的聊天'
        ok, reason = send_background(self.job, phase, self.access, self.desktop)
        self.assertFalse(ok)
        self.assertIn('未写入正文', reason)
        self.assert_coded(self.job['_diagnostic'], 'chat_not_open')
        self.value.SetValue.assert_not_called()

    def test_button_read_error_is_not_blamed_on_extra_dialogs(self):
        # 树里有读错误和"开着别的发送框"是两回事：只报"唯一确认"会让用户永远修不好。
        broken = SimpleNamespace(ClassName='mmui::Broken', AutomationId='', Name='',
            ControlTypeName='PaneControl', GetChildren=Mock(side_effect=RuntimeError('boom')))
        self.root.GetChildren = lambda: [self.field, broken, self.button]
        info, controls = inspect_background('test', '测试群', self.access, self.desktop)
        self.assertIsNone(controls)
        self.assert_coded(info, 'accessibility_unavailable')
        self.assertEqual(info['button_search']['errors'], 1)
        self.assertEqual(info['button_search']['buttons'], 1)
        self.assertIn('读取错误', info['reason'])

    def test_button_search_budget_exhaustion_says_so(self):
        deep = SimpleNamespace(ClassName='mmui::Deep', AutomationId='', Name='',
            ControlTypeName='PaneControl', GetChildren=lambda: [])
        node = deep
        for _ in range(42):
            child = node
            node = SimpleNamespace(ClassName='mmui::Deep', AutomationId='', Name='',
                ControlTypeName='PaneControl', GetChildren=lambda child=child: [child])
        self.root.GetChildren = lambda: [self.field, self.button, node]
        info, controls = inspect_background('test', '测试群', self.access, self.desktop)
        self.assertIsNone(controls)
        self.assert_coded(info, 'accessibility_unavailable')
        self.assertEqual(info['button_search']['search_status'], 'budget')
        self.assertIn('未遍历完整棵控件树', info['reason'])

    def test_extra_send_dialogs_keep_the_original_wording(self):
        self.root.GetChildren = lambda: [self.field, self.button, self.button]
        info, controls = inspect_background('test', '测试群', self.access, self.desktop)
        self.assertIsNone(controls)
        self.assert_coded(info, 'accessibility_unavailable')
        self.assertEqual(info['button_search']['buttons'], 2)
        self.assertIn('无法唯一确认发送按钮', info['reason'])

    def test_successful_send_leaves_no_failure_code(self):
        ok, _ = send_background(self.job, self.phase, self.access, self.desktop)
        self.assertTrue(ok)
        self.assertNotIn('waiting_code', self.job['_diagnostic'])


if __name__ == '__main__':
    unittest.main()
