"""微信版的安全边界：**静态**检查"我们不做的事"没有被写进代码。

为什么静态检查有价值：这些能力（标签群发、朋友圈互动、语音通话、拍一拍、@所有人）
在库里有、我们**刻意不调用**，因为每一条都对应一种封号风险或社交事故
（见 封号风险与功能取舍.md 与 契约第 5 节）。动态测试只能覆盖"跑到的分支"，
而这类回归往往以"某天顺手加了一行"的形式出现 —— 所以直接盯源码文本。

这里的判定一律用**调用形态**（形如 `.SendToLabel(`），不用裸名字：
`wechat_bridge.py` 的模块文档里就把这些名字作为"不做的事"列出来过，
裸名字断言会把注释也算进去，等于自己制造假失败。
"""

import re
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "src" / "wechat_bridge.py"
APP = ROOT / "src" / "wechat_app.py"

# 禁止出现的能力（微信侧有封号风险或平台不存在）
FORBIDDEN_CALLS = {
    "标签群发（SendToLabel）": r"\.\s*SendToLabel\s*\(",
    "标签转发（ForwardToLabel）": r"\.\s*ForwardToLabel\s*\(",
    "批量加标签成员（AddLabelMembers）": r"\.\s*AddLabelMembers\s*\(",
    "批量移除标签成员（RemoveLabelMembers）": r"\.\s*RemoveLabelMembers\s*\(",
    "消息转发（ForwardMessage）": r"\.\s*ForwardMessage\s*\(",
    "语音转发（ForwardVoiceMessage）": r"\.\s*ForwardVoiceMessage\s*\(",
    "切到朋友圈（SwitchToMoments）": r"\.\s*SwitchToMoments\s*\(",
    "朋友圈监听（MomentObserver）": r"\.\s*MomentObserver\s*\(",
    "语音通话（VoiceCall）": r"\.\s*VoiceCall\s*\(",
    "撤回消息（RecallLastMessage）": r"\.\s*RecallLastMessage\s*\(",
    "撤回守卫（RecallGuard）": r"\.\s*RecallGuard\s*\(",
    "拍一拍（Poke）": r"\.\s*Poke\s*\(",
    "@所有人（AtAll）": r"\.\s*AtAll\s*\(",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class BridgeSourceTests(unittest.TestCase):
    """桥是唯一碰微信的地方：这里必须干净。"""

    def setUp(self):
        self.source = read(BRIDGE)

    def test_no_forbidden_wechat_capability_is_called(self):
        for label, pattern in FORBIDDEN_CALLS.items():
            with self.subTest(capability=label):
                match = re.search(pattern, self.source)
                self.assertIsNone(
                    match,
                    f"微信桥里出现了不该调用的能力：{label}"
                    f"（第 {self.source[:match.start()].count(chr(10)) + 1} 行附近）"
                    if match else "")

    def test_forbidden_names_may_appear_in_comments_only(self):
        # 反向确认：这些名字确实在文件里（作为"不做的事"被写进文档），
        # 说明上面的断言真的是靠"调用形态"区分注释与代码的。
        self.assertIn("SendToLabel", self.source)

    def test_bridge_does_not_import_wechat_libraries_at_module_level(self):
        # 顶层 import 会让"没装微信库"的机器连控制台都起不来；
        # 库只能在真正连接时按需导入。
        for line in self.source.splitlines():
            if not line.startswith(("import ", "from ")):
                continue
            for library in ("wechatauto", "wxauto4", "wxauto"):
                with self.subTest(library=library, line=line):
                    self.assertNotIn(library, line)


class AppSourceTests(unittest.TestCase):
    def test_app_does_not_call_forbidden_capabilities(self):
        source = read(APP)
        for label, pattern in FORBIDDEN_CALLS.items():
            with self.subTest(capability=label):
                self.assertIsNone(re.search(pattern, source), f"运行时里出现了：{label}")

    def test_transport_exposes_only_the_contracted_actions(self):
        # 契约第 5 节动作表：插件只能通过 call() 调用表里列出的这些动作（含 video_frames）
        source = read(ROOT / "src" / "wechat_transports.py")
        block = source[source.index("async def call"):]
        allowed = set(re.findall(r'action == "([a-z_]+)"', block))
        self.assertEqual(allowed, {
            "send_to", "download_media", "video_frames",
            "get_member_names", "get_self_info"})


class ProactiveDisabledTests(unittest.TestCase):
    def test_default_config_keeps_proactive_off(self):
        from src.config_manager import DEFAULT_CONFIG

        self.assertIs(DEFAULT_CONFIG["wechat"]["proactive"]["enabled"], False)

    def test_can_send_proactive_is_false_when_config_disabled(self):
        from src.wechat_app import WeChatRuntime

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            config = root / "config.wechat.yaml"
            config.write_text(yaml.safe_dump({
                "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "m"},
                "wechat": {"backend": "mock", "proactive": {"enabled": False}},
                "wechat_only": {"backend": "mock"},
            }), encoding="utf-8")
            runtime = WeChatRuntime(root=root, config_path=config, db_path=root / "m.sqlite3")
            try:
                self.assertIs(runtime.config.get("wechat.proactive.enabled"), False)
                for kind, target in (("group", "12345678@chatroom"), ("private", "wxid_a")):
                    with self.subTest(kind=kind):
                        self.assertFalse(runtime.can_send_proactive(kind, target))
                # 入队也一并拒绝（不能绕过 can_send_proactive 写进 outbox）
                self.assertFalse(runtime.queue_outgoing("k", "group", "12345678@chatroom", "在吗"))
                self.assertEqual(
                    runtime.db.execute("SELECT COUNT(*) FROM chat_outbox").fetchone()[0], 0)
            finally:
                runtime.db.close()

    def test_default_config_proactive_can_be_turned_on_explicitly(self):
        from src.wechat_app import WeChatRuntime

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            config = root / "config.wechat.yaml"
            config.write_text(yaml.safe_dump({
                "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "m"},
                "wechat": {"backend": "mock", "proactive": {"enabled": True},
                           "target_groups": ["12345678@chatroom"]},
                "wechat_only": {"backend": "mock", "groups": ["12345678@chatroom"]},
            }), encoding="utf-8")
            runtime = WeChatRuntime(root=root, config_path=config, db_path=root / "m.sqlite3")
            try:
                # 这是默认关闭的开关"打开后确实生效"的对照：只有 running + 白名单内才放行
                self.assertTrue(runtime.config.get("wechat.proactive.enabled"))
                runtime.running = True
                self.assertTrue(runtime.can_send_proactive("group", "12345678@chatroom"))
                self.assertFalse(runtime.can_send_proactive("group", "99887766@chatroom"))
            finally:
                runtime.db.close()


class PluginBaselineTests(unittest.TestCase):
    """有风险的插件必须**不在**插件集里（不是"加载了但关着"）。"""

    REMOVED = ("proactive_chat", "scheduled_announcer", "silence_ping", "icebreaker",
               "affinity_proactive", "poke_counter", "probability_reply", "face_decode",
               "face_names", "sticker_cap", "pixiv_illust", "forward_summary",
               "speech_to_text_qq", "typing_indicator", "share_card")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        from src.plugins.manager import PluginManager

        self.manager = PluginManager(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_risky_plugins_are_absent(self):
        for plugin_id in self.REMOVED:
            with self.subTest(plugin_id=plugin_id):
                self.assertNotIn(plugin_id, self.manager._plugins)

    def test_wechat_plugins_are_present(self):
        for plugin_id in ("conversation_memory", "chat_controls", "image_understanding",
                          "sticker_memory", "reply_dedupe", "mention_gate",
                          "speech_to_text_wechat", "image_ocr"):
            with self.subTest(plugin_id=plugin_id):
                self.assertIn(plugin_id, self.manager._plugins)

    def test_risky_plugin_ids_are_not_in_the_module_list(self):
        from src.plugins.manager import PLUGIN_MODULES

        for plugin_id in self.REMOVED:
            with self.subTest(plugin_id=plugin_id):
                self.assertNotIn(plugin_id, PLUGIN_MODULES)


class ChatControlsTests(unittest.TestCase):
    def test_interject_percent_defaults_to_zero(self):
        from src.plugins.chat_controls import ChatControls

        self.assertEqual(ChatControls.META.default_settings["interjectPercent"], 0)
        self.assertEqual(ChatControls.META.config_schema["interjectPercent"].min, 0)

    def test_file_exists_after_load(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            from src.plugins.manager import PluginManager

            manager = PluginManager(Path(tmp))
            settings = manager.get_settings("chat_controls")
            self.assertEqual(settings["interjectPercent"], 0)


if __name__ == "__main__":
    unittest.main()
