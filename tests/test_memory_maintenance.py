"""记忆预算与整理（1.3.0 P1-3 收尾）。"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from src.memory_maintenance import MAX_CHARS, MAX_ITEMS, budget, maintain


class BudgetTests(unittest.TestCase):
    def test_caps_item_count(self):
        items = [{'content': f'第{i}条'} for i in range(20)]
        self.assertEqual(len(budget(items)), MAX_ITEMS)

    def test_caps_characters(self):
        items = [{'content': '很长的记忆' * 100} for _ in range(5)]
        out = budget(items, max_chars=200)
        self.assertEqual(len(out), 1)

    def test_empty_input(self):
        self.assertEqual(budget([]), [])

    def test_single_overlong_item_still_returned(self):
        out = budget([{'content': 'x' * (MAX_CHARS + 100)}])
        self.assertEqual(len(out), 1)


class MaintainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(Path(self.tmp.name) / 'm.sqlite3')
        self.db.executescript(
            'CREATE TABLE chat_memories(id TEXT PRIMARY KEY,scope TEXT,subject TEXT,content TEXT,'
            'status TEXT,source TEXT,hits INTEGER,created REAL,updated REAL);')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, mid, content, status='confirmed', hits=2, age_days=0):
        now = time.time() - age_days * 86400
        self.db.execute('INSERT INTO chat_memories VALUES(?,?,?,?,?,?,?,?,?)',
                        (mid, 's', 'u', content, status, 'x', hits, now, now))

    def test_near_duplicates_are_merged(self):
        self.add('a', '我喜欢乌龙茶', hits=2, age_days=10)
        self.add('b', '我喜欢乌龙茶。', hits=2, age_days=1)
        result = maintain(self.db)
        self.assertEqual(result['merged'], 1)
        statuses = dict(self.db.execute('SELECT id,status FROM chat_memories').fetchall())
        self.assertEqual(statuses['b'], 'confirmed')     # 保留较新的
        self.assertEqual(statuses['a'], 'superseded')

    def test_old_superseded_is_cleaned(self):
        self.add('a', '住在杭州', status='superseded', age_days=10)
        self.assertGreaterEqual(maintain(self.db)['expired'], 1)
        self.assertIsNone(self.db.execute("SELECT 1 FROM chat_memories WHERE id='a'").fetchone())

    def test_recent_superseded_is_kept(self):
        self.add('a', '住在杭州', status='superseded', age_days=2)
        maintain(self.db)
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM chat_memories WHERE id='a'").fetchone())

    def test_single_evidence_candidate_dropped_after_a_month(self):
        self.add('a', '可能喜欢爬山', status='candidate', hits=1, age_days=40)
        self.assertGreaterEqual(maintain(self.db)['dropped'], 1)

    def test_confirmed_single_evidence_never_dropped(self):
        self.add('a', '喜欢深紫色', status='confirmed', hits=1, age_days=400)
        maintain(self.db)
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM chat_memories WHERE id='a'").fetchone())

    def test_different_subjects_are_not_merged(self):
        self.add('a', '我喜欢乌龙茶')
        self.db.execute("UPDATE chat_memories SET subject='other' WHERE id='a'")
        self.add('b', '我喜欢乌龙茶')
        self.assertEqual(maintain(self.db)['merged'], 0)


if __name__ == '__main__':
    unittest.main()
