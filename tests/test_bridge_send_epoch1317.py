"""桥层慢解析与 RPC 排队不能跨越停止代次；全部使用假客户端和临时目录。"""
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.wechat_bridge import WeChatBridge, BACKEND_WECHATAUTO
from src.isolated_wechat.host_bridge import SandboxBridge, RpcError
from tests.test_isolated_transport1315 import ChannelCase


class ObservedLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = threading.Event()
        self.lock.acquire()

    def acquire(self, *args, **kwargs):
        self.waiting.set()
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class NativeBridgeSendEpochTests(ChannelCase):
    def test_name_resolution_started_before_disconnect_cannot_use_the_reconnected_client(self):
        bridge = WeChatBridge(self.root / 'reconnect-native')
        bridge.backend = BACKEND_WECHATAUTO
        bridge.online = True
        bridge._wx = Mock()
        entered, release = threading.Event(), threading.Event()
        def name(*_):
            entered.set()
            if not release.wait(2):
                raise AssertionError('blocked name was not released')
            return '当前会话'
        bridge._chat_name = name
        results = []
        worker = threading.Thread(target=lambda: results.append(bridge.send_text('wxid_friend', '旧正文')))
        worker.start()
        self.assertTrue(entered.wait(1))
        bridge.disconnect()
        replacement = Mock()
        replacement.SendMsg.return_value = True
        bridge._wx = replacement
        bridge.online = True  # 仅模拟新的连接，绝不加载微信库。
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(results[0][0])
        self.assertEqual(results[0].diagnostic['submission_state'], 'not_submitted')
        replacement.SendMsg.assert_not_called()
        self.assertTrue(bridge.send_text('wxid_friend', '新正文')[0])

    def test_text_and_file_name_resolution_cannot_revive_after_stop_and_resume(self):
        for kind in ('text', 'file'):
            with self.subTest(kind=kind):
                bridge = WeChatBridge(self.root / ('native-' + kind))
                bridge.backend = BACKEND_WECHATAUTO
                bridge.online = True
                bridge._wx = Mock()
                entered, release = threading.Event(), threading.Event()
                def name(*_):
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError('blocked name was not released')
                    return '当前会话'
                bridge._chat_name = name
                attachment = self.root / '附件.txt'
                attachment.write_text('离线附件', 'utf-8')
                operation = (lambda: bridge.send_text('wxid_friend', '旧正文')) if kind == 'text' else (
                    lambda: bridge.send_files('wxid_friend', [str(attachment)]))
                results = []
                worker = threading.Thread(target=lambda: results.append(operation()))
                worker.start()
                self.assertTrue(entered.wait(1))
                bridge.cancel_pending_sends()
                bridge.resume_sends()
                replacement = Mock()
                replacement.SendMsg.return_value = True
                replacement.SendFiles.return_value = True
                bridge._wx = replacement
                release.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertFalse(results[0][0])
                self.assertEqual(results[0].diagnostic['submission_state'], 'not_submitted')
                replacement.SendMsg.assert_not_called()
                replacement.SendFiles.assert_not_called()
                self.assertTrue(operation()[0])


class SandboxBridgeSendEpochTests(ChannelCase):
    def host(self):
        service = self.service()
        service.heartbeat()
        host = SandboxBridge(self.root / 'host-data', self.channel)
        host.online = True
        host._control = Mock()
        return host

    def resume_without_guest(self, host):
        with patch.object(host, '_rpc', return_value={'ok': True}):
            host.resume_sends()

    def test_send_waiting_for_send_lock_is_not_posted_after_stop_and_resume(self):
        host = self.host()
        lock = ObservedLock()
        host._send_lock = lock
        results = []
        with patch.object(host, '_rpc', return_value={'ok': True}) as rpc:
            worker = threading.Thread(target=lambda: results.append(host.send_text('wxid_friend', '旧正文')))
            worker.start()
            self.assertTrue(lock.waiting.wait(1))
            host.cancel_pending_sends()
            host.resume_sends()
            lock.release()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(results[0][0])
        self.assertEqual(results[0].diagnostic['submission_state'], 'not_submitted')
        self.assertEqual([call.args[0] for call in rpc.call_args_list], ['resume_sends'])

    def test_send_rpc_waiting_for_rpc_lock_is_not_posted_after_stop_and_resume(self):
        host = self.host()
        lock = ObservedLock()
        host._rpc_lock = lock
        rpc = host._rpc
        failures = []
        def send():
            try:
                rpc('send_text', {'target': 'wxid_friend', 'text': '旧正文'}, timeout=.5)
            except RpcError as error:
                failures.append(error)
        worker = threading.Thread(target=send)
        with patch('src.isolated_wechat.host_bridge.write_document') as write:
            worker.start()
            self.assertTrue(lock.waiting.wait(1))
            host.cancel_pending_sends()
            self.resume_without_guest(host)
            lock.release()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].diagnostic['submission_state'], 'not_submitted')
        write.assert_not_called()

    def test_old_rpc_cleanup_does_not_cancel_the_resumed_generation(self):
        host = self.host()
        def interrupted_write(*_, **__):
            host.cancel_pending_sends()
            self.resume_without_guest(host)
            raise OSError('old request write finished after cancellation')
        with patch('src.isolated_wechat.host_bridge.write_document', side_effect=interrupted_write):
            with self.assertRaises(RpcError):
                host._rpc('send_text', {'target': 'wxid_friend', 'text': '旧正文'}, timeout=.5)
        self.assertEqual(host._control.call_count, 1)
        host._control.assert_called_once_with('cancel')


if __name__ == '__main__':
    unittest.main()
