"""人设日记、只读进程发现及旧热激活封禁；真实模型/微信均不调用。"""
import asyncio
from datetime import datetime,timedelta
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,Mock,patch
from types import SimpleNamespace

from fastapi.testclient import TestClient
from src.agent_authority import authority
from src.agent_diary import TZ
from src.agent_workspace import workspace
from src.dsh_discovery import candidates,discover
from src.wechat_app import WeChatRuntime,create_app

ROOT=Path(__file__).resolve().parents[1]


class Diary136Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True);root=Path(self.tmp.name)
        shutil.copytree(ROOT/'styles',root/'styles');cfg=root/'config.wechat.yaml'
        cfg.write_text('wechat_only:\n  backend: mock\n','utf-8')
        self.bot=WeChatRuntime(root=root,config_path=cfg)
        authority(self.bot).bind(dict(platform='desktop',account='local',subject='owner'))
        self.area=workspace(self.bot);self.ai=self.bot.ai.chat=AsyncMock(return_value='今天我和主人聊了灯塔项目，心里暖暖的。明天也想陪着他把事情做好。')
        self.client=TestClient(create_app(self.bot));self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None);asyncio.run(self.bot.close());self.tmp.cleanup()

    def seed(self,offset=0):
        stamp=datetime.now(TZ)-timedelta(days=offset)
        stamp=stamp.replace(hour=12,minute=0,second=0).timestamp() if offset else time.time()
        key,_=self.area.identity();persona=self.bot.style.active_name
        self.bot.db.executemany('INSERT INTO agent_dialogue VALUES(?,?,?,?,?,?,?)',[
            (key,'daily','source-136',persona,'user','今天一起做好灯塔项目',stamp),
            (key,'daily','source-136',persona,'assistant','我陪你整理项目',stamp+.01)])
        self.bot.db.commit()
        return datetime.fromtimestamp(stamp,TZ).date().isoformat()

    def test_ai_writes_in_persona_once_and_statistics_never_overwrite(self):
        day=self.seed();result=self.client.post('/api/agent/workspace/diary',json={}).json()
        self.assertTrue(result['success']);self.assertEqual(result['content'],self.ai.return_value)
        self.assertIn('第一人称',self.ai.call_args.kwargs['system_prompt'])
        self.assertIn('不能编造主人',self.ai.call_args.kwargs['system_prompt'])
        self.assertIn('灯塔',str(self.ai.call_args.args[0]));self.assertEqual(self.ai.call_args.kwargs['retries'],1)
        self.assertEqual(self.ai.call_args.kwargs['max_tokens'],600)
        self.area.diary()
        self.assertEqual(self.client.get('/api/agent/workspace/diary').json()['content'],result['content'])
        self.assertTrue(self.client.post('/api/agent/workspace/diary',json={}).json()['replayed'])
        self.assertEqual(self.ai.call_count,1)
        self.assertTrue(list((self.area.folder().parent/'diaries').glob(day+'*.md')))
        self.assertFalse(self.bot.bridge.sent_log)

    def test_no_records_or_model_failure_never_fabricates_diary(self):
        self.assertFalse(self.client.post('/api/agent/workspace/diary').json()['success'])
        self.ai.assert_not_called()
        self.seed();self.ai.side_effect=RuntimeError('do-not-leak-secret')
        result=self.client.post('/api/agent/workspace/diary').json()
        self.assertFalse(result['success']);self.assertNotIn('do-not-leak',json.dumps(result))
        self.assertEqual(result['content'],'')
        self.assertFalse(self.client.post('/api/agent/workspace/diary').json()['success'])
        self.assertEqual(self.ai.call_count,1)

    def test_automatic_diary_uses_ended_day_once_and_respects_disable(self):
        day=self.seed(1)
        asyncio.run(self.bot.diary_writer.tick())
        self.assertEqual(self.bot.diary_writer.read(day)['status'],'done')
        asyncio.run(self.bot.diary_writer.tick());self.assertEqual(self.ai.call_count,1)
        self.bot.config.set('agent.diary.enabled',False)
        asyncio.run(self.bot.diary_writer.tick());self.assertEqual(self.ai.call_count,1)

    def test_cancelled_generation_not_repeated_after_restart(self):
        self.seed()
        self.ai.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):asyncio.run(self.bot.diary_writer.generate())
        self.ai.side_effect=None
        result=asyncio.run(self.bot.diary_writer.generate())
        self.assertFalse(result['success']);self.assertEqual(result['status'],'running')
        self.assertEqual(self.ai.call_count,1)

    def test_rebinding_discards_inflight_diary(self):
        self.seed()
        async def model(*args,**kwargs):
            authority(self.bot).bind(dict(platform='desktop',account='local',subject='new_owner'))
            return '旧主人的日记'
        self.bot.ai.chat=model
        from src.agent_authority import AuthorityError
        with self.assertRaises(AuthorityError):asyncio.run(self.bot.diary_writer.generate())
        self.assertEqual(self.bot.diary_writer.read()['content'],'')

    def test_bridge_writer_without_read_cannot_generate_private_diary(self):
        self.seed();key=authority(self.bot).issue('writer',['write_memory'],1)
        response=self.client.post('/api/agent/bridge/execute',headers={'Authorization':'Bearer '+key['token']},
            json=dict(action='workspace.diary',request_id='diary-write136'))
        self.assertFalse(response.json()['success']);self.ai.assert_not_called()

    def test_read_diary_is_readonly_and_settings_persist(self):
        self.seed();self.client.get('/api/agent/workspace/diary');self.ai.assert_not_called()
        self.assertTrue(self.client.post('/api/agent/workspace/diary-settings',json={'enabled':False}).json()['success'])
        from src.config_manager import ConfigManager
        cfg=ConfigManager(self.bot.config.config_path);cfg.load()
        self.assertFalse(cfg.get('agent.diary.enabled'))

    def test_diary_settings_disk_failure_restores_live_value(self):
        with patch.object(self.bot.config,'save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.client.post('/api/agent/workspace/diary-settings',json={'enabled':False})
        self.assertTrue(self.bot.config.get('agent.diary.enabled'))

    def test_sensitive_turn_not_sent_to_diary_model(self):
        self.seed();key,_=self.area.identity();persona=self.bot.style.active_name
        self.bot.db.execute('INSERT INTO agent_dialogue VALUES(?,?,?,?,?,?,?)',
            (key,'daily','secret-turn',persona,'user','密码是hidden-password136',time.time()));self.bot.db.commit()
        self.assertTrue(self.client.post('/api/agent/workspace/diary').json()['success'])
        self.assertNotIn('hidden-password136',str(self.ai.call_args))

    def test_recent_chat_defers_automatic_diary_and_group_speaker_is_not_owner(self):
        day=self.seed(1);self.seed()
        asyncio.run(self.bot.diary_writer.tick());self.ai.assert_not_called()
        msg=dict(kind='group',target='room@chatroom',sender_id='wxid_other',text='群友今天整理笔记',
            _persona_name=self.bot.style.active_name)
        self.bot.db.execute('INSERT INTO inbox(key,mode,account,room,payload,state,reply,created) VALUES(?,?,?,?,?,?,?,?)',
            ('diary-group',self.bot.mode,self.bot.account(),'group:room@chatroom',json.dumps(msg),'sent','知道了',time.time()))
        self.bot.db.commit()
        records=self.bot.diary_writer.records(datetime.now(TZ).date(),self.bot.style.active_name)
        self.assertEqual(next(r for r in records if r['source']=='已回复微信')['speaker'],'群成员')

    def test_dsh_discovery_is_local_only_and_connect_checks_before_save(self):
        with patch('src.dsh_discovery.discover',AsyncMock(return_value=dict(success=True,candidates=[]))):
            self.assertTrue(self.client.post('/api/dsh/discover').json()['success'])
        reader=authority(self.bot).issue('reader',['read_console'],1)
        response=self.client.post('/api/dsh/discover',headers={'Authorization':'Bearer '+reader['token']})
        self.assertEqual(response.status_code,403)
        before=self.bot.dsh.settings()
        with patch.object(self.bot.dsh,'probe',AsyncMock(return_value=dict(preset_ready=False,message='缺少人设'))):
            self.assertFalse(self.client.post('/api/dsh/connect',json=dict(base_url='http://127.0.0.1:5947')).json()['success'])
        self.assertEqual(self.bot.dsh.settings(),before)
        with patch.object(self.bot.dsh,'probe',AsyncMock(return_value=dict(preset_ready=True))):
            result=self.client.post('/api/dsh/connect',json=dict(base_url='http://127.0.0.1:5947',model='local-model',provider='local-provider')).json()
        self.assertTrue(result['success']);self.assertTrue(self.bot.dsh.settings()['enabled'])
        self.assertEqual(self.bot.dsh.settings()['base_url'],'http://127.0.0.1:5947')
        self.assertFalse(self.bot.running)


class Discovery136Tests(unittest.TestCase):
    def test_release_excludes_backup_implementations(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('release136',ROOT/'tools/make_release.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'src').mkdir()
            for name in ('kept.py','old.py.bak','old.py.bak-hot-20261026','old.py.backup'):
                (root/'src'/name).write_text('placeholder','utf-8')
            module.ROOT=root
            self.assertEqual([arc for p,arc in module.iter_source()],['src/kept.py'])
    def test_only_named_process_tree_listeners_are_considered(self):
        processes=[dict(pid=1,parent=0,name='DeepSeek Harness.exe'),dict(pid=2,parent=1,name='node.exe'),
                   dict(pid=3,parent=99,name='node.exe')]
        ports=[dict(pid=2,address='127.0.0.1',port=5947),dict(pid=3,address='127.0.0.1',port=9999),
               dict(pid=1,address='192.168.1.1',port=5555),dict(pid=2,address='0.0.0.0',port=5948)]
        rows=candidates(processes,ports)
        self.assertEqual({r['base_url'] for r in rows},{'http://127.0.0.1:5947','http://127.0.0.1:5948'})

    def test_protocol_mismatch_is_not_a_dsh_endpoint_and_nothing_is_saved(self):
        from src.dsh_bridge import DshError
        async def rpc(client,base,method,*args):
            if base.endswith('2'):raise DshError('bad')
            return {'presets':[dict(id='whale-wechat-chat')]} if method=='agentPresets/list' else {}
        inventory=([dict(pid=1,parent=0,name='dsh.exe')],[dict(pid=1,address='127.0.0.1',port=p) for p in (1,2)])
        with patch('src.dsh_discovery.cookie_for',return_value='not-exported'):
            result=asyncio.run(discover(SimpleNamespace(rpc=rpc),inventory))
        self.assertEqual(len(result['candidates']),1);self.assertEqual(result['unverified_ports'],1)
        self.assertNotIn('not-exported',json.dumps(result))

    def test_legacy_hot_flags_cannot_call_process_activation(self):
        from src.wechat_send_worker import restrict_driver,execute,_hot_activate
        hot=Mock();driver=SimpleNamespace(ensure_window=lambda **kw:True,_hot_activate_accessibility=hot)
        restrict_driver(driver,hot_activate=True)
        self.assertFalse(driver._hot_activate_accessibility(1));hot.assert_not_called()
        self.assertFalse(_hot_activate({'hot_activate':True})['ok'])
        with patch('src.wechat_send_worker._hot_activate',side_effect=AssertionError('process modified')), \
             patch('src.wechat_background.send_background',return_value=(False,'wait')):
            self.assertEqual(execute(dict(action='text',hot_activate=True,background_send=True)),(False,'wait'))
