"""隔离轮询韧性：单次超时只退避重试，预算耗尽或协议错误才拆监听（1340）。"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import src.isolated_wechat.host_bridge as hostmod
from src.isolated_wechat.host_bridge import SandboxBridge
from src.isolated_wechat.protocol import Pairing, ProtocolError, write_document

STATUS_ON = {'online': True, 'listening': True, 'self_id': 'wxid_fixture_self',
             'account': 'fixture', 'send_environment': {}}
STALE_HEARTBEAT = {'epoch': 'c' * 32, 'active': True, 'heartbeat': 1.0}


class FakeStop:
    """最小停止事件：不花真实时间，并记录轮询与退避等待。"""

    def __init__(self):
        self.flag = False
        self.waits = []

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.flag


class FakeClock:
    """替换模块级 time：每次读 monotonic 前进一格，故障预算可精确复现。"""

    def __init__(self, start=1000.0, step=1.0):
        self.now = start
        self.step = step
        self.sleeps = []

    def monotonic(self):
        self.now += self.step
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def time(self):
        return self.now


class PollResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = SandboxBridge(Path(self.temp.name) / 'data', Path(self.temp.name) / 'absent')
        self.host.online = self.host._listening = True
        self.host._receive_generation = 7
        self.host._poll_stop = threading.Event()
        self.host._poll_thread = threading.current_thread()
        self.host._pairing = {'channel_id': '0' * 32}
        self.host._epoch = '1' * 32
        self.host._service = Mock(return_value=(None, {'status': {}}))
        self.host.note = Mock()

    def drive(self, rpc):
        """在宿主线程内同步驱动一轮监听；返回时循环已按停止或拆线结束。"""
        stop = FakeStop()
        self.host._poll_stop = stop
        self.host._poll_thread = threading.current_thread()
        self.host._rpc = rpc
        self.host._poll_remote(stop, 7)
        return stop

    def notes(self):
        return [call.args[0] for call in self.host.note.call_args_list]

    def channel(self, service=None):
        root = Path(self.temp.name) / 'channel'
        for name in ('requests', 'responses', 'consumed', 'events', 'control'):
            (root / name).mkdir(parents=True, exist_ok=True)
        pairing = Pairing({'version': 1, 'channel_id': 'a' * 32, 'secret': 'b' * 64}, root)
        if service is not None:
            write_document(root / 'service.json', pairing, service)
        self.host._pairing = pairing
        self.host._epoch = None
        return pairing

    def test_repeated_timeouts_retry_without_teardown_and_note_recovery_once(self):
        calls, healthy = [], []

        def rpc(action, args, timeout):
            calls.append(timeout)
            if len(calls) <= 3:
                raise hostmod.RpcTimeout('隔离请求超时，本次不重试')
            # 超时风暴期间监听必须仍然在线、线程代次仍然有效。
            healthy.append((self.host.online, self.host._listening, self.host.is_receiving()))
            if len(calls) == 5:
                self.host._poll_stop.set()
            return {'status': dict(STATUS_ON), 'events': []}

        with patch.object(hostmod, 'POLL_TIMEOUT', 7.5), patch.object(hostmod, 'POLL_INTERVAL', .01):
            stop = self.drive(rpc)
        self.assertEqual(len(calls), 5)
        self.assertEqual(set(calls), {7.5})
        self.assertEqual(healthy, [(True, True, True), (True, True, True)])
        self.assertIs(self.host.online, True)
        self.assertEqual(self.host.last_error, '')
        self.assertEqual(self.notes(), ['隔离通道响应变慢，正在重试（第 1 次）', '隔离通道已恢复'])
        self.assertFalse([note for note in self.notes() if note.startswith('隔离监听已停止：')])
        self.assertEqual(stop.waits, [.5, 1.0, 1.5, .01])
        self.assertFalse(self.host._listening)

    def test_poll_thread_survives_timeout_storm_and_keeps_receiving(self):
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def rpc(action, args, timeout):
            calls.append(timeout)
            if len(calls) <= 3:
                raise hostmod.RpcTimeout('隔离响应核验超时，本次不重试')
            entered.set()
            self.assertTrue(release.wait(3))
            return {'status': dict(STATUS_ON), 'events': []}

        self.host._poll_stop = stop
        self.host._rpc = rpc
        thread = threading.Thread(target=self.host._poll_remote, args=(stop, 7), daemon=True)
        self.host._poll_thread = thread
        with patch.object(hostmod, 'POLL_INTERVAL', .01), \
                patch.object(hostmod, 'POLL_RETRY_DELAY', .01), \
                patch.object(hostmod, 'POLL_RETRY_MAX_DELAY', .02):
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(thread.is_alive())
                self.assertTrue(self.host.online)
                self.assertTrue(self.host._listening)
                self.assertTrue(self.host.is_receiving())
                self.assertEqual(self.notes(), ['隔离通道响应变慢，正在重试（第 1 次）'])
            finally:
                release.set()
                stop.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_timeout_budget_exhausted_tears_down_once_with_original_notice(self):
        clock, calls = FakeClock(), []

        def rpc(action, args, timeout):
            calls.append(timeout)
            self.assertLess(len(calls), 20, '超时预算未生效，循环在空转')
            raise hostmod.RpcTimeout('隔离请求超时，本次不重试')

        with patch.object(hostmod, 'time', clock), \
                patch.object(hostmod, 'POLL_TIMEOUT_BUDGET', 3.0), \
                patch.object(hostmod, 'POLL_RETRY_DELAY', .01), \
                patch.object(hostmod, 'POLL_RETRY_MAX_DELAY', .02):
            stop = self.drive(rpc)
        self.assertEqual(len(calls), 4)
        self.assertIs(self.host.online, False)
        self.assertIs(self.host._listening, False)
        self.assertFalse(self.host.is_receiving())
        self.assertEqual(self.host.last_error, '隔离请求超时，本次不重试')
        self.assertEqual(self.notes(), ['隔离通道响应变慢，正在重试（第 1 次）',
                                        '隔离监听已停止：隔离请求超时，本次不重试'])
        self.assertEqual(stop.waits, [.01, .02, .02])

    def test_protocol_error_tears_down_immediately_without_retry(self):
        calls = []

        def rpc(action, args, timeout):
            calls.append(timeout)
            raise ProtocolError('隔离事件水位不连续，停止接收以免漏消息')

        self.drive(rpc)
        self.assertEqual(len(calls), 1)
        self.assertIs(self.host.online, False)
        self.assertIs(self.host._listening, False)
        self.assertEqual(self.host.last_error, '隔离事件水位不连续，停止接收以免漏消息')
        self.assertEqual(self.notes(), ['隔离监听已停止：隔离事件水位不连续，停止接收以免漏消息'])

    def test_guest_reports_listening_false_still_stops_as_before(self):
        calls = []

        def rpc(action, args, timeout):
            calls.append(timeout)
            return {'status': dict(STATUS_ON, listening=False), 'events': []}

        self.drive(rpc)
        self.assertEqual(len(calls), 1)
        self.assertIs(self.host.online, False)
        self.assertEqual(self.host.last_error, '客体消息监听已停止，等待重连')
        self.assertEqual(self.notes(), ['隔离监听已停止：客体消息监听已停止，等待重连'])

    def test_busy_keeps_retrying_without_teardown_or_noise(self):
        calls = []

        def rpc(action, args, timeout):
            calls.append(timeout)
            if len(calls) > 3:
                self.host._poll_stop.set()
            raise hostmod.RpcBusy('隔离通道正忙，本次未投递请求')

        stop = self.drive(rpc)
        self.assertEqual(len(calls), 4)
        self.assertIs(self.host.online, True)
        self.assertEqual(self.host.last_error, '')
        self.assertEqual(self.notes(), [])
        self.assertEqual(stop.waits, [.1, .1, .1])

    def test_rpc_timeout_is_retryable_but_protocol_failures_stay_fatal(self):
        self.assertTrue(issubclass(hostmod.RpcTimeout, hostmod.RpcError))
        del self.host._service  # 用真实服务核验：缺心跳文件与心跳过期必须区分
        self.channel()
        with self.assertRaises(hostmod.RpcTimeout) as transient:
            self.host._rpc('poll', {'ack': 0}, timeout=.2)
        self.assertIn('隔离通道不可用（', str(transient.exception))

        self.channel(dict(STALE_HEARTBEAT))
        with self.assertRaises(hostmod.RpcError) as fatal:
            self.host._rpc('poll', {'ack': 0}, timeout=.2)
        self.assertNotIsInstance(fatal.exception, hostmod.RpcTimeout)
        self.assertIn('隔离通道不可用（ProtocolError）', str(fatal.exception))


if __name__ == '__main__':
    unittest.main()
