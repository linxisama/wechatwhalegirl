"""监听水位基线回归（缺陷：重启窗口内到达的消息被初始水位永久吃掉）。

库 `Listener` 只在**成功投递**之后才 `save_watermark()`：冷启动一条消息都没投递就退出时
`listener_watermark.json` 根本不存在；下一个 `Listener` 于是把每个会话重新播种成
「当前最新 seq」，而增量查询是 `sort_seq > since` —— 停机窗口里到达的那条消息 seq 恰好
等于该值，永久跳过。

这里用库自己的 `wechatauto.db.Listener` + 假库跑真实时序：启动 → 进程被硬杀（不调
`stop()`，所以库不会兜底落盘）→ 停机期间到达一条消息 → 再次启动 → 回调必须收到它。
全程不连真实微信库、不解密任何 .db、不发网络请求。
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

WORK = Path(__file__).resolve().parent
ROOT = WORK.parent
sys.path.insert(0, str(ROOT))

from wechatauto.db import Listener as RealListener

from src.wechat_bridge import WeChatBridge

WATERMARK_NAME = 'listener_watermark.json'


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return predicate()


def read_watermarks(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class FakeWeChatDB:
    """只读库替身：会话列表 / 最新 N 条（sort_seq 降序）/ 增量（严格大于 since）。

    轮询线程和用例线程会同时读，所以每次读取都在锁内拷一份。
    """

    def __init__(self, sessions=None):
        self._lock = threading.Lock()
        self._rows = {user: [dict(row) for row in rows] for user, rows in (sessions or {}).items()}

    def push(self, user, seq, content='fixture'):
        with self._lock:
            self._rows.setdefault(user, []).append({'sort_seq': seq, 'content': content})

    def get_sessions(self, limit=500):
        with self._lock:
            return [{'username': user} for user in sorted(self._rows)[:limit]]

    def get_messages(self, user, limit=20, offset=0):
        with self._lock:
            rows = sorted(self._rows.get(user, []), key=lambda row: row['sort_seq'], reverse=True)
            return [dict(row) for row in rows[offset:offset + limit]]

    def get_new_messages(self, user, since_seq=0, limit=200):
        with self._lock:
            rows = sorted(self._rows.get(user, []), key=lambda row: row['sort_seq'])
            return [dict(row) for row in rows if row['sort_seq'] > since_seq][:limit]


class BridgeCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='listener-continuity-1340-')
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def make_bridge(self, db):
        bridge = WeChatBridge(self.folder / 'data', 'mock')
        bridge.backend = 'wechatauto'        # 只借后端分派；mock 连接态不连微信
        bridge._db = db
        bridge.display_name = lambda username: str(username or '')
        self.addCleanup(bridge.stop_receiving)
        return bridge

    def watch(self, bridge):
        seen = []
        delivered = threading.Event()

        def callback(raw):
            seen.append((raw.get('username'), raw.get('sort_seq')))
            delivered.set()

        return callback, seen, delivered

    def start(self, bridge, callback):
        ok, why = bridge.start_listening(callback)
        self.assertTrue(ok, '监听启动失败：' + str(why))
        return bridge._listener

    def watermark_file(self, bridge):
        return bridge.data_dir / WATERMARK_NAME

    def crash(self, bridge):
        """模拟进程被硬杀：只让轮询线程死掉，**不调** Listener.stop()。

        stop() 自己会 save_watermark(force=True)，用它来模拟重启会把本用例要验证的
        「启动时就把播种基线写下去」偷换成「停止时兜底写下去」，那样不修也能过。
        """
        listener, bridge._listener = bridge._listener, None
        if listener is None:
            return
        stop_event = getattr(listener, '_stop', None)
        if stop_event is not None:
            stop_event.set()
        thread = getattr(listener, '_thread', None)
        if thread is not None:
            thread.join(5)


class SeededBaselineTests(BridgeCase):
    def test_first_start_persists_seeded_baseline_and_skips_history(self):
        """① 启动后基线立即落盘 ③ 全新安装不重放启动前的历史。"""
        db = FakeWeChatDB({'wxid_seed_1340': [{'sort_seq': 1000, 'content': '更早的历史'},
                                              {'sort_seq': 1001, 'content': '启动前最新'}]})
        bridge = self.make_bridge(db)
        callback, seen, _delivered = self.watch(bridge)
        self.start(bridge, callback)

        path = self.watermark_file(bridge)
        self.assertTrue(path.exists(),
                        '启动后没有立刻写下水位基线：下一个 Listener 会把会话重新播种成最新 seq')
        payload = read_watermarks(path)
        self.assertEqual(payload.get('wxid_seed_1340'), 1001,
                         '基线应等于启动瞬间的最新 seq：' + repr(payload))
        time.sleep(.7)                  # 至少跑过两轮 0.25s 轮询
        self.assertEqual(seen, [], '全新安装不该重放启动前的历史消息：' + repr(seen))


class RestartWindowTests(BridgeCase):
    def test_message_arriving_while_stopped_is_delivered_after_restart(self):
        """② 核心断言：停机窗口里到达的消息，重启后必须被投递。"""
        db = FakeWeChatDB({'wxid_restart_1340': [{'sort_seq': 500, 'content': '旧消息'}]})
        bridge = self.make_bridge(db)
        callback, seen, delivered = self.watch(bridge)
        self.start(bridge, callback)

        path = self.watermark_file(bridge)
        self.crash(bridge)                          # 进程没了：无 stop()、无投递、无兜底落盘
        db.push('wxid_restart_1340', 501, content='停机期间到达')
        self.start(bridge, callback)                # 新 Listener：这次必须从 500 继续

        self.assertTrue(delivered.wait(5),
                        '停机窗口里到达的消息被初始水位吃掉了：回调未触发，水位文件='
                        + repr(read_watermarks(path)))
        self.assertEqual(seen, [('wxid_restart_1340', 501)],
                         '重启后应恰好投递那一条窗口消息：' + repr(seen))

    def test_graceful_restart_does_not_replay_a_message_already_delivered(self):
        """优雅停止之后重启，不能把已经回复过的消息再投一次。

        硬杀（进程直接消失）按库的语义是 at-least-once：投递后 1s 内的提交会被
        persist_interval 节流，没来得及落盘的那一条重启后会再投一次（宁可重复不可丢），
        所以这里只断言正常运行路径上的优雅停止。
        """
        db = FakeWeChatDB({'wxid_once_1340': [{'sort_seq': 700, 'content': '旧消息'}]})
        bridge = self.make_bridge(db)
        callback, seen, delivered = self.watch(bridge)
        listener = self.start(bridge, callback)

        db.push('wxid_once_1340', 701, content='运行中到达')
        self.assertTrue(delivered.wait(5), '运行中到达的消息没投递：' + repr(seen))
        self.assertTrue(wait_until(lambda: listener.watermark.get('wxid_once_1340') == 701),
                        '投递后水位没有推进：' + repr(listener.watermark))
        bridge.stop_receiving()          # 优雅停止：Listener.stop() 自己会 force 落盘

        self.start(bridge, callback)
        time.sleep(.7)
        self.assertEqual(seen, [('wxid_once_1340', 701)],
                         '重启后重复投递了已经回复过的消息：' + repr(seen))


class BaselineContractTests(BridgeCase):
    def test_baseline_is_forced_to_disk_after_seeding_and_before_polling_starts(self):
        events = []

        class SpyListener:
            def __init__(self, db, **kwargs):
                self.watermark_file = kwargs.get('watermark_file')
                events.append('init')

            def add_all(self, callback, discover=True):
                events.append(('add_all', discover))

            def save_watermark(self, force=False):
                events.append(('save_watermark', force))

            def start(self):
                events.append('start')

            def stop(self):
                events.append('stop')

        bridge = self.make_bridge(FakeWeChatDB())
        callback, _seen, _delivered = self.watch(bridge)
        with patch('wechatauto.db.Listener', SpyListener):
            self.start(bridge, callback)

        self.assertEqual(events, ['init', ('add_all', True), ('save_watermark', True), 'start'],
                         '基线必须在播种之后、轮询启动之前强制（force=True）落盘：' + repr(events))

    def test_startup_survives_a_listener_without_save_watermark(self):
        class NoSaveListener(RealListener):
            save_watermark = None       # 等价于「库没有这个方法」

        db = FakeWeChatDB({'wxid_nosave_1340': [{'sort_seq': 1, 'content': '旧消息'}]})
        bridge = self.make_bridge(db)
        callback, seen, _delivered = self.watch(bridge)
        with patch('wechatauto.db.Listener', NoSaveListener):
            self.start(bridge, callback)        # 守卫：缺方法时不能抛 AttributeError
        self.assertIsInstance(bridge._listener, NoSaveListener)
        self.assertTrue(bridge.is_receiving(),
                        '库没有 save_watermark 时监听仍必须正常启动：' + repr(seen))


if __name__ == '__main__':
    unittest.main(verbosity=2)
