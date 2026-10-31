"""隔离代理超时判定与请求交接竞态；可控时钟/线程，不启动进程或访问微信。"""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from src.isolated_wechat import guest_service as guest
from tests import test_isolated_transport1315 as fixtures


class OneWatchCycle:
    def __init__(self):
        self.calls = 0
        self.stopped = False

    def wait(self, _seconds):
        self.calls += 1
        return self.stopped or self.calls > 1

    def set(self):
        self.stopped = True

    def is_set(self):
        return self.stopped


class ObservedControlLock:
    def __init__(self, progress):
        self.lock = threading.Lock()
        self.progress = progress

    def __enter__(self):
        if threading.current_thread().name == 'new-readonly-request':
            self.progress.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


class GuestLeaseTests(fixtures.ChannelCase):
    def test_cold_readonly_completion_starts_idle_lease_at_completion_without_resuming(self):
        service = self.service()
        started = time.time()
        clock = [started]
        self.request(service, 'connect')
        def cold_connect():
            clock[0] = started + 51
            service.bridge.online = True
            return True, '假冷连接结束'
        service.bridge.connect = cold_connect
        with patch.object(guest.time, 'time', side_effect=lambda: clock[0]):
            self.assertTrue(service.process_once())
        self.assertEqual(service._busy_deadline, 0)
        self.assertEqual(service._client_touch, started + 51)
        self.assertEqual(service.bridge.resumed, 0)
        service._stop = OneWatchCycle()
        service._heartbeat_at = float('inf')
        with patch.object(guest.time, 'time', return_value=started + 51.1):
            service._watch()
        self.assertEqual(service.bridge.cancelled, 0)

    def test_expired_snapshot_cannot_cancel_the_next_readonly_worker(self):
        service = self.service()
        now = time.time()
        service._client_touch = now - 100
        service._heartbeat_at = float('inf')
        service._stop = OneWatchCycle()
        self.request(service, 'inspect_environment')
        snapshot_taken, release_snapshot = threading.Event(), threading.Event()
        request_progress, inspecting, release_inspect = threading.Event(), threading.Event(), threading.Event()
        service._control_lock = ObservedControlLock(request_progress)
        watch_calls = [0]
        cancelled_during_inspect, errors = [], []

        def clock():
            if threading.current_thread().name == 'old-expiry-watch':
                watch_calls[0] += 1
                # 两个 control 槽各校验一次时间；第三次是租约比较。
                if watch_calls[0] == 3:
                    snapshot_taken.set()
                    if not release_snapshot.wait(2):
                        raise AssertionError('旧超时判断未获释放')
            return now

        def inspect(_who):
            inspecting.set()
            request_progress.set()
            if not release_inspect.wait(2):
                raise AssertionError('只读替身未获释放')
            return {'ready': True}

        def cancel():
            service.bridge.cancelled += 1
            cancelled_during_inspect.append(inspecting.is_set())
            release_inspect.set()

        def run(action):
            try:
                action()
            except BaseException as error:
                errors.append(error)

        service.bridge.inspect_send_environment = inspect
        service.bridge.cancel_pending_sends = cancel
        with patch.object(guest.time, 'time', side_effect=clock):
            watcher = threading.Thread(target=lambda: run(service._watch), name='old-expiry-watch')
            caller = threading.Thread(target=lambda: run(service.process_once), name='new-readonly-request')
            try:
                watcher.start()
                self.assertTrue(snapshot_taken.wait(1))
                caller.start()
                self.assertTrue(request_progress.wait(1))
            finally:
                release_snapshot.set()
                watcher.join(2)
                release_inspect.set()
                if caller.ident is not None:
                    caller.join(2)
        self.assertFalse(watcher.is_alive())
        self.assertFalse(caller.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(inspecting.is_set())
        self.assertEqual(cancelled_during_inspect, [False])
        self.assertEqual(service.bridge.resumed, 0)

    def test_marker_write_failure_also_finishes_lease_without_dispatch(self):
        service = self.service()
        now = time.time()
        self.request(service, 'connect')
        service.bridge.connect = Mock(side_effect=AssertionError('落盘失败不可连接'))
        with patch.object(guest, 'write_document', side_effect=OSError('fixture full disk')):
            self.assertFalse(service.process_once())
        self.assertEqual(service._busy_deadline, 0)
        self.assertGreaterEqual(service._client_touch, now)
        service.bridge.connect.assert_not_called()

    def test_expired_busy_deadline_still_cancels_without_extending_send_lease(self):
        service = self.service()
        now = time.time()
        service._client_touch = now
        service._busy_deadline = now - 1
        service._stop = OneWatchCycle()
        service._heartbeat_at = float('inf')
        service.bridge.listening = True
        with patch.object(guest.time, 'time', return_value=now):
            service._watch()
        self.assertEqual(service.bridge.cancelled, 1)
        self.assertFalse(service.bridge.listening)
        self.assertEqual(service._busy_deadline, now - 1)
        self.assertEqual(service.bridge.resumed, 0)

    def test_explicit_stop_during_readonly_is_not_undone_by_finishing_the_lease(self):
        service = self.service()
        self.request(service, 'inspect_environment')
        entered, release = threading.Event(), threading.Event()
        stopped = []
        def inspect(_who):
            entered.set()
            if not release.wait(2):
                raise AssertionError('只读替身未获释放')
            return {'ready': False, 'waiting_code': 'cancelled'}
        def cancel():
            service.bridge.cancelled += 1
            stopped.append(True)
            release.set()
        service.bridge.inspect_send_environment = inspect
        service.bridge.cancel_pending_sends = cancel
        errors = []
        def run():
            try:
                service.process_once()
            except BaseException as error:
                errors.append(error)
        caller = threading.Thread(target=run)
        watcher = threading.Thread(target=service._watch)
        try:
            caller.start()
            self.assertTrue(entered.wait(1))
            now = time.time()
            guest.write_document(service.root / 'control/stop.json', service.pairing,
                {'epoch': service.epoch, 'action': 'stop', 'request_id': guest.uuid.uuid4().hex,
                 'created': now, 'deadline': now + 30})
            watcher.start()
            self.assertTrue(service._stop.wait(1))
        finally:
            release.set()
            service._stop.set()
            caller.join(2)
            if watcher.ident is not None:
                watcher.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(caller.is_alive())
        self.assertFalse(watcher.is_alive())
        self.assertTrue(stopped)
        self.assertEqual(service._busy_deadline, 0)
        self.assertTrue(service._stop.is_set())
        self.assertEqual(service.bridge.resumed, 0)
        # 停止后即使队列中还有旧的 resume 请求，也不能进入 dispatch。
        self.request(service, 'resume_sends')
        self.assertFalse(service.process_once())
        self.assertEqual(service.bridge.resumed, 0)
