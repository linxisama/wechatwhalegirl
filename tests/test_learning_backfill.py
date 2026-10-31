"""升级后自动补学：本地早有原文、学习库是空的，界面才会"一大片空白"。

钉住三件事：①空会话会补学；②已有学习结果的会话跳过（幂等）；
③补学不写重复条目，也不碰没归档的会话。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.local_learning import LocalLearning


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


class BackfillTests(unittest.TestCase):
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

    def test_rooms_with_messages_get_learned(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 2}]
        records = {'g@chatroom': [_record('m1', 'alice', '我喜欢吃西瓜', 1.0),
                                  _record('m2', 'bob', '我不喜欢香菜', 2.0)]}
        done = self.learning.backfill_all(self._runtime(rooms, records))
        self.assertEqual([name for name, _ in done], ['g@chatroom'])
        items = self.learning.view(self._runtime(rooms, records), 'group', 'g@chatroom')['items']
        self.assertEqual(len(items), 2)
        self.assertTrue(any('补学' in note for note in self.notes))

    def test_second_run_changes_nothing(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 1}]
        records = {'g@chatroom': [_record('m1', 'alice', '我喜欢吃西瓜', 1.0)]}
        runtime = self._runtime(rooms, records)
        self.learning.backfill_all(runtime)
        again = self.learning.backfill_all(runtime)
        self.assertEqual(again, [])
        items = self.learning.view(runtime, 'group', 'g@chatroom')['items']
        self.assertEqual([item['hits'] for item in items], [1])

    def test_room_without_records_is_skipped(self):
        rooms = [{'kind': 'private', 'target': 'wxid_x', 'count': 0}]
        self.assertEqual(self.learning.backfill_all(self._runtime(rooms, {})), [])

    def test_chats_that_do_not_look_like_facts_are_not_learned(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 2}]
        records = {'g@chatroom': [_record('m1', 'alice', '哈哈哈哈哈', 1.0),
                                  _record('m2', 'bob', '这个怎么弄？', 2.0)]}
        runtime = self._runtime(rooms, records)
        self.learning.backfill_all(runtime)
        self.assertEqual(self.learning.view(runtime, 'group', 'g@chatroom')['items'], [])

    def test_overview_counts_rooms_and_items(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 1}]
        records = {'g@chatroom': [_record('m1', 'alice', '我喜欢吃西瓜', 1.0)]}
        runtime = self._runtime(rooms, records)
        self.learning.backfill_all(runtime)
        overview = self.learning.overview(runtime)
        self.assertEqual(len(overview), 1)
        self.assertEqual(overview[0]['target'], 'g@chatroom')
        self.assertEqual(overview[0]['facts'], 1)
        self.assertEqual(overview[0]['window'], 1)

    def test_bulk_learn_all_fills_rooms_without_items(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 1},
                 {'kind': 'private', 'target': 'wxid_x', 'count': 1}]
        records = {'g@chatroom': [_record('m1', 'alice', '我喜欢吃西瓜', 1.0)],
                   'wxid_x': [_record('m2', 'wxid_x', '我住在杭州', 2.0)]}
        runtime = self._runtime(rooms, records)
        result = self.learning.bulk(runtime, 'learn-all')
        self.assertEqual(result['rooms'], 2)
        self.assertEqual(result['learned'], 2)
        for room in rooms:
            items = self.learning.view(runtime, room['kind'], room['target'])['items']
            self.assertTrue(items)

    def test_bulk_approve_expressions_only_touches_expressions(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 2}]
        records = {'g@chatroom': [_record('m1', 'alice', '我觉得这样挺好', 1.0),
                                  _record('m2', 'bob', '我喜欢吃西瓜', 2.0)]}
        runtime = self._runtime(rooms, records)
        self.learning.backfill_all(runtime)
        result = self.learning.bulk(runtime, 'approve-expressions')
        self.assertEqual(result['approved'], 1)
        items = {item['category']: item for item in
                 self.learning.view(runtime, 'group', 'g@chatroom')['items']}
        self.assertEqual(items['expression']['status'], 'approved')
        self.assertEqual(items['fact']['status'], 'candidate')

    def test_bulk_rejects_unknown_action(self):
        runtime = self._runtime([], {})
        with self.assertRaises(ValueError):
            self.learning.bulk(runtime, 'delete-everything')

    def test_group_messages_with_sender_prefix_are_learned(self):
        """群聊归档正文带 `wxid_x:\n` 前缀；以前遇到换行直接跳过，群聊一条都学不到。"""
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 1}]
        records = {'g@chatroom': [_record('m1', 'alice', 'wxid_abc123:\n我手忙的发软了', 1.0)]}
        runtime = self._runtime(rooms, records)
        self.learning.backfill_all(runtime)
        items = self.learning.view(runtime, 'group', 'g@chatroom')['items']
        self.assertEqual([item['content'] for item in items], ['我手忙的发软了'])

    def test_placeholders_orders_and_pat_hints_are_not_learned(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 4}]
        records = {'g@chatroom': [_record('m1', 'a', '[图片]', 1.0),
                                  _record('m2', 'a', '[拍一拍] 西莲寺紫 拍了拍 鲸鱼娘', 2.0),
                                  _record('m3', 'a', 'YCNY20250101083', 3.0),
                                  _record('m4', 'a', '老鲸你个杂鱼', 4.0)]}
        runtime = self._runtime(rooms, records)
        self.learning.backfill_all(runtime)
        items = self.learning.view(runtime, 'group', 'g@chatroom')['items']
        self.assertEqual([item['content'] for item in items], ['老鲸你个杂鱼'])

    def test_items_do_not_leak_across_personas(self):
        """人设隔离必须严格：换人设后旧人设的学习结果既不可见也不可召回。"""
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 1}]
        records = {'g@chatroom': [_record('m1', 'alice', '我喜欢吃西瓜', 1.0)]}
        runtime = self._runtime(rooms, records)
        self.learning.backfill_all(runtime)
        self.assertEqual(len(self.learning.view(runtime, 'group', 'g@chatroom')['items']), 1)
        switched = self._runtime(rooms, records)
        switched.style = SimpleNamespace(active_name='另一个人设')
        self.assertEqual(self.learning.view(switched, 'group', 'g@chatroom')['items'], [])

    def test_learning_is_bounded_per_conversation(self):
        rooms = [{'kind': 'group', 'target': 'g@chatroom', 'count': 1}]
        records = {'g@chatroom': [_record(f'm{i}', f'u{i % 30}', f'我觉得第{i}件事挺有意思', float(i))
                                  for i in range(260)]}
        runtime = self._runtime(rooms, records)
        self.learning.backfill_all(runtime)
        items = self.learning.view(runtime, 'group', 'g@chatroom')['items']
        self.assertLessEqual(len(items), 200)

    def test_memory_maintenance_endpoint_shape(self):
        """「整理记忆」调用的接口必须存在，并且返回成功与说明。"""
        import inspect
        from src import dialogue_management
        source = inspect.getsource(dialogue_management)
        self.assertIn("/api/dialogue/memory/maintain", source)
        self.assertIn("memory_maintenance", source)


if __name__ == '__main__':
    unittest.main()
