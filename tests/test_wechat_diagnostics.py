import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.wechat_bridge import WeChatBridge
from src.wechat_listener import ListenerDatabase


class ListenerScopeTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.db.get_sessions.return_value = [
            {"username": "allowed@chatroom"}, {"username": "other@chatroom"},
            {"username": "wxid_friend"}]
        self.db.group_id_to_name.side_effect = lambda user: {
            "allowed@chatroom": "测试群", "other@chatroom": "无关群"}[user]

    def test_named_whitelist_excludes_other_groups_and_disabled_private_chats(self):
        scoped = ListenerDatabase(self.db, ["测试群"], False)
        self.assertEqual(scoped.get_sessions(), [{"username": "allowed@chatroom"}])
        scoped.get_sessions()
        self.assertEqual(self.db.group_id_to_name.call_count, 2)

    def test_private_chats_and_username_whitelist_are_preserved(self):
        scoped = ListenerDatabase(self.db, ["allowed@chatroom"], True)
        self.assertEqual([s["username"] for s in scoped.get_sessions()],
                         ["allowed@chatroom", "wxid_friend"])
        scoped.get_new_messages("allowed@chatroom", since_seq=42)
        self.db.get_new_messages.assert_called_once_with("allowed@chatroom", since_seq=42)

    def test_empty_whitelist_preserves_all_groups(self):
        self.assertEqual(ListenerDatabase(self.db, [], True).get_sessions(),
                         self.db.get_sessions.return_value)


class PlainMentionSendTests(unittest.TestCase):
    def test_wechat_backend_never_passes_mentions_to_ocr_panel(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = WeChatBridge(Path(folder), backend="wechatauto")
            bridge.backend = "wechatauto"
            bridge._wx = Mock()
            bridge._wx.SendMsg.return_value = True
            ok, _ = bridge._send_text_locked("测试群", "你好", ["小明", "小红"])
            self.assertTrue(ok)
            bridge._wx.SendMsg.assert_called_once_with(
                "@小明 @小红 你好", who="测试群", at=None, exact=True)

    def test_failed_send_is_not_retried_and_keeps_dependency_error(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = WeChatBridge(Path(folder), backend="wechatauto")
            bridge.backend = "wechatauto"
            bridge._wx = Mock()
            bridge._wx.SendMsg.return_value = False
            ok, detail = bridge._send_text_locked("测试群", "你好", [])
            self.assertFalse(ok)
            self.assertIn("False", detail)
            self.assertEqual(bridge._wx.SendMsg.call_count, 1)


class ModelDiagnosticsTests(unittest.TestCase):
    def test_reasoning_counters_are_kept_without_reasoning_text(self):
        from src.ai_engine import AIEngine
        engine = AIEngine(api_url="https://example.com/v1", api_key="test", model="test", api_style="openai")
        content, finish = engine._read_completion({
            "choices": [{"message": {"content": "回答", "reasoning_content": "不应写入诊断的思维链"},
                         "finish_reason": "stop"}],
            "usage": {"completion_tokens": 42, "completion_tokens_details": {"reasoning_tokens": 30}}})
        self.assertEqual(content, "回答")
        self.assertEqual(finish, "stop")
        self.assertTrue(engine.last_completion_diagnostics["has_reasoning"])
        self.assertEqual(engine.last_completion_diagnostics["reasoning_tokens"], 30)
        self.assertNotIn("不应写入诊断", str(engine.last_completion_diagnostics))
