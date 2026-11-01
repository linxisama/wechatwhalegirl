"""1.3.76：升级后学习库空白的老机器要能自己补回来（用户 2026-11-01 反馈）。

旧版把群聊正文存成 `wxid_xxx:\n内容`，学习逻辑见换行整条跳过，但消息已经写进
learning_messages；于是 backfill_candidates 因为"活动窗口非空"永远跳过，rebuild 又因为
INSERT OR IGNORE 拿到 changed=0 空转。这里钉住：①窗口非空也要补；②走 relearn 直接重新提炼；
③重复跑不翻倍；④总览给出可点的下一步；⑤status.memory 同时报候选数。
"""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.local_learning import LocalLearning
from src.wechat_app import WeChatRuntime

ROOT = Path(__file__).resolve().parent.parent


class _Conversations:
    def __init__(self, rooms, records):
        self._rooms = rooms
        self._records = records

    def rooms(self, runtime):
        return self._rooms

    def records(self, runtime, kind, target, query='', limit=100, offset=0, since=0, before=None):
        return self._records.get(target, []), len(self._records.get(target, []))


def _record(mid, sender, text, created):
    return {'id': mid, 'sender_id': sender, 'sender_name': sender, 'content': text,
            'created': created, 'persona': '小夜'}


class RelearnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(Path(self.tmp.name) / 'm.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.learning = LocalLearning(self.db)
        self.notes = []

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _runtime(self, rooms, records):
        return SimpleNamespace(
            mode='wechat', account=lambda: 'acc1', style=SimpleNamespace(active_name='小夜'),
            conversations=_Conversations(rooms, records), plugins=SimpleNamespace(_plugins={}),
            bridge=SimpleNamespace(self_id='bot'), note=self.notes.append)

    def _window(self, target='g@chatroom', mid='m1'):
        self.db.execute('INSERT INTO learning_messages VALUES(?,?,?,?,?,?,?,?,?)',
                        ('wechat', 'acc1', 'group', target, mid, 'alice', 'alice', '哈哈哈哈哈', 1.0))
        self.db.commit()

    def test_room_with_window_but_no_items_is_still_a_candidate(self):
        """窗口里有消息、学习结果一条都没有 → 必须仍然算补学候选（旧版在这里永久跳过）。"""
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 3}]
        runtime = self._runtime(rooms, {})
        self._window()
        self.assertEqual(self.learning.backfill_candidates(runtime), [('group', 'g@chatroom')])

    def test_room_with_learned_items_is_skipped(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 3}]
        records = {'g@chatroom': [_record('m1', 'alice', '我喜欢吃西瓜', 1.0)]}
        runtime = self._runtime(rooms, records)
        self.learning.rebuild(runtime, 'group', 'g@chatroom')
        self.assertEqual(self.learning.backfill_candidates(runtime), [])

    def test_backfill_one_prefers_relearn_when_window_is_full(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 3}]
        runtime = self._runtime(rooms, {})
        self._window()
        calls = []
        self.learning.relearn = lambda *a, **k: calls.append((a, k)) or 9
        self.assertEqual(self.learning.backfill_one(runtime, 'group', 'g@chatroom'), 9)
        self.assertEqual(len(calls), 1)

    def test_relearn_recovers_group_text_with_sender_prefix(self):
        """升级前的老群聊正文带 `wxid_x:\n` 前缀：重新提炼要能学出来。"""
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 2}]
        records = {'g@chatroom': [_record('m1', 'alice', 'wxid_2rc4z7dz591922:\n我喜欢吃西瓜', 1.0),
                                  _record('m2', 'bob', 'wxid_2rc4z7dz591922:\n我不喜欢香菜', 2.0)]}
        runtime = self._runtime(rooms, records)
        self.assertEqual(self.learning.relearn(runtime, 'group', 'g@chatroom'), 2)
        items = self.learning.view(runtime, 'group', 'g@chatroom')['items']
        self.assertEqual(len(items), 2)

    def test_relearn_twice_does_not_double_count(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 1}]
        records = {'g@chatroom': [_record('m1', 'alice', '我喜欢吃西瓜', 1.0)]}
        runtime = self._runtime(rooms, records)
        self.learning.relearn(runtime, 'group', 'g@chatroom')
        self.learning.relearn(runtime, 'group', 'g@chatroom')
        items = self.learning.view(runtime, 'group', 'g@chatroom')['items']
        self.assertEqual([(item['content'], item['hits']) for item in items], [('我喜欢吃西瓜', 1)])

    def test_overview_reports_pending_archive(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 5}]
        runtime = self._runtime(rooms, {})
        room = self.learning.overview(runtime)['rooms'][0] if isinstance(
            self.learning.overview(runtime), dict) else self.learning.overview(runtime)[0]
        self.assertEqual(room['pending'], 5)
        self.assertIn('还没提炼', room['hint'])

    def test_overview_clears_pending_after_learning(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 1}]
        records = {'g@chatroom': [_record('m1', 'alice', '我喜欢吃西瓜', 1.0)]}
        runtime = self._runtime(rooms, records)
        self.learning.relearn(runtime, 'group', 'g@chatroom')
        room = self.learning.overview(runtime)[0]
        self.assertEqual(room['pending'], 0)
        self.assertEqual(room['hint'], '')


class StatusMemoryCandidateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        shutil.copytree(ROOT / 'styles', self.root / 'styles')
        config = self.root / 'config.wechat.yaml'
        config.write_text((ROOT / 'config.wechat.yaml').read_text('utf-8'), 'utf-8')
        self.bot = WeChatRuntime(root=self.root, config_path=config,
                                 db_path=self.root / 'messages.sqlite3')

    def tearDown(self):
        try:
            import asyncio
            asyncio.run(self.bot.close())
        except Exception:
            pass
        self._tmp.cleanup()

    def test_status_memory_counts_candidates_and_total(self):
        self.bot.learning.db.execute(
            "INSERT INTO learning_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ('id1', 'wechat', 'acc', 'group', 'g', 'alice', 'expression', '嗯哼', 'candidate',
             1, '[]', 1.0, 1.0, '小夜'))
        self.bot.learning.db.commit()
        data = self.bot.status()['memory']
        self.assertEqual(data['learning_candidate'], 1)
        self.assertEqual(data['learning_total'], 1)
        self.assertEqual(data['learned'], 0)


if __name__ == '__main__':
    unittest.main()
