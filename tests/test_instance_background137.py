"""实例归属与后台失败状态回归；全部使用临时目录和假发送器。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.agent_authority import authority
from src.wechat_bridge import WeChatBridge
from src.wechat_sender import SendResult
from tests import test_desktop_140 as background_fixtures
from tests import test_diary_discovery136 as api_fixtures
from tests.test_wechat_app_flow import RuntimeTestCase
from tests.test_dsh_bridge import Socket


class FailureState137Tests(unittest.TestCase):
    def test_failure_cannot_reuse_window_ready_or_mutate_original_diagnostic(self):
        original = {'ready': True, 'reason': '已确认账号窗口', 'hwnd': 10}
        result = SendResult(False, '会话不符，本次未输入', False, original)
        self.assertFalse(result.diagnostic['ready'])
        self.assertEqual(result.diagnostic['reason'], result.message)
        self.assertEqual(result.diagnostic['waiting_code'], 'send_failed')
        self.assertTrue(original['ready'])

    def test_failure_preserves_specific_reason_code_and_uncertain_submission(self):
        result = SendResult(False, '结果待核实', True,
            {'ready':True,'waiting_code':'submission_uncertain','submission_state':'uncertain'})
        self.assertFalse(result.diagnostic['ready'])
        self.assertEqual(result.diagnostic['waiting_code'], 'submission_uncertain')
        self.assertEqual(result.diagnostic['submission_state'], 'uncertain')
        self.assertFalse(SendResult(False, '子进程超时').diagnostic['ready'])

    def test_bridge_reports_waiting_after_failed_send_instead_of_green(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = WeChatBridge(Path(folder))
            bridge.backend = 'wechatauto'; bridge.online = True
            bridge._wx = SimpleNamespace(SendMsg=Mock(return_value=SendResult(
                False, '当前不是目标聊天', False, {'ready':True,'hwnd':10})))
            ok, _ = bridge._send_text_locked('测试群', '答复', [])
            self.assertFalse(ok)
            self.assertEqual(bridge.status()['send_state'], 'waiting')
            self.assertIn('不是目标', bridge.status()['send_reason'])
            self.assertFalse(bridge._last_send_requires_reconnect)

    def test_partial_background_write_invalidates_check_and_preserves_draft(self):
        from src.wechat_background import send_background
        background_fixtures.BackgroundTests.setUp(self)
        self.value.SetValue.side_effect = lambda text: setattr(self.value, 'Value', text[:2])
        self.assertFalse(send_background(self.job,self.phase,self.access,self.desktop)[0])
        self.assertFalse(self.job['_diagnostic']['ready'])
        self.assertIn('回读不一致', self.job['_diagnostic']['reason'])
        self.assertEqual(self.value.Value, self.job['text'][:2])
        self.invoke.Invoke.assert_not_called()

    def test_changed_chat_before_submit_invalidates_check_without_retry(self):
        from src.wechat_background import send_background
        background_fixtures.BackgroundTests.setUp(self)
        def change(stage):
            if stage == 'send_key': self.field.Name = '另一个群'
        self.assertFalse(send_background(self.job,change,self.access,self.desktop)[0])
        self.assertFalse(self.job['_diagnostic']['ready'])
        self.assertEqual(self.value.Value, self.job['text'])
        self.invoke.Invoke.assert_not_called()


class ConnectionOwner137Tests(unittest.TestCase):
    setUp = api_fixtures.Diary136Tests.setUp
    tearDown = api_fixtures.Diary136Tests.tearDown

    def rebind(self):
        authority(self.bot).bind(dict(platform='desktop',account='local',subject='new-owner'))

    def test_connect_rebinding_during_probe_cannot_save_old_request(self):
        before = self.bot.dsh.settings()
        async def probe(**kw):
            self.rebind()
            return {'preset_ready':True}
        with patch.object(self.bot.dsh, 'probe', probe):
            response=self.client.post('/api/dsh/connect',json={'base_url':'http://127.0.0.1:5947'})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.bot.dsh.settings(),before)
        self.assertFalse(self.bot.running)

    def test_save_rebinding_during_stop_cannot_save_and_restores_run_intent(self):
        before=self.bot.dsh.settings()
        async def stop():
            self.bot.running=False
            self.rebind()
        self.bot.running=True
        try:
            with patch.object(self.bot,'stop',stop), patch.object(self.bot,'start',AsyncMock()) as start:
                response=self.client.post('/api/dsh',json={'base_url':'http://127.0.0.1:5947'})
                start.assert_awaited_once()
        finally:self.bot.running=False
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.bot.dsh.settings(),before)

    def test_save_rebinding_while_waiting_for_lock_cannot_save(self):
        before=self.bot.dsh.settings();old=self.bot.lock
        owner=self
        class Lock:
            async def __aenter__(self):owner.rebind()
            async def __aexit__(self,*args):pass
        try:
            self.bot.lock=Lock()
            response=self.client.post('/api/dsh',json={'base_url':'http://127.0.0.1:5947'})
        finally:self.bot.lock=old
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.bot.dsh.settings(),before)


class InstanceWorkspace137Tests(RuntimeTestCase):
    async def test_new_scopes_get_distinct_cwd_and_keep_existing_shared_files(self):
        bot=await self.start_runtime()
        authority(bot).bind(dict(platform='desktop',account='local',subject='owner'))
        bot.config._config['dsh']={'enabled':True,'base_url':'http://127.0.0.1:3080',
            'model':'deepseek-flash','provider':'local'}
        legacy=bot.root/'data/dsh_bridge/workspace';legacy.mkdir(parents=True)
        marker=legacy/'existing.md';marker.write_text('旧工作区保留','utf-8')
        sockets=[];folders=[]
        def connect(*args,**kwargs):
            socket=Socket();sockets.append(socket);return socket
        async def rpc(client,base,method,request=None):
            if method=='session/create':
                folders.append(Path(request['cwd']))
                return {'sessionId':'isolated-'+str(len(folders))}
            if method=='session/prompt':sockets[-1].answer()
            return {}
        with patch.object(bot.dsh,'probe',AsyncMock(return_value={'preset_ready':True})), \
                patch.object(bot.dsh,'rpc',rpc), patch('src.dsh_bridge.cookie_for',return_value='opaque'), \
                patch('websockets.asyncio.client.connect',connect):
            for target in ('one@chatroom','two@chatroom','one@chatroom'):
                await bot.dsh.generate({'kind':'group','target':target},[{'role':'user','content':'测试'}],'人设')
            self.assertEqual(len(folders),2)
            bot.config._config['dsh']['base_url']='http://127.0.0.1:5947'
            await bot.dsh.generate({'kind':'group','target':'one@chatroom'},[{'role':'user','content':'测试'}],'人设')
        self.assertEqual(len(set(folders)),3)
        for folder in folders:
            self.assertEqual(folder.parent,bot.root/'data/dsh_bridge/workspaces')
            self.assertRegex(folder.name,r'^[a-f0-9]{64}$')
            self.assertTrue(folder.is_dir())
        self.assertEqual(marker.read_text('utf-8'),'旧工作区保留')
        self.assertFalse(bot.bridge.sent_log)
