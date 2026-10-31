"""隔离管理 API 的权限、取消及输出边界；沙盒命令与代理全部使用替身。"""
import asyncio
import tempfile
import threading
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


class SandboxApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bound = True
        self.revision = 1
        self.before_switch_commit = None
        self.switches = []
        self.bot = SimpleNamespace(root=Path(self.temp.name), lock=asyncio.Lock(),
            config={'wechat.environment': 'local', 'wechat.sandbox_channel_id': ''},
            running=False, note=Mock())

        async def switch(environment, channel_id='', sandbox_id='', check_access=None):
            await asyncio.sleep(0)
            if self.before_switch_commit:
                self.before_switch_commit()
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
            'stage': 'prepared', 'message': '已准备，未启动机器人',
            'channel_dir': r'C:\private\channel', 'secret': 'never-return',
            'internal': {'key': 'never-return'}}
        self.pair_status = {'success': True, 'available': True, 'message': '仅当前会话',
            'sandboxes': [{'id': SANDBOX, 'path': r'C:\private'}],
            'pairs': [{'channel_id': CHANNEL, 'sandbox_id': SANDBOX,
                'stage': 'agent_ready', 'agent_online': True, 'wechat_online': False,
                'app_version': APP_VERSION, 'secret': 'never-return',
                'channel_dir': r'C:\private\channel'}], 'secret': 'never-return'}
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
        self.temp.cleanup()

    async def test_mutation_requires_bound_local_actor_before_any_operation(self):
        self.bound = False
        for path, data in [('prepare', {'id': SANDBOX}),
                           ('activate', {'channel_id': CHANNEL}), ('deactivate', {})]:
            result = await self.client.post('/api/sandbox/' + path, json=data)
            self.assertEqual(result.status_code, 403)
        self.prepare_mock.assert_not_called()
        self.path_mock.assert_not_called()
        self.stop_mock.assert_not_called()
        self.assertEqual(self.switches, [])

    async def test_prepare_exposes_only_defined_fields_and_never_starts_robot(self):
        result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(result.json()),
            {'success', 'channel_id', 'sandbox_id', 'stage', 'message'})
        self.assertNotIn('private', result.text)
        self.assertNotIn('never-return', result.text)
        self.assertFalse(self.bot._sandbox_preparing)
        self.assertEqual(self.switches, [])
        self.assertFalse(self.bot.running)

    async def test_status_strips_paths_secrets_and_unregistered_nested_fields(self):
        result = await self.client.get('/api/sandbox/status')
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('private', result.text)
        self.assertNotIn('secret', result.text)
        self.assertEqual(result.json()['sandboxes'], [{'id': SANDBOX}])

    async def test_unbind_during_prepare_stops_own_paired_agent_and_does_not_activate(self):
        def prepare(*args):
            self.bound = False
            return self.prepared
        self.prepare_mock.side_effect = prepare
        result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX})
        self.assertEqual(result.status_code, 409)
        self.path_mock.assert_called_once_with(self.bot.root, CHANNEL)
        self.stop_mock.assert_called_once_with(self.bot.root / 'channel')
        self.assertEqual(self.switches, [])
        self.assertFalse(self.bot._sandbox_preparing)

    async def test_rebind_cleanup_failure_does_not_mask_permission_failure_or_leak_path(self):
        def prepare(*args):
            self.revision += 1
            return self.prepared
        self.prepare_mock.side_effect = prepare
        self.stop_mock.side_effect = OSError(r'C:\private\secret.json')
        result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX})
        self.assertEqual(result.status_code, 409)
        self.assertNotIn('private', result.text)
        self.assertEqual(self.switches, [])

    async def test_prepare_failure_releases_busy_and_sanitizes_unknown_errors(self):
        self.prepare_mock.side_effect = OSError(r'C:\private\secret.json')
        result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX})
        self.assertEqual(result.status_code, 400)
        self.assertNotIn('private', result.text)
        self.assertFalse(self.bot._sandbox_preparing)

    async def test_busy_prepare_never_starts_a_second_mapping(self):
        self.bot._sandbox_preparing = True
        result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX})
        self.assertEqual(result.status_code, 409)
        self.prepare_mock.assert_not_called()

    async def test_cancelled_http_prepare_holds_gate_until_worker_finishes_and_stops_agent(self):
        started, release = threading.Event(), threading.Event()
        def prepare(*args):
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
            second = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX})
            self.assertEqual(second.status_code, 409)
        finally:
            release.set()
        with self.assertRaises(HTTPException) as caught:
            await self.bot._sandbox_prepare_task
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(self.bot._sandbox_preparing)
        self.stop_mock.assert_called_once()
        self.assertEqual(self.prepare_mock.call_count, 1)
        self.assertEqual(self.switches, [])

    async def test_activate_rejects_live_agent_of_different_program_version(self):
        self.live_mock.return_value = {'app_version': '1.3.14'}
        result = await self.client.post('/api/sandbox/activate', json={'channel_id': CHANNEL})
        self.assertEqual(result.status_code, 400)
        self.assertIn('版本不一致', result.json()['detail'])
        self.assertEqual(self.switches, [])

    async def test_activate_rejects_unready_pair_even_when_previous_heartbeat_was_live(self):
        self.pair_status['pairs'][0].update(stage='prepared', agent_online=False)
        result = await self.client.post('/api/sandbox/activate', json={'channel_id': CHANNEL})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.switches, [])

    async def test_activate_rechecks_owner_after_status_thread_before_switch(self):
        def status(*args):
            self.revision += 1
            return self.pair_status
        self.status_mock.side_effect = status
        result = await self.client.post('/api/sandbox/activate', json={'channel_id': CHANNEL})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.switches, [])

    async def test_activate_preserves_revision_check_through_runtime_await(self):
        self.before_switch_commit = lambda: setattr(self, 'revision', self.revision + 1)
        result = await self.client.post('/api/sandbox/activate', json={'channel_id': CHANNEL})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.switches, [])
        self.assertEqual(self.bot.config['wechat.environment'], 'local')

    async def test_activate_only_switches_selected_channel_and_keeps_robot_stopped(self):
        result = await self.client.post('/api/sandbox/activate', json={'channel_id': CHANNEL})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.switches, [('sandbox', CHANNEL, SANDBOX)])
        self.assertFalse(self.bot.running)

    async def test_deactivate_stops_only_previous_agent_after_switching_to_local(self):
        self.bot.config.update({'wechat.environment': 'sandbox', 'wechat.sandbox_channel_id': CHANNEL})
        self.stop_mock.side_effect = OSError(r'C:\private\secret.json')
        result = await self.client.post('/api/sandbox/deactivate', json={})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.switches, [('local', '', '')])
        self.stop_mock.assert_called_once_with(self.bot.root / 'channel')
        self.assertNotIn('private', result.text)
        self.assertFalse(self.bot.running)

    async def test_invalid_body_never_runs_mapping_or_switch(self):
        result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX, 'command': 'forbidden'})
        self.assertEqual(result.status_code, 422)
        result = await self.client.post('/api/sandbox/activate', json={'channel_id': '../outside'})
        self.assertEqual(result.status_code, 422)
        self.prepare_mock.assert_not_called()
        self.assertEqual(self.switches, [])


if __name__ == '__main__':
    unittest.main()
