"""连接草稿和管理页按钮的离线回归；HTTP 服务与模型均不连接真实微信。"""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication, QWidget, QPushButton

import wechat_gui as gui
from src.ai_engine import AIEngine
from src.gui.session_management import SessionManagementDialog
from src.wechat_app import WeChatRuntime, create_app
from test_wechat_app_flow import write_config, ROOT

APP = QApplication.instance() or QApplication([])


class ProbeAndManagementHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT/'styles', self.root/'styles')
        self.config = write_config(self.root)
        self.bot = WeChatRuntime(root=self.root, config_path=self.config, db_path=self.root/'messages.sqlite')
        app = create_app(self.bot)
        app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.bot.close()
        self.temp.cleanup()

    async def send_probe(self, handler, body=None, path='probe'):
        body = body or {'api_url':'https://draft.test/v1', 'api_key':'', 'model':'kimi-k2.6', 'api_style':'auto'}
        real_client=httpx.AsyncClient
        clients=[]
        def client_factory(**kwargs):
            provider=real_client(transport=httpx.MockTransport(handler),**kwargs)
            clients.append(provider)
            return provider
        with patch('src.ai_engine.httpx.AsyncClient',side_effect=client_factory):
            response = await self.client.post('/api/ai/'+path, json=body)
        self.assertTrue(all(client.is_closed for client in clients))
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_draft_uses_saved_key_without_saving_or_waiting_for_runtime_locks(self):
        before = self.config.read_bytes()
        requests = []
        def provider(request):
            requests.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer test-key')
            self.assertEqual(request.url.host, 'draft.test')
            if request.method=='GET':
                return httpx.Response(200, json={'data':[{'id':'kimi-k2.6'}]})
            body=json.loads(request.content)
            self.assertEqual(body['model'], 'kimi-k2.6')
            self.assertEqual(body['thinking'], {'type':'disabled'})
            return httpx.Response(200, json={'choices':[{'message':{'content':'OK'}}]})
        async with self.bot.lock, self.bot.ai_lock:
            result = await asyncio.wait_for(self.send_probe(provider), 3)
        self.assertTrue(result['success'])
        self.assertTrue(result['model_listed'])
        self.assertEqual(result['preview'], 'OK')
        self.assertIn('ms',result['message'])
        self.assertEqual(len(requests), 2)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.bot.ai.model, 'deepseek-chat')
        self.assertFalse(self.bot.bridge.online)

    async def test_missing_models_endpoint_does_not_hide_successful_chat(self):
        def provider(request):
            return (httpx.Response(404, json={'error':{'message':'no models'}}) if request.method=='GET'
                    else httpx.Response(200, json={'choices':[{'message':{'content':'OK'}}]}))
        result=await self.send_probe(provider)
        self.assertTrue(result['success'])
        self.assertIsNone(result['model_listed'])
        self.assertIn('404', result['models_error'])

    async def test_chat_error_and_secret_echo_are_displayed_without_leaking_key(self):
        def provider(request):
            return (httpx.Response(200,json={'data':[{'id':'other'}]}) if request.method=='GET' else
                    httpx.Response(401,json={'error':{'message':'invalid test-key'}}))
        result=await self.send_probe(provider)
        self.assertFalse(result['success'])
        self.assertIn('401',result['message'])
        self.assertNotIn('test-key',json.dumps(result))
        self.assertFalse(result['model_listed'])

    async def test_timeout_is_reported_and_not_retried(self):
        posts=[]
        def provider(request):
            if request.method=='GET':
                return httpx.Response(200,json={'data':[{'id':'kimi-k2.6'}]})
            posts.append(request)
            raise httpx.ReadTimeout('slow',request=request)
        result=await self.send_probe(provider)
        self.assertFalse(result['success'])
        self.assertIn('超时',result['message'])
        self.assertEqual(len(posts),1)

    async def test_draft_model_list_uses_typed_key_and_current_address(self):
        def provider(request):
            self.assertEqual(request.method,'GET')
            self.assertEqual(str(request.url),'https://different.test/v1/models')
            self.assertEqual(request.headers['Authorization'],'Bearer unsaved-key')
            return httpx.Response(200,json={'data':[{'id':'draft-model'}]})
        result=await self.send_probe(provider,{'api_url':'https://different.test/v1','api_key':'unsaved-key'},'models')
        self.assertEqual(result['models'],['draft-model'])

    async def test_anthropic_probe_uses_its_protocol(self):
        def provider(request):
            self.assertEqual(request.headers['x-api-key'],'new-key')
            if request.method=='GET':
                return httpx.Response(200,json={'data':[{'id':'claude-test'}]})
            self.assertEqual(request.url.path,'/v1/messages')
            self.assertNotIn('thinking',json.loads(request.content))
            return httpx.Response(200,json={'content':[{'type':'text','text':'OK'}]})
        result=await self.send_probe(provider,{'api_url':'https://draft.test/v1','api_key':'new-key','model':'claude-test','api_style':'anthropic'})
        self.assertTrue(result['success'])

    async def test_invalid_settings_are_rejected_before_network(self):
        with patch.object(AIEngine,'_get_client',side_effect=AssertionError('不应联网')):
            for body in ({'api_url':'','model':'ok'}, {'api_url':'https://draft.test','model':'Wrong Display Name'}):
                response=await self.client.post('/api/ai/probe',json=body)
                self.assertFalse(response.json()['success'])
            response=await self.client.post('/api/ai/probe',json={'api_style':'bad'})
            self.assertEqual(response.status_code,422)

    async def test_offline_pause_resume_and_enable_do_not_wait_for_connection_lock(self):
        with patch.object(self.bot.bridge,'connect',side_effect=AssertionError('本机设置不应连接微信')):
            async with self.bot.lock:
                for body in ({'paused':True},{'paused':False},{'enabled':True}):
                    response=await asyncio.wait_for(self.client.post('/api/plugins/robot_admin',json=body),1)
                    self.assertTrue(response.json()['success'])
        self.bot.plugins.reload()
        self.assertTrue(self.bot.plugins._plugins['robot_admin'].enabled)
        self.assertFalse(self.bot.plugins._plugins['robot_admin'].get('paused'))

    async def test_status_separates_other_account_backlog_without_changing_it(self):
        self.bot.db.execute("INSERT INTO inbox(key,mode,account,payload,created,state) VALUES(?,?,?,?,?,?)",
                            ('other','wechat','old-account','{}',1,'pending'))
        self.bot.db.commit()
        response=await self.client.get('/api/status')
        result=response.json()
        self.assertEqual(result['pending'],1)
        self.assertEqual(result['pending_current'],0)
        self.assertEqual(result['pending_other_accounts'],1)
        self.assertEqual(self.bot.db.execute('SELECT state FROM inbox').fetchone()[0],'pending')

    async def test_password_api_enforces_new_policy_without_modifying_valid_password(self):
        for value in ('1','管理123','x'*128):
            response=await self.client.post('/api/management/password',json={'password':value})
            self.assertTrue(response.json()['success'])
        before=self.bot.admin_sessions.path.read_bytes()
        for value in ('','x'*129,'bad!','bad password','bad_name','😀',123,None):
            response=await self.client.post('/api/management/password',json={'password':value})
            self.assertEqual(response.status_code,400)
            self.assertIn('特殊符号',response.json()['detail'])
            self.assertEqual(self.bot.admin_sessions.path.read_bytes(),before)


class ManagementButtonTests(unittest.TestCase):
    def setUp(self):
        self.windows=[]
        self.posts=[]
        self.snapshot={'success':True,'account':'测试账号','owners':['wxid_owner'],
                       'sessions':[],'reply_paused':False,'password_state':'unset'}

    def tearDown(self):
        for window in self.windows:
            window.deleteLater()
        APP.processEvents()

    def request(self,owner,method,path,*args,**kwargs):
        if method=='POST':
            self.posts.append((path,args[0],kwargs['on_done']))
        else:
            kwargs['on_done'](self.snapshot if path=='management/sessions' else
                              {'connection':{'online':False},'pending':106,'pending_current':0,'pending_other_accounts':106})

    def dialog(self):
        window=SessionManagementDialog(self.request,auto_refresh=False)
        self.windows.append(window)
        window.refresh()
        return window

    def click(self,window,label):
        next(b for b in window.findChildren(QPushButton) if b.text()==label).click()

    def test_blank_inputs_have_feedback_without_http(self):
        window=self.dialog()
        self.click(window,'直接绑定微信账号')
        self.assertIn('请先输入',window.result.text())
        self.click(window,'保存管理员密码')
        self.assertIn('1–128',window.result.text())
        self.assertEqual(self.posts,[])

    def test_all_local_controls_dispatch_and_keep_success_after_refresh(self):
        window=self.dialog()
        for label,body in [('暂停自动回复',{'paused':True}),('恢复自动回复',{'paused':False}),('启用管理员私聊指令',{'enabled':True})]:
            self.click(window,label)
            self.assertIn('正在处理',window.result.text())
            self.assertEqual(self.posts[-1][:2],('plugins/robot_admin',body))
            self.assertFalse(next(b for b in window.findChildren(QPushButton) if b.text()==label).isEnabled())
            self.posts[-1][2]({'success':True,'message':'操作已保存'})
            self.assertIn('操作已保存',window.result.text())
            window.refresh()
            self.assertIn('操作已保存',window.result.text())
        self.assertIn('本账号待处理：0',window.status.text())
        self.assertIn('106',window.status.text())
        self.assertIn('微信离线',window.status.text())

    def test_password_failure_retains_input_success_clears_and_updates_hint(self):
        window=self.dialog()
        window.password.setText('examplePassword')
        self.click(window,'保存管理员密码')
        self.posts[-1][2]({'success':False,'message':'写入失败'})
        self.assertEqual(window.password.text(),'examplePassword')
        self.assertIn('写入失败',window.result.text())
        self.click(window,'保存管理员密码')
        self.snapshot['password_state']='set'
        self.posts[-1][2]({'success':True,'message':'密码已保存'})
        self.assertEqual(window.password.text(),'')
        self.assertIn('密码已保存',window.result.text())
        self.assertIn('已设置',window.guidance.text())

    def test_manual_bind_and_unbind_dispatch_and_show_failure(self):
        window=self.dialog()
        window.manual_target.setText('wxid_new')
        self.click(window,'直接绑定微信账号')
        self.assertEqual(self.posts[-1][:2],('management/owner',{'target':'wxid_new','bind':True,'manual':True}))
        self.posts[-1][2]({'success':False,'message':'请先测试微信连接'})
        self.assertIn('请先测试微信连接',window.result.text())
        self.click(window,'解除所选管理员')
        self.assertEqual(self.posts[-1][:2],('management/owner',{'target':'wxid_owner','bind':False}))
        self.posts[-1][2]({'success':True,'message':'已解除'})
        self.assertIn('已解除',window.result.text())

    def test_modal_dialog_uses_host_background_runner(self):
        calls=[]
        class Host(QWidget):
            def _run_api(self,*args):
                calls.append(args)
        host=Host()
        self.windows.append(host)
        dialog=SessionManagementDialog(gui.call_api,parent=host,auto_refresh=False)
        self.windows.append(dialog)
        with patch.object(gui,'_request',side_effect=AssertionError('不应阻塞 UI')):
            dialog.refresh()
        self.assertEqual([row[1] for row in calls],['management/sessions','status'])
        self.assertFalse(dialog.refresh_button.isEnabled())
        calls[0][-1](self.snapshot)
        calls[1][-1]({'connection':{'online':False}})
        self.assertTrue(dialog.refresh_button.isEnabled())

    def test_settings_test_and_models_use_current_form(self):
        window=gui.SettingsTab()
        self.windows.append(window)
        window.api_url.setText('https://unsaved.test/v1')
        window.api_key.setText('draft-key')
        window.model_name.setText('draft-model')
        callbacks=[]
        def request(owner,method,path,data=None,**kwargs):
            callbacks.append((method,path,data,kwargs['on_done']))
        with patch.object(gui,'call_api',side_effect=request):
            window.btn_diagnose.click()
            self.assertFalse(window.btn_diagnose.isEnabled())
            self.assertEqual(callbacks[-1][:3],('POST','ai/probe',{'api_url':'https://unsaved.test/v1','api_key':'draft-key','model':'draft-model','api_style':'auto','request_timeout':20}))
            callbacks[-1][-1]({'success':True,'message':'成功 · 100 ms'})
            self.assertTrue(window.btn_diagnose.isEnabled())
            self.assertIn('100 ms',window.probe_lbl.text())
            window.btn_models.click()
            self.assertEqual(callbacks[-1][1],'ai/models')
            self.assertEqual(callbacks[-1][2]['api_key'],'draft-key')
            callbacks[-1][-1]({'success':False,'message':'凭据无效'})
            self.assertTrue(window.btn_models.isEnabled())
            self.assertIn('凭据无效',window.save_lbl.text())

    def test_unsaved_timeout_reaches_probe_and_gui_wait_budget(self):
        window=gui.SettingsTab();self.windows.append(window)
        window.request_timeout.setValue(47)
        calls=[]
        with patch.object(gui,'call_api',side_effect=lambda *args,**kwargs:calls.append((args,kwargs))):
            window.btn_diagnose.click()
        self.assertEqual(calls[0][0][3]['request_timeout'],47)
        self.assertEqual(calls[0][1]['timeout'],52)
        self.assertIn('47',window.probe_lbl.text())

    def test_password_button_allows_short_and_maximum_values_but_rejects_symbols(self):
        window=self.dialog()
        self.assertIn('1–128',window.password.placeholderText())
        for value in ('1','管理Admin123','a'*128):
            window.password.setText(value)
            self.click(window,'保存管理员密码')
            self.assertEqual(self.posts[-1][:2],('management/password',{'password':value}))
            self.posts[-1][2]({'success':True,'message':'密码已保存'})
            self.assertEqual(window.password.text(),'')
        sent=len(self.posts)
        for value in ('bad!','a b','a_b','x'*129):
            window.password.setText(value)
            self.click(window,'保存管理员密码')
            self.assertIn('特殊符号',window.result.text())
            self.assertEqual(window.password.text(),value)
            self.assertEqual(len(self.posts),sent)
