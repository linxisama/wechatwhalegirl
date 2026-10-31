"""接收链路两处缺陷的离线用例（全部替身，不读真实微信库、不发消息）。

缺陷 A：新会话的第一条消息被初始水位吃掉。
  库 `Listener.add_listener` 首次见到会话时用 `db.get_messages(user, limit=1)` 的
  sort_seq 当水位，而增量查询是 `sort_seq > since`；让会话"出现"的那条消息恰好等于水位，
  于是永远取不到。这里直接用库里真实的 `wechatauto.db.Listener` 跑，避免只测自己的替身。

缺陷 B：`stop_receiving` 只置停止标志，不回收监听线程 → 僵尸监听。
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

WORK = Path(__file__).resolve().parent
ROOT = WORK.parent
sys.path.insert(0, str(ROOT))

from wechatauto.db import Listener as RealListener

from src.wechat_bridge import WeChatBridge
from src.wechat_listener import ListenerDatabase


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return predicate()


class FakeWeChatDB:
    """只读库替身：会话列表、最新 N 条（sort_seq 降序）、增量（严格大于 since）。"""

    def __init__(self, sessions=None):
        self._rows = {user: [dict(row) for row in rows] for user, rows in (sessions or {}).items()}
        self.calls = []

    def push(self, user, seq, content='fixture'):
        """模拟一条新入站消息；新会话第一次 push 就等于"运行中出现了这个会话"。"""
        self._rows.setdefault(user, []).append({'sort_seq': seq, 'content': content})

    def get_sessions(self, limit=500):
        return [{'username': user} for user in sorted(self._rows)[:limit]]

    def get_messages(self, user, limit=20, offset=0):
        self.calls.append(('get_messages', user, limit, offset))
        rows = sorted(self._rows.get(user, []), key=lambda row: row['sort_seq'], reverse=True)
        return [dict(row) for row in rows[offset:offset + limit]]

    def get_new_messages(self, user, since_seq=0, limit=200):
        self.calls.append(('get_new_messages', user, since_seq, limit))
        rows = [dict(row) for row in sorted(self._rows.get(user, []), key=lambda row: row['sort_seq'])
                if row['sort_seq'] > since_seq]
        return rows[:limit]


class ListenerCase(unittest.TestCase):
    def setUp(self):
        self.listeners = []
        self.addCleanup(self.cleanup_listeners)

    def cleanup_listeners(self):
        for listener in self.listeners:
            listener.stop()

    def watched(self, db, listen_private=True, listen_groups=True):
        listener = RealListener(ListenerDatabase(db, [], listen_private, listen_groups),
                                interval=.01, watermark_file="", max_retries=0, retry_delay=0)
        self.listeners.append(listener)
        return listener


class NewSessionFirstMessageTests(ListenerCase):
    def test_first_message_of_a_session_discovered_while_running_is_delivered_once(self):
        db = FakeWeChatDB({'wxid_existing_1338': [{'sort_seq': 50, 'content': 'old'}]})
        listener = self.watched(db)
        seen, delivered = [], threading.Event()

        def on_message(msg, _listener):
            seen.append((msg.get('username'), msg.get('sort_seq')))
            delivered.set()

        listener.add_all(on_message, discover=True)      # 启动快照里只有老会话
        db.push('wxid_new_1338', 100)                    # 新朋友的第一条消息
        listener._poll_once()
        self.assertTrue(delivered.wait(3), '新会话的第一条消息被水位吃掉了：回调未触发')
        self.assertEqual(seen, [('wxid_new_1338', 100)])
        # 投递后水位必须正常推进，否则下一轮会重复投递。
        self.assertTrue(wait_until(lambda: listener.watermark.get('wxid_new_1338') == 100),
                        '投递后水位没有推进到该条消息：' + repr(listener.watermark))
        listener._poll_once()
        self.assertEqual(len(seen), 1, '同一条消息被重复投递：' + repr(seen))

    def test_first_message_of_a_new_group_is_delivered_once(self):
        db = FakeWeChatDB({'wxid_existing_1338': [{'sort_seq': 50, 'content': 'old'}]})
        listener = self.watched(db)
        seen, delivered = [], threading.Event()

        def on_message(msg, _listener):
            seen.append((msg.get('username'), msg.get('sort_seq')))
            delivered.set()

        listener.add_all(on_message, discover=True)
        db.push('13380@chatroom', 300, content='大家好')   # 新群的第一条消息
        listener._poll_once()
        self.assertTrue(delivered.wait(3), '新群的第一条消息被水位吃掉了：回调未触发')
        self.assertEqual(seen, [('13380@chatroom', 300)])
        self.assertTrue(wait_until(lambda: listener.watermark.get('13380@chatroom') == 300))
        listener._poll_once()
        self.assertEqual(len(seen), 1, '同一条消息被重复投递：' + repr(seen))

    def test_sessions_that_existed_at_startup_still_skip_history(self):
        db = FakeWeChatDB({'wxid_friend_1338': [{'sort_seq': 40, 'content': 'older'},
                                                {'sort_seq': 50, 'content': 'oldest-not-replayed'}]})
        listener = self.watched(db)
        seen = []
        listener.add_all(lambda msg, _listener: seen.append(msg.get('sort_seq')), discover=True)
        listener._poll_once()
        listener._poll_once()
        self.assertEqual(seen, [], '启动时已存在的会话不应重放历史消息：' + repr(seen))
        db.push('wxid_friend_1338', 51, content='new')
        listener._poll_once()
        self.assertTrue(wait_until(lambda: seen == [51]), '启动后到达的新消息未投递：' + repr(seen))

    def test_watermark_file_session_is_not_replayed_again(self):
        """持久化水位命中的会话（上次运行已推进过）不能被本用例的修复重放一次。"""
        db = FakeWeChatDB({'wxid_known_1338': [{'sort_seq': 10}, {'sort_seq': 20}]})
        with tempfile.TemporaryDirectory(prefix='inbound-watermark-1338-') as folder:
            watermark = str(Path(folder) / 'listener_watermark.json')
            Path(watermark).write_text('{"wxid_known_1338": 20}', encoding='utf-8')
            listener = RealListener(ListenerDatabase(db, [], True, True), interval=.01,
                                    watermark_file=watermark, max_retries=0, retry_delay=0)
            self.listeners.append(listener)
            seen = []
            listener.add_all(lambda msg, _listener: seen.append(msg.get('sort_seq')), discover=True)
            listener._poll_once()
            listener._poll_once()
            self.assertEqual(seen, [], '已有持久化水位的会话被重放了：' + repr(seen))


class ListenerDatabaseContractTests(unittest.TestCase):
    def test_unknown_attribute_and_incremental_queries_still_delegate(self):
        db = FakeWeChatDB({'wxid_a_1338': [{'sort_seq': 1}, {'sort_seq': 2}]})
        db.extra_marker = 'forwarded'
        watched = ListenerDatabase(db, [], True, True)
        self.assertEqual(watched.extra_marker, 'forwarded')
        self.assertEqual([row['sort_seq'] for row in watched.get_messages('wxid_a_1338', limit=5)], [2, 1])
        self.assertEqual([row['sort_seq'] for row in watched.get_new_messages('wxid_a_1338', since_seq=1)], [2])
        self.assertEqual(watched.get_new_messages('wxid_a_1338', since_seq=2), [])

    def test_session_filtering_still_respects_private_and_group_switches(self):
        db = FakeWeChatDB({'wxid_p_1338': [{'sort_seq': 1}], '13380@chatroom': [{'sort_seq': 2}]})
        self.assertEqual([s['username'] for s in ListenerDatabase(db, [], False, True).get_sessions()],
                         ['13380@chatroom'])
        self.assertEqual([s['username'] for s in ListenerDatabase(db, [], True, False).get_sessions()],
                         ['wxid_p_1338'])


class StopReceivingReclaimsListenerTests(unittest.TestCase):
    def bridge(self):
        folder = tempfile.TemporaryDirectory(prefix='inbound-stop-1338-')
        self.addCleanup(folder.cleanup)
        return WeChatBridge(Path(folder.name) / 'data', 'mock')

    def test_stop_receiving_stops_the_listener_once_and_clears_it(self):
        bridge = self.bridge()
        fake = SimpleNamespace(stop=Mock())
        bridge._listener = fake
        bridge.stop_receiving()
        fake.stop.assert_called_once_with()
        self.assertIsNone(bridge._listener)
        bridge.stop_receiving()                 # 幂等：重复停止不再调 stop、不抛错
        fake.stop.assert_called_once_with()
        self.assertIsNone(bridge._listener)
        bridge.stop_listening()                 # 之后 stop_listening 仍然可用
        fake.stop.assert_called_once_with()
        self.assertIsNone(bridge._listener)

    def test_listener_stop_failure_is_swallowed_and_reference_cleared(self):
        bridge = self.bridge()

        def boom():
            raise RuntimeError('监听线程回收失败')

        bridge._listener = SimpleNamespace(stop=boom)
        bridge.stop_receiving()
        self.assertIsNone(bridge._listener)
        self.assertTrue(bridge._poll_stop.is_set(), '回收失败也不能丢掉"已停止接收"的语义')

    def test_stop_listening_stops_a_live_listener_exactly_once(self):
        bridge = self.bridge()
        fake = SimpleNamespace(stop=Mock())
        bridge._listener = fake
        bridge.stop_listening()
        fake.stop.assert_called_once_with()
        self.assertIsNone(bridge._listener)
        bridge.stop_listening()
        fake.stop.assert_called_once_with()
        self.assertIsNone(bridge._listener)

    def test_stop_receiving_leaves_no_receiving_listener_behind(self):
        bridge = self.bridge()
        stop_event = threading.Event()
        fake = SimpleNamespace(_thread=SimpleNamespace(is_alive=lambda: True),
                               _stop=stop_event, stop=Mock())
        bridge._listener = fake
        bridge.backend = 'wechatauto'
        self.assertTrue(bridge.is_receiving())
        bridge.stop_receiving()
        self.assertFalse(bridge.is_receiving())
        self.assertIsNone(bridge._listener, '停止接收后不应继续持有监听对象')
        fake.stop.assert_called_once_with()


if __name__ == '__main__':
    unittest.main(verbosity=2)
