"""协议管理边界：假进程核验、真实 ASGI 路由，不接触微信。"""
import asyncio
import copy
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI, HTTPException

from src import hook_api as api
from src.config_manager import ConfigManager
from src.hook_gateway import CallbackToken, MAX_CALLBACK_BYTES

SETTINGS = {'api_url': 'http://127.0.0.1:30001', 'account': 'wxid_test_owner',
            'account_path': 'C:/private/wxid_test_owner_abcd', 'target': 'filehelper'}


class HookApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.yaml')
        self.config.set('wechat.environment', 'local')
        self.config.set('wechat.hook', dict(SETTINGS, enabled=False))
        self.bound, self.revision = True, 1
        self.switches = []
        self.before_commit = None
        self.bridge = SimpleNamespace(callback_token=CallbackToken(),
            ingest_raw=Mock(return_value=True), send_environment={'ready': False})
        self.bot = SimpleNamespace(config=self.config, bridge=self.bridge, running=False,
                                   lock=asyncio.Lock())

        async def switch(environment, check_access=None):
            await asyncio.sleep(0)
            if self.before_commit:
                self.before_commit()
            if check_access:
                check_access()
            self.switches.append(environment)
            self.config.set('wechat.environment', environment)
            if environment == 'hook':
                self.config.set('wechat.hook.enabled', True)
            self.bot.running = False
        self.bot.switch_wechat_environment = switch

        def actor(bot):
            if not self.bound:
                raise HTTPException(403, '未绑定')
            return {'revision': self.revision}

        patches = [patch.object(api, 'require_bound', side_effect=actor),
                   patch.object(api, 'probe_hook_identity', return_value={
                       'ready': False, 'reason': '未经审计的原版模块',
                       'waiting_code': 'provider_unapproved', 'secret': 'never-return'})]
        self.actor, self.probe = [item.start() for item in patches]
        for item in reversed(patches):
            self.addCleanup(item.stop)
        self.app = FastAPI()
        self.app.state.bot = self.bot
        api.add_routes(self.app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url='http://testserver')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def post(self, action, body=None):
        return await self.client.post('/api/wechat/hook/' + action,
                                      json={} if body is None else body)

    async def test_mutations_require_local_binding_before_probe_or_save(self):
        self.bound = False
        for action in ('probe', 'config', 'activate', 'deactivate'):
            response = await self.post(action, SETTINGS if action in ('probe', 'config') else {})
            self.assertEqual(response.status_code, 403)
        self.probe.assert_not_called()
        self.assertEqual(self.switches, [])

    async def test_save_is_disabled_and_probe_never_exposes_extra_evidence(self):
        result = await self.post('config', SETTINGS)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(self.config.get('wechat.hook.enabled'))
        self.assertEqual(self.config.get('wechat.environment'), 'local')
        self.assertFalse(self.bot.running)
        self.assertEqual(self.switches, [])
        result = await self.post('probe', SETTINGS)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()['ready'])
        self.assertNotIn('never-return', result.text)

    async def test_remote_address_and_extra_fields_refused_without_network_probe(self):
        for url in ('http://example.com:30001', 'http://0.0.0.0:30001',
                    'http://127.0.0.1:30001/arbitrary'):
            result = await self.post('probe', dict(SETTINGS, api_url=url))
            self.assertEqual(result.status_code, 400)
        result = await self.post('config', dict(SETTINGS, install=True))
        self.assertEqual(result.status_code, 422)
        self.probe.assert_not_called()

    async def test_rebind_during_probe_prevents_result_and_activation(self):
        def revoke(*args):
            self.revision += 1
            return {'ready': True}
        self.probe.side_effect = revoke
        for action, body in (('probe', SETTINGS), ('activate', {})):
            result = await self.post(action, body)
            self.assertEqual(result.status_code, 409)
        self.assertEqual(self.switches, [])

    async def test_unapproved_provider_and_incomplete_saved_config_cannot_activate(self):
        result = await self.post('activate')
        self.assertEqual(result.status_code, 409)
        self.config.set('wechat.hook.account', '')
        result = await self.post('activate')
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.switches, [])

    async def test_activation_rechecks_owner_through_switch_and_keeps_stopped(self):
        self.probe.return_value = {'ready': True}
        self.before_commit = lambda: setattr(self, 'revision', self.revision + 1)
        before = copy.deepcopy(self.config.config)
        result = await self.post('activate')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.config.config, before)
        self.before_commit = None
        result = await self.post('activate')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.switches, ['hook'])
        self.assertFalse(self.bot.running)
        self.assertTrue(self.config.get('wechat.hook.enabled'))

    async def test_active_config_not_replaced_and_deactivation_stops_first(self):
        self.config.set('wechat.environment', 'hook')
        self.config.set('wechat.hook.enabled', True)
        self.bot.running = True
        result = await self.post('config', dict(SETTINGS, target='wxid_other'))
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.config.get('wechat.hook.target'), 'filehelper')
        result = await self.post('deactivate')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.switches, ['local'])
        self.assertFalse(self.bot.running)
        self.assertFalse(self.config.get('wechat.hook.enabled'))

    async def test_failed_save_rolls_back_and_sanitizes_error(self):
        before = copy.deepcopy(self.config.config)
        with patch.object(self.config, 'save', side_effect=OSError('private-secret-directory')):
            result = await self.post('config', dict(SETTINGS, target='wxid_other'))
        self.assertEqual(result.status_code, 500)
        self.assertNotIn('private-secret', result.text)
        self.assertEqual(self.config.config, before)

    def enable_callback(self):
        self.config.set('wechat.environment', 'hook')
        self.bot.running = True
        return self.bridge.callback_token.path

    async def test_callback_requires_secret_loopback_running_and_correct_environment(self):
        path = self.enable_callback()
        self.assertEqual((await self.client.post(path + 'wrong', json={})).status_code, 403)
        self.bot.running = False
        self.assertEqual((await self.client.post(path, json={})).status_code, 403)
        self.bot.running = True
        self.config.set('wechat.environment', 'local')
        self.assertEqual((await self.client.post(path, json={})).status_code, 403)
        self.config.set('wechat.environment', 'hook')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=('192.0.2.1', 123)),
                                     base_url='http://testserver') as remote:
            self.assertEqual((await remote.post(path, json={})).status_code, 403)
        self.bridge.ingest_raw.assert_not_called()

    async def test_callback_rejects_oversize_duplicate_and_nan_before_bridge(self):
        path = self.enable_callback()
        for data in (b'{"msgid":1,"msgid":2}', b'{"timestamp":NaN}', b'[]', b''):
            response = await self.client.post(path, content=data)
            self.assertEqual(response.status_code, 400)
        result = await self.client.post(path, content=b' ' * (MAX_CALLBACK_BYTES + 1))
        self.assertEqual(result.status_code, 413)
        self.bridge.ingest_raw.assert_not_called()

    async def test_switch_or_rotation_during_stream_cannot_deliver_old_callback(self):
        for mutate in (lambda: setattr(self.bot, 'running', False),
                       self.bridge.callback_token.rotate,
                       lambda: setattr(self.bot, 'bridge', SimpleNamespace())):
            self.bot.bridge = self.bridge
            path = self.enable_callback()
            async def body():
                yield b'{'
                mutate()
                yield b'}'
            response = await self.client.post(path, content=body())
            self.assertEqual(response.status_code, 409)
        self.bridge.ingest_raw.assert_not_called()

    async def test_callback_preserves_raw_bytes_and_status_does_not_leak_nonce(self):
        path = self.enable_callback()
        payload = json.dumps({'msgid': '12345678901234567890'}, separators=(',', ':')).encode()
        result = await self.client.post(path, content=payload)
        self.assertEqual(result.json(), {'accepted': True})
        self.bridge.ingest_raw.assert_called_once_with(payload)
        status = await self.client.get('/api/wechat/hook')
        self.assertNotIn(path, status.text)

    def test_access_log_redacts_callback_path(self):
        path = self.bridge.callback_token.path
        record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0,
                                   '%s %s %s', ('POST', path + '?x=1', '200'), None)
        api.CallbackAccessFilter().filter(record)
        self.assertNotIn(path, record.getMessage())

