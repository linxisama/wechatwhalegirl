"""1.3.110 接线：人格记忆与心情真正接进运行路径。

1.3.109 交付了 persona_memory 与 persona_mood 两个纯逻辑模块，但它们是孤岛：
除了自己内部互相调用，没有任何外部调用方。纯逻辑加测试只能算半成品——
用户要的是「日记每存储一周会删除一次」和「4 小时不聊天随机重置」真的发生。

这一批把它们接进两条真实路径：
  - agent_diary 写完日记 → 同时写人格独立目录；tick 顺手做周清/月清
  - conversation_memory 的 on_pre_ai → 注入当前心情（只对主人）

运行：./.venv-wechat/Scripts/python.exe -m unittest tests.test_persona_wiring_1110
"""
from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from src.agent_diary import DiaryWriter
from src.persona_memory import list_daily, monthly_keywords, write_daily


class FakeDB:
    def execute(self, *args, **kwargs):
        return types.SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])

    def commit(self):
        pass


class FakeBot:
    def __init__(self, root):
        self.root = root
        self.db = FakeDB()
        self.style = types.SimpleNamespace(active_name="鲸鱼娘")
        self.config = {}
        self.ai_lock = types.SimpleNamespace(locked=lambda: True)

    def note(self, *args):
        pass


class DiaryRollupWiringTests(unittest.TestCase):
    """agent_diary.rollup 真的会压缩，且每天最多一次。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bot = FakeBot(self.root)
        self.writer = DiaryWriter(self.bot)

    def tearDown(self):
        self._tmp.cleanup()

    def _fill(self, count=8):
        for index in range(count):
            write_daily(self.root, "鲸鱼娘", "2026-11-%02d" % (index + 1),
                        "第%d天" % (index + 1), ["主人"])

    def test_rollup_with_no_days_reports_not_rolled(self):
        result = self.writer.rollup()
        self.assertTrue(result["ok"])
        self.assertFalse(result["weekly"]["rolled"])

    def test_rollup_compresses_after_seven_days(self):
        self._fill(8)
        result = self.writer.rollup()
        self.assertTrue(result["ok"])
        self.assertTrue(result["weekly"]["rolled"])
        self.assertEqual(list_daily(self.root, "鲸鱼娘"), ["2026-11-08"])

    def test_rollup_runs_at_most_once_per_day(self):
        self._fill(8)
        first = self.writer.rollup()
        self.assertTrue(first["ok"])
        second = self.writer.rollup()
        self.assertFalse(second["ok"])
        self.assertIn("已经跑过", second["reason"])

    def test_marker_written(self):
        self._fill(8)
        self.writer.rollup()
        marker = self.root / "data" / "personas" / ".last-rollup"
        self.assertTrue(marker.exists())
        self.assertIn("鲸鱼娘", marker.read_text("utf-8"))

    def test_rollup_returns_failure_instead_of_raising(self):
        """压缩失败不该影响日记本身。"""
        writer = DiaryWriter(self.bot)
        with unittest.mock.patch("src.agent_diary.rollup_week",
                                 side_effect=OSError("磁盘满了")):
            result = writer.rollup()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "OSError")

    def test_keywords_readable_after_month_rollup(self):
        self._fill(8)
        self.writer.rollup()
        self.assertIn("主人", self.writer.keywords())

    def test_keywords_empty_when_nothing_written(self):
        self.assertEqual(self.writer.keywords(), [])


class MoodWiringTests(unittest.TestCase):
    """conversation_memory.on_pre_ai 真的会注入心情，且只对主人。"""

    def _plugin(self):
        from src.plugins.conversation_memory import ConversationMemory
        from src.plugins.manager import PluginManager
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        manager = PluginManager(Path(self._tmp.name))
        return ConversationMemory(manager)

    def test_mood_hint_present_for_owner(self):
        plugin = self._plugin()
        hint = plugin._mood_hint({"_is_owner": True}, types.SimpleNamespace())
        self.assertIn("心情", hint)

    def test_mood_hint_empty_for_others(self):
        plugin = self._plugin()
        self.assertEqual(plugin._mood_hint({"_is_owner": False}, types.SimpleNamespace()), "")

    def test_mood_hint_empty_when_unknown(self):
        """拿不准时保守：宁可不注入，也不要把角色状态发给陌生人。"""
        plugin = self._plugin()
        self.assertEqual(plugin._mood_hint({}, types.SimpleNamespace()), "")

    def test_mood_hint_respects_owner_only_off(self):
        plugin = self._plugin()
        plugin._manager.update_settings("conversation_memory", {"moodOwnerOnly": False})
        self.assertIn("心情", plugin._mood_hint({}, types.SimpleNamespace()))

    def test_mood_hint_respects_enabled_off(self):
        plugin = self._plugin()
        plugin._manager.update_settings("conversation_memory", {"moodEnabled": False})
        self.assertEqual(plugin._mood_hint({"_is_owner": True}, types.SimpleNamespace()), "")

    def test_mood_state_persisted_after_reset(self):
        plugin = self._plugin()
        plugin.save_state("mood.json", {"updated": 0, "mood": 5, "energy": 5, "stress": 95})
        plugin._mood_hint({"_is_owner": True}, types.SimpleNamespace())
        stored = plugin.load_state("mood.json", {})
        self.assertGreaterEqual(float(stored.get("mood", 0)), 40.0,
                                "重置后必须立刻落盘，否则会一直显示 idle_reset")

    def test_mood_hint_never_raises(self):
        plugin = self._plugin()
        with unittest.mock.patch("src.plugins.conversation_memory.resolve_mood",
                                 side_effect=RuntimeError("坏了")):
            self.assertEqual(plugin._mood_hint({"_is_owner": True}, types.SimpleNamespace()), "")


class SchemaTests(unittest.TestCase):
    def test_mood_fields_in_schema_and_defaults(self):
        from src.plugins.conversation_memory import ConversationMemory
        schema = ConversationMemory.META.config_schema
        defaults = ConversationMemory.META.default_settings
        for field in ("moodEnabled", "moodOwnerOnly", "moodIdleHours"):
            with self.subTest(field=field):
                self.assertIn(field, schema)
                self.assertIn(field, defaults)
        self.assertEqual(defaults["moodIdleHours"], 4)


import unittest.mock  # noqa: E402  (放在末尾避免与上面的 unittest 导入混淆)


if __name__ == "__main__":
    unittest.main()