"""隔离代理失联首帧的状态回归；只构造临时对象，不启动真实收发。"""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from src.isolated_wechat.host_bridge import SandboxBridge
from src.isolated_wechat.protocol import ProtocolError
from src.wechat_bridge import WeChatBridge


class _InertAliveThread:
    """仅供 is_receiving 查询，绝不创建或启动真实线程。"""

    def is_alive(self):
        return True


class IsolatedStatusSnapshot1337Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='whale-status1337-')
        self.addCleanup(temporary.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.forbidden = []
        # 这些测试不需要进程、网络或真实接收线程，走偏即失败而非触碰现场。
        for name in ('subprocess.Popen', 'subprocess.run', 'socket.socket.connect',
                     'socket.socket.connect_ex', 'socket.create_connection',
                     'threading.Thread.start'):
            self.forbidden.append(self.stack.enter_context(
                patch(name, side_effect=AssertionError('forbidden side effect: ' + name))))
        self.stack.enter_context(patch('src.wechat_bridge.is_admin', return_value=False))
        root = Path(temporary.name)
        self.bridge = SandboxBridge(root / 'data', root / 'channel')
        for method in ('_rpc', 'connect', 'reconnect', 'start_listening', 'send_text',
                       'inspect_send_environment'):
            self.forbidden.append(self.stack.enter_context(patch.object(
                self.bridge, method,
                side_effect=AssertionError('forbidden bridge action: ' + method))))
        self.bridge.online = True
        self.bridge._listening = True
        self.bridge._poll_thread = _InertAliveThread()
        self.bridge.send_environment = {
            'ready': True, 'reason': 'readonly_check_passed',
            'execution_environment': 'sandbox', 'current_chat_only': True,
        }

    def tearDown(self):
        for guard in self.forbidden:
            guard.assert_not_called()

    def unavailable(self, exception=ProtocolError):
        return patch.object(self.bridge, '_service',
                            side_effect=exception('inert expired service'))

    def available(self):
        return patch.object(self.bridge, '_service',
                            return_value=(None, {'status': {'safety': {'active': False}}}))

    def assert_disconnected(self, snapshot):
        self.assertIs(snapshot['online'], False)
        self.assertIs(snapshot['isolated_agent_available'], False)
        self.assertIs(snapshot['listening'], False)
        self.assertEqual(snapshot['read_state'], 'disconnected')
        self.assertEqual(snapshot['send_state'], 'disconnected')
        self.assertEqual(snapshot['send_code'], 'isolated_unavailable')
        self.assertIs(snapshot['send_environment']['ready'], False)

    def test_first_expired_service_snapshot_is_consistently_disconnected(self):
        with self.unavailable():
            self.assert_disconnected(self.bridge.status())

    def test_first_inaccessible_service_snapshot_is_consistently_disconnected(self):
        with self.unavailable(OSError):
            self.assert_disconnected(self.bridge.status())

    def test_repeated_unavailable_snapshots_are_identical(self):
        with self.unavailable():
            snapshots = [self.bridge.status() for _ in range(3)]
        for snapshot in snapshots:
            self.assert_disconnected(snapshot)
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[1], snapshots[2])

    def test_normal_ready_snapshot_retains_base_fields_and_sandbox_contract(self):
        expected = WeChatBridge.status(self.bridge)
        expected.update(
            backend='sandbox', native_backend='', execution_environment='sandbox',
            current_chat_only=True, text_only=True, isolated_agent_available=True,
            guest_safety={'active': False}, scope='current_open_chat',
            listening=True, read_state='listening',
        )
        with self.available():
            actual = self.bridge.status()
        self.assertEqual(actual, expected)
        self.assertIs(actual['send_environment']['ready'], True)
        self.assertEqual(actual['send_state'], 'ready')

    def test_ready_snapshot_environment_is_not_a_live_mutable_reference(self):
        with self.available():
            snapshot = self.bridge.status()
        self.assertIsNot(snapshot['send_environment'], self.bridge.send_environment)
        snapshot['send_environment'].update(ready=False, reason='edited_export')
        self.assertEqual(self.bridge.send_environment['reason'], 'readonly_check_passed')
        self.assertIs(self.bridge.send_environment['ready'], True)

    def test_unavailable_snapshots_are_independent_and_preserve_cached_check(self):
        with self.unavailable():
            first, second = self.bridge.status(), self.bridge.status()
        self.assertIsNot(first['send_environment'], second['send_environment'])
        self.assertIsNot(first['send_environment'], self.bridge.send_environment)
        first['send_environment']['reason'] = 'edited_first_export'
        self.assertNotEqual(second['send_environment']['reason'], 'edited_first_export')
        self.assertEqual(self.bridge.send_environment['reason'], 'readonly_check_passed')

    def test_online_waiting_environment_is_unchanged(self):
        self.bridge.send_environment = {
            'ready': False, 'waiting_code': 'draft_not_empty', 'reason': 'keep_draft',
        }
        with self.available():
            snapshot = self.bridge.status()
        self.assertIs(snapshot['online'], True)
        self.assertEqual(snapshot['send_state'], 'waiting')
        self.assertEqual(snapshot['send_code'], 'draft_not_empty')
        self.assertEqual(snapshot['send_environment'], self.bridge.send_environment)

    def test_already_offline_unavailable_snapshot_stays_disconnected(self):
        self.bridge.online = False
        with self.unavailable():
            self.assert_disconnected(self.bridge.status())

    def test_new_service_alone_cannot_restore_old_read_or_send_readiness(self):
        with self.unavailable():
            self.bridge.status()
        with self.available():
            snapshot = self.bridge.status()
        self.assertIs(snapshot['isolated_agent_available'], True)
        self.assertIs(snapshot['online'], False)
        self.assertIs(snapshot['send_environment']['ready'], False)
        self.assertIs(snapshot['listening'], False)


if __name__ == '__main__':
    unittest.main()
