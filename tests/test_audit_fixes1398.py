"""1.3.98 审计修复：0x7B 错码模板、强信号紧凑匹配、沙盒死代码、提交失败上报。"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from src.plugins import computer_helper
from src import sandbox_doctor
from src import memory_maintenance as mm
from src.chat_state import ChatState


class TemplateCodeTests(unittest.TestCase):
    def test_bare_0x7b_code_picks_boot_template(self):
        """用户只写 0000007b（没有 0x）也必须进专项模板，而不是通用兜底。"""
        picked = computer_helper.pick_templates("蓝屏代码 0000007b 进不去系统", 1)
        self.assertEqual(picked[0][0], "0x0000007B")

    def test_uppercase_and_0x_forms_still_pick_boot_template(self):
        """改错码时不能把原来能命中的大写与 0x 写法弄丢。"""
        for text in ("蓝屏代码 0000007B", "蓝屏 0x0000007B"):
            with self.subTest(text=text):
                picked = computer_helper.pick_templates(text, 1)
                self.assertEqual(picked[0][0], "0x0000007B")

    def test_every_whitelisted_code_picks_a_specific_template(self):
        """白名单里每个错误码都要命中它自己的专项模板，一个都不许落到通用兜底。"""
        expected = {
            "0000007b": ("0x0000007B",),
            "c000000f": ("0xc000000f",),
            "00000050": ("0x00000050/0x3B",),
            "0000003b": ("0x00000050/0x3B",),
            "00000116": ("0x116/0x133",),
            "00000133": ("0x116/0x133",),
        }
        for code in computer_helper.COMPUTER_ERROR_CODES:
            with self.subTest(code=code):
                picked = computer_helper.pick_templates(f"蓝屏 {code}", 1)
                self.assertTrue(picked)
                self.assertIn(picked[0][0], expected[code])

    def test_underscored_stop_code_is_recognised(self):
        """用户按微软原文写 INACCESSIBLE_BOOT_DEVICE 时要认成电脑问题。"""
        for text in ("开机就 INACCESSIBLE_BOOT_DEVICE 怎么办", "inaccessible boot device"):
            with self.subTest(text=text):
                self.assertTrue(computer_helper.looks_like_computer_issue(text))

    def test_chitchat_still_not_a_computer_issue(self):
        """强信号放宽到 compact 后不能把日常闲聊一起收进来。"""
        for text in ("内存怎么这么贵", "他唱歌没声音的"):
            with self.subTest(text=text):
                self.assertFalse(computer_helper.looks_like_computer_issue(text))


class SandboxDoctorTests(unittest.TestCase):
    def test_dead_duplicate_helper_is_gone(self):
        """_sandbox_windows 定义两遍且无人调用，删掉后模块里不该再留下这个名字。"""
        self.assertFalse(hasattr(sandbox_doctor, "_sandbox_windows"))

    def test_handles_helper_still_exists(self):
        """probe_launch 依赖的 _sandbox_handles 必须还在。"""
        self.assertTrue(callable(getattr(sandbox_doctor, "_sandbox_handles", None)))


class FakeDb:
    """假连接：commit 可按需失败，用来模拟磁盘写不进去。"""

    def __init__(self, rows, fail=False):
        self.rows = rows
        self.fail = fail
        self.rolled = 0
        self.committed = 0

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            rows = self.rows
            return type("Cursor", (), {"fetchall": lambda _self: rows, "rowcount": len(rows)})()
        return type("Cursor", (), {"fetchall": lambda _self: [], "rowcount": 0})()

    def commit(self):
        self.committed += 1
        if self.fail:
            raise RuntimeError("disk full")

    def rollback(self):
        self.rolled += 1


def _over_capacity_rows(count=3, old=100000.0):
    """count 条都在很久以前更新过，grace=0 时全部可淘汰。"""
    stamp = time.time() - old
    return [(f"id{i}", "s", f"u{i}", "喜欢乌龙茶" * 3, "confirmed", 2, stamp)
            for i in range(count)]


class CommitFailureTests(unittest.TestCase):
    def test_evict_reports_zero_when_commit_fails(self):
        """提交失败时一条都没删掉，evict 必须报 0 而不是报删了几条。"""
        db = FakeDb(_over_capacity_rows(), fail=True)
        self.assertEqual(mm.evict(db, maximum=1, grace=0.0), 0)

    def test_evict_rolls_back_when_commit_fails(self):
        """失败事务不能留在连接上，必须回滚。"""
        db = FakeDb(_over_capacity_rows(), fail=True)
        mm.evict(db, maximum=1, grace=0.0)
        self.assertGreaterEqual(db.rolled, 1)

    def test_evict_reports_count_when_commit_succeeds(self):
        """提交成功时照常返回淘汰条数，修复不能把正常路径一起改坏。"""
        db = FakeDb(_over_capacity_rows(), fail=False)
        self.assertGreater(mm.evict(db, maximum=1, grace=0.0), 0)

    def test_maintain_reports_not_persisted_when_commit_fails(self):
        """整理没落盘时不能报非零计数，persisted 必须是 False。"""
        db = FakeDb(_over_capacity_rows(), fail=True)
        result = mm.maintain(db)
        self.assertFalse(result["persisted"])

    def test_maintain_zeroes_every_count_when_commit_fails(self):
        """没落盘时四个计数全归零，界面不会显示"合并 N 条"。"""
        db = FakeDb(_over_capacity_rows(), fail=True)
        result = mm.maintain(db)
        self.assertEqual(
            (result["merged"], result["expired"], result["dropped"], result["evicted"]),
            (0, 0, 0, 0))

    def test_maintain_reports_persisted_on_success(self):
        """真实 sqlite 上提交成功时 persisted 为 True。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp) / "m.sqlite3")
            try:
                db.executescript(
                    "CREATE TABLE chat_memories(id TEXT PRIMARY KEY,scope TEXT,subject TEXT,"
                    "content TEXT,status TEXT,source TEXT,hits INTEGER,created REAL,updated REAL);")
                now = time.time()
                db.execute("INSERT INTO chat_memories VALUES(?,?,?,?,?,?,?,?,?)",
                           ("a", "s", "u", "喜欢深紫色", "confirmed", "x", 2, now, now))
                self.assertTrue(mm.maintain(db)["persisted"])
            finally:
                db.close()


class FlakyCommit:
    """真实连接的包装：第 fail_at 次 commit 抛 database is locked，其余原样转发。"""

    def __init__(self, db, fail_at):
        self._db = db
        self.fail_at = fail_at
        self.commits = 0
        self.rollbacks = 0

    def __getattr__(self, name):
        return getattr(self._db, name)

    def commit(self):
        self.commits += 1
        if self.commits == self.fail_at:
            raise sqlite3.OperationalError("database is locked")
        self._db.commit()

    def rollback(self):
        self.rollbacks += 1
        self._db.rollback()


class CallerOwnedTransactionTests(unittest.TestCase):
    def test_evict_with_commit_false_leaves_transaction_to_caller(self):
        """commit=False 时 evict 既不提交也不回滚，事务留给 caller 自己收尾。"""
        db = FakeDb(_over_capacity_rows(), fail=False)
        removed = mm.evict(db, maximum=1, grace=0.0, commit=False)
        self.assertGreater(removed, 0)
        self.assertEqual(db.committed, 0)
        self.assertEqual(db.rolled, 0)

    def test_evict_with_commit_false_does_not_rollback_on_failing_db(self):
        """就算连接本身提交会失败，commit=False 也绝不能替 caller 回滚。"""
        db = FakeDb(_over_capacity_rows(), fail=True)
        mm.evict(db, maximum=1, grace=0.0, commit=False)
        self.assertEqual(db.rolled, 0)

    def test_remember_keeps_new_memory_and_only_caller_commits(self):
        """回归钉死：remember 全程只提交一次，evict 不许替 caller 提交或回滚。

        1.3.98 首版让 evict 自己提交、失败即 rollback，而 remember 的 INSERT
        （src/chat_state.py:100-102）要到 :122 才提交，于是 evict 的 rollback 把用户
        刚说的那条记忆一起丢掉，调用方还拿到成功的 digest。这里直接钉住"evict 一次都
        不提交、也不回滚"，回退到首版会在 commit 计数断言上立刻失败。
        """
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "c.sqlite3")
            try:
                flaky = FlakyCommit(conn, fail_at=10**9)
                state = ChatState(flaky)
                # ChatState 初始化自己提交过一次（src/chat_state.py:57），先记下来
                self.assertEqual(flaky.commits, 1)
                now = time.time()
                for i in range(11):
                    conn.execute(
                        "INSERT INTO chat_memories VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"old{i}", "s", f"u{i}", f"旧事实{i}", "confirmed", "chat", 2,
                         now - 90000, now - 90000))
                flaky.commit()  # 预填走包装连接，提交计数保持可数
                self.assertEqual(flaky.commits, 2)
                digest = state.remember("s", "u", "用户住在杭州", "chat", confirmed=True, maximum=10)
                self.assertTrue(digest)
                # 整个 remember 只允许 caller 那一次提交（:122）：evict 一次都没提交
                self.assertEqual(flaky.commits, 3,
                                 f"remember 里出现了 {flaky.commits - 2} 次提交，evict 不该自己提交")
                self.assertEqual(flaky.rollbacks, 0, "evict 不该替 caller 回滚")
                kept = conn.execute("SELECT COUNT(*) FROM chat_memories WHERE id=?", (digest,)).fetchone()[0]
                self.assertEqual(kept, 1, "evict 回滚把用户刚说的这条记忆弄丢了")
                total = conn.execute("SELECT COUNT(*) FROM chat_memories").fetchone()[0]
                self.assertEqual(total, 10)
            finally:
                conn.close()

if __name__ == "__main__":
    unittest.main()
