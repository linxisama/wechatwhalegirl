"""人设隔离与「每个人格独立学习区域」的回归验证（1.3.92）。

钉住的缺陷（都是改动前实测过的）：
① 批量批准表达不限定人设，B 人设一点就把 A 人设的候选全批成 approved；
② overview 统计不按人设分组，B 人设看到的是 A 的数字；
③ clear 整段按会话删 learning_items，清一个会话会清光所有人设的学习数据；
④ _extract 的 200 条淘汰不限定人设，B 学一条就挤掉 A 一条；
⑤ 手动记忆进提示词没有任何上限。
view/recall/change 原本已走 _visibility，本文件把它们钉死，防止以后被改回去。
"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.local_learning import LocalLearning
from src.plugins.conversation_memory import (ConversationMemory, MANUAL_ITEMS, MANUAL_ITEM_CHARS,
                                             MANUAL_TOTAL_CHARS, manual_budget)
from src.style_manager import DEFAULT_PERSONA
from test_wechat_app_flow import RuntimeTestCase, GROUP


class _Plugin:
    """替身插件：只提供 _visibility 用到的 get() 与 enabled。"""

    def __init__(self, **values):
        self._values = values
        self.enabled = True

    def get(self, key, default=None):
        return self._values.get(key, default)


class _Conversations:
    """替身归档：overview/backfill 只用 rooms，recall 只用 search_terms。"""

    def __init__(self, rooms=None, records=None, terms=()):
        self._rooms = list(rooms or [])
        self._records = dict(records or {})
        self._terms = list(terms)

    def rooms(self, runtime):
        return self._rooms

    def records(self, runtime, kind, target, query="", limit=100, offset=0, since=0, before=None):
        rows = self._records.get(target, [])
        return rows, len(rows)

    def search_terms(self, text):
        return list(self._terms)


def runtime_of(persona, db=None, rooms=None, records=None, terms=(), plugins=None):
    value = SimpleNamespace(mode="wechat", account=lambda: "acc1",
                            style=SimpleNamespace(active_name=persona),
                            conversations=_Conversations(rooms, records, terms),
                            plugins=SimpleNamespace(_plugins=dict(plugins or {})),
                            bridge=SimpleNamespace(self_id="bot"), note=lambda text: None)
    if db is not None:
        value.db = db
    return value


def message_of(index, text, **extra):
    row = dict(id=str(index), kind="group", target=GROUP, sender_id="wxid_a",
               sender_name="小田", text=text, created=time.time() + index / 1000.0)
    row.update(extra)
    return row


class IsolationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = sqlite3.connect(Path(self.tmp.name) / "m.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.learn = LocalLearning(self.db)
        self._mid = 0

    def tearDown(self):
        try:
            self.db.close()
        finally:
            self.tmp.cleanup()

    def learn_fact(self, persona, text="我喜欢吃西瓜", count=2):
        """让某个人设从两条独立消息学到一条已确认事实。

        消息 id 每次自增：活动窗口 learning_messages 按 (会话, mid) 去重且不分人设
        （原文本身不区分人设，一条消息只属于收到它时的那个人设）。复用同一个 mid
        会被窗口当成重复消息忽略，那样测的就不是人设隔离了。
        """
        runtime = runtime_of(persona)
        for _ in range(count):
            self._mid += 1
            self.learn.observe(message_of(self._mid, text), runtime)
        return runtime

    def items(self, persona):
        return self.learn.view(runtime_of(persona), "group", GROUP)["items"]

    def count(self, persona):
        return self.db.execute("SELECT count(*) FROM learning_items WHERE persona=?",
                               (persona,)).fetchone()[0]


class PersonaIsolationTests(IsolationCase):
    def test_persona_isolation_defaults_to_on(self):
        """没有插件设置时也必须隔离：默认值不能靠界面去写。"""
        self.assertTrue(ConversationMemory.META.default_settings["personaIsolation"])
        self.learn_fact("人设甲")
        # 未注册插件 = 读不到设置 = 按默认（隔离）处理
        self.assertEqual(self.items("人设乙"), [])
        clause, params = self.learn._visibility(runtime_of("人设乙"))
        self.assertEqual((clause.strip(), params), ("AND persona=?", ("人设乙",)))

    def test_view_returns_only_active_persona(self):
        self.learn_fact("人设甲")
        self.learn_fact("人设乙", "我喜欢喝牛奶")
        mine = self.learn.view(runtime_of("人设甲"), "group", GROUP)
        self.assertEqual([item["content"] for item in mine["items"]], ["我喜欢吃西瓜"])
        self.assertEqual({item["persona"] for item in mine["items"]}, {"人设甲"})
        # 别人设的条目只报数量，不报内容
        self.assertEqual((mine["persona_items"], mine["other_persona_items"]), (1, 1))
        self.assertTrue(mine["isolation"])

    def test_isolation_off_shows_other_personas(self):
        """用户显式关掉隔离时必须能跨人设看见，否则这个开关就是摆设。"""
        self.learn_fact("人设甲")
        self.learn_fact("人设乙", "我喜欢喝牛奶")
        plugin = _Plugin(personaIsolation=False)
        runtime = runtime_of("人设乙", plugins={"conversation_memory": plugin})
        view = self.learn.view(runtime, "group", GROUP)
        self.assertEqual({item["persona"] for item in view["items"]}, {"人设甲", "人设乙"})
        # 关闭隔离时 items 已是全量，再报"不可见"就自相矛盾
        self.assertFalse(view["isolation"])
        self.assertEqual((view["persona_items"], view["other_persona_items"]), (None, 0))
        recalled = self.learn.recall(message_of(9, "吃点什么好"), runtime)
        self.assertTrue(any("西瓜" in item["text"] for item in recalled))

    def test_recall_only_returns_active_persona(self):
        """核心用例：A 人设学过的事实，B 人设不能召回。"""
        self.learn_fact("人设甲")
        self.learn_fact("人设乙", "我喜欢喝牛奶")
        mine = self.learn.recall(message_of(9, "想喝点什么"), runtime_of("人设乙"))
        self.assertEqual([item["text"] for item in mine], ["我喜欢喝牛奶"])
        self.assertNotIn("西瓜", "".join(item["text"] for item in mine))
        theirs = self.learn.recall(message_of(9, "想喝点什么"), runtime_of("人设甲"))
        self.assertEqual([item["text"] for item in theirs], ["我喜欢吃西瓜"])

    def test_clear_removes_only_active_persona_items(self):
        """清一个会话不能让其他人设的学习区域一起消失。"""
        self.learn_fact("人设甲")
        self.learn_fact("人设乙", "我喜欢喝牛奶")
        self.learn.clear(runtime_of("人设乙"), "group", GROUP)
        self.assertEqual(self.count("人设乙"), 0)
        self.assertEqual(self.count("人设甲"), 1)
        self.assertEqual(len(self.items("人设甲")), 1)

    def test_clear_single_message_keeps_other_persona_items(self):
        """删一条原文只撤销引用它的条目，不按人设连坐。

        单条清空走的是证据撤销（evidence 里含这个 mid 才删），不是按人设删：
        这条原文确实没了，引用它的条目就该消失；没引用它的其他人设条目必须留下。
        """
        self.learn_fact("人设甲")
        other = self._mid + 1
        self.learn_fact("人设乙", "我喜欢喝牛奶")
        self.learn.clear(runtime_of("人设乙"), "group", GROUP, "999")
        self.assertEqual((self.count("人设甲"), self.count("人设乙")), (1, 1))
        self.learn.clear(runtime_of("人设乙"), "group", GROUP, str(other))
        self.assertEqual(self.count("人设甲"), 1)
        self.assertEqual(self.count("人设乙"), 0)

    def test_change_rejects_item_of_other_persona(self):
        self.learn_fact("人设甲", "好耶出发啦", count=1)
        identity = self.items("人设甲")[0]["id"]
        with self.assertRaises(ValueError) as raised:
            self.learn.change(runtime_of("人设乙"), "group", GROUP, identity, "approve")
        self.assertIn("其他人设", str(raised.exception))
        status = self.db.execute("SELECT status FROM learning_items WHERE id=?",
                                 (identity,)).fetchone()[0]
        self.assertEqual(status, "candidate")
        self.learn.change(runtime_of("人设甲"), "group", GROUP, identity, "approve")
        status = self.db.execute("SELECT status FROM learning_items WHERE id=?",
                                 (identity,)).fetchone()[0]
        self.assertEqual(status, "approved")

    def test_observe_keeps_persona_captured_at_entry_when_style_switches(self):
        """处理中途切人设，这条消息仍记在进门时的人设上，否则会"写了却看不见"。"""
        runtime = runtime_of("人设甲")
        original = self.learn._extract

        def switching(*args, **kwargs):
            runtime.style.active_name = "人设乙"
            return original(*args, **kwargs)

        self.learn._extract = switching
        self.learn.observe(message_of(102, "我喜欢吃西瓜"), runtime)
        self.assertEqual(self.db.execute("SELECT persona FROM learning_items").fetchone()[0], "人设甲")
        self.assertEqual(len(self.items("人设乙")), 0)
        self.assertEqual(len(self.items("人设甲")), 1)

    def test_observe_honours_explicit_learning_persona(self):
        self.learn.observe(message_of(101, "我喜欢吃西瓜", _learning_persona="人设甲"), runtime_of("人设乙"))
        self.assertEqual(self.db.execute("SELECT persona FROM learning_items").fetchone()[0], "人设甲")

    def test_bulk_approve_only_touches_active_persona(self):
        self.learn_fact("人设甲", "好耶出发啦", count=1)
        result = self.learn.bulk(runtime_of("人设乙"), "approve-expressions", "group", GROUP)
        self.assertEqual(result["approved"], 0)
        self.assertEqual(self.db.execute("SELECT status FROM learning_items").fetchone()[0], "candidate")
        result = self.learn.bulk(runtime_of("人设甲"), "approve-expressions", "group", GROUP)
        self.assertEqual(result["approved"], 1)
        self.assertEqual(self.db.execute("SELECT status FROM learning_items").fetchone()[0], "approved")

    def test_extract_cap_is_per_persona(self):
        """每人设各 200 条：B 学一条不能把 A 的挤掉一条。"""
        scope = ("wechat", "acc1", "group", GROUP)
        for index in range(200):
            self.learn._extract(scope, "m%d" % index, "wxid_a", "这是第%d种说法" % index, "人设甲")
        self.assertEqual(self.count("人设甲"), 200)
        self.learn._extract(scope, "x1", "wxid_a", "乙的独有说法", "人设乙")
        self.assertEqual(self.count("人设甲"), 200)
        self.assertEqual(self.count("人设乙"), 1)

    def test_overview_groups_by_persona(self):
        self.learn_fact("人设甲")
        rooms = [{"kind": "group", "target": GROUP, "count": 7}]
        mine = self.learn.overview(runtime_of("人设甲", rooms=rooms))[0]
        self.assertEqual((mine["persona"], mine["facts"], mine["persona_items"]), ("人设甲", 1, 1))
        self.assertEqual(mine["other_persona_items"], 0)
        theirs = self.learn.overview(runtime_of("人设乙", rooms=rooms))[0]
        self.assertEqual((theirs["facts"], theirs["persona_items"]), (0, 0))
        self.assertEqual(theirs["other_persona_items"], 1)
        self.assertEqual(theirs["personas"], {"人设甲": 1})
        self.assertIn("其他人设", theirs["hint"])

    def test_empty_database_does_not_crash(self):
        runtime = runtime_of("人设甲", rooms=[{"kind": "group", "target": GROUP, "count": 0}])
        view = self.learn.view(runtime, "group", GROUP)
        self.assertEqual((view["items"], view["persona_items"], view["other_persona_items"]), ([], 0, 0))
        self.assertEqual(view["persona"], "人设甲")
        self.assertEqual(self.learn.recall(message_of(1, "在吗"), runtime), [])
        self.assertEqual(self.learn.overview(runtime)[0]["persona_items"], 0)
        empty = self.learn.empty_view(runtime)
        self.assertEqual((empty["items"], empty["window_count"], empty["isolation"]), ([], 0, True))
        self.assertEqual(set(empty), set(view))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = sqlite3.connect(Path(self.tmp.name) / "old.sqlite3")
        self.db.row_factory = sqlite3.Row
        # 1.3.91 及以前的库：learning_items 没有 persona 列
        self.db.executescript("""
        CREATE TABLE learning_messages(
          mode TEXT,account TEXT,kind TEXT,target TEXT,mid TEXT,subject TEXT,name TEXT,
          content TEXT,created REAL,PRIMARY KEY(mode,account,kind,target,mid));
        CREATE TABLE learning_items(
          id TEXT PRIMARY KEY,mode TEXT,account TEXT,kind TEXT,target TEXT,subject TEXT,
          category TEXT,content TEXT,status TEXT,hits INTEGER,evidence TEXT,created REAL,updated REAL);
        """)
        self.db.execute("INSERT INTO learning_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("old1", "wechat", "acc1", "group", GROUP, "wxid_a", "fact",
                         "我喜欢吃西瓜", "approved", 2, "[]", 1.0, 1.0))
        self.db.commit()

    def tearDown(self):
        try:
            self.db.close()
        finally:
            self.tmp.cleanup()

    def test_migration_adds_persona_and_backfills_default(self):
        LocalLearning(self.db)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(learning_items)")}
        self.assertIn("persona", columns)
        row = self.db.execute("SELECT persona FROM learning_items WHERE id='old1'").fetchone()
        self.assertEqual(row[0], DEFAULT_PERSONA)
        # 老数据归默认人设：默认人设看得见，其他人设看不见
        learn = LocalLearning(self.db)
        self.assertEqual(len(learn.view(runtime_of(DEFAULT_PERSONA), "group", GROUP)["items"]), 1)
        self.assertEqual(len(learn.view(runtime_of("人设乙"), "group", GROUP)["items"]), 0)

    def test_migration_creates_persona_index(self):
        LocalLearning(self.db)
        found = self.db.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                "AND name='learning_persona'").fetchone()
        self.assertIsNotNone(found)
        columns = [row[2] for row in self.db.execute("PRAGMA index_info('learning_persona')")]
        self.assertEqual(columns, ["mode", "account", "kind", "target", "persona", "updated"])

    def test_reopening_is_idempotent(self):
        LocalLearning(self.db)
        LocalLearning(self.db)
        self.assertEqual(self.db.execute("SELECT count(*) FROM learning_items").fetchone()[0], 1)


class ManualMemoryBudgetTests(unittest.TestCase):
    def test_defaults_are_twelve_sixty_nine_hundred(self):
        self.assertEqual((MANUAL_ITEMS, MANUAL_ITEM_CHARS, MANUAL_TOTAL_CHARS), (12, 60, 900))
        settings = ConversationMemory.META.default_settings
        self.assertNotIn("manualItems", settings)  # 默认走常量，避免两份数字漂移

    def test_item_count_is_capped_keeping_the_newest(self):
        items = ["第%d条手动记忆" % index for index in range(20)]
        kept = manual_budget(items)
        self.assertEqual(len(kept), 12)
        self.assertEqual(kept[-1], "第19条手动记忆")
        self.assertEqual(kept[0], "第8条手动记忆")

    def test_single_item_is_truncated(self):
        kept = manual_budget(["字" * 200])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(kept[0]), 60)
        self.assertTrue(kept[0].endswith("…"))

    def test_total_chars_stops_appending(self):
        kept = manual_budget(["字" * 60, "字" * 60, "字" * 60], total_chars=100)
        self.assertEqual(len(kept), 1)
        self.assertEqual(sum(len(text) for text in kept), 60)

    def test_empty_and_blank_items_are_ignored(self):
        self.assertEqual(manual_budget([]), [])
        self.assertEqual(manual_budget(["", "   ", None]), [])


class ManualMemoryInPromptTests(RuntimeTestCase):
    """真正走一遍插件：手动记忆进 extra_system 前必须被截断。"""

    async def asyncSetUp(self):
        self.bot = await self.start_runtime()

    async def test_pre_ai_caps_manual_memories(self):
        from src.dialogue_management import ensure_schema
        ensure_schema(self.bot)
        persona = self.bot.style.active_name
        for index in range(20):
            self.bot.db.execute("INSERT INTO persona_notes(id,persona,content,enabled) VALUES(?,?,?,?)",
                                ("n%d" % index, persona, "第%d条手动记忆" % index + "补充说明" * 12, 1))
        self.bot.db.commit()
        message = {"id": "m-cap", "kind": "private", "target": "wxid_a", "sender_id": "wxid_a",
                   "sender_name": "阿明", "text": "在吗", "created": time.time()}
        hint = self.bot.plugins._plugins["conversation_memory"].on_pre_ai(
            message, [{"role": "user", "content": "在吗"}], self.bot)
        manual = message["_memory_items"]["manual"]
        self.assertLessEqual(len(manual), MANUAL_ITEMS)
        self.assertTrue(all(len(text) <= MANUAL_ITEM_CHARS for text in manual))
        self.assertLessEqual(sum(len(text) for text in manual), MANUAL_TOTAL_CHARS)
        joined = "".join(manual)
        self.assertIn("第19条手动记忆", joined)
        self.assertNotIn("第0条手动记忆", joined)
        self.assertLessEqual(len(hint["extra_system"]), MANUAL_TOTAL_CHARS + 40)
        # 记账字段仍是四个键，界面与 dsh_memory 都按它读
        self.assertEqual(set(message["_memory_items"]),
                         {"manual", "notes", "learned", "archive_recalled"})


if __name__ == "__main__":
    unittest.main()
