"""1.3.62：状态页不再永久显示启动时的「微信已最小化」。"""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from src.wechat_app import WeChatRuntime, create_app, send_channel_hint

ROOT = Path(__file__).resolve().parent.parent


class HintTests(unittest.TestCase):
    def test_minimized_hint_tells_user_send_restores_itself(self):
        text = send_channel_hint({'reason': '微信已最小化，请手动恢复并打开目标聊天',
                                  'waiting_code': 'minimized'})
        self.assertIn('自动恢复', text)
        self.assertIn('测试发送', text)

    def test_other_codes_still_ask_for_manual_step(self):
        text = send_channel_hint({'reason': '没有打开的会话', 'waiting_code': 'chat_not_open'})
        self.assertIn('先在微信里手动点开目标会话', text)
        self.assertNotIn('自动恢复', text)


class _Bridge:
    backend = 'wechatauto'
    online = True

    def __init__(self):
        self.calls = 0
        self.boom = False

    def inspect_send_environment(self, who=''):
        self.calls += 1
        if self.boom:
            raise RuntimeError('探测炸了')
        return {'ready': True}


class _Runtime:
    running = True

    def __init__(self, stage='idle'):
        self.bridge = _Bridge()
        self.pipeline = {'stage': stage}


class RefreshTests(unittest.TestCase):
    def _run(self, fake, ttl=0.0):
        return asyncio.run(WeChatRuntime.refresh_send_state_if_stale(fake, ttl=ttl))

    def test_idle_foreground_runtime_is_probed_then_throttled(self):
        fake = _Runtime()
        self._run(fake, ttl=0.0)
        self.assertEqual(fake.bridge.calls, 1)
        self._run(fake, ttl=15.0)
        self.assertEqual(fake.bridge.calls, 1)

    def test_sending_or_offline_or_mock_is_never_probed(self):
        busy = _Runtime('sending')
        self._run(busy)
        offline = _Runtime()
        offline.bridge.online = False
        self._run(offline)
        mock = _Runtime()
        mock.bridge.backend = 'mock'
        self._run(mock)
        stopped = _Runtime()
        stopped.running = False
        self._run(stopped)
        for fake in (busy, offline, mock, stopped):
            self.assertEqual(fake.bridge.calls, 0)

    def test_probe_failure_never_breaks_status(self):
        fake = _Runtime()
        fake.bridge.boom = True
        self._run(fake)
        self.assertEqual(fake.bridge.calls, 1)


class StatusEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'styles', root / 'styles')
        config = root / 'config.wechat.yaml'
        config.write_text((ROOT / 'config.wechat.yaml').read_text('utf-8'), 'utf-8')
        self.bot = WeChatRuntime(root=root, config_path=config, db_path=root / 'm.sqlite3')
        self.bot.running = True
        self.bot.pipeline = {'stage': 'idle'}
        self.bot.bridge.online = True
        self.bot.bridge.backend = 'wechatauto'
        self.bot.bridge.send_environment = {'ready': False, 'waiting_code': 'minimized',
                                            'reason': '微信已最小化，请手动恢复并打开目标聊天'}
        self._cm = TestClient(create_app(self.bot))
        self.client = self._cm.__enter__()
        self.addCleanup(self._close)

    def _close(self):
        try:
            self._cm.__exit__(None, None, None)
        except Exception:
            pass

    def _fresh(self):
        self.bot.bridge.send_environment = {'ready': True, 'reason': '发送环境正常'}
        return dict(self.bot.bridge.send_environment)

    def test_status_refreshes_stale_minimized_state(self):
        with patch.object(self.bot.bridge, 'inspect_send_environment', side_effect=self._fresh):
            body = self.client.get('/api/status').json()
        self.assertEqual(body['connection']['send_state'], 'ready')
        self.assertEqual(body['connection']['send_code'], '')

    def test_test_wechat_reports_fresh_state_not_stale_code(self):
        with patch.object(self.bot.bridge, 'inspect_send_environment', side_effect=self._fresh):
            body = self.client.post('/api/test-wechat', json={}).json()
        self.assertTrue(body['send_ready'])
        self.assertNotIn('发送原因码：minimized', body['message'])
        self.assertIn('发送环境：发送环境正常', body['message'])


if __name__ == '__main__':
    unittest.main()
