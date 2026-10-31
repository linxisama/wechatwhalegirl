"""界定 bot._sandbox_preparing 的滞留条件：它只在配对操作无法推进时保持为真，且一旦能推进就立刻复位。
主张（详见同目录 1315/1317/1319）是「等待锁或 operation 不返回时 finally 不执行，flag 永久为真，只能重启」。
本文件用真实 asyncio.Lock 与真实取消路径证明：取消不会泄漏 flag；锁释放后 flag 立刻复位、后续操作立刻恢复。
"""
import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI, HTTPException

from src import sandbox_api as api
from src.runtime_identity import APP_VERSION

SANDBOX = '7e2838d2-db90-448c-aced-96ecab3b5f8e'
CHANNEL = 'a' * 32


class PreparingStuckBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bound = True
        self.revision = 1
        self.switches = []
        self.bot = SimpleNamespace(root=Path(self.temp.name), lock=asyncio.Lock(),
            config={'wechat.environment': 'local', 'wechat.sandbox_channel_id': ''},
            running=False, note=Mock())

        async def switch(environment, channel_id='', sandbox_id='', check_access=None):
            await asyncio.sleep(0)
            if check_access:
                check_access()
            self.switches.append((environment, channel_id, sandbox_id))
            self.bot.config['wechat.environment'] = environment
            self.bot.config['wechat.sandbox_channel_id'] = channel_id
            self.bot.running = False

        self.bot.switch_wechat_environment = switch

        def actor(bot):
            if not self.bound:
                raise HTTPException(403, '未绑定管理员')
            return {'revision': self.revision, 'peer': 'local_console'}

        self.prepared = {'success': True, 'channel_id': CHANNEL, 'sandbox_id': SANDBOX,
            'stage': 'prepared', 'message': '已准备，未启动机器人', 'secret': 'never-return'}
        self.pair_status = {'success': True, 'available': True, 'message': '仅当前会话',
            'sandboxes': [{'id': SANDBOX, 'path': r'C:\private'}],
            'pairs': [{'channel_id': CHANNEL, 'sandbox_id': SANDBOX, 'stage': 'agent_ready',
                'agent_online': True, 'wechat_online': False, 'app_version': APP_VERSION}]}
        patches = [
            patch.object(api, 'require_bound', side_effect=actor),
            patch.object(api.manager, 'prepare_sandbox', return_value=self.prepared),
            patch.object(api.manager, 'paired_status', return_value=self.pair_status),
            patch.object(api.manager, 'channel_path', return_value=self.bot.root / 'channel'),
            patch.object(api.manager, 'stop_guest', return_value={'requested': True}),
            patch.object(api, 'load_pairing', return_value={'channel_id': CHANNEL}),
            patch.object(api, 'live_service', return_value={'app_version': APP_VERSION}),
        ]
        self.mocks = [item.start() for item in patches]
        for item in reversed(patches):
            self.addCleanup(item.stop)
        (_, self.prepare_mock, self.status_mock, self.path_mock, self.stop_mock,
            self.pair_mock, self.live_mock) = self.mocks
        app = FastAPI()
        app.state.bot = self.bot
        api.add_routes(app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url='http://testserver')

    async def asyncTearDown(self):
        await self.client.aclose()
        if self.bot.lock.locked():
            self.bot.lock.release()
        self.temp.cleanup()

    async def wait_for_queued_prepare(self, limit=2.0):
        """等到配对任务已经把 flag 置真并停在 bot.lock 上。"""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if getattr(self.bot, '_sandbox_prepare_task', None) is not None:
                return True
            await asyncio.sleep(.005)
        return False

    async def test_flag_lasts_only_while_the_lock_holder_refuses_to_yield(self):
        """锁被占住时 flag 确实为真并让三个端点立即 409；锁一释放 flag 立刻复位、无需重启。"""
        await self.bot.lock.acquire()
        request = None
        try:
            # 客户端一直等：没有取消、没有超时，正是主张里「永挂」的构造。
            request = asyncio.create_task(self.client.post('/api/sandbox/prepare', json={'id': SANDBOX}))
            self.assertTrue(await self.wait_for_queued_prepare())
            await asyncio.sleep(.05)
            self.assertTrue(self.bot._sandbox_preparing)
            self.prepare_mock.assert_not_called()
            # 三个端点都是立刻 409（不是挂起），说明 flag 是「即时拒绝」而不是死锁。
            for path, data in [('prepare', {'id': SANDBOX}), ('activate', {'channel_id': CHANNEL}),
                               ('deactivate', {})]:
                result = await asyncio.wait_for(
                    self.client.post('/api/sandbox/' + path, json=data), 2)
                self.assertEqual(result.status_code, 409, path)
            status = await asyncio.wait_for(self.client.get('/api/sandbox/status'), 2)
            self.assertTrue(status.json()['preparing'])
            self.assertEqual(self.switches, [])
            self.assertFalse(request.done())
        finally:
            self.bot.lock.release()
        started = time.monotonic()
        result = await asyncio.wait_for(request, 3)
        self.assertEqual(result.status_code, 200)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(self.bot._sandbox_preparing)
        self.assertEqual(self.prepare_mock.call_count, 1)
        # 事后同样的操作立刻可用：不存在「只能重启进程」。
        self.assertEqual((await asyncio.wait_for(
            self.client.post('/api/sandbox/prepare', json={'id': SANDBOX}), 2)).status_code, 200)
        self.assertEqual((await asyncio.wait_for(
            self.client.post('/api/sandbox/activate', json={'channel_id': CHANNEL}), 2)).status_code, 200)
        self.assertEqual(self.switches, [('sandbox', CHANNEL, SANDBOX)])
        status = await asyncio.wait_for(self.client.get('/api/sandbox/status'), 2)
        self.assertFalse(status.json()['preparing'])

    async def test_cancelled_http_prepare_releases_the_flag_when_the_worker_returns(self):
        """客户端断连（CancelledError）不会泄漏 flag：worker 返回后 finally 一定执行。"""
        started, release = threading.Event(), threading.Event()
        def prepare(*_args):
            started.set()
            if not release.wait(3):
                raise AssertionError('test worker was not released')
            return self.prepared
        self.prepare_mock.side_effect = prepare
        request = asyncio.create_task(self.client.post('/api/sandbox/prepare', json={'id': SANDBOX}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertTrue(self.bot._sandbox_preparing)
        finally:
            release.set()
        with self.assertRaises(HTTPException) as caught:
            await asyncio.wait_for(asyncio.shield(self.bot._sandbox_prepare_task), 3)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(self.bot._sandbox_preparing)
        self.assertEqual((await asyncio.wait_for(
            self.client.post('/api/sandbox/activate', json={'channel_id': CHANNEL}), 2)).status_code, 200)
        self.assertEqual(self.switches, [('sandbox', CHANNEL, SANDBOX)])

    async def test_cancel_while_waiting_for_the_lock_still_clears_the_flag_after_it_is_released(self):
        """锁被占 + 客户端取消（主张的最坏组合）：锁一释放 flag 就复位，且没有创建/停止任何沙盒。"""
        await self.bot.lock.acquire()
        request = None
        try:
            request = asyncio.create_task(self.client.post('/api/sandbox/prepare', json={'id': SANDBOX}))
            self.assertTrue(await self.wait_for_queued_prepare())
            self.assertTrue(self.bot._sandbox_preparing)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertTrue(self.bot._sandbox_preparing)
        finally:
            self.bot.lock.release()
        with self.assertRaises(HTTPException) as caught:
            await asyncio.wait_for(asyncio.shield(self.bot._sandbox_prepare_task), 3)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(self.bot._sandbox_preparing)
        self.assertEqual(self.bot.lock._waiters and len(self.bot.lock._waiters) or 0, 0)
        self.prepare_mock.assert_not_called()
        self.stop_mock.assert_not_called()
        self.assertEqual((await asyncio.wait_for(
            self.client.post('/api/sandbox/activate', json={'channel_id': CHANNEL}), 2)).status_code, 200)


if __name__ == '__main__':
    unittest.main()
