"""账号绑定及 WeFlow 只读工具的隔离回归；不访问真实聊天或发送。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.wechat_bridge import WeChatBridge
from src.wechat_paths import account_root, path_account
from src.weflow_api import local_url, read_json
from src.wechat_app import create_app
from test_wechat_app_flow import RuntimeTestCase


class AccountBinding151Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / '其他盘 中文' / 'xwechat_files'
        for name in ('wxid_first_aaaa', 'wxid_second_bbbb'):
            (self.root / name / 'db_storage/message').mkdir(parents=True)
        self.account = 'wxid_second_bbbb'
        self.storage = self.root / self.account / 'db_storage'
        self.database = self.storage / 'message/message_0.db'
        self.database.touch()
        self.bridge = WeChatBridge(Path(self.tmp.name) / 'data', backend='wechatauto')

    def test_explicit_levels_and_db_file_keep_account(self):
        for value in (self.storage.parent, self.storage, self.database.parent, self.database):
            with self.subTest(value=value):
                self.assertEqual(account_root(str(value)), str(self.root))
                self.assertEqual(path_account(str(value)), self.account)
        self.assertEqual(path_account(str(self.root)), '')

    def test_invalid_or_media_file_is_not_accepted(self):
        self.assertEqual(path_account(str(self.database.with_name('missing.db'))), '')
        media = self.database.with_suffix('.png')
        media.touch()
        self.assertEqual(account_root(str(media)), '')

    def test_selected_path_pins_second_account_without_auto_guess(self):
        self.bridge.db_dir = str(self.database)
        db = SimpleNamespace(account=self.account, db_dir=str(self.root), wxid='wxid_second',
                             get_self_info=lambda:{'username': 'wxid_second'})
        with patch('wechatauto.db.WeChatDB', return_value=db) as factory, \
                patch.object(self.bridge, 'choose_account', side_effect=AssertionError('must not guess')), \
                patch('src.wechat_sender.IsolatedWeChatSender'):
            self.assertTrue(self.bridge.connect()[0])
        self.assertEqual(factory.call_args.kwargs['account'], self.account)
        self.assertEqual(self.bridge.account_dir, self.account)

    def test_conflicting_choice_never_creates_database_or_sender(self):
        self.bridge.db_dir = str(self.storage)
        self.bridge.configured_account = 'wxid_first_aaaa'
        with patch('wechatauto.db.WeChatDB') as database, patch('src.wechat_sender.IsolatedWeChatSender') as sender:
            ok, error = self.bridge.connect()
        self.assertFalse(ok)
        self.assertIn('不一致', error)
        database.assert_not_called()
        sender.assert_not_called()

    def test_missing_explicit_account_never_falls_back(self):
        self.bridge.db_dir = str(self.root)
        self.bridge.configured_account = 'wxid_missing_1111'
        with patch('wechatauto.db.WeChatDB') as database:
            ok, error = self.bridge.connect()
        self.assertFalse(ok)
        self.assertIn('不存在', error)
        database.assert_not_called()

    def test_library_switch_is_rejected_before_hint_or_sender(self):
        self.bridge.db_dir = str(self.storage)
        wrong = SimpleNamespace(account='wxid_first_aaaa', db_dir=str(self.root))
        with patch('wechatauto.db.WeChatDB', return_value=wrong), \
                patch.object(self.bridge, 'write_account_hint') as hint, \
                patch('src.wechat_sender.IsolatedWeChatSender') as sender:
            ok, error = self.bridge.connect()
        self.assertFalse(ok)
        self.assertIn('账号不符', error)
        self.assertIsNone(self.bridge._db)
        self.assertFalse(self.bridge.online)
        hint.assert_not_called()
        sender.assert_not_called()

    def test_disconnected_status_does_not_keep_stale_ready(self):
        self.bridge.send_environment = {'ready': True}
        status = self.bridge.status()
        self.assertEqual(status['read_state'], 'disconnected')
        self.assertEqual(status['send_state'], 'disconnected')
        self.assertEqual(status['client_login_state'], 'unknown')
        self.assertFalse(status['send_environment']['ready'])


class WeFlowReader151Tests(unittest.IsolatedAsyncioTestCase):
    def test_only_literal_loopback_http_allowed(self):
        for url in ('http://127.0.0.1:5031/', 'http://[::1]:5031'):
            self.assertEqual(local_url(url), url.rstrip('/'))
        for url in ('https://127.0.0.1:5031', 'http://example.com', 'http://localhost:5031',
                    'http://192.168.1.1', 'http://user:secret@127.0.0.1', 'http://127.0.0.1/api',
                    'http://127.0.0.1?access_token=secret', 'http://127.0.0.1:99999'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                local_url(url)

    async def request_with(self, response, endpoint='/api/v1/sessions'):
        original = httpx.AsyncClient
        calls = []
        def handler(request):
            calls.append(request)
            return response(request) if callable(response) else response
        def client(**kw):
            self.assertFalse(kw['trust_env'])
            self.assertFalse(kw['follow_redirects'])
            return original(transport=httpx.MockTransport(handler), **kw)
        with patch('src.weflow_api.httpx.AsyncClient', side_effect=client):
            result = await read_json('http://127.0.0.1:5031', 'local-test-token', endpoint, {'limit': 1})
        self.assertEqual(calls[0].headers['authorization'], 'Bearer local-test-token')
        self.assertNotIn('token', str(calls[0].url))
        self.assertEqual(calls[0].method, 'GET')
        return result

    async def test_query_sends_header_and_ignores_proxy_environment(self):
        result = await self.request_with(httpx.Response(200, json={'success': True, 'sessions': []}))
        self.assertTrue(result['success'])

    async def test_sensitive_server_errors_not_disclosed(self):
        for response in (httpx.Response(403, text='secret-from-server'),
                         httpx.Response(200, json={'success': False, 'error': 'secret-from-server'}),
                         httpx.Response(200, text='secret-from-server'),
                         httpx.Response(302, headers={'location': 'http://example.com/secret-from-server'})):
            with self.subTest(status=response.status_code), self.assertRaises(ValueError) as exc:
                await self.request_with(response)
            self.assertNotIn('secret-from-server', str(exc.exception))

    async def test_oversized_response_refused(self):
        with self.assertRaisesRegex(ValueError, '4MB'):
            await self.request_with(httpx.Response(200, content=b'x' * (4 * 1024 * 1024 + 1)))

    async def test_timeout_is_actionable_and_sanitized(self):
        def timeout(request):
            raise httpx.ReadTimeout('secret-from-server', request=request)
        with self.assertRaisesRegex(ValueError, '超时'):
            await self.request_with(timeout)

    async def test_write_endpoints_and_empty_token_never_create_client(self):
        with patch('src.weflow_api.httpx.AsyncClient') as client:
            with self.assertRaises(ValueError):
                await read_json('http://127.0.0.1:5031', 'key', '/api/v1/send')
            with self.assertRaises(ValueError):
                await read_json('http://127.0.0.1:5031', '', '/api/v1/sessions')
        client.assert_not_called()


class WeFlowAPI151Tests(RuntimeTestCase):
    async def asyncSetUp(self):
        self.bot = await self.start_runtime()
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_token_encrypted_and_empty_save_preserves_then_clear_removes(self):
        result = (await self.client.post('/api/weflow/config', json={'token': 'test-weflow-secret'})).json()
        self.assertTrue(result['success'])
        self.assertTrue(result['token_set'])
        self.assertNotIn('test-weflow-secret', json.dumps(result))
        self.assertNotIn('test-weflow-secret', self.bot.config.config_path.read_text('utf-8'))
        await self.client.post('/api/weflow/config', json={'token': ''})
        self.assertEqual(self.bot.secret('weflow.token'), 'test-weflow-secret')
        result = (await self.client.get('/api/weflow/config')).json()
        self.assertTrue(result['token_set'])
        await self.client.post('/api/weflow/config', json={'clear_token': True})
        self.assertFalse(self.bot.secret('weflow.token'))

    async def test_probe_requires_authenticated_sessions_and_does_not_save_draft(self):
        with patch('src.weflow_api.read_json', new_callable=AsyncMock) as read:
            read.side_effect = [{'status': 'ok'}, {'sessions': []}]
            result = (await self.client.post('/api/weflow/probe', json={'token': 'draft-only'})).json()
        self.assertTrue(result['success'])
        self.assertEqual(result['send_state'], 'not_provided')
        self.assertEqual(result['login_state'], 'unknown')
        self.assertFalse(self.bot.secret('weflow.token'))
        self.assertEqual(read.await_count, 2)

    async def test_messages_preview_does_not_enqueue_or_leak_raw_media_fields(self):
        before = self.bot.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]
        with patch('src.weflow_api.read_json', new_callable=AsyncMock) as read:
            read.return_value = {'messages': [{'serverId': '1234567890123456789', 'content': '<b>原文</b>',
                                               'rawContent': 'private-key', 'mediaLocalPath': 'private-path'}],
                                 'hasMore': True}
            result = (await self.client.post('/api/weflow/messages', json={'talker': '123@chatroom', 'token': 'draft'})).json()
        self.assertTrue(result['success'])
        self.assertTrue(result['hasMore'])
        self.assertEqual(result['messages'][0]['serverId'], '1234567890123456789')
        self.assertNotIn('private-', json.dumps(result))
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], before)
        self.assertFalse(self.bot.bridge.sent_log)
        self.assertEqual(read.call_args.args[3]['media'], 0)

    async def test_save_failure_rolls_back_token_and_url(self):
        with patch.object(self.bot.config, 'save', side_effect=OSError('disk full')):
            result = (await self.client.post('/api/weflow/config', json={'token': 'not-saved'})).json()
        self.assertFalse(result['success'])
        self.assertFalse(self.bot.secret('weflow.token'))

    async def test_restart_loads_saved_encrypted_token_and_clear_persists(self):
        from src.wechat_app import WeChatRuntime
        await self.client.post('/api/weflow/config', json={'token': 'restart-secret', 'url': 'http://127.0.0.1:5042'})
        restarted = WeChatRuntime(root=self.root, config_path=self.bot.config.config_path,
                                 db_path=self.root/'restart.sqlite')
        try:
            self.assertEqual(restarted.secret('weflow.token'), 'restart-secret')
            self.assertEqual(restarted.config.get('weflow.url'), 'http://127.0.0.1:5042')
        finally:
            await restarted.close()
        await self.client.post('/api/weflow/config', json={'clear_token': True})
        restarted = WeChatRuntime(root=self.root, config_path=self.bot.config.config_path,
                                 db_path=self.root/'restart2.sqlite')
        try:
            self.assertFalse(restarted.secret('weflow.token'))
        finally:
            await restarted.close()

    async def test_missing_talker_or_remote_url_refused_without_request(self):
        with patch('src.weflow_api.read_json', new_callable=AsyncMock) as read:
            for path, body in [('messages', {}), ('sessions', {'url': 'http://10.0.0.1'})]:
                result = (await self.client.post('/api/weflow/' + path, json=body)).json()
                self.assertFalse(result['success'])
        read.assert_not_called()


class WeFlowGUI151Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_plain_text_preview_and_connection_change_invalidate_export(self):
        from src.gui.weflow_dialog import WeFlowDialog
        calls = []
        def request(owner, method, path, body, **kw):
            calls.append((method, path, body))
            if method == 'GET':
                result = {'success': True, 'token_set': True}
            elif path.endswith('sessions'):
                result = {'success': True, 'sessions': [{'username': '123@chatroom', 'displayName': '测试群'}]}
            else:
                result = {'success': True, 'messages': [{'content': '<b>原文</b>'}], 'hasMore': True}
            kw['on_done'](result)
        dialog = WeFlowDialog(None, request)
        dialog.sessions()
        dialog.preview()
        self.assertIn('<b>原文</b>', dialog.text.toPlainText())
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertEqual(calls[-1][2]['talker'], '123@chatroom')
        dialog.url.setText('http://127.0.0.1:5032')
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertIsNone(dialog.bundle)
        self.assertEqual(dialog.rooms.count(), 0)
        dialog.close()
