"""真实本机API、持久工作区和假模型回合；不连接微信或外部模型。"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from src.agent_authority import authority
from src.agent_workspace import workspace, WorkspaceError, WorkspaceAccess
from src.agent_workbench import owner_matches
from src.dialogue_context import temperature, recent_turns
from src.wechat_app import WeChatRuntime, create_app

ROOT = Path(__file__).resolve().parents[1]


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        shutil.copytree(ROOT/'styles', root/'styles')
        config = root/'config.wechat.yaml'
        config.write_bytes((ROOT/'config.wechat.yaml').read_bytes())
        self.bot = WeChatRuntime(root=root,config_path=config,db_path=root/'memory.sqlite3')
        self.bot.config.set('dsh.enabled',False)
        self.ai = self.bot.ai.chat = AsyncMock(return_value='可以，我继续整理。')
        self.cm = TestClient(create_app(self.bot))
        self.client = self.cm.__enter__()
        authority(self.bot).bind({'platform':'wechat','account':'wxid_bot','subject':'wxid_owner'})
        self.area = workspace(self.bot)

    def tearDown(self):
        self.cm.__exit__(None,None,None)
        asyncio.run(self.bot.close())
        self.tmp.cleanup()

    def talk(self,text,rid='request-0001',session='main',persona=''):
        return self.client.post('/api/agent/chat',json=dict(session_id=session,request_id=rid,text=text,persona=persona)).json()

    def test_short_confirmation_keeps_offer_and_replays_without_second_model(self):
        self.ai.return_value='我可以帮你创建一份角色配置，需要继续吗？'
        self.assertTrue(self.talk('帮我整理角色')['success'])
        self.ai.return_value='好的，接着整理。'
        self.assertTrue(self.talk('也行','request-0002')['success'])
        args = self.ai.call_args.args[0]
        self.assertEqual([r['role'] for r in args],['user','assistant','user'])
        self.assertIn('需要继续吗', args[-2]['content'])
        self.assertIn('也行',self.ai.call_args.kwargs['system_prompt'])
        self.assertTrue(self.talk('也行','request-0002')['replayed'])
        self.assertEqual(self.ai.call_count,2)

    def test_persona_switch_keeps_conversation_meaning_but_current_prompt(self):
        self.talk('帮我写一份角色配置')
        persona=next(r['name'] for r in self.bot.style.list_styles() if r['name'] != self.bot.style.active_name)
        self.assertTrue(self.talk('继续','request-0002',persona=persona)['success'])
        self.assertIn('角色配置',json.dumps(self.ai.call_args.args[0],ensure_ascii=False))
        self.assertIn(self.bot.style.get(persona).system_prompt,self.ai.call_args.kwargs['system_prompt'])
        self.assertEqual(len(self.client.get('/api/agent/history',params={'session_id':'main','persona':persona}).json()['messages']),4)

    def test_sessions_and_rebinding_do_not_leak(self):
        self.talk('甲会话机密')
        self.talk('乙问题','request-0002',session='second')
        self.assertNotIn('甲会话机密',json.dumps(self.ai.call_args.args[0],ensure_ascii=False))
        old=self.area.info()['workspace']
        authority(self.bot).bind({'platform':'wechat','account':'wxid_bot','subject':'wxid_new'})
        self.assertNotEqual(old,self.area.info()['workspace'])
        self.assertEqual(self.area.history('main','*'),[])

    def test_notes_conflicts_and_path_rejection(self):
        row=self.area.write('偏好.md','简短回复','')
        self.assertEqual(self.area.read('偏好.md')['content'],'简短回复')
        self.area.write('偏好.md','新内容',row['version'])
        with self.assertRaises(WorkspaceError): self.area.write('偏好.md','旧内容',row['version'])
        for name in ('../config.md',r'C:\test.md','CON.txt','a/b.md','x.md:secret'):
            with self.subTest(name=name),self.assertRaises(WorkspaceError): self.area.read(name)
        with self.assertRaises(WorkspaceError): self.area.write('大.md','字'*23000,'')

    def test_linked_note_refused(self):
        target=Path(self.tmp.name)/'other.txt';target.write_text('private')
        link=self.area.folder()/'linked.md'
        try: link.symlink_to(target)
        except OSError: self.skipTest('本机不允许创建符号链接')
        with self.assertRaises(WorkspaceError): self.area.read('linked.md')

    def test_memory_dedup_learning_forgetting_expiry_preserves_chat(self):
        self.assertTrue(self.talk('我喜欢简洁的回答')['success'])
        rows=self.area.memories(self.bot.style.active_name)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['category'],'user')
        self.talk('我喜欢简洁的回答','request-0002')
        self.assertEqual(len(self.area.memories(self.bot.style.active_name)),1)
        self.area.forget(rows[0]['id'])
        self.talk('我喜欢简洁的回答','request-0003')
        self.assertEqual(self.area.memories(self.bot.style.active_name),[])
        self.assertEqual(len(self.area.history('main','*')),6)
        self.area.learn('我叫小田',self.bot.style.active_name,'evidence')
        key,_=self.area.identity()
        self.bot.db.execute('UPDATE agent_memory_meta SET expires=? WHERE workspace=?',(time.time()-1,key));self.bot.db.commit()
        self.assertEqual(self.area.memories(self.bot.style.active_name),[])

    def test_model_error_is_idempotent_and_does_not_fabricate_history(self):
        self.ai.side_effect=RuntimeError('secret-API-key')
        result=self.talk('问个问题')
        self.assertFalse(result['success'])
        self.assertNotIn('secret',json.dumps(result))
        self.assertEqual(self.area.history('main','*'),[])
        self.assertTrue(self.talk('问个问题')['replayed'])
        self.assertEqual(self.ai.call_count,1)

    def test_readonly_grants_and_stale_access(self):
        access=WorkspaceAccess(self.area,{'read_memory'})
        with self.assertRaises(WorkspaceError): access.remember('不能写入')
        authority(self.bot).unbind()
        with self.assertRaises(Exception): access.files()

    def test_owner_is_stable_account_and_private_identity(self):
        bot=SimpleNamespace(self_id='wxid_bot')
        with patch('src.agent_workbench.authority',return_value=SimpleNamespace(status=lambda:authority(self.bot).status())):
            msg=dict(kind='private',target='wxid_owner',sender_id='wxid_owner')
            self.assertTrue(owner_matches(bot,msg))
            for modified in (dict(msg,kind='group'),dict(msg,sender_id='wxid_other'),dict(msg,target='wxid_other')):
                self.assertFalse(owner_matches(bot,modified))

    def test_mcp_export_does_not_execute_or_embed_credentials(self):
        row=self.client.post('/api/agent/workspace/connector').json()
        self.assertTrue(row['success'])
        script=Path(row['script'])
        payload='not-json\n'+json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'})+'\n'
        process=subprocess.run([sys.executable,str(script)],input=payload,text=True,encoding='utf-8',capture_output=True,timeout=10)
        self.assertEqual(process.returncode,0,process.stderr)
        responses=[json.loads(s) for s in process.stdout.splitlines()]
        self.assertIn('error',responses[0])
        self.assertIn('tools',responses[1]['result'])
        script.write_text('local modifications','utf-8')
        self.assertEqual(self.client.post('/api/agent/workspace/connector').status_code,400)

    def test_history_limit_and_temperature_bounds(self):
        rows=[]
        for i in range(40):rows += [dict(role='user',content=str(i)),dict(role='assistant',content='回答'+str(i))]
        self.assertEqual(len(recent_turns(rows)),20)
        self.assertIn('回答39',recent_turns(rows)[-1]['content'])
        rows=[dict(role='user',content='很长'*6000),dict(role='assistant',content='故事'*6000+'最后的提议')]
        self.assertIn('最后的提议',recent_turns(rows)[-1]['content'])
        self.assertLessEqual(sum(len(r['content']) for r in recent_turns(rows)),8000)
        self.assertLess(temperature(.7,'修复报错'),.7)
        self.assertGreater(temperature(.7,'写个开心故事'),.7)
        self.assertEqual(temperature(.7,'写故事',enabled=False),.7)
        self.assertGreaterEqual(temperature(0,'认真'),0)
        self.assertLessEqual(temperature(2,'哈哈'),2)

    def test_parallel_messages_wait_for_previous_answer(self):
        from src.agent_workbench import chat
        async def run():
            entered=asyncio.Event();release=asyncio.Event();seen=[]
            async def model(messages,**kwargs):
                seen.append(messages)
                if len(seen)==1:
                    entered.set();await release.wait()
                    return '要继续创建角色吗？'
                return '好的，继续创建。'
            self.bot.ai.chat=model
            first=asyncio.create_task(chat(self.bot,'parallel','parallel-0001','创建角色'))
            await entered.wait()
            second=asyncio.create_task(chat(self.bot,'parallel','parallel-0002','也行'))
            await asyncio.sleep(.01)
            self.assertEqual(len(seen),1)
            release.set()
            self.assertTrue((await first)['success']);self.assertTrue((await second)['success'])
            self.assertIn('要继续创建角色吗',seen[1][-2]['content'])
        asyncio.run(run())

    def test_bridge_readonly_cannot_write_or_chat_and_revocation_applies(self):
        credential=authority(self.bot).issue('local-agent',['read_memory'],1)
        headers={'Authorization':'Bearer '+credential['token']}
        def execute(action,body=None):
            return self.client.post('/api/agent/bridge/execute',headers=headers,json={
                'action':action,'request_id':'bridge-0001','body':body or {}})
        self.assertTrue(execute('workspace.info').json()['success'])
        self.assertEqual(execute('workspace.write',dict(name='secret.md',content='no',version='')).status_code,403)
        self.assertEqual(execute('agent.chat',dict(session_id='main',request_id='inner-0001',text='你好')).status_code,403)
        self.assertFalse((self.area.folder()/'secret.md').exists())
        authority(self.bot).revoke(credential['id'])
        self.assertEqual(execute('workspace.info').status_code,401)

    def test_plugin_tool_fault_does_not_stop_other_plugin(self):
        from src.plugins import PluginBase,PluginMeta
        class Bad(PluginBase):
            META=PluginMeta(id='faulty_workbench',name='损坏工具',description='测试')
            def agent_tools(self):raise SystemExit('should be isolated')
        class Good(PluginBase):
            META=PluginMeta(id='good_workbench',name='笔记工具',description='测试')
            def agent_tools(self):return [dict(name='hello',capability='read_memory',description='测试读取')]
            async def agent_invoke(self,name,arguments,access):return '健康插件仍可工作'
        self.bot.plugins._plugins['faulty_workbench']=Bad(self.bot.plugins)
        self.bot.plugins._plugins['good_workbench']=Good(self.bot.plugins)
        result=self.client.get('/api/agent/workspace/tools').json()
        self.assertIn('good_workbench.hello',[r['name'] for r in result['tools']])
        self.assertTrue(self.bot.plugins.is_quarantined('faulty_workbench'))
        self.assertTrue(self.talk('/技能 good_workbench.hello')['success'])

    def test_revoke_during_model_wait_discards_private_result_and_history(self):
        from src.agent_control import ConsoleControl
        from src.agent_authority import AuthorityError
        credential=authority(self.bot).issue('local-agent',['manage_console','read_memory'],1)
        actor=authority(self.bot).authenticate(credential['token'])
        control=ConsoleControl(create_app(self.bot),self.bot)
        async def run():
            entered=asyncio.Event(); release=asyncio.Event()
            async def model(*args,**kwargs):
                entered.set();await release.wait();return '不能泄露的旧结果'
            self.bot.ai.chat=model
            raw=dict(action='agent.chat',request_id='outer-revoke-001',body=dict(
                session_id='revoke',request_id='inner-revoke-001',text='问个问题'))
            task=asyncio.create_task(control.run(raw,actor))
            await entered.wait()
            authority(self.bot).revoke(credential['id']); release.set()
            with self.assertRaises(AuthorityError): await task
            self.assertEqual(self.area.history('revoke','*'),[])
            state=self.bot.db.execute('SELECT state FROM agent_operations WHERE request_id=?',('outer-revoke-001',)).fetchone()[0]
            self.assertEqual(state,'running')
        asyncio.run(run())

    def test_revoked_plugin_access_does_not_write_or_quarantine_healthy_plugin(self):
        from src.plugins import PluginBase,PluginMeta
        from src.agent_workbench import invoke_extension
        from src.agent_authority import AuthorityError
        credential=authority(self.bot).issue('local-agent',['write_memory'],1)
        actor=authority(self.bot).authenticate(credential['token'])
        class Writer(PluginBase):
            META=PluginMeta(id='guarded_writer',name='受控笔记',description='测试')
            def agent_tools(self):return [dict(name='write',capability='write_memory',description='测试')]
            async def agent_invoke(plugin,name,args,access):
                authority(self.bot).revoke(credential['id'])
                access.write('不应创建.md','拒绝','')
                return '已写入'
        self.bot.plugins._plugins['guarded_writer']=Writer(self.bot.plugins)
        with self.assertRaises(AuthorityError):
            asyncio.run(invoke_extension(self.bot,'guarded_writer.write',{},actor['capabilities'],
                lambda:authority(self.bot).validate_actor(actor)))
        self.assertFalse((self.area.folder()/'不应创建.md').exists())
        self.assertFalse(self.bot.plugins.is_quarantined('guarded_writer'))

    def test_manual_memory_survives_learning_and_can_reconfirm_expired_item(self):
        persona=self.bot.style.active_name
        self.area.learn('我喜欢蓝色',persona,'one')
        row=self.area.memories(persona)[0];key,_=self.area.identity()
        self.bot.db.execute('UPDATE agent_memory_meta SET expires=1 WHERE workspace=?',(key,));self.bot.db.commit()
        self.assertEqual(self.area.memories(persona),[])
        self.area.remember('我喜欢蓝色',persona)
        self.assertEqual(self.area.memories(persona)[0]['expires'],0)
        self.area.learn('我喜欢蓝色',persona,'two')
        self.assertEqual(self.area.memories(persona)[0]['expires'],0)
        self.assertEqual(self.area.memories(persona)[0]['id'],row['id'])


class WindowNameTests(unittest.TestCase):
    def test_known_accessibility_hint_and_exact_chat_name(self):
        from src.wechat_chat_name import clean_chat_name
        for name in ('一群鱼 按住 Ctrl + Win 使用','一群鱼按住Ctrl+Win使用','一群鱼按住鼠标 语音输入文字'):
            self.assertEqual(clean_chat_name(name,'一群鱼'),'一群鱼')
        self.assertEqual(clean_chat_name('讨论按住 Ctrl + Win 使用案例'),'讨论按住 Ctrl + Win 使用案例')
        self.assertEqual(clean_chat_name('按住 Ctrl + Win 使用','按住 Ctrl + Win 使用'),'按住 Ctrl + Win 使用')
        self.assertNotEqual(clean_chat_name('一群鱼二按住 Ctrl + Win 使用','一群鱼'),'一群鱼')

    def test_production_window_prepare_never_restores_minimized(self):
        from src.wechat_window import install_window_discovery, inspect_window
        access=Mock(); access.candidates.return_value=[dict(hwnd=1,pid=2,minimized=True)]
        uia=SimpleNamespace()
        with patch('src.wechat_window.NativeWindowAccess',return_value=access),patch('src.interactive_desktop.interactive_desktop_status',return_value=(True,'')):
            install_window_discovery(uia)
            self.assertFalse(uia.ensure_window())
            self.assertFalse(inspect_window(access=access,desktop_status=lambda:(True,''),allow_restore=False)['ready'])
        access.restore.assert_not_called()
        access.control.assert_not_called()  # 最小化在前置判断拒绝，体检和发送准备均不读取控件。

    def test_id_whitelist_cannot_be_impersonated_by_nickname(self):
        from src.conversation_access import matches
        self.assertFalse(matches(dict(target='wxid_other',sender_name='wxid_owner'),['wxid_owner']))
        self.assertFalse(matches(dict(target='other@chatroom',target_name='ok@chatroom'),['ok@chatroom']))
        self.assertTrue(matches(dict(target='ok@chatroom'),['ok@chatroom']))
        self.assertFalse(matches(dict(target='wxid_other',sender_name='小田'),['小田'],True))
        self.assertTrue(matches(dict(target='wxid_other',sender_name='小田'),['小田']))

    def test_background_hint_sends_once_and_other_group_never_written(self):
        from tests.test_desktop_140 import BackgroundTests
        from src.wechat_background import send_background
        fixture=BackgroundTests();fixture.setUp()
        fixture.field.Name='测试群 按住 Ctrl + Win 使用'
        self.assertTrue(send_background(fixture.job,fixture.phase,fixture.access,fixture.desktop)[0])
        fixture.invoke.Invoke.assert_called_once()
        fixture.value.SetValue.reset_mock();fixture.value.Value=''
        fixture.field.Name='测试群二 按住 Ctrl + Win 使用'
        self.assertFalse(send_background(fixture.job,fixture.phase,fixture.access,fixture.desktop)[0])
        fixture.value.SetValue.assert_not_called()

    def test_token_throttle_keeps_latest_offer_before_confirmation(self):
        from src.plugins.token_saver import compact
        rows=[{'role':'user','content':'本会话相关旧记录'+'旧资料'*200},
              {'role':'user','content':'整理角色'}, {'role':'assistant','content':'需要继续创建配置吗？'},
              {'role':'user','content':'也行'}]
        packed,stats=compact(rows,max_prompt_chars=60,max_prompt_tokens=90,preserve_last_pair=True)
        self.assertIn('需要继续创建配置吗',str(packed))
        self.assertNotIn('旧资料',str(packed))
        self.assertEqual(packed[-1]['content'],'也行')
        self.assertNotIn('_token_continuity',str(packed))
