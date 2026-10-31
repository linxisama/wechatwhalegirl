"""管理员绑定、权限边界和原控制台真实HTTP贯通；全部使用隔离mock。"""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src.agent_authority import AgentAuthority, AuthorityError, authority
from src.agent_control import ConsoleControl
from src.wechat_app import WeChatRuntime, create_app
from tests.test_wechat_app_flow import ROOT, write_config

ADMIN = {'platform': 'qq', 'account': '123456', 'subject': '654321'}


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AgentAuthority(Path(self.temp.name) / 'authority.json')

    def tearDown(self):
        self.temp.cleanup()

    def test_unbound_bind_token_restart_revoke_and_rebind(self):
        with self.assertRaises(AuthorityError): self.store.local_actor()
        self.store.bind(ADMIN)
        key = self.store.issue('dsh', ['read_memory'])
        self.assertNotIn(key['token'], self.store.path.read_text('utf-8'))
        self.assertNotIn('digest', json.dumps(self.store.status()))
        again = AgentAuthority(self.store.path)
        self.assertEqual(again.authenticate(key['token'])['admin'], ADMIN)
        again.revoke(key['id'])
        with self.assertRaises(AuthorityError): again.authenticate(key['token'])
        key = again.issue('desktop', ['read_console'])
        again.bind(dict(ADMIN, subject='999999'))
        with self.assertRaises(AuthorityError): again.authenticate(key['token'])
        with self.assertRaises(AuthorityError): again.unbind(revision=1)

    def test_bad_identity_unknown_grant_and_corrupt_file_fail_closed(self):
        for identity in [dict(ADMIN, subject='nickname'), dict(ADMIN, platform='all'), dict(ADMIN, account='../bad'), []]:
            with self.assertRaises(AuthorityError): self.store.bind(identity)
        self.store.bind(ADMIN)
        with self.assertRaises(AuthorityError): self.store.issue('dsh', ['shell_execute'])
        self.store.path.write_text('{bad', 'utf-8')
        self.assertFalse(self.store.status()['bound'])
        with self.assertRaises(AuthorityError): self.store.local_actor()
        self.assertFalse(self.store.repair()['bound'])
        self.assertTrue(list(self.store.path.parent.glob('*.corrupt-*.json')))
        self.store.bind(ADMIN)
        self.assertTrue(self.store.status()['bound'])

    def test_disk_failure_does_not_grant_in_memory_identity(self):
        with patch('src.agent_authority.atomic_text', side_effect=OSError()):
            with self.assertRaises(OSError): self.store.bind(ADMIN)
        self.assertFalse(self.store.status()['bound'])


class ControlFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        shutil.copytree(ROOT / 'styles', root / 'styles')
        self.bot = WeChatRuntime(root=root, config_path=write_config(root), db_path=root/'test.sqlite')
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.bot.close()
        self.temp.cleanup()

    async def bind(self):
        response = await self.client.post('/api/agent/authority', json=ADMIN)
        self.assertEqual(response.status_code, 200, response.text)

    async def execute(self, action, body=None, params=None, request_id='test-request-001', headers=None):
        return await self.client.post('/api/agent/control/execute', json={
            'action': action, 'body': body or {}, 'params': params or {}, 'request_id': request_id}, headers=headers)

    async def test_unbound_agent_cannot_write_but_original_console_still_works(self):
        response = await self.execute('plugins.configure', {'replyMaxChars': 44}, {'plugin_id': 'chat_controls'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual((await self.client.post('/api/plugins/chat_controls', json={'replyMaxChars': 44})).status_code, 200)
        catalog = (await self.client.get('/api/agent/control/catalog')).json()
        self.assertFalse(catalog['bound'])
        self.assertFalse(any(row['available'] for row in catalog['operations']))
        self.assertEqual((await self.client.post('/api/agent/apply', json={'session_id': 'x', 'card_id': 'y'})).status_code, 403)

    async def test_catalog_routes_exist_and_console_status_has_no_chat_history(self):
        from src.agent_control import CATALOG
        routes = {(route.path, method) for route in self.app.routes for method in getattr(route, 'methods', [])}
        for spec in CATALOG.values():
            self.assertIn((spec['path'], spec['method']), routes)
        await self.bind()
        result = (await self.execute('console.status')).json()['data']
        self.assertNotIn('messages', result)
        self.assertNotIn('events', result)

    async def test_actual_plugin_configuration_is_idempotent_and_audited_without_body(self):
        await self.bind()
        first = await self.execute('plugins.configure', {'replyMaxChars': 44}, {'plugin_id': 'chat_controls'})
        self.assertTrue(first.json()['success'], first.text)
        self.assertEqual(self.bot.plugins.get_settings('chat_controls')['replyMaxChars'], 44)
        self.bot.plugins.update_settings('chat_controls', {'replyMaxChars': 33})
        second = await self.execute('plugins.configure', {'replyMaxChars': 44}, {'plugin_id': 'chat_controls'})
        self.assertTrue(second.json()['replayed'])
        self.assertEqual(self.bot.plugins.get_settings('chat_controls')['replyMaxChars'], 33)
        conflict = await self.execute('plugins.configure', {'replyMaxChars': 50}, {'plugin_id': 'chat_controls'})
        self.assertEqual(conflict.status_code, 409)
        audit = (await self.client.get('/api/agent/control/audit')).json()['items']
        self.assertEqual(len(audit), 1)
        self.assertNotIn('body', audit[0])

    async def test_scope_limited_bridge_handshake_cannot_write_or_self_authorize(self):
        await self.bind()
        key = (await self.client.post('/api/agent/credentials', json={'peer': 'dsh', 'capabilities': ['read_console']})).json()['credential']
        headers = {'Authorization': 'Bearer ' + key['token']}
        handshake = await self.client.get('/api/agent/bridge/handshake', headers=headers)
        self.assertEqual(handshake.json()['protocol'], 'whale-agent-bridge/1')
        self.assertEqual(handshake.json()['source']['platform'], 'wechat')
        self.assertNotIn('token', handshake.text)
        response = await self.execute('plugins.configure', {'replyMaxChars': 44}, {'plugin_id': 'chat_controls'}, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertTrue((await self.execute('plugins.list', headers=headers)).json()['success'])
        self.assertEqual((await self.client.post('/api/agent/authority', json=ADMIN, headers=headers)).status_code, 403)
        self.assertEqual((await self.client.post('/api/plugins/chat_controls', json={'replyMaxChars': 1}, headers=headers)).status_code, 403)
        self.assertEqual((await self.client.post('/api/agent/bridge/execute', json={})).status_code, 401)
        self.assertEqual((await self.execute('/api/agent/authority', headers=headers)).status_code, 400)
        await self.client.delete('/api/agent/credentials/'+key['id'])
        self.assertEqual((await self.client.get('/api/agent/bridge/handshake', headers=headers)).status_code, 401)

    async def test_cancelled_write_never_repeats_after_new_control_instance(self):
        await self.bind()
        control = ConsoleControl(self.app, self.bot)
        raw = {'action': 'console.stop', 'request_id': 'cancelled-request'}
        with patch.object(control, '_invoke', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await control.run(raw, authority(self.bot).local_actor())
        restarted = ConsoleControl(self.app, self.bot)
        with patch.object(restarted, '_invoke') as invoke:
            from src.agent_control import ControlError
            with self.assertRaises(ControlError) as error:
                await restarted.run(raw, authority(self.bot).local_actor())
            self.assertEqual(error.exception.status, 409)
            invoke.assert_not_called()

    async def test_slow_control_request_does_not_hold_stop_or_run_duplicate(self):
        await self.bind()
        control = ConsoleControl(self.app, self.bot)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def invoke(spec, *args):
            calls.append(spec['action'])
            if spec['action'] == 'console.test_model':
                entered.set()
                await release.wait()
            return {'success': True, 'status': 200, 'data': {}}
        raw = {'action': 'console.test_model', 'request_id': 'slow-test-model'}
        with patch.object(control, '_invoke', side_effect=invoke):
            job = asyncio.create_task(control.run(raw, authority(self.bot).local_actor()))
            try:
                await entered.wait()
                from src.agent_control import ControlError
                with self.assertRaises(ControlError):
                    await control.run(raw, authority(self.bot).local_actor())
                result = await asyncio.wait_for(control.run({'action': 'console.stop', 'request_id': 'urgent-stop-001'},
                                                            authority(self.bot).local_actor()), 1)
                self.assertTrue(result['success'])
                self.assertEqual(calls, ['console.test_model', 'console.stop'])
            finally:
                release.set()
                await job

    async def test_send_risk_raw_paths_and_cross_target_paths_are_rejected(self):
        await self.bind()
        for action, body, params in [
            ('send_message', {}, {}),
            ('plugins.configure', {'enabled': True}, {'plugin_id': 'group_ambient'}),
            ('console.settings.patch', {'proactive_enabled': True}, {}),
            ('console.settings.patch', {'send_compat': True}, {}),
            ('plugins.configure', {'replyProbability': 100}, {'plugin_id': 'chat_controls'}),
            ('personas.read', {}, {'name': '../../api/start'}),
        ]:
            response = await self.execute(action, body, params)
            self.assertIn(response.status_code, (400, 403), response.text)
        self.assertFalse(self.bot.running)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 0)

    async def test_original_persona_api_persists_and_service_is_same_runtime(self):
        await self.bind()
        self.bot.style.save_persona({'name': 'test_new_persona', 'display_name': '测试人设', 'system_prompt': '测试'})
        response = await self.execute('personas.activate', params={'name': 'test_new_persona'})
        self.assertTrue(response.json()['success'], response.text)
        self.assertEqual(self.bot.config.get('style.active'), 'test_new_persona')
        response = await self.execute('console.start', request_id='start-request')
        self.assertTrue(response.json()['success'], response.text)
        self.assertTrue(self.bot.running)
        response = await self.execute('console.stop', request_id='stop-request')
        self.assertTrue(response.json()['success'], response.text)
        self.assertFalse(self.bot.running)

    async def test_settings_patch_preserves_other_fields_and_secret(self):
        await self.bind()
        before = self.bot.public_settings()
        key = self.bot.secret('ai.api_key')
        response = await self.execute('console.settings.patch', {'request_timeout': 28})
        self.assertTrue(response.json()['success'], response.text)
        self.assertEqual(self.bot.config.get('ai.timeout'), 28)
        self.assertEqual(self.bot.secret('ai.api_key'), key)
        self.assertEqual(self.bot.public_settings()['groups'], before['groups'])
        self.assertEqual(self.bot.public_settings()['listen_private'], before['listen_private'])


class BindingUITests(unittest.TestCase):
    def test_binding_dialog_and_agent_apply_state_follow_authority(self):
        import os
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        from src.gui.agent_binding import AgentBindingDialog
        import wechat_gui as gui
        app = QApplication.instance() or QApplication([])
        requests = []
        def fake(owner, method, path, body=None, **kwargs):
            requests.append((method, path, body))
            result = {'success': True, 'bound': False, 'revision': 0, 'credentials': []}
            if method == 'POST' and path == 'agent/authority':
                result.update(bound=True, admin=ADMIN, revision=1)
            kwargs['on_done'](result)
        dialog = AgentBindingDialog(fake)
        tab = gui.AgentTab()
        try:
            self.assertFalse(dialog.issue_button.isEnabled())
            dialog.platform.setCurrentIndex(dialog.platform.findData('qq'))
            dialog.account.setText(ADMIN['account']); dialog.subject.setText(ADMIN['subject'])
            dialog.bind_button.click()
            self.assertTrue(dialog.issue_button.isEnabled())
            self.assertEqual(requests[-1][2]['revision'], 0)
            tab._authority({'bound': False})
            self.assertFalse(tab.btn_apply.isEnabled())
            tab._authority({'bound': True, 'admin': ADMIN})
            self.assertTrue(tab.btn_apply.isEnabled())
            self.assertIn(ADMIN['subject'], tab.authority_status.text())
        finally:
            dialog.close(); tab.close(); app.processEvents()
