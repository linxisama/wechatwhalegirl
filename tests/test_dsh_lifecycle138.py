"""DSH权限撤销/上下文隔离与表单迟到响应，使用假协议流。"""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock,patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from tests import test_dsh_bridge as fixture
from src.agent_authority import authority,AuthorityError
from src.dsh_bridge import DshError
from src.dsh_bridge import TurnCollector


class DshErrorMessages138(unittest.TestCase):
    def test_known_provider_quota_and_auth_errors_are_actionable_without_raw_secrets(self):
        for code,expected in [('QUOTA','额度已用尽'),('AUTH','认证失败'),('RATE_LIMIT','限流')]:
            collector=TurnCollector()
            collector.push(fixture.event('turn/start'))
            with self.assertRaisesRegex(DshError,expected) as failed:
                collector.push(fixture.event('turn/end',reason={'kind':'error','error':{'code':code,'message':'private-secret'}}))
            self.assertNotIn('private-secret',str(failed.exception))


class DshLifecycle138(fixture.RuntimeTestCase):
    make_service=fixture.DshFlowTests.make_service

    async def test_rebind_during_probe_does_not_submit_old_data(self):
        bot=await self.make_service();rpc=AsyncMock()
        async def probe(**kw):
            authority(bot).bind(dict(platform='desktop',account='new',subject='new'))
            return {'preset_ready':True}
        with patch.object(bot.dsh,'probe',probe),patch.object(bot.dsh,'rpc',rpc):
            with self.assertRaises(DshError):await bot.dsh.generate(dict(kind='private',target='owner'),[dict(role='user',content='private')],'人设')
        rpc.assert_not_called()

    async def test_revoked_bridge_before_prompt_is_rechecked(self):
        bot=await self.make_service();calls=[];socket=fixture.Socket();state={'valid':True}
        def check():
            if not state['valid']:raise AuthorityError('已撤销')
        async def rpc(client,base,method,request=None):
            calls.append(method)
            if method=='session/create':return {'sessionId':'owned'}
            if method=='session/selectModel':state['valid']=False
            return {}
        with patch.object(bot.dsh,'probe',AsyncMock(return_value={'preset_ready':True})),patch.object(bot.dsh,'rpc',rpc),patch('src.dsh_bridge.cookie_for',return_value='opaque'):
            with self.assertRaises(DshError):await bot.dsh.generate(dict(kind='private',target='owner'),[dict(role='user',content='private')],'人设',check_access=check)
        self.assertNotIn('session/prompt',calls)

    async def test_permission_scope_does_not_reuse_privileged_model_history(self):
        bot=await self.make_service();calls=[];sockets=[]
        def connect(*args,**kw):
            socket=fixture.Socket();sockets.append(socket);return socket
        async def rpc(client,base,method,request=None):
            calls.append(method)
            if method=='session/create':return {'sessionId':'scope-'+str(len(calls))}
            if method=='session/prompt':sockets[-1].answer()
            return {}
        with patch.object(bot.dsh,'probe',AsyncMock(return_value={'preset_ready':True})),patch.object(bot.dsh,'rpc',rpc),patch('src.dsh_bridge.cookie_for',return_value='opaque'),patch('websockets.asyncio.client.connect',connect):
            for scope in ('manage_console,read_memory','manage_console','manage_console'):
                await bot.dsh.generate(dict(kind='private',target='owner'),[dict(role='user',content='question')],'人设',context_scope=scope)
        self.assertEqual(calls.count('session/create'),2)


_APP=None
class DshGui138(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        global _APP
        _APP=QApplication.instance() or QApplication([])

    def test_initial_load_and_connect_late_callback_keep_new_edit(self):
        from src.gui.dsh_panel import DshDialog
        pending=[]
        def request(*args,**kw):pending.append(kw['on_done'])
        dialog=DshDialog(request)
        dialog.url.setText('http://127.0.0.1:9999')
        pending.pop()(dict(success=True,enabled=False,base_url='http://127.0.0.1:1234'))
        self.assertEqual(dialog.url.text(),'http://127.0.0.1:9999')
        dialog.connect_selected();dialog.model.setText('new-model')
        pending.pop()(dict(success=True,enabled=True))
        self.assertFalse(dialog.enabled.isChecked());self.assertEqual(dialog.model.text(),'new-model')
        dialog.close()


if __name__=='__main__':unittest.main()
