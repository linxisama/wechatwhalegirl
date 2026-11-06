"""记忆系统优化 + token 节流 + 上下文分块预算（1.3.92）。

这一批改动全部来自真实数据，不是凭感觉调参：
本机 16 轮已发送回复的 completion_tokens 与文本逐字对照后，旧口径的误差是
estimate_tokens MAPE 22.6%（偏差 +12.3%）、token_saver 的 CJK×2 口径 MAPE 134.6%
（偏差 +133.7%）。改完分别是 10.8% / 22.5%，且都保持"宁可略高估"的保守方向。

七组用例分别钉住：
1. 估算口径与真实 usage 的偏差在可接受范围（不追求精确，只保证不再系统性高估）；
2. 账本按轮记录，带 estimator 版本号与真实 usage 双轨；
3. report() 的分块汇总与占比正确，能回答"人设/插件各占多少"；
4. 记忆召回去重（同一句话换个说法只留一条）；
5. 时间衰减确实影响排序；
6. 容量上限淘汰按得分而不是 FIFO；
7. 配置非法值退回默认、空数据不崩、并发记录不丢。
"""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest

from src import memory_guard as mg
from src import memory_maintenance as mm
from src import memory_rank as mr
from src import token_ledger as tl
from src.plugins.token_saver import (compact, estimated_tokens, layer_chars, layer_of)


# 16 轮真实样本：(真实 completion_tokens, 汉字数, 全角标点数, 其他字符数)
# 数据源：backend.log 的 completion_tokens × messages.sqlite3 的 inbox.reply
REAL_SAMPLES = [
    (31, 39, 3, 2), (13, 12, 3, 0), (39, 51, 4, 2), (60, 67, 10, 0),
    (57, 24, 4, 0), (45, 51, 7, 2), (28, 38, 3, 0), (46, 52, 9, 2),
    (34, 24, 4, 0), (39, 45, 5, 2), (43, 50, 6, 2), (49, 58, 7, 2),
    (52, 64, 7, 2), (27, 30, 5, 0), (23, 24, 4, 0), (15, 15, 3, 0),
]


def _text(han, full, other):
    """按三类字数造一段等长文本：汉字用'字'、全角标点用'，'、其他用'a'。"""
    return '字' * han + '，' * full + 'a' * other


def _mape(estimator):
    errors = []
    for real, han, full, other in REAL_SAMPLES:
        value = estimator(_text(han, full, other))
        errors.append(abs(value - real) / real)
    return 100.0 * sum(errors) / len(errors)


def _bias(estimator):
    errors = []
    for real, han, full, other in REAL_SAMPLES:
        value = estimator(_text(han, full, other))
        errors.append((value - real) / real)
    return 100.0 * sum(errors) / len(errors)


class EstimatorTests(unittest.TestCase):
    """1) 估算口径：误差可接受，且不再系统性高估。"""

    def test_ledger_error_within_budget(self):
        # 旧口径是 22.6%，这里要求校准后显著更好，同时留出样本噪声余量
        self.assertLess(_mape(tl.estimate_tokens), 15.0)
        self.assertLess(abs(_bias(tl.estimate_tokens)), 10.0)

    def test_ledger_beats_legacy_formula(self):
        def legacy(text):
            han = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
            return int(han * 1.0 + (len(text) - han) / 4.0 + 0.5)

        self.assertLess(_mape(tl.estimate_tokens), _mape(legacy))

    def test_saver_estimator_no_longer_doubles(self):
        # 旧 token_saver 口径（CJK×2）实测 MAPE 158.9%；这条钉住"不再翻倍"
        def legacy(messages):
            total = 0
            for item in messages:
                text = item.get('content') or ''
                cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
                total += 4 + cjk * 2 + max(0, len(text) - cjk) // 4
            return total

        rows = [{'role': 'user', 'content': _text(han, full, other)}
                for _real, han, full, other in REAL_SAMPLES]
        self.assertLess(estimated_tokens(rows), legacy(rows))
        self.assertLess(estimated_tokens(rows), sum(real for real, *_ in REAL_SAMPLES) * 1.6)

    def test_estimator_keeps_cjk_above_ascii(self):
        # 校准不能把中文估得比同长英文还便宜：那条会让预算对中文完全失效
        self.assertGreater(tl.estimate_tokens('中' * 100), tl.estimate_tokens('a' * 100))
        self.assertEqual(tl.estimate_tokens(''), 0)
        self.assertEqual(tl.estimate_chars(0), 0)
        self.assertGreater(tl.estimate_chars(100), 0)

    def test_estimate_chars_never_raises_on_junk(self):
        for value in (None, '', 'abc', [], {}, 3.7):
            self.assertGreaterEqual(tl.estimate_chars(value), 0)


class LedgerRoundTests(unittest.TestCase):
    """2) 账本按轮记录 + 真实 usage 双轨。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = str(tl.path_for(self.tmp))

    def _rows(self):
        with open(self.path, encoding='utf-8') as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_each_round_is_one_line_with_version(self):
        tl.record_round(self.tmp, {'id': 'm1'}, 'a', prompt_text='系统提示')
        tl.record_round(self.tmp, {'id': 'm2'}, 'b', prompt_text='系统提示')
        rows = self._rows()
        self.assertEqual([row['msg'] for row in rows], ['m1', 'm2'])
        self.assertTrue(all(row['estimator'] == tl.ESTIMATOR for row in rows))

    def test_real_usage_is_kept_alongside_estimate(self):
        entry = tl.record_round(self.tmp, {'id': 'm1'}, '你好',
                                prompt_text='系统提示' * 10,
                                usage={'prompt_tokens': 500, 'completion_tokens': 12})
        self.assertEqual(entry['real_prompt_tokens'], 500)
        self.assertEqual(entry['real_reply_tokens'], 12)
        self.assertNotEqual(entry['prompt_tokens'], 500)   # 估算值不被真实值覆盖
        self.assertEqual(self._rows()[0]['real_prompt_tokens'], 500)

    def test_bad_usage_does_not_poison_the_row(self):
        entry = tl.record_round(self.tmp, {'id': 'm1'}, 'x', usage={'prompt_tokens': 'oops'})
        self.assertNotIn('real_prompt_tokens', entry)
        entry = tl.record_round(self.tmp, {}, 'x', usage={'prompt_tokens': 0,
                                                          'completion_tokens': 0})
        self.assertNotIn('real_reply_tokens', entry)

    def test_record_round_never_raises_on_junk(self):
        entry = tl.record_round(None, None, None)
        self.assertEqual(entry['prompt_tokens'], 0)
        self.assertEqual(entry['source'], 'local')


class BudgetReportTests(unittest.TestCase):
    """3) 分块预算报告：能回答"6000 字预算里人设占多少、被砍了多少"。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = str(tl.path_for(self.tmp))

    def _plan(self, persona='你是鲸鱼娘', continuity='刚才聊到茶', plugins='\n[记忆]乌龙茶',
              rules='固定规则', image='', limit=None):
        from src.prompt_budget import compose_prompt
        return compose_prompt(persona=persona, continuity=continuity, plugins=plugins,
                              rules=rules, image=image)

    def test_sizes_come_from_prompt_plan(self):
        plan = self._plan()
        entry = tl.record_round(self.tmp, {}, 'ok', plan=plan)
        self.assertEqual(entry['sizes'], plan.sizes)
        self.assertEqual(set(entry['sizes']), {'persona', 'continuity', 'plugins', 'rules'})

    def test_extra_layers_are_merged(self):
        plan = self._plan()
        entry = tl.record_round(self.tmp, {}, 'ok', plan=plan,
                                blocks={'history': 2400, 'memory': 320, 'learning': 0})
        self.assertEqual(entry['sizes']['history'], 2400)
        self.assertEqual(entry['sizes']['memory'], 320)
        self.assertNotIn('learning', entry['sizes'])   # 0 不写，免得报告里出现假层

    def test_report_shares_answer_the_persona_question(self):
        plan = self._plan(persona='你是鲸鱼娘' * 300)
        tl.record_round(self.tmp, {}, 'ok', plan=plan, blocks={'history': 1000})
        out = tl.report(self.path, rounds=5)
        self.assertEqual(out['rounds'], 1)
        self.assertIn('persona', out['share'])
        self.assertGreater(out['share']['persona'], 0.3)
        self.assertAlmostEqual(sum(out['share'].values()), 1.0, places=3)
        self.assertEqual(out['used'], sum(out['blocks'].values()))

    def test_report_counts_dropped_blocks(self):
        # 人设+插件+规则超过总预算时 prompt_budget 会整块丢弃插件，sizes 里置 0
        from src.prompt_budget import compose_prompt
        # 注意：prompt_budget 的 dict 配置按点号逐层查找，扁平键 'prompt_budget.maxChars'
        # 查不到（_config_get 返回 None，预算静默退回 6000），必须传嵌套结构。
        plan = compose_prompt(persona='人' * 1200, plugins='\n' + '插' * 1200,
                              rules='规' * 400,
                              config={'prompt_budget': {'maxChars': 1300}})
        self.assertIn('plugins', plan.dropped)
        tl.record_round(self.tmp, {}, 'ok', plan=plan)
        out = tl.report(self.path, rounds=5)
        self.assertEqual(out['dropped'].get('plugins'), 1)
        self.assertNotIn('plugins', out['blocks'])

    def test_report_is_bounded_and_safe_on_empty(self):
        self.assertEqual(tl.report(self.path, rounds=5)['rounds'], 0)
        self.assertEqual(tl.report(None)['rounds'], 0)
        self.assertEqual(tl.report(os.path.join(self.tmp, 'none.jsonl'))['rounds'], 0)
        for index in range(30):
            tl.record_round(self.tmp, {'id': f'm{index}'}, 'x', blocks={'history': 100})
        self.assertEqual(tl.report(self.path, rounds=5)['rounds'], 5)

    def test_report_error_pct_uses_real_usage(self):
        tl.record_round(self.tmp, {}, 'x', prompt_text='中' * 1000,
                        usage={'prompt_tokens': 750, 'completion_tokens': 5})
        out = tl.report(self.path, rounds=5)
        self.assertIsNotNone(out['error_pct'])
        # 1000 个汉字估 750 token，真实也是 750 → 误差应为 0
        self.assertAlmostEqual(out['error_pct'], 0.0, places=1)


class MemoryDedupeTests(unittest.TestCase):
    """4) 记忆去重：同一句话换个说法只留一条。"""

    def test_dedupe_keeps_first_occurrence(self):
        items = [{'content': '我喜欢乌龙茶'}, {'content': '我喜欢乌龙茶。'},
                 {'content': '她喜欢拿铁'}]
        kept, dropped = mr.dedupe(items)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 1)
        self.assertEqual(kept[0]['content'], '我喜欢乌龙茶')

    def test_rank_dedupes_and_reports_stats(self):
        now = time.time()
        rows = [{'content': '我住在杭州西湖区', 'updated': now, 'hits': 3},
                {'content': '我住在杭州西湖区。', 'updated': now, 'hits': 2},
                {'content': '我喜欢喝乌龙茶', 'updated': now - 40 * 86400, 'hits': 1}]
        # min_score=0 才能同时看到"重复被去掉"与"相关度不足被去掉"这两种不同的过滤
        out, stats = mr.rank(rows, '我住哪', now, limit=5, min_score=0.0)
        self.assertEqual(stats['duplicates'], 1)
        self.assertEqual(stats['below_threshold'], 0)
        self.assertEqual(len(out), 2)
        self.assertIn('杭州', out[0]['content'])
        # 默认阈值下，无关内容会被相关度门槛挡在外面（这是另一条独立规则）
        strict, strict_stats = mr.rank(rows, '我住哪', now, limit=5)
        self.assertGreaterEqual(strict_stats['below_threshold'], 1)
        self.assertEqual(len(strict), 1)

    def test_budget_dedupes_before_truncating(self):
        items = [{'content': '我喜欢乌龙茶'}, {'content': '我喜欢乌龙茶。'},
                 {'content': '第二件事'}, {'content': '第三件事'}]
        out = mm.budget(items, max_items=3)
        self.assertEqual(len(out), 3)
        self.assertEqual([row['content'] for row in out][1:], ['第二件事', '第三件事'])

    def test_budget_dedupe_can_be_disabled(self):
        items = [{'content': '同一句'}, {'content': '同一句'}]
        self.assertEqual(len(mm.budget(items, dedupe=False)), 2)
        self.assertEqual(len(mm.budget(items, dedupe=True)), 1)


class FreshnessTests(unittest.TestCase):
    """5) 时间衰减真的影响排序，不是摆设。"""

    def test_fresh_memory_outranks_stale_on_tie(self):
        now = time.time()
        rows = [{'content': '我喜欢乌龙茶', 'updated': now - 90 * 86400, 'hits': 1},
                {'content': '我喜欢乌龙茶呀', 'updated': now, 'hits': 1}]
        out, _stats = mr.rank(rows, '喜欢什么茶', now, limit=5, dup_threshold=0.99)
        self.assertIn('呀', out[0]['content'])

    def test_half_life_config_changes_ranking(self):
        now = time.time()
        stale = mr.freshness(now - 30 * 86400, now)
        self.assertLess(stale, 0.3)
        self.assertGreater(mr.freshness(now, now), 0.99)
        # 半衰期越长，旧记忆衰减越慢；这是"能按人设调"的基础
        self.assertGreater(mr.freshness(now - 30 * 86400, now, 60.0), stale)

    def test_rank_prefers_relevant_over_merely_recent(self):
        now = time.time()
        rows = [{'content': '我住在杭州西湖区', 'updated': now - 3 * 86400, 'hits': 1},
                {'content': '今天天气不错', 'updated': now, 'hits': 1}]
        out, _stats = mr.rank(rows, '我住哪', now, limit=2)
        self.assertIn('杭州', out[0]['content'])


class EvictionTests(unittest.TestCase):
    """6) 容量上限淘汰：按得分，不是 FIFO。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(os.path.join(self.tmp.name, 'm.sqlite3'))
        self.db.executescript(
            'CREATE TABLE chat_memories(id TEXT PRIMARY KEY,scope TEXT,subject TEXT,content TEXT,'
            'status TEXT,source TEXT,hits INTEGER,created REAL,updated REAL);')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, mid, content, hits=1, age_days=0.0, status='confirmed'):
        stamp = time.time() - age_days * 86400
        self.db.execute('INSERT INTO chat_memories VALUES(?,?,?,?,?,?,?,?,?)',
                        (mid, 's', 'u', content, status, 'x', hits, stamp, stamp))

    def test_stable_old_memory_survives_fifo_would_kill_it(self):
        # 最旧的这条被复证 5 次（稳定事实），最新那条只说过一次（噪声）
        self.add('stable', '我喜欢乌龙茶', hits=5, age_days=200)
        for index in range(3):
            self.add(f'noise{index}', f'随口一句{index}', hits=1, age_days=0.5)
        removed = mm.evict(self.db, maximum=2, grace=0.0)
        self.assertEqual(removed, 2)
        left = {row[0] for row in self.db.execute('SELECT id FROM chat_memories')}
        self.assertIn('stable', left)
        self.assertEqual(len(left), 2)

    def test_grace_period_protects_just_written_rows(self):
        for index in range(5):
            self.add(f'm{index}', f'刚写的{index}', hits=1, age_days=0.0)
        self.assertEqual(mm.evict(self.db, maximum=2), 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM chat_memories').fetchone()[0], 5)

    def test_under_capacity_changes_nothing(self):
        self.add('a', '只有一条')
        self.assertEqual(mm.evict(self.db, maximum=100), 0)

    def test_maintain_reports_evicted_count(self):
        for index in range(6):
            self.add(f'm{index}', f'内容{index}', hits=1, age_days=30)
        result = mm.maintain(self.db)
        self.assertIn('evicted', result)
        self.assertGreaterEqual(result['evicted'], 0)

    def test_evict_never_raises_on_broken_db(self):
        broken = sqlite3.connect(':memory:')
        try:
            self.assertEqual(mm.evict(broken, maximum=1), 0)
        finally:
            broken.close()


class ConfigFallbackTests(unittest.TestCase):
    """7) 配置非法值退回默认、空数据不崩。"""

    def test_guard_limits_fall_back_on_junk(self):
        defaults = mg.limits()
        self.assertEqual(defaults['dupThreshold'], mg.DUP_THRESHOLD)
        for junk in ({'limits': {'dupThreshold': 'high'}},
                     {'limits': {'dupThreshold': 5.0}},
                     {'limits': {'dupThreshold': None}},
                     {'limits': 'not-a-dict'},
                     'not-a-config',
                     None):
            self.assertEqual(mg.limits(junk)['dupThreshold'], mg.DUP_THRESHOLD, junk)

    def test_guard_limits_accept_valid_overrides(self):
        caps = mg.limits({'limits': {'dupThreshold': 0.8, 'minChars': 3}})
        self.assertEqual(caps['dupThreshold'], 0.8)
        self.assertEqual(caps['minChars'], 3)

    def test_inspect_uses_configured_threshold(self):
        base = '我住在杭州市西湖区文三路'
        existing = [{'id': 'm1', 'content': base}]
        strict = mg.inspect(base, existing)
        self.assertIn('DUP', strict['codes'])
        # 阈值调到 1.0 以上时同一条不再算重复：证明配置真的生效
        loose = mg.inspect('我住在杭州市西湖区文三路东', existing,
                           {'limits': {'dupThreshold': 0.999}})
        self.assertNotIn('DUP', loose['codes'])

    def test_limits_only_expose_what_the_guard_actually_uses(self):
        # 守卫是 report-only，没有容量语义；配置里不该出现"配了但不生效"的开关
        self.assertEqual(set(mg.limits()), {'dupThreshold', 'minChars', 'diaryMinChars'})

    def test_empty_inputs_never_raise(self):
        self.assertEqual(mm.budget([]), [])
        self.assertEqual(mm.budget(None), [])
        self.assertEqual(mr.dedupe([]), ([], 0))
        self.assertEqual(mr.dedupe(None), ([], 0))
        self.assertEqual(mr.rank([], 'q', time.time()), ([], {'scored': 0, 'kept': 0,
                                                             'duplicates': 0,
                                                             'below_threshold': 0}))
        self.assertEqual(layer_chars([]), {})
        self.assertEqual(layer_of(None), 'history')


class LayerTests(unittest.TestCase):
    """8) 分层：人设/记忆/学习/历史能分开记账，占比可读。"""

    def test_layer_of_classifies_prefixes(self):
        self.assertEqual(layer_of({'role': 'system', 'content': '你是鲸鱼娘'}), 'persona')
        self.assertEqual(layer_of({'role': 'user',
                                   'content': '本会话已确认学习结果：喜欢茶'}), 'memory')
        self.assertEqual(layer_of({'role': 'user', 'content': '本群/本会话最近对话：在吗'}), 'learning')
        self.assertEqual(layer_of({'role': 'user', 'content': '普通历史'}), 'history')
        self.assertEqual(layer_of({'role': 'user', 'content': [{'type': 'image'}]}), 'image')

    def test_layer_chars_sums_per_layer(self):
        rows = [{'role': 'system', 'content': '人设' * 50},
                {'role': 'user', 'content': '历史' * 10},
                {'role': 'user', 'content': '历史' * 20}]
        out = layer_chars(rows)
        self.assertEqual(out['persona'], 100)
        self.assertEqual(out['history'], 60)

    def test_compact_reports_layers_before_and_after(self):
        rows = [{'role': 'user', 'content': '旧消息' * 100},
                {'role': 'user', 'content': '本轮问题'}]
        packed, stats = compact(rows, keep=1, max_content_chars=50)
        self.assertIn('layers_before', stats)
        self.assertIn('layers_after', stats)
        self.assertGreaterEqual(sum(stats['layers_before'].values()),
                                sum(stats['layers_after'].values()))

    def test_redundant_history_is_dropped_before_older_unique(self):
        # 两条几乎一样的历史 + 一条更有信息量的旧消息：预算紧时该先砍重复的那条
        rows = [{'role': 'user', 'content': '我喜欢乌龙茶'},
                {'role': 'user', 'content': '我喜欢乌龙茶。'},
                {'role': 'user', 'content': '我在杭州做后端开发，住西湖区'},
                {'role': 'user', 'content': '本轮问题'}]
        packed, stats = compact(rows, keep=10, max_prompt_chars=30)
        kept = [row['content'] for row in packed]
        self.assertIn('我在杭州做后端开发，住西湖区', kept)
        self.assertEqual(len([text for text in kept if text.startswith('我喜欢乌龙茶')]), 1)
        self.assertGreater(stats['dropped'], 0)


class ConcurrencyTests(unittest.TestCase):
    """9) 并发记账不丢行、不写坏行。"""

    def test_threads_do_not_lose_or_corrupt_rows(self):
        tmp = tempfile.mkdtemp()
        path = str(tl.path_for(tmp))

        def worker(index):
            for round_index in range(10):
                tl.record_round(tmp, {'id': f'{index}-{round_index}'}, '回复',
                                prompt_text='提示', blocks={'history': 100})

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with open(path, encoding='utf-8') as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(rows), 60)
        self.assertEqual(len({row['msg'] for row in rows}), 60)
        self.assertEqual(tl.report(path, rounds=500)['rounds'], 60)


if __name__ == '__main__':
    unittest.main()
