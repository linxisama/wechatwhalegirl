import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.wechat_send_worker import literal_keys, send_text, optimize_lookups
from src.wechat_sender import IsolatedWeChatSender


class TextInputTests(unittest.TestCase):
    def driver(self):
        uia = Mock()
        uia.ensure_window.return_value = True
        uia.current_chat.return_value = "测试群"
        control = uia._chat_input.return_value
        control.HasKeyboardFocus = True
        uia._read_edit_value.side_effect = ["", "", "你好", ""]
        uia._compare_norm.side_effect = lambda text: "".join(text.split())
        return uia, control

    def test_literal_text_cannot_execute_keyboard_shortcuts(self):
        self.assertEqual(literal_keys("{Ctrl}a\r\n你好"), "{{}Ctrl{}}a{Shift}{Enter}你好")

    def test_focus_input_readback_then_only_one_send_key(self):
        uia, control = self.driver()
        with patch("src.wechat_send_worker.time.sleep"):
            ok, _ = send_text(uia, "测试群", "你好")
        self.assertTrue(ok)
        control.SetFocus.assert_called_once()
        self.assertEqual(control.SendKeys.call_args_list[0].args[0], "你好")
        self.assertEqual(sum(call.args[0] == "{Enter}" for call in control.SendKeys.call_args_list), 1)

    def test_empty_input_after_typing_never_presses_send(self):
        uia, control = self.driver()
        uia._read_edit_value.side_effect = None
        uia._read_edit_value.return_value = ""
        ok, detail = send_text(uia, "测试群", "你好")
        self.assertFalse(ok)
        self.assertIn("没有完整写入", detail)
        self.assertNotIn("{Enter}", [call.args[0] for call in control.SendKeys.call_args_list])

    def test_missing_focus_never_types_or_sends(self):
        uia, control = self.driver()
        control.HasKeyboardFocus = False
        ok, detail = send_text(uia, "测试群", "你好")
        self.assertFalse(ok)
        self.assertIn("键盘焦点", detail)
        control.SendKeys.assert_not_called()

    def test_wrong_chat_never_types_or_sends(self):
        uia, control = self.driver()
        uia.current_chat.return_value = "其他群"
        uia.open_chat.return_value = True
        ok, _ = send_text(uia, "测试群", "你好")
        self.assertFalse(ok)
        control.SendKeys.assert_not_called()

    def test_enter_without_empty_input_is_uncertain_and_not_retried(self):
        uia, control = self.driver()
        uia._read_edit_value.side_effect = ["", "", "你好", "你好\n"]
        with patch("src.wechat_send_worker.time.sleep"):
            ok, detail = send_text(uia, "测试群", "你好")
        self.assertFalse(ok)
        self.assertIn("结果待核实", detail)
        self.assertEqual(sum(call.args[0] == "{Enter}" for call in control.SendKeys.call_args_list), 1)

    def test_writable_value_pattern_avoids_ime_typing_and_only_sends_once(self):
        uia, control = self.driver()
        pattern = control.GetValuePattern.return_value
        pattern.IsReadOnly = False
        with patch("src.wechat_send_worker.time.sleep"):
            ok, _ = send_text(uia, "测试群", "你好")
        self.assertTrue(ok)
        pattern.SetValue.assert_called_once_with("你好")
        self.assertEqual([call.args[0] for call in control.SendKeys.call_args_list], ["{Enter}"])


class SenderIsolationTests(unittest.TestCase):
    def test_real_child_entry_point_offline_and_cleans_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            sender = IsolatedWeChatSender(Path(folder))
            result = sender._run({"action": "self-test"})
            self.assertTrue(result, str(result))
            self.assertEqual(list((Path(folder) / "send_jobs").iterdir()), [])

    def test_native_crash_is_a_failure_instead_of_parent_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            process = Mock()
            process.wait.return_value = 0xC0000005
            process.poll.return_value = 0xC0000005
            with patch("src.wechat_sender.subprocess.Popen", return_value=process) as popen:
                result = IsolatedWeChatSender(Path(folder)).SendMsg("你好", "测试群")
            self.assertFalse(result)
            self.assertIn("C0000005", str(result))
            self.assertEqual(popen.call_count, 1)


class ScopedLookupTests(unittest.TestCase):
    def test_collapsed_search_returns_without_old_edit_timeouts(self):
        uia = Mock()
        root = Mock(ClassName="mmui::MainWindow", ControlTypeName="WindowControl",
                    AutomationId="", Name="微信")
        button = Mock(ClassName="mmui::XButton", ControlTypeName="ButtonControl",
                      AutomationId="", Name="搜索")
        root.GetChildren.return_value = [button]
        button.GetChildren.return_value = []
        uia._win = root
        optimize_lookups(uia)
        self.assertIsNone(uia._search_box_present(root))
        root.EditControl.assert_not_called()

    def test_chat_input_is_found_under_new_nested_structure(self):
        uia = Mock()
        root = Mock(ClassName="mmui::MainWindow", AutomationId="")
        control = Mock(ClassName="mmui::ChatInputField", AutomationId="chat_input_field")
        root.GetChildren.return_value = [control]
        control.GetChildren.return_value = []
        uia._win = root
        optimize_lookups(uia)
        self.assertIs(uia._chat_input(), control)
