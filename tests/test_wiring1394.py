"""五处「实现已写好、就差接线」的收尾验收（1.3.94）。

这一轮只做接线，不新增功能，因此每个用例都盯住"实现到底有没有被真正调用"：

1. 分块账本：record_round 必须拿到 PromptPlan，report() 才说得清人设/插件各占多少；
2. 记忆淘汰：从 FIFO 换成按得分，且刚写入的记录受 grace 保护、superseded 不动；
3. 召回：换成 rank()，跨条近重复只留一条，返回结构仍是原来那四个键；
4. 历史：超预算时跳过当前这条继续看更早的，不再无脑砍掉全部旧历史；
5. 人格冲突：StyleManager 的 _conflicts 必须能被读出来并写进启动日志。

测试全部离线：不连微信、不连模型、不读写仓库里的真实数据目录。
"""
import inspect
import json
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from src import token_ledger as tl
from src.chat_state import ChatState, build_history, persona_key
from src.prompt_budget import compose_prompt
from src.style_manager import StyleManager


def make_db(tmp):
    """建一个只有 chat_memories 的最小库（列序与 chat_state.remember 的 SQL 一致）。"""
    db = sqlite3.connect(str(Path(tmp) / 'm.sqlite3'))
    db.executescript(
        'CREATE TABLE chat_memories(id TEXT PRIMARY KEY,scope TEXT,subject TEXT,content TEXT,'
        'status TEXT,source TEXT,hits INTEGER,created REAL,updated REAL);')
    return db


def insert_rows(db, rows):
    db.executemany('INSERT INTO chat_memories VALUES(?,?,?,?,?,?,?,?,?)', rows)
    db.commit()


class TokenLedgerPlanTests(unittest.TestCase):
    """1) 分块账本：plan 必须真的进到 record_round 里。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self):
        return compose_prompt(persona='你是鲸鱼娘，说话短一点。' * 5,
                              continuity='连续性提示',
                              plugins='插件提示：联网资讯',
                              rules='宿主固定规则')

    def test_prompt_plan_sizes_reach_the_ledger_entry(self):
        plan = self.plan()
        entry = tl.record_round(self.root, {'id': 'm1', 'kind': 'private'}, '回答',
                                prompt_text=plan.text, model='deepseek-chat', plan=plan)
        self.assertIsNotNone(entry)
        sizes = entry.get('sizes') or {}
        # 假 PromptPlan 走的是 _sizes_of 的鸭子类型通路：只认 .sizes，不 import prompt_budget
        for name in ('persona', 'continuity', 'plugins', 'rules'):
            self.assertIn(name, sizes)
            self.assertGreater(sizes[name], 0)
        self.assertEqual(entry['budget']['limit'], plan._limit)
        self.assertGreater(entry['budget']['used'], 0)
        self.assertNotIn('image', sizes)

    def test_report_reads_blocks_back_from_the_same_round(self):
        plan = self.plan()
        tl.record_round(self.root, {'id': 'm2', 'kind': 'private'}, '回答',
                        prompt_text=plan.text, model='m', plan=plan)
        report = tl.report(tl.path_for(self.root))
        self.assertEqual(report['rounds'], 1)
        self.assertEqual(report['blocks']['persona'], plan.sizes['persona'])
        self.assertIn('persona', report['share'])
        self.assertGreater(report['avg_chars']['persona'], 0)

    def test_send_path_passes_the_prompt_plan(self):
        from src.wechat_app import WeChatRuntime
        source = inspect.getsource(WeChatRuntime._work)
        self.assertRegex(source, r"plan\s*=\s*locals\(\)\.get\(['\"]prompt_plan['\"]\)")


class MemoryEvictionTests(unittest.TestCase):
    """2) 淘汰按得分：稳定事实不被 FIFO 误杀，刚写入的受 grace 保护。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = make_db(self.tmp.name)
        self.state = ChatState(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_high_hits_old_memory_survives_capacity(self):
        now = time.time()
        rows = [('stable', 's', 'u', '我喜欢乌龙茶', 'confirmed', 'x', 5,
                 now - 200 * 86400, now - 200 * 86400)]
        for index in range(11):
            stamp = now - 0.5 * 86400
            rows.append((f'noise{index}', 's', 'u', f'随口一句{index}', 'confirmed', 'x', 1,
                         stamp, stamp))
        insert_rows(self.db, rows)
        # maximum=3 会被 remember 的下限抬到 10：13 条里淘汰 3 条，正好挑最没价值的
        self.state.remember('s', 'u', '我喜欢柚子茶', 'src', confirmed=True, maximum=3)
        left = {row[0] for row in self.db.execute('SELECT id FROM chat_memories')}
        self.assertIn('stable', left)
        self.assertEqual(len(left), 10)

    def test_just_written_row_is_protected_by_grace(self):
        now = time.time()
        # 12 条老记录（早过 grace）已超容量，刚写的第 13 条 hits=0 也必须活着：
        # 否则"刚记住就被自己的淘汰规则清掉"，用户会觉得记忆功能是坏的。
        rows = [(f'old{index}', 's', 'u', f'旧记录{index}', 'confirmed', 'x', 0,
                 now - 30 * 86400, now - 30 * 86400) for index in range(12)]
        insert_rows(self.db, rows)
        digest = self.state.remember('s', 'u', '刚写的偏好', 'src', confirmed=True,
                                     maximum=3, now=now)
        kept = {row[0] for row in self.db.execute('SELECT id FROM chat_memories')}
        self.assertIn(digest, kept)
        self.assertEqual(len(kept), 10)

    def test_superseded_rows_are_never_evicted(self):
        now = time.time()
        rows = [(f'old{index}', 's', 'u', f'过期事实{index}', 'superseded', 'x', 1,
                 now - 30 * 86400, now - 30 * 86400) for index in range(12)]
        insert_rows(self.db, rows)
        self.state.remember('s', 'u', '新偏好', 'src', confirmed=True, maximum=3)
        count = self.db.execute('SELECT COUNT(*) FROM chat_memories').fetchone()[0]
        self.assertEqual(count, 13)


class RecallTests(unittest.TestCase):
    """3) 召回：去重生效、无关查询为空、返回结构不变。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = make_db(self.tmp.name)
        self.state = ChatState(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_near_duplicate_phrasings_collapse_to_one(self):
        now = time.time()
        insert_rows(self.db, [
            ('a', 's', 'u', '我喜欢乌龙茶', 'confirmed', 'x', 3, now, now),
            ('b', 's', 'u', '我喜欢乌龙茶。', 'confirmed', 'x', 2, now - 60, now - 60),
        ])
        got = self.state.recall('s', 'u', query='')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['content'], '我喜欢乌龙茶')

    def test_unrelated_query_returns_empty(self):
        now = time.time()
        insert_rows(self.db, [('a', 's', 'u', '喜欢吃西瓜', 'confirmed', 'x', 1,
                               now - 60 * 86400, now - 60 * 86400)])
        self.assertEqual(self.state.recall('s', 'u', query='我住哪'), [])

    def test_recall_keeps_the_four_documented_keys(self):
        now = time.time()
        insert_rows(self.db, [('a', 's', 'u', '住在杭州西湖区', 'confirmed', 'x', 2,
                               now - 3 * 86400, now - 3 * 86400)])
        got = self.state.recall('s', 'u', query='我住哪')
        self.assertEqual(len(got), 1)
        self.assertEqual(set(got[0]), {'id', 'content', 'status', 'evidence'})
        self.assertEqual(got[0]['id'], 'a')
        self.assertEqual(got[0]['evidence'], 2)


class HistoryBudgetTests(unittest.TestCase):
    """4) 历史超预算时跳过当前这条，继续看更早的。"""

    def setUp(self):
        self.mark = persona_key('人设')

    def row(self, mid, text, reply='回答'):
        return (json.dumps({'id': mid, 'text': text, 'kind': 'private',
                            '_persona_key': self.mark}), reply)

    def test_over_budget_turn_is_skipped_instead_of_cutting_all_older(self):
        rows = [self.row('1', '很长的当前问题' * 400, '很长的回答' * 400),
                self.row('2', '你好', '在的')]
        ids = []
        result = build_history(rows, self.mark, 100, included_ids=ids)
        self.assertEqual(ids, ['2'])
        self.assertEqual(len(result), 2)
        self.assertIn('你好', result[0]['content'])

    def test_history_stays_chronological_and_records_real_ids(self):
        rows = [self.row('3', '第三句'), self.row('2', '第二句'), self.row('1', '第一句')]
        ids = []
        result = build_history(rows, self.mark, 6000, included_ids=ids)
        self.assertEqual(ids, ['3', '2', '1'])
        self.assertIn('第一句', result[0]['content'])
        self.assertIn('第三句', result[-2]['content'])

    def test_preserve_latest_still_keeps_the_newest_turn(self):
        rows = [self.row('1', '只有这一轮' * 200, '也只有这一句' * 200)]
        result = build_history(rows, self.mark, 100, preserve_latest=True)
        self.assertEqual(len(result), 2)
        self.assertLessEqual(sum(len(item['content']) for item in result), 100)


class PersonaConflictTests(unittest.TestCase):
    """5) 人设冲突：读得出来，且启动时写进日志。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.builtin = self.root / 'styles'
        self.custom = self.root / 'styles' / 'custom'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, name, display):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({'name': name, 'display_name': display,
                                        'description': '', 'system_prompt': '你是' + display},
                                       allow_unicode=True), encoding='utf-8')

    def manager(self, same_name=True):
        self.write(self.builtin / 'whale.yaml', 'whale', '鲸鱼娘')
        duplicate = self.custom / 'whale.yaml'
        if same_name:
            self.write(duplicate, 'whale', '鲸鱼娘')
        elif duplicate.exists():
            # 同一用例里先造冲突再造无冲突：残留文件必须清掉，否则冲突还在
            duplicate.unlink()
        manager = StyleManager(str(self.builtin), str(self.custom), None)
        manager.load_all()
        return manager

    def test_same_name_conflict_is_visible_with_kind_and_name(self):
        conflicts = self.manager().conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['kind'], 'same_name')
        self.assertEqual(conflicts[0]['name'], 'whale')
        self.assertEqual(conflicts[0]['current'], 'custom')

    def test_conflicts_returns_a_copy_and_is_empty_without_duplicates(self):
        manager = self.manager()
        manager.conflicts()[0]['name'] = '被改掉了'
        self.assertEqual(manager.conflicts()[0]['name'], 'whale')
        self.assertEqual(self.manager(same_name=False).conflicts(), [])

    def test_startup_logs_one_line_when_conflicts_exist(self):
        from src.wechat_app import WeChatRuntime
        notes = []
        bot = SimpleNamespace(style=self.manager(), note=notes.append)
        WeChatRuntime._note_persona_conflicts(bot)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith('人设冲突 1 条：'))
        self.assertIn('whale', notes[0])
        self.assertIn('自定义覆盖内置', notes[0])

    def test_startup_writes_nothing_without_conflicts(self):
        from src.wechat_app import WeChatRuntime
        notes = []
        bot = SimpleNamespace(style=self.manager(same_name=False), note=notes.append)
        WeChatRuntime._note_persona_conflicts(bot)
        self.assertEqual(notes, [])


if __name__ == '__main__':
    unittest.main()
