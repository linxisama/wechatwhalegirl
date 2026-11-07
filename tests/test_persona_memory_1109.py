"""1.3.109 人格独立记忆 + 日记周期 + 心情重置。

用户 2026-11-08 的原话（拆成四条）：
  1. 主人格为基：日记默认写到主人格目录
  2. 人格独立目录：每个人格只读自己那份
  3. 日记周期：7 天提炼成周记 → 月记 → 高频条目永久保留
  4. 心情：4 小时不聊天随机重置，只对主人的回复生效

运行：./.venv-wechat/Scripts/python.exe -m unittest tests.test_persona_memory_1109
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.persona_memory import (
    PersonaMemoryError,
    list_daily,
    list_weekly,
    monthly_keywords,
    persona_memory_dir,
    read_daily,
    rollup_month,
    rollup_week,
    safe_persona,
    write_daily,
)
from src.persona_mood import (
    DEFAULT_IDLE_HOURS,
    clamp_idle_hours,
    is_owner_message,
    label,
    reset_values,
    resolve,
    should_reset,
)

NOW = 1_800_000_000.0


class PersonaNameTests(unittest.TestCase):
    """人格名会成为目录名，一个 .. 就跳出根目录。"""

    def test_chinese_names_accepted(self):
        for name in ("鲸鱼娘", "小猫", "default", "a_b", "persona 1"):
            with self.subTest(name=name):
                self.assertEqual(safe_persona(name), name)

    def test_traversal_rejected(self):
        for bad in ("", "..", ".", "a/b", "a\\b", "C:x"):
            with self.subTest(bad=bad):
                with self.assertRaises(PersonaMemoryError):
                    safe_persona(bad)

    def test_raises_instead_of_sanitizing(self):
        """清洗会让用户以为记到了 A 人格，实际记到了别处。"""
        with self.assertRaises(PersonaMemoryError):
            safe_persona("鲸鱼娘/../别的")


class DirectoryTests(unittest.TestCase):
    def test_layout_is_under_persona_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = persona_memory_dir(root, "鲸鱼娘")
            self.assertEqual(path, root / "data" / "personas" / "鲸鱼娘" / "memory")
            self.assertTrue(path.is_dir())

    def test_create_false_does_not_mkdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = persona_memory_dir(root, "鲸鱼娘", create=False)
            self.assertFalse(path.exists())


class DailyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read(self):
        write_daily(self.root, "鲸鱼娘", "2026-11-01", "今天很开心", ["主人", "微信"])
        entry = read_daily(self.root, "鲸鱼娘", "2026-11-01")
        self.assertEqual(entry["content"], "今天很开心")
        self.assertEqual(entry["keywords"], ["主人", "微信"])

    def test_rewrite_overwrites(self):
        write_daily(self.root, "鲸鱼娘", "2026-11-01", "第一版")
        write_daily(self.root, "鲸鱼娘", "2026-11-01", "第二版")
        self.assertEqual(read_daily(self.root, "鲸鱼娘", "2026-11-01")["content"], "第二版")

    def test_list_is_sorted(self):
        for day in ("2026-11-03", "2026-11-01", "2026-11-02"):
            write_daily(self.root, "鲸鱼娘", day, day)
        self.assertEqual(list_daily(self.root, "鲸鱼娘"),
                         ["2026-11-01", "2026-11-02", "2026-11-03"])

    def test_missing_returns_none(self):
        self.assertIsNone(read_daily(self.root, "鲸鱼娘", "2099-01-01"))


class RollupWeekTests(unittest.TestCase):
    """7 天压成周记，原始文件删掉 —— 「每存储一周会删除一次」的字面意思。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for index in range(8):
            day = "2026-11-%02d" % (index + 1)
            keywords = ["主人", "微信"] if index < 6 else ["主人"]
            write_daily(self.root, "鲸鱼娘", day, "第%d天" % (index + 1), keywords)

    def tearDown(self):
        self._tmp.cleanup()

    def test_not_enough_days_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_daily(root, "鲸鱼娘", "2026-11-01", "只有一天")
            result = rollup_week(root, "鲸鱼娘", keep_days=7)
            self.assertFalse(result["rolled"])
            self.assertEqual(list_daily(root, "鲸鱼娘"), ["2026-11-01"])

    def test_rolls_oldest_seven_days(self):
        result = rollup_week(self.root, "鲸鱼娘", keep_days=7, min_frequency=2)
        self.assertTrue(result["rolled"])
        self.assertEqual(result["from_day"], "2026-11-01")
        self.assertEqual(result["to_day"], "2026-11-07")
        self.assertEqual(result["removed"], [
            "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04",
            "2026-11-05", "2026-11-06", "2026-11-07"])

    def test_remaining_day_survives(self):
        rollup_week(self.root, "鲸鱼娘", keep_days=7, min_frequency=2)
        self.assertEqual(list_daily(self.root, "鲸鱼娘"), ["2026-11-08"])

    def test_frequent_keywords_kept(self):
        result = rollup_week(self.root, "鲸鱼娘", keep_days=7, min_frequency=2)
        self.assertIn("主人", result["keywords"])
        self.assertIn("微信", result["keywords"])

    def test_rare_keyword_dropped(self):
        """只出现 1 次的关键词不进周记 —— 「只留取重要的内容」。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(7):
                write_daily(root, "鲸鱼娘", "2026-11-%02d" % (index + 1), "d",
                            ["常见"] + (["罕见"] if index == 0 else []))
            result = rollup_week(root, "鲸鱼娘", keep_days=7, min_frequency=2)
            self.assertIn("常见", result["keywords"])
            self.assertNotIn("罕见", result["keywords"])

    def test_weekly_file_written(self):
        result = rollup_week(self.root, "鲸鱼娘", keep_days=7, min_frequency=2)
        self.assertEqual(list_weekly(self.root, "鲸鱼娘"), ["2026-11-01_2026-11-07"])
        payload = json.loads(Path(result["path"]).read_text("utf-8"))
        self.assertEqual(payload["from_day"], "2026-11-01")
        self.assertTrue(payload["summary"])


class RollupMonthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_weeks_does_nothing(self):
        self.assertFalse(rollup_month(self.root, "鲸鱼娘")["rolled"])

    def test_month_merges_weeks(self):
        for start in (1, 9):
            for offset in range(7):
                write_daily(self.root, "鲸鱼娘", "2026-11-%02d" % (start + offset), "d", ["主人"])
            rollup_week(self.root, "鲸鱼娘", keep_days=7, min_frequency=2)
        result = rollup_month(self.root, "鲸鱼娘", min_frequency=2)
        self.assertTrue(result["rolled"])
        self.assertEqual(result["month"], "2026-11")
        self.assertIn("主人", result["keywords"])

    def test_month_does_not_delete_weeks(self):
        """周记是「这一周发生了什么」的原始记录，删了就没法回溯。"""
        for offset in range(7):
            write_daily(self.root, "鲸鱼娘", "2026-11-%02d" % (offset + 1), "d", ["主人"])
        rollup_week(self.root, "鲸鱼娘", keep_days=7, min_frequency=2)
        before = list_weekly(self.root, "鲸鱼娘")
        rollup_month(self.root, "鲸鱼娘", min_frequency=2)
        self.assertEqual(list_weekly(self.root, "鲸鱼娘"), before)

    def test_monthly_keywords_readable(self):
        for offset in range(7):
            write_daily(self.root, "鲸鱼娘", "2026-11-%02d" % (offset + 1), "d", ["主人", "微信"])
        rollup_week(self.root, "鲸鱼娘", keep_days=7, min_frequency=2)
        rollup_month(self.root, "鲸鱼娘", min_frequency=2)
        self.assertIn("主人", monthly_keywords(self.root, "鲸鱼娘"))


class IsolationTests(unittest.TestCase):
    """每个人格只读自己那份 —— 这是整批的核心。"""

    def test_two_personas_do_not_see_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_daily(root, "鲸鱼娘", "2026-11-01", "鲸鱼娘的日记", ["鲸鱼"])
            write_daily(root, "小猫", "2026-11-01", "小猫的日记", ["猫"])
            self.assertEqual(list_daily(root, "鲸鱼娘"), ["2026-11-01"])
            self.assertEqual(list_daily(root, "小猫"), ["2026-11-01"])
            self.assertEqual(read_daily(root, "鲸鱼娘", "2026-11-01")["content"], "鲸鱼娘的日记")
            self.assertEqual(read_daily(root, "小猫", "2026-11-01")["content"], "小猫的日记")

    def test_rollup_does_not_touch_other_persona(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(7):
                write_daily(root, "鲸鱼娘", "2026-11-%02d" % (index + 1), "d", ["主人"])
            write_daily(root, "小猫", "2026-11-01", "猫", ["猫"])
            rollup_week(root, "鲸鱼娘", keep_days=7, min_frequency=2)
            self.assertEqual(list_daily(root, "小猫"), ["2026-11-01"])

    def test_monthly_keywords_empty_for_other_persona(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for offset in range(7):
                write_daily(root, "鲸鱼娘", "2026-11-%02d" % (offset + 1), "d", ["主人"])
            rollup_week(root, "鲸鱼娘", keep_days=7, min_frequency=2)
            rollup_month(root, "鲸鱼娘", min_frequency=2)
            self.assertEqual(monthly_keywords(root, "小猫"), [])


class MoodIdleResetTests(unittest.TestCase):
    """4 小时不聊天随机重置。"""

    def test_default_is_four_hours(self):
        self.assertEqual(DEFAULT_IDLE_HOURS, 4)

    def test_clamp_bounds(self):
        self.assertEqual(clamp_idle_hours(4), 4)
        self.assertEqual(clamp_idle_hours(0), 1)
        self.assertEqual(clamp_idle_hours(-5), 1)
        self.assertEqual(clamp_idle_hours(999), 48)
        self.assertEqual(clamp_idle_hours("6"), 6)
        self.assertEqual(clamp_idle_hours("abc"), DEFAULT_IDLE_HOURS)

    def test_no_record_means_reset(self):
        self.assertTrue(should_reset(None, NOW))

    def test_under_four_hours_does_not_reset(self):
        self.assertFalse(should_reset(NOW - 3 * 3600, NOW))

    def test_at_four_hours_resets(self):
        self.assertTrue(should_reset(NOW - 4 * 3600, NOW))

    def test_custom_hours(self):
        self.assertTrue(should_reset(NOW - 1.5 * 3600, NOW, 1))
        self.assertFalse(should_reset(NOW - 1.5 * 3600, NOW, 2))

    def test_reset_is_deterministic(self):
        """同一段静置反复查询必须拿到同一个值，否则刷新一次变一次像 bug。"""
        first = reset_values(NOW - 5 * 3600, NOW)
        second = reset_values(NOW - 5 * 3600, NOW)
        self.assertEqual(first, second)

    def test_different_last_seen_gives_different_mood(self):
        self.assertNotEqual(reset_values(NOW - 5 * 3600, NOW),
                            reset_values(NOW - 5 * 3600 + 1, NOW))

    def test_reset_values_are_in_middle_band(self):
        """不回到固定值，也不落在极端 —— 静置之后情绪本来就该飘回中段。"""
        for offset in range(1, 50):
            mood, energy, stress = reset_values(NOW - offset * 3600, NOW)
            with self.subTest(offset=offset):
                self.assertGreaterEqual(mood, 40.0)
                self.assertLessEqual(mood, 70.0)
                self.assertGreaterEqual(energy, 70.0)
                self.assertLessEqual(stress, 20.0)

    def test_resolve_without_reset_keeps_values(self):
        state = {"updated": NOW - 3600, "mood": 90, "energy": 90, "stress": 50}
        result = resolve(state, NOW)
        self.assertFalse(result["idle_reset"])
        self.assertEqual((result["mood"], result["energy"], result["stress"]),
                         (90.0, 90.0, 50.0))

    def test_resolve_with_reset_replaces_values(self):
        state = {"updated": NOW - 5 * 3600, "mood": 5, "energy": 5, "stress": 95}
        result = resolve(state, NOW)
        self.assertTrue(result["idle_reset"])
        self.assertGreaterEqual(result["mood"], 40.0)

    def test_resolve_handles_none_and_broken_state(self):
        self.assertTrue(resolve(None, NOW)["idle_reset"])
        broken = resolve({"updated": NOW - 100, "mood": "x", "energy": None}, NOW)
        self.assertFalse(broken["idle_reset"])
        self.assertEqual((broken["mood"], broken["energy"]), (60.0, 80.0))

    def test_label_thresholds(self):
        self.assertEqual(label(80), "开心")
        self.assertEqual(label(70), "开心")
        self.assertEqual(label(60), "平静")
        self.assertEqual(label(40), "平静")
        self.assertEqual(label(30), "有点低落")


class OwnerOnlyTests(unittest.TestCase):
    """只对主人的回复生效。判不准时保守当作不是主人。"""

    def test_explicit_flag_true(self):
        self.assertTrue(is_owner_message({"_is_owner": True}))

    def test_explicit_flag_false(self):
        self.assertFalse(is_owner_message({"_is_owner": False}))

    def test_missing_flag_is_conservative(self):
        """拿不准时返回 False —— 宁可不注入，也不要把状态发给陌生人。"""
        self.assertFalse(is_owner_message({}))

    def test_runtime_checker_used(self):
        import types
        runtime = types.SimpleNamespace(is_owner=lambda message: True)
        self.assertTrue(is_owner_message({}, runtime))

    def test_runtime_checker_failure_is_conservative(self):
        import types
        def boom(_message):
            raise RuntimeError("坏了")
        runtime = types.SimpleNamespace(is_owner=boom)
        self.assertFalse(is_owner_message({}, runtime))

    def test_explicit_flag_wins_over_runtime(self):
        import types
        runtime = types.SimpleNamespace(is_owner=lambda message: True)
        self.assertFalse(is_owner_message({"_is_owner": False}, runtime))


if __name__ == "__main__":
    unittest.main()