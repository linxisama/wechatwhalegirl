"""原文召回、统一预算与配置贯通；不接触真实微信或外部模型。"""
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from src.agent_authority import authority
from src.agent_context import build, DEFAULTS, settings, relevance
from src.agent_workspace import workspace
from src.wechat_app import WeChatRuntime, create_app

ROOT=Path(__file__).resolve().parents[1]


class AgentRecall135Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root=Path(self.tmp.name);shutil.copytree(ROOT/'styles',root/'styles')
        config=root/'config.wechat.yaml';config.write_text('wechat_only:\n  backend: mock\n','utf-8')
        self.bot=WeChatRuntime(root=root,config_path=config)
        self.ai=self.bot.ai.chat=AsyncMock(return_value='灯塔项目使用17421端口。')
        self.client=TestClient(create_app(self.bot));self.client.__enter__()
        authority(self.bot).bind(dict(platform='desktop',account='local',subject='owner'))
        self.area=workspace(self.bot);self.persona=self.bot.style.active_name

    def tearDown(self):
        self.client.__exit__(None,None,None);asyncio.run(self.bot.close());self.tmp.cleanup()

    def seed(self,session='main',persona=None):
        key,_=self.area.identity();rows=[]
        for i in range(20):
            text='灯塔项目的端口是17421' if i==0 else f'第{i}次无关闲聊'
            rows += [(key,session,f'old-{i}',persona or self.persona,'user',text,float(i)),
                     (key,session,f'old-{i}',persona or self.persona,'assistant','已了解'+str(i),float(i)+.1)]
        self.bot.db.executemany('INSERT INTO agent_dialogue VALUES(?,?,?,?,?,?,?)',rows);self.bot.db.commit()

    def talk(self,text='灯塔项目的端口是多少',session='main'):
        return self.client.post('/api/agent/chat',json=dict(session_id=session,request_id='recall-135001',text=text)).json()

    def test_old_raw_dialogue_is_recalled_after_recent_ten_rounds(self):
        self.seed();result=self.talk()
        self.assertTrue(result['success'])
        self.assertEqual(result['context']['history_turns'],10)
        self.assertEqual(result['context']['recalled'],1)
        content=self.ai.call_args.args[0][0]['content']
        self.assertIn('17421',content);self.assertIn('old-0',content)
        self.assertIn('仅作为引用',content);self.assertEqual(self.ai.call_count,1)

    def test_other_session_or_persona_not_recalled(self):
        self.seed('other')
        self.assertEqual(self.talk()['context']['recalled'],0)
        self.assertNotIn('17421',str(self.ai.call_args.args[0]))
        self.bot.db.execute('DELETE FROM agent_turns');self.bot.db.execute('DELETE FROM agent_dialogue');self.bot.db.commit()
        self.seed(persona='different_persona')
        self.assertEqual(self.talk()['context']['recalled'],0)

    def test_unrelated_recent_memory_not_forced_into_model(self):
        self.area.remember('我喜欢抹茶冰淇淋',self.persona)
        self.area.remember('我叫小田',self.persona)
        result=self.talk('修复数据库连接报错')
        content=str(self.ai.call_args.args[0])
        self.assertNotIn('抹茶',content);self.assertIn('小田',content)
        self.assertEqual(result['context']['memories'],1)

    def test_context_settings_persist_and_zero_disables_old_recall(self):
        self.seed();body=dict(history_turns=2,max_chars=1500,recall_limit=0)
        self.assertTrue(self.client.post('/api/agent/workspace/context',json=body).json()['success'])
        self.assertEqual(self.client.get('/api/agent/workspace').json()['context'],body)
        self.assertEqual(settings(self.bot),body)
        result=self.talk()
        self.assertEqual(result['context']['history_turns'],2)
        self.assertEqual(result['context']['recalled'],0)
        from src.config_manager import ConfigManager
        restored=ConfigManager(self.bot.config.config_path);restored.load()
        self.assertEqual(restored.get('agent.context'),body)

    def test_runtime_restart_preserves_agent_and_dsh_sections_without_starting_them(self):
        self.bot.config.set('dsh.enabled',True)
        self.bot.config.set('dsh.base_url','http://127.0.0.1:15678')
        self.bot.config.set('agent.owner_commands_enabled',True)
        body=dict(history_turns=4,max_chars=2000,recall_limit=2)
        self.assertTrue(self.client.post('/api/agent/workspace/context',json=body).json()['success'])
        other=WeChatRuntime(root=self.bot.root,config_path=self.bot.config.config_path,db_path=self.bot.root/'restart.sqlite')
        try:
            self.assertEqual(settings(other),body)
            self.assertTrue(other.config.get('agent.owner_commands_enabled'))
            self.assertTrue(other.dsh.settings()['enabled'])
            self.assertEqual(other.dsh.settings()['base_url'],'http://127.0.0.1:15678')
            self.assertFalse(other.running);self.assertFalse(other.bridge.online)
        finally:asyncio.run(other.close())

    def test_invalid_settings_rejected_without_changing_previous(self):
        before=settings(self.bot)
        for body in (dict(history_turns=0),dict(max_chars=999),dict(recall_limit=6),
                     dict(history_turns=True),dict(history_turns='3'),dict(extra='unknown')):
            with self.subTest(body=body):
                self.assertEqual(self.client.post('/api/agent/workspace/context',json=body).status_code,422)
        self.assertEqual(settings(self.bot),before)

    def test_bridge_memory_reader_cannot_change_context(self):
        key=authority(self.bot).issue('reader',['read_memory'],1)
        response=self.client.post('/api/agent/bridge/execute',headers={'Authorization':'Bearer '+key['token']},
            json=dict(action='agent.context.configure',request_id='context-135-001',body=dict(history_turns=1)))
        self.assertEqual(response.status_code,403);self.assertEqual(settings(self.bot),DEFAULTS)

    def test_memory_search_reports_more_than_display_window(self):
        key,_=self.area.identity()
        self.bot.db.executemany('INSERT INTO agent_memories VALUES(?,?,?,?,?,?,1)',
            [(key,str(i),'',f'工程计划{i}','explicit',float(i)) for i in range(105)])
        self.bot.db.commit()
        result=self.client.get('/api/agent/workspace/memories',params={'query':'工程计划'}).json()
        self.assertEqual(len(result['items']),100);self.assertEqual(result['total'],105);self.assertTrue(result['has_more'])
        self.assertEqual(self.client.get('/api/agent/workspace/memories',params={'query':'9999'}).json()['total'],0)

    def test_single_budget_includes_references_and_preserves_current_question(self):
        self.seed();history=self.area.history('main','*')
        history[-2]['content']='最近的问题'*800
        history[-1]['content']='最近的回答'*800+'最后提议'
        question='灯塔项目端口是多少'+('当前细节'*500)
        rows,stats=build(history,[],question,self.persona,dict(history_turns=10,max_chars=1000,recall_limit=3))
        self.assertEqual(rows[-1]['content'],question)
        self.assertLessEqual(stats['chars'],1000);self.assertEqual(stats['recalled'],0)
        self.assertIn('最后提议',str(rows));self.assertEqual(stats['chars'],sum(len(r['content']) for r in rows[:-1]))

    def test_generic_short_query_does_not_pull_unrelated_records(self):
        self.assertEqual(relevance('你好 可以吗','你好，我喜欢蓝色'),0)
        self.assertGreater(relevance('Lighthouse port','Lighthouse uses port 17421'),0)

    def test_bad_saved_configuration_uses_safe_defaults(self):
        self.bot.config.set('agent.context',dict(history_turns=True,max_chars=-1,recall_limit='5'))
        self.assertEqual(settings(self.bot),DEFAULTS)

    def test_forgotten_statement_and_old_tool_receipts_not_recalled(self):
        self.seed()
        item=self.area.remember('灯塔项目的端口是17421',self.persona);self.area.forget(item['id'])
        self.assertEqual(self.talk()['context']['recalled'],0)
        self.assertNotIn('17421',str(self.ai.call_args.args[0]))
        history=[dict(role='user',content='/读笔记 灯塔项目.md',persona=self.persona),
                 dict(role='assistant',content='秘密端口',persona=self.persona)]
        history += [dict(role=role,content='最近讨论',persona=self.persona) for i in range(10) for role in ('user','assistant')]
        rows,stats=build(history,[],'灯塔项目',self.persona,DEFAULTS)
        self.assertEqual(stats['recalled'],0);self.assertNotIn('秘密端口',str(rows))
