"""记忆排序/冲突/敏感信息（1.3.0 P1-3）。"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from src.memory_rank import (bigrams, evidence_score, freshness, is_sensitive,
                             near_duplicate, score, similarity, supersedes)
from src.chat_state import ChatState, safe_fact


class RankTests(unittest.TestCase):
    def test_bigrams_split_chinese_and_english(self):
        grams = bigrams('住在杭州 abc')
        self.assertIn('住在', grams)
        self.assertIn('abc', grams)

    def test_similarity_prefers_related_content(self):
        self.assertGreater(similarity('我住哪', '住在杭州西湖区'),
                           similarity('我住哪', '喜欢吃西瓜'))

    def test_concept_normalisation_helps(self):
        self.assertGreater(similarity('我现在住哪', '搬家到杭州'), 0.2)

    def test_freshness_decays(self):
        now = time.time()
        self.assertGreater(freshness(now, now), freshness(now - 30 * 86400, now))

    def test_evidence_score_caps(self):
        self.assertEqual(evidence_score(0), 0)
        self.assertEqual(evidence_score(5), 1.0)
        self.assertEqual(evidence_score(50), 1.0)

    def test_score_rewards_relevant_recent_confirmed(self):
        now = time.time()
        good = score('住哪', '住在杭州', now - 3600, 4, now)
        bad = score('住哪', '喜欢西瓜', now - 60 * 86400, 1, now)
        self.assertGreater(good, bad)

    def test_sensitive_detection(self):
        self.assertTrue(is_sensitive('身份证 110101199001011234'))
        self.assertTrue(is_sensitive('银行卡 6222021234567890123'))
        self.assertTrue(is_sensitive('验证码 123456'))
        self.assertFalse(is_sensitive('我喜欢乌龙茶'))

    def test_safe_fact_rejects_sensitive(self):
        with self.assertRaises(ValueError):
            safe_fact('我的身份证 110101199001011234')

    def test_supersede_on_move(self):
        self.assertTrue(supersedes('住在杭州', '搬家到上海了'))
        self.assertFalse(supersedes('喜欢猫', '搬家到上海了'))

    def test_near_duplicate(self):
        self.assertTrue(near_duplicate('我喜欢乌龙茶', '我喜欢乌龙茶。'))
        self.assertFalse(near_duplicate('我喜欢乌龙茶', '我讨厌香菜'))


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = ChatState.__new__(ChatState)
        import sqlite3
        self.db = sqlite3.connect(Path(self.tmp.name) / 'm.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.state.db = self.db
        self.db.executescript(
            'CREATE TABLE chat_memories(id TEXT PRIMARY KEY,scope TEXT,subject TEXT,content TEXT,'
            'status TEXT,source TEXT,hits INTEGER,created REAL,updated REAL);')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_relevant_memory_ranks_first(self):
        now = time.time()
        rows = [('m1', 's', 'u', '喜欢吃西瓜', 'confirmed', 'x', 5, now, now),
                ('m2', 's', 'u', '住在杭州西湖区', 'confirmed', 'x', 2, now - 3 * 86400, now - 3 * 86400),
                ('m3', 's', 'u', '周末去爬山', 'confirmed', 'x', 1, now - 60 * 86400, now - 60 * 86400)]
        self.db.executemany('INSERT INTO chat_memories VALUES(?,?,?,?,?,?,?,?,?)', rows)
        self.db.commit()
        got = self.state.recall('s', 'u', query='我住哪')
        self.assertTrue(got)
        self.assertEqual(got[0]['id'], 'm2')

    def test_superseded_memory_is_written(self):
        self.state.remember('s', 'u', '住在杭州', 'src1', confirmed=True, now=time.time() - 86400)
        self.state.remember('s', 'u', '搬家到上海了', 'src2', confirmed=True)
        rows = dict(self.db.execute('SELECT content,status FROM chat_memories').fetchall())
        self.assertEqual(rows['住在杭州'], 'superseded')
        self.assertNotEqual(rows['搬家到上海了'], 'superseded')


if __name__ == '__main__':
    unittest.main()
