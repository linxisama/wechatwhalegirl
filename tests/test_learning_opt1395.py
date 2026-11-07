"""学习系统四项优化（1.3.95 候选）：表达自动批准、按价值淘汰、view/overview 查询次数。

钉住的缺陷（改动前实测）：
① expression 永远停在 candidate，recall 只读 approved —— 自动学习开着，AI 也拿不到语气样本；
② 200 条淘汰在同状态内是 FIFO，被复证 20 次的老条目先于刚建的 hits=1 条目被删；
③ view() 每条一次 SELECT name（200 条 = 203 次查询）；
④ overview() 每个房间两次查询（50 个房间 = 100 次）。

为什么自动批准默认关闭：tests/test_learning_156.py:60-63 已经钉住"表达必须人工批准"这条
既有行为，现有测试一个字节都不许改；能力由 conversation_memory.expressionApproveHits
显式打开，打开后仍要过长度与噪声门槛，且 rejected 永远是终态。
"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.local_learning import LocalLearning, expression_quality
from src.plugins.conversation_memory import ConversationMemory

GROUP = '12345678@chatroom'


class _Plugin:
    """替身插件：只提供 _settings 用到的 get() 与 enabled。"""

    def __init__(self, **values):
        self._values = values
        self.enabled = True

    def get(self, key, default=None):
        return self._values.get(key, default)


class _Conversations:
    """替身归档：overview 只用 rooms，recall 只用 search_terms。"""

    def __init__(self, rooms=None, records=None, terms=()):
        self._rooms = list(rooms or [])
        self._records = dict(records or {})
        self._terms = list(terms)

    def rooms(self, runtime):
        return self._rooms

    def records(self, runtime, kind, target, query='', limit=100, offset=0, since=0, before=None):
        rows = self._records.get(target, [])
        return rows, len(rows)

    def search_terms(self, text):
        return list(self._terms)


def runtime_of(persona='小夜', rooms=None, records=None, terms=(), plugins=None):
    return SimpleNamespace(mode='wechat', account=lambda: 'acc1',
                           style=SimpleNamespace(active_name=persona),
                           conversations=_Conversations(rooms, records, terms),
                           plugins=SimpleNamespace(_plugins=dict(plugins or {})),
                           bridge=SimpleNamespace(self_id='bot'), note=lambda text: None)


def message_of(index, text, sender='wxid_a'):
    return dict(id=str(index), kind='group', target=GROUP, sender_id=sender,
                sender_name=sender, text=text, created=1000.0 + index)


class _LearningCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = sqlite3.connect(Path(self.tmp.name) / 'm.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.learn = LocalLearning(self.db)

    def tearDown(self):
        try:
            self.db.close()
        finally:
            self.tmp.cleanup()

    def observe(self, runtime, index, text, sender='wxid_a'):
        self.learn.observe(message_of(index, text, sender), runtime)

    def items(self, runtime=None):
        return self.learn.view(runtime or runtime_of(), 'group', GROUP)['items']

    def status_of(self, runtime, content):
        return next((item['status'] for item in self.items(runtime) if item['content'] == content), None)

    def insert_item(self, identity, status='approved', hits=1, updated=None, content='内容',
                    category='expression', persona='小夜'):
        updated = time.time() if updated is None else updated
        self.db.execute('INSERT INTO learning_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (identity, 'wechat', 'acc1', 'group', GROUP, 'wxid_a', category, content,
                         status, hits, '[]', updated, updated, persona))

    def count_items(self):
        return self.db.execute('SELECT count(*) FROM learning_items').fetchone()[0]

    def ids(self):
        return {row[0] for row in self.db.execute('SELECT id FROM learning_items')}

    def count_queries(self, action):
        calls = {'n': 0}

        def trace(statement):
            calls['n'] += 1

        self.db.set_trace_callback(trace)
        try:
            value = action()
        finally:
            self.db.set_trace_callback(None)
        return value, calls['n']


class ExpressionQualityTests(unittest.TestCase):
    def test_length_bounds_and_noise_are_rejected(self):
        self.assertFalse(expression_quality('好耶呀'))
        self.assertTrue(expression_quality('走吧我们去看看'))
        self.assertFalse(expression_quality('字' * 33))
        self.assertFalse(expression_quality('看这个 https://a.cn/x'))
        self.assertFalse(expression_quality('发我 abc@qq.com'))
        self.assertFalse(expression_quality('订单 20250101083'))
        self.assertFalse(expression_quality(''))


class ExpressionApprovalTests(_LearningCase):
    def test_plugin_ships_the_switch_off_by_default(self):
        """默认必须与旧行为一致，否则 test_learning_156 会红。"""
        self.assertEqual(ConversationMemory.META.default_settings['expressionApproveHits'], 0)
        self.assertIn('expressionApproveHits', ConversationMemory.META.config_schema)

    def test_default_off_keeps_repeated_expression_as_candidate(self):
        runtime = runtime_of()
        for index in range(3):
            self.observe(runtime, index, '好耶，出发啦')
        self.assertEqual(self.status_of(runtime, '好耶，出发啦'), 'candidate')
        self.assertEqual(self.learn.recall(message_of(9, '出发啦'), runtime), [])

    def test_enabled_approves_only_at_configured_hits(self):
        runtime = runtime_of(plugins={'conversation_memory': _Plugin(expressionApproveHits=3)})
        self.observe(runtime, 1, '走吧我们去看看')
        self.observe(runtime, 2, '走吧我们去看看')
        self.assertEqual(self.status_of(runtime, '走吧我们去看看'), 'candidate')
        self.observe(runtime, 3, '走吧我们去看看')
        self.assertEqual(self.status_of(runtime, '走吧我们去看看'), 'approved')
        self.assertTrue(self.learn.recall(message_of(9, '走吧'), runtime))

    def test_short_expression_is_never_auto_approved(self):
        runtime = runtime_of(plugins={'conversation_memory': _Plugin(expressionApproveHits=2)})
        for index in range(4):
            self.observe(runtime, index, '好耶呀')
        self.assertEqual(self.status_of(runtime, '好耶呀'), 'candidate')

    def test_rejected_is_terminal_even_when_hits_keep_growing(self):
        runtime = runtime_of(plugins={'conversation_memory': _Plugin(expressionApproveHits=2)})
        self.observe(runtime, 1, '我们去吃点什么吧')
        identity = self.items(runtime)[0]['id']
        self.learn.change(runtime, 'group', GROUP, identity, 'reject')
        for index in (2, 3, 4):
            self.observe(runtime, index, '我们去吃点什么吧')
        self.assertEqual(self.status_of(runtime, '我们去吃点什么吧'), 'rejected')
        self.assertEqual(self.learn.recall(message_of(9, '吃点什么'), runtime), [])

    def test_invalid_threshold_falls_back_to_off(self):
        for value in ('abc', 1, 99, None, 0, -3):
            runtime = runtime_of(plugins={'conversation_memory': _Plugin(expressionApproveHits=value)})
            self.assertEqual(self.learn.expression_threshold(runtime), 0, value)
        self.assertEqual(self.learn.expression_threshold(runtime_of()), 0)
        enabled = runtime_of(plugins={'conversation_memory': _Plugin(expressionApproveHits=3)})
        self.assertEqual(self.learn.expression_threshold(enabled), 3)


class ValueEvictionTests(_LearningCase):
    def _learn_fact(self, times=2):
        """先把条目学成 approved 再塞满表，最后补一次复证触发淘汰。

        不能等到表满了才开始学：新条目第一跳是 candidate，按价值排序必然垫底，
        一落库就被自己这条淘汰掉，测到的就不是排序而是"新候选活不下来"。
        """
        runtime = runtime_of()
        for index in range(1, times + 1):
            self.observe(runtime, index, '我喜欢吃西瓜')
        self.assertEqual(self.status_of(runtime, '我喜欢吃西瓜'), 'approved')
        return runtime

    def test_high_hits_old_item_survives_fresh_low_hits_item(self):
        """验收点：hits=20/很旧 与 hits=1/很新 两条 approved，超 200 条时新的先被删。"""
        runtime = self._learn_fact()
        now = time.time()
        # 填充 hits=2：学到的那条已是 hits=2、再来一次就是 3，
        # 淘汰必须落在 hits=1 的 fresh 上，否则测到的只是"新条目自己被删"。
        for index in range(198):
            self.insert_item('filler%d' % index, hits=2, updated=now - 1000 - index)
        self.insert_item('keeper', hits=20, updated=now - 86400 * 60)
        self.insert_item('fresh', hits=1, updated=now)
        self.db.commit()
        # 198 填充 + keeper + fresh + 已学的那条 = 201，下一次复证必然要淘汰一条。
        self.assertEqual(self.count_items(), 201)
        self.observe(runtime, 3, '我喜欢吃西瓜')
        ids = self.ids()
        self.assertIn('keeper', ids)
        self.assertNotIn('fresh', ids)
        self.assertEqual(len(ids), 200)

    def test_approved_outranks_candidate_regardless_of_hits(self):
        runtime = self._learn_fact()
        now = time.time()
        for index in range(199):
            self.insert_item('c%d' % index, status='candidate', hits=50, updated=now - index)
        self.insert_item('low', status='approved', hits=1, updated=now - 99999)
        self.db.commit()
        self.observe(runtime, 3, '我喜欢吃西瓜')
        self.assertIn('low', self.ids())
        self.assertEqual(self.count_items(), 200)

    def test_equal_hits_keeps_the_newer_entry(self):
        runtime = self._learn_fact()
        now = time.time()
        for index in range(199):
            self.insert_item('e%d' % index, hits=3, updated=now - index)
        self.insert_item('oldest', hits=3, updated=now - 99999)
        self.db.commit()
        self.observe(runtime, 3, '我喜欢吃西瓜')
        ids = self.ids()
        self.assertIn('e0', ids)
        self.assertNotIn('oldest', ids)


class ViewQueryCountTests(_LearningCase):
    def _seed(self, count=200):
        now = time.time()
        for index in range(count):
            self.db.execute('INSERT INTO learning_messages VALUES(?,?,?,?,?,?,?,?,?)',
                            ('wechat', 'acc1', 'group', GROUP, 'm%d' % index, 'wxid_a', '小田',
                             '普通消息%d' % index, now - index))
            self.insert_item('id%d' % index, hits=1, updated=now - index,
                             content='这是第%d种说法' % index)
        self.db.commit()

    def test_view_query_count_is_constant(self):
        self._seed(200)
        view, queries = self.count_queries(lambda: self.learn.view(runtime_of(), 'group', GROUP))
        self.assertEqual(len(view['items']), 200)
        self.assertLessEqual(queries, 5, 'view 仍是 N+1：200 条用了 %d 次查询' % queries)

    def test_subject_name_uses_the_latest_message(self):
        now = time.time()
        for mid, name, created in (('m1', '旧名字', now - 10), ('m2', '新名字', now)):
            self.db.execute('INSERT INTO learning_messages VALUES(?,?,?,?,?,?,?,?,?)',
                            ('wechat', 'acc1', 'group', GROUP, mid, 'wxid_a', name, '消息', created))
        self.insert_item('a', content='第一句', hits=1, updated=now)
        self.db.commit()
        self.assertEqual(self.items()[0]['subject_name'], '新名字')

    def test_missing_or_blank_name_falls_back_to_subject_id(self):
        self.insert_item('b', content='第二句', hits=1)
        self.assertEqual(self.items()[0]['subject_name'], 'wxid_a')
        self.db.execute('INSERT INTO learning_messages VALUES(?,?,?,?,?,?,?,?,?)',
                        ('wechat', 'acc1', 'group', GROUP, 'm9', 'wxid_a', '', '消息', time.time() + 10))
        self.db.commit()
        self.assertEqual(self.items()[0]['subject_name'], 'wxid_a')

    def test_persona_counts_survive_the_batch_lookup(self):
        self.insert_item('mine', hits=1, persona='小夜')
        self.insert_item('theirs', hits=1, persona='别人设')
        self.db.commit()
        view = self.learn.view(runtime_of('小夜'), 'group', GROUP)
        self.assertEqual([item['id'] for item in view['items']], ['mine'])
        self.assertEqual((view['persona_items'], view['other_persona_items']), (1, 1))


class OverviewQueryCountTests(_LearningCase):
    def _rooms(self, count=50):
        rooms = [{'kind': 'group', 'target': 'r%d@chatroom' % index, 'count': index}
                 for index in range(count)]
        for index in range(count):
            target = 'r%d@chatroom' % index
            self.db.execute('INSERT INTO learning_messages VALUES(?,?,?,?,?,?,?,?,?)',
                            ('wechat', 'acc1', 'group', target, 'm%d' % index, 'wxid_a', '小田',
                             '消息%d' % index, 1.0))
            self.db.execute('INSERT INTO learning_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            ('x%d' % index, 'wechat', 'acc1', 'group', target, 'wxid_a', 'fact',
                             '我喜欢吃西瓜', 'approved', 2, '[]', 1.0, 1.0, '小夜'))
        self.db.commit()
        return rooms

    def test_overview_query_count_is_constant(self):
        rooms = self._rooms(50)
        value, queries = self.count_queries(lambda: self.learn.overview(runtime_of(rooms=rooms)))
        self.assertEqual(len(value), 50)
        self.assertLessEqual(queries, 4, 'overview 仍是每房间两次查询：50 个房间用了 %d 次' % queries)

    def test_overview_numbers_are_unchanged(self):
        rooms = self._rooms(3)
        value = self.learn.overview(runtime_of(rooms=rooms))
        self.assertEqual([room['facts'] for room in value], [1, 1, 1])
        self.assertEqual([room['window'] for room in value], [1, 1, 1])
        self.assertEqual([room['approved'] for room in value], [1, 1, 1])
        self.assertEqual([room['pending'] for room in value], [0, 0, 0])
        self.assertEqual([room['hint'] for room in value], ['', '', ''])

    def test_overview_hides_other_personas_but_counts_them(self):
        rooms = [{'kind': 'group', 'target': 'r0@chatroom', 'count': 1}]
        self.db.execute('INSERT INTO learning_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('y', 'wechat', 'acc1', 'group', 'r0@chatroom', 'wxid_a', 'fact',
                         '我喜欢喝牛奶', 'approved', 2, '[]', 1.0, 1.0, '别人设'))
        self.db.commit()
        room = self.learn.overview(runtime_of(rooms=rooms))[0]
        self.assertEqual((room['facts'], room['persona_items'], room['other_persona_items']), (0, 0, 1))
        self.assertEqual(room['personas'], {'别人设': 1})
        self.assertIn('其他人设', room['hint'])


if __name__ == '__main__':
    unittest.main()