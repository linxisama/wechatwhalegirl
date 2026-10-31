"""1.3.51：慢机器上「准备监听」失败的可见性与预算内重试。

只用假 RPC：不写真实通道、不启动控制台、不碰微信。每个用例都钉住一条红线——
单次超时预算没被拍大、可重试的只有「请求没落地」的错误、代际竞争照旧 fail-closed。
"""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from src.isolated_wechat.host_bridge import (
    LISTEN_ATTEMPTS, LISTEN_ATTEMPT_TIMEOUT, LISTEN_PREPARE_BUDGET,
    RpcBusy, RpcError, RpcTimeout, SandboxBridge)
from src.logging_setup import RUNTIME_LOGGER_NAME

SECRET = '12' * 32


class FakeRpc:
    """按剧本返回/抛出；非 start_listening 的请求一律成功，免得收尾时炸在假对象里。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, action, args=None, *, timeout=None, send_epoch=None):
        self.calls.append((action, timeout))
        if action != 'start_listening':
            return {'ok': True}
        if not self.outcomes:
            return {'ok': True}
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    def actions(self):
        return [action for action, _timeout in self.calls]


class ListenerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'profile'
        self.channel = self.root / 'channel'
        self.channel.mkdir(parents=True)
        ident = uuid.uuid4().hex
        (self.channel / 'pairing.json').write_text(
            '{"version": 1, "channel_id": "%s", "secret": "%s"}' % (ident, SECRET),
            encoding='utf-8')
        self.host = SandboxBridge(self.root / 'host-data', self.channel)
        # 不要真的起轮询线程：本文件只验收「准备监听」这一段。
        poll = patch.object(SandboxBridge, '_poll_remote', return_value=None)
        poll.start()
        self.addCleanup(poll.stop)

    def tearDown(self):
        try:
            self.host.stop_listening()
        except Exception:
            pass
        self.temp.cleanup()

    def patch_rpc(self, fake):
        patcher = patch.object(self.host, '_rpc', fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_single_attempt_budget_is_not_inflated(self):
        # 红线：不许用「把 5 秒改成 30 秒」冒充健壮性。
        self.assertEqual(LISTEN_ATTEMPT_TIMEOUT, 5.0)
        self.assertEqual(LISTEN_ATTEMPTS, 2)
        self.assertLessEqual(LISTEN_PREPARE_BUDGET, 30.0)

    def test_timeout_then_success_starts_listening(self):
        fake = self.patch_rpc(FakeRpc(RpcTimeout('隔离请求超时，本次不重试'), {'ok': True}))
        ok, message = self.host.start_listening(lambda event: None)
        self.assertTrue(ok, message)
        self.assertEqual(message, '隔离数据库只读监听已启动')
        self.assertEqual(fake.actions(), ['start_listening', 'start_listening'])
        for _action, timeout in fake.calls:
            self.assertLessEqual(timeout, LISTEN_ATTEMPT_TIMEOUT)

    def test_busy_lock_is_retried_within_budget(self):
        fake = self.patch_rpc(FakeRpc(RpcBusy('隔离通道正忙，本次未投递请求'), {'ok': True}))
        ok, message = self.host.start_listening(lambda event: None)
        self.assertTrue(ok, message)
        self.assertEqual(len(fake.calls), 2)

    def test_exhausted_budget_returns_false_with_readable_reason(self):
        fake = self.patch_rpc(FakeRpc(RpcTimeout('隔离请求超时，本次不重试'),
                                      RpcTimeout('隔离请求超时，本次不重试')))
        with self.assertLogs(RUNTIME_LOGGER_NAME, level='WARNING') as captured:
            ok, message = self.host.start_listening(lambda event: None)
        self.assertFalse(ok)
        self.assertFalse(self.host._listening)
        self.assertEqual(len(fake.calls), LISTEN_ATTEMPTS)
        self.assertIn('隔离请求超时', message)
        self.assertIn('已用', message)
        self.assertIn('预算', message)
        self.assertIn('仍未启动', message)
        text = '\n'.join(captured.output)
        self.assertIn('隔离监听准备第 1 次未完成', text)
        self.assertIn('再试一次', text)
        self.assertIn('不再重试', text)

    def test_non_retryable_error_is_not_retried(self):
        fake = self.patch_rpc(FakeRpc(RpcError('隔离响应请求摘要不匹配')))
        ok, message = self.host.start_listening(lambda event: None)
        self.assertFalse(ok)
        self.assertEqual(message, '隔离响应请求摘要不匹配')
        # 可能已经执行过的错误重试不安全：只许一次。
        self.assertEqual(len(fake.calls), 1)

    def test_failed_value_from_guest_is_reported_not_retried(self):
        fake = self.patch_rpc(FakeRpc({'ok': False, 'message': '隔离入站记录未完整落盘，未恢复监听'}))
        ok, message = self.host.start_listening(lambda event: None)
        self.assertFalse(ok)
        self.assertEqual(message, '隔离入站记录未完整落盘，未恢复监听')
        self.assertEqual(len(fake.calls), 1)

    def test_generation_race_still_refuses_after_retry(self):
        def race():
            # 模拟准备期间发生了 stop/新一轮准备：代际必须拦住旧完成。
            self.host._receive_generation += 1
            return {'ok': True}

        self.patch_rpc(FakeRpc(race))
        ok, message = self.host.start_listening(lambda event: None)
        self.assertFalse(ok)
        self.assertEqual(message, '隔离监听准备期间已停止，本轮未启动')
        self.assertFalse(self.host._listening)


if __name__ == '__main__':
    unittest.main()
