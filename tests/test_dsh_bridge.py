"""DSH协议、会话边界与故障行为离线验证；不接真实DSH或微信。"""
import asyncio
import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import yaml
from src.dsh_bridge import DshBridge, DshError, GUARD, PRESET, TurnCollector, cookie_for, install_preset, local_url
from tests.test_agent_api import AgentApiTests
from tests.test_wechat_app_flow import RuntimeTestCase, raw_message


def event(kind, **data):
    return {'type': kind, 'data': {'turn': 'turn-1', **data}}


class ProtocolTests(unittest.TestCase):
    def test_loopback_only_and_no_embedded_credentials(self):
        self.assertEqual(local_url('http://127.0.0.1:10529/'), 'http://127.0.0.1:10529')
        for value in ['https://example.com:80', 'http://127.0.0.1:1@evil.test:80',
                      'http://localhost:80/api', 'http://localhost:80?token=x', 'http://localhost',
                      'http://localhost:99999']:
            with self.subTest(value=value), self.assertRaises(DshError):
                local_url(value)

    def test_cookie_is_bound_to_authority_and_not_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            secret = bytes(range(32))
            encoded = base64.urlsafe_b64encode(secret).decode().rstrip('=')
            (p / '.credentials.yaml').write_text(yaml.safe_dump({'records': {
                'client-connection/browser-session': {'kind': 'grant', 'payload': {'version': 1, 'secret': encoded}}}}), 'utf-8')
            cookie = cookie_for('http://127.0.0.1:3080', p)
            name, body, signature = cookie.split('=v1.')[0], *cookie.split('=v1.')[1].split('.')
            decoded = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
            self.assertEqual(decoded['authority'], '127.0.0.1:3080')
            self.assertEqual(decoded['expiresAt'] - decoded['issuedAt'], 3600000)
            expected = base64.urlsafe_b64encode(hmac.new(secret, body.encode(), hashlib.sha256).digest()).decode().rstrip('=')
            self.assertEqual(expected, signature)
            self.assertTrue(name.startswith('dsh-auth-'))
            self.assertNotIn(encoded, cookie)

    def test_collector_discards_reasoning_chunks_duplicates_and_old_turns(self):
        c = TurnCollector()
        self.assertIsNone(c.push(event('turn/end', reason={'kind': 'completed'})))
        c.push(event('turn/start'))
        c.push(event('assistant/chunk', text='不要重复'))
        item = event('assistant/message', step='1', message={'id':'m', 'content':[
            {'type':'thinking', 'thinking':'内部内容'}, {'type':'text', 'text':'最终答复'}]})
        c.push(item); c.push(item)
        self.assertEqual(c.push(event('turn/end', reason={'kind':'completed'})), '最终答复')

    def test_failed_turn_never_releases_partial_answer(self):
        c = TurnCollector(); c.push(event('turn/start'))
        c.push(event('assistant/message', message={'content':[{'type':'text','text':'尚未完成'}]}))
        with self.assertRaises(DshError):
            c.push(event('turn/end', reason={'kind':'error'}))

    def test_preset_install_preserves_other_plugins_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'home/profiles/desktop'; folder.mkdir(parents=True)
            (folder / 'node_modules/@deepseek-ai/dsh-agent-preset').mkdir(parents=True)
            path = folder / 'cordis.patch.yml'
            original = [{'id':'existing','name':'other'}, {'insert':[{'id':'untouched','name':'keep'}]}]
            path.write_text(yaml.safe_dump(original), 'utf-8')
            installed = install_preset(root/'app', root/'home')
            install_preset(root/'app', root/'home')
            rows = yaml.safe_load(path.read_text('utf-8'))
            self.assertEqual(rows[:2], original)
            self.assertEqual(len(rows), 3)
            self.assertTrue(Path(installed['backup']).is_file())
            self.assertEqual(rows[-1]['insert'][0]['config']['id'], PRESET)
            self.assertIn('allow: []', GUARD)
            self.assertIn('tools.guard', GUARD)
            self.assertEqual((root/'app/data/dsh_bridge/no-tools.mjs').read_text('utf-8'), GUARD)

    def test_legacy_directory_install_does_not_add_unsupported_profile_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'home/profiles/desktop'
            (folder / 'node_modules/@deepseek-ai/dsh-agent-presets').mkdir(parents=True)
            patch = folder / 'cordis.patch.yml'; patch.write_text('[]\n', 'utf-8')
            installed = install_preset(root/'app', root/'home')
            self.assertEqual(installed['format'], 'directory')
            self.assertEqual(patch.read_text('utf-8'), '[]\n')
            entries = yaml.safe_load(Path(installed['path']).read_text('utf-8'))
            self.assertEqual([r['id'] for r in entries], ['persona','no-tools'])
            self.assertTrue((root/'home/.agent-presets'/PRESET/'no-tools.mjs').is_file())


class Socket:
    def __init__(self, fail=False):
        self.fail = fail; self.frames = []; self.stream = None

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    async def send(self, text):
        open_ = json.loads(text); self.stream = open_['streamId']
        self.frames = [{'type':'item', 'streamId':self.stream, 'value': {'type':'snapshot',
            'records':[event('turn/end',reason={'kind':'completed'})]}}]

    async def recv(self):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        raise RuntimeError('断流')

    def answer(self):
        self.frames += [{'type':'item', 'streamId':self.stream, 'value':{'type':'event', 'event':e}} for e in [
            event('turn/start'), event('assistant/chunk', text='分块不能重复'),
            event('assistant/message', message={'id':'m', 'content':[{'type':'text','text':'DSH完整答复'}]}),
            event('turn/end', reason={'kind':'error' if self.fail else 'completed'})]]


class DshFlowTests(RuntimeTestCase):
    async def make_service(self):
        bot = await self.start_runtime()
        from src.agent_authority import authority
        authority(bot).bind({'platform':'desktop','account':'mock_machine','subject':'owner'})
        bot.config._config['dsh'] = {'enabled':True,'base_url':'http://127.0.0.1:3080','model':'deepseek-flash'}
        return bot

    async def test_persistent_scope_uses_initial_history_once_and_no_main_api_fallback(self):
        bot = await self.make_service()
        service = bot.dsh; calls = []; sockets = []
        def connect(*args, **kwargs):
            s = Socket(); sockets.append(s); return s
        async def rpc(client, base, method, request=None):
            calls.append((method,request))
            if method == 'session/create': return {'sessionId':'sid-'+str(len(calls))}
            if method == 'session/selectModel':
                self.assertEqual(request['provider'], 'test-provider')
            if method == 'session/prompt': sockets[-1].answer()
            return {}
        conversation = [{'role':'user','content':'旧问题'}, {'role':'assistant','content':'旧回答'}, {'role':'user','content':'现在的问题'}]
        bot.config._config['dsh']['provider'] = 'test-provider'
        message = {'kind':'group','target':'room@chatroom'}
        with patch.object(service, 'probe', AsyncMock(return_value={'preset_ready':True})), \
             patch.object(service, 'rpc', rpc), patch('src.dsh_bridge.cookie_for', return_value='opaque'), \
             patch('websockets.asyncio.client.connect', connect):
            for _ in range(2):
                self.assertEqual(await service.generate(message, conversation, '人设+当前记忆'), 'DSH完整答复')
            self.assertEqual(len([1 for m,p in calls if m=='session/create']), 1)
            payloads = [json.loads(p['content'][0]['text']) for m,p in calls if m=='session/prompt']
            self.assertEqual(len(payloads[0]['records']), 2)
            self.assertEqual(payloads[1]['records'], [])
            self.assertEqual(len(conversation), 3)
            await service.generate(dict(message,target='another@chatroom'), conversation, '人设+当前记忆')
            self.assertEqual(len([1 for m,p in calls if m=='session/create']), 2)
        self.assertEqual(bot.db.execute('SELECT COUNT(*) FROM dsh_sessions').fetchone()[0], 2)

    async def test_config_change_after_submission_discards_late_reply(self):
        bot = await self.make_service(); socket = Socket(); prompts = []
        async def rpc(client, base, method, request=None):
            if method == 'session/create': return {'sessionId':'owned'}
            if method == 'session/prompt':
                prompts.append(request)
                bot.config._config['dsh']['enabled'] = False
                socket.answer()
            return {}
        with patch.object(bot.dsh, 'probe', AsyncMock(return_value={'preset_ready':True})), \
             patch.object(bot.dsh, 'rpc', rpc), patch('src.dsh_bridge.cookie_for', return_value='opaque'), \
             patch('websockets.asyncio.client.connect', return_value=socket):
            with self.assertRaisesRegex(DshError, '设置改变'):
                await bot.dsh.generate({'kind':'private','target':'user'},[{'role':'user','content':'问题'}],'人设')
        self.assertEqual(len(prompts), 1)
        self.assertFalse(bot.bridge.sent_log)

    async def test_same_named_unverified_preset_is_not_ready(self):
        bot = await self.make_service()
        async def rpc(client, base, method, request=None):
            return {'presets':[{'id':PRESET}]} if method=='agentPresets/list' else {}
        with patch.object(bot.dsh, 'rpc', rpc), patch('src.dsh_bridge.cookie_for', return_value='opaque'), \
             patch('src.dsh_bridge.Path.home', return_value=bot.root/'unconfigured-dsh-home'):
            result = await bot.dsh.probe()
        self.assertTrue(result['connected'])
        self.assertFalse(result['preset_ready'])
        self.assertFalse(result['delivery_verified'])

    async def test_reused_session_receives_new_references_without_repeating_history(self):
        bot=await self.make_service();service=bot.dsh;calls=[];sockets=[]
        def connect(*args,**kwargs):
            socket=Socket();sockets.append(socket);return socket
        async def rpc(client,base,method,request=None):
            calls.append((method,request))
            if method=='session/create':return {'sessionId':'memory-session'}
            if method=='session/prompt':sockets[-1].answer()
            return {}
        messages=[{'role':'user','content':'旧问题'},{'role':'assistant','content':'旧回答'},{'role':'user','content':'当前问题'}]
        with patch.object(service,'probe',AsyncMock(return_value={'preset_ready':True})), \
             patch.object(service,'rpc',rpc),patch('src.dsh_bridge.cookie_for',return_value='opaque'), \
             patch('websockets.asyncio.client.connect',connect):
            await service.generate(dict(kind='private',target='owner'),messages,'当前人设')
            await service.generate(dict(kind='private',target='owner'),messages,'当前人设',
                context_references=[dict(role='user',content='本轮新检索：灯塔端口17421')])
        payloads=[json.loads(request['content'][0]['text']) for method,request in calls if method=='session/prompt']
        self.assertEqual(len(payloads),2);self.assertEqual(payloads[1]['records'],[])
        self.assertIn('17421',payloads[1]['references'][0]['content'])
        self.assertEqual(payloads[0]['references'],[])
        self.assertEqual(len([m for m,p in calls if m=='session/create']),1)

    async def test_failed_turn_stops_owned_queue_once_and_does_not_submit_again(self):
        bot = await self.make_service(); calls = []; socket = Socket(True)
        async def rpc(client, base, method, request=None):
            calls.append(method)
            if method == 'session/create': return {'sessionId':'sid'}
            if method == 'session/prompt': socket.answer()
            if method == 'session/projections': return {'values':{'inbox':{'next-turn':[{'id':'pending'}]}}}
            return {}
        with patch.object(bot.dsh, 'probe', AsyncMock(return_value={'preset_ready':True})), \
             patch.object(bot.dsh, 'rpc', rpc), patch('src.dsh_bridge.cookie_for', return_value='opaque'), \
             patch('websockets.asyncio.client.connect', return_value=socket):
            with self.assertRaises(DshError):
                await bot.dsh.generate({'kind':'private','target':'user'},[{'role':'user','content':'问题'}],'人设')
        self.assertEqual(calls.count('session/prompt'), 1)
        self.assertEqual(calls.count('session/updateQueue'), 1)
        self.assertEqual(calls.count('session/cancel'), 1)
        self.assertEqual(bot.db.execute('SELECT state FROM dsh_sessions').fetchone()[0], 'uncertain')

    async def test_runtime_dsh_reply_uses_original_send_guard_and_suppresses_mentions(self):
        bot = await self.make_service()
        self.install_ai('不应调用原模型')
        with patch.object(bot.dsh, 'generate', AsyncMock(return_value='@无关昵称 这是DSH答复')) as generate:
            bot.ingest(raw_message('鲸鱼娘 说句话'))
            await self.wait_for_send()
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(bot.bridge.sent_log[0]['text'], '这是DSH答复')
        self.assertFalse(bot.bridge.sent_log[0]['at'])

    async def test_rpc_validates_envelope_and_hides_remote_error_body(self):
        bot = await self.make_service()
        def handler(req):
            payload = json.loads(req.content)
            self.assertIn('request',payload['payload']['args'])
            return httpx.Response(200,json={'type':'server-response','rpcId':payload['rpcId'],
                'result':{'ok':False,'error':{'code':'denied','message':'secret-not-displayed'}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(DshError, '^DSH拒绝请求：denied$'):
                await bot.dsh.rpc(client,'http://127.0.0.1:3080','session/create',{})


class DshApiTests(AgentApiTests):
    def test_default_off_save_and_scope_requirement(self):
        self.assertFalse(self.client.get('/api/dsh').json()['enabled'])
        self.assertEqual(self.client.post('/api/dsh',json={'base_url':'http://evil.test:80'}).status_code,400)
        result = self.client.post('/api/dsh',json={'base_url':'http://127.0.0.1:10529'})
        self.assertTrue(result.json()['success'])
        self.assertFalse(self.runtime.config.get('dsh.enabled'))
        from src.agent_authority import authority
        auth = authority(self.runtime); auth.unbind(auth.status()['revision'])
        self.assertEqual(self.client.post('/api/dsh/probe',json={}).status_code,403)

    def test_inbound_bridge_cannot_reconfigure_dsh(self):
        from src.agent_authority import authority
        key = authority(self.runtime).issue('dsh', ['manage_console'])['token']
        result = self.client.post('/api/dsh',json={'enabled':True},headers={'Authorization':'Bearer '+key})
        self.assertEqual(result.status_code,403)


class DshUITests(unittest.TestCase):
    def test_import_copies_only_connection_fields_and_never_enables_or_saves(self):
        from PyQt6.QtWidgets import QApplication
        from src.gui.dsh_panel import DshDialog
        self.app = QApplication.instance() or QApplication([])
        calls = []
        def request(owner, method, path, data=None, **kw):
            calls.append((method,path,data))
            kw['on_done']({'success':True,'enabled':False})
        dialog = DshDialog(request)
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp)/'config.json'
            file.write_text(json.dumps({'dsh':{'baseUrl':'http://127.0.0.1:10529', 'provider':'custom-provider',
                'model':'custom-model', 'reasoningEffort':'max', 'authToken':'do-not-copy'},
                'apiKey':'also-do-not-copy'}), 'utf-8')
            with patch('src.gui.dsh_panel.QFileDialog.getOpenFileName', return_value=(str(file),'')):
                dialog.import_bridge()
        self.assertEqual(dialog.provider.text(), 'custom-provider')
        self.assertEqual(dialog.url.text(), 'http://127.0.0.1:10529')
        self.assertFalse(dialog.enabled.isChecked())
        self.assertEqual(calls, [('GET','dsh',None)])
        from PyQt6.QtWidgets import QLineEdit
        self.assertFalse(any('do-not-copy' in field.text() for field in dialog.findChildren(QLineEdit)))
        dialog.close()
