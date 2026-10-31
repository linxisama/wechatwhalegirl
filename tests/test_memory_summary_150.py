"""本地记录、群总结、人设切换的隔离验收，不向真实微信发送。"""
import asyncio
import copy
import json
import os
import time
import sqlite3
import unittest
from unittest.mock import patch, AsyncMock

import httpx
from test_wechat_app_flow import RuntimeTestCase, raw_message, GROUP, OTHER_GROUP
from src.wechat_app import create_app
from src.chat_state import persona_key
from src.conversation_store import ConversationStore


class MemorySummary150Tests(RuntimeTestCase):
    async def asyncSetUp(self):
        self.bot = await self.start_runtime()
        self.ai = self.install_ai('已核对，答案是 42。')
        self.app = create_app(self.bot); self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app),base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    def record(self, mid='1', target=GROUP, content='项目会议改到周五', kind='group', **extra):
        message = dict(id=mid,kind=kind,target=target,text=content,sender_id='wxid_a',sender_name='阿明',created=time.time()-10)
        message.update(extra)
        self.bot.conversations.observe(message,self.bot)
        return message

    def rows(self, target=GROUP, kind='group'):
        return self.bot.conversations.records(self.bot,kind,target)[0]

    def request_body(self, **extra):
        return dict(kind='group',target=GROUP,account=self.bot.account(),**extra)

    async def test_unaddressed_messages_are_archived_without_reply(self):
        self.bot.ingest(raw_message('项目会议改到周五',local_id=31001))
        self.assertEqual(len(self.rows()),1)
        self.assertFalse(self.bot.bridge.sent_log)
        self.assertFalse(self.ai.calls)

    async def test_archive_persists_when_store_reopens(self):
        self.record()
        connection=sqlite3.connect(self.root/'messages1.sqlite3')
        try:
            store=ConversationStore(connection)
            self.assertEqual(store.records(self.bot,'group',GROUP)[1],1)
        finally:
            connection.close()
        self.assertEqual(self.rows()[0]['content'],'项目会议改到周五')

    async def test_group_private_and_account_scopes_are_separate(self):
        self.record('1',GROUP,'甲群事项')
        self.record('2',OTHER_GROUP,'乙群秘密')
        self.record('3','wxid_a','私聊秘密',kind='private')
        self.assertEqual([r['content'] for r in self.rows()],['甲群事项'])
        with patch.object(self.bot,'account',return_value='other_account'):
            self.assertEqual(self.rows(),[])
            self.assertEqual(self.bot.conversations.rooms(self.bot),[])

    async def test_context_reads_configured_ten_and_omits_current(self):
        for index in range(14):self.record(str(index),content=f'对话 {index}',created=time.time()-100+index)
        current=dict(id='14',kind='group',target=GROUP,created=time.time(),sender_id='wxid_a')
        context=self.bot.conversations.context(current,self.bot,10)
        self.assertEqual(len(context),10)
        self.assertEqual(context[0]['user'],'对话 4')
        self.assertEqual(context[-1]['user'],'对话 13')
        self.assertNotIn('乙群',json.dumps(context,ensure_ascii=False))

    async def test_other_persona_bot_replies_are_not_reused(self):
        msg=self.record();msg['_persona_key']=persona_key(self.bot.style.get().prompt_text())
        self.bot.conversations.sent(msg,'旧人设口头禅',self.bot)
        self.bot.style.save_persona(dict(name='new150',system_prompt='我是新的助手'))
        self.bot.activate_persona('new150')
        context=self.bot.conversations.context(dict(msg,id='new',created=time.time()),self.bot)
        self.assertIn('项目会议',json.dumps(context,ensure_ascii=False))
        self.assertNotIn('旧人设口头禅',json.dumps(context,ensure_ascii=False))

    async def test_export_merge_is_idempotent_and_does_not_queue(self):
        self.record();bundle=self.bot.conversations.export(self.bot,'group',GROUP)
        for _ in range(2):
            response=await self.client.post('/api/dialogue/archive/import',json=self.request_body(bundle=bundle,mode='merge'))
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.assertFalse(self.bot.bridge.sent_log)

    async def test_training_export_has_user_and_sent_assistant_roles(self):
        message=self.record();message['_persona_key']=persona_key(self.bot.style.get().prompt_text())
        self.bot.conversations.sent(message,'周五，已记下',self.bot)
        sample=self.bot.conversations.export(self.bot,'group',GROUP)['training_samples'][0]
        self.assertEqual([r['role'] for r in sample['messages']],['user','assistant'])
        self.assertEqual(sample['messages'][1]['content'],'周五，已记下')

    async def test_restore_replace_is_atomic_and_not_cross_room(self):
        self.record();backup=self.bot.conversations.export(self.bot,'group',GROUP)
        self.record('new');self.record('other',OTHER_GROUP)
        self.bot.conversations.restore(self.bot,'group',GROUP,backup,'replace')
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(len(self.rows(OTHER_GROUP)),1)

    async def test_probability_zero_and_hundred_are_bounded_by_access_policy(self):
        controls=self.bot.plugins._plugins['chat_controls']
        message=dict(id='chance',kind='group',target=GROUP,text='普通聊天',sender_id='wxid_a')
        self.assertFalse(controls.should_reply(message,self.bot))
        self.bot.plugins.update_settings('chat_controls',dict(replyProbability=100))
        self.assertTrue(controls.should_reply(message,self.bot))
        self.bot.plugins.update_settings('group_monitor',dict(mode='selected',groups=OTHER_GROUP))
        self.assertFalse(self.bot.reply_allowed(message))

    async def test_cleared_and_deleted_original_inbox_history_not_used(self):
        self.bot.ingest(raw_message('@鲸鱼娘 会议是周五吗',at=True,local_id=31009))
        await self.wait_for_send()
        first=self.rows()[0]
        self.bot.conversations.delete(self.bot,'group',GROUP,first['key'])
        self.ai.calls.clear()
        self.bot.ingest(raw_message('@鲸鱼娘 再问一个问题',at=True,local_id=31010))
        await self.wait_for_send(2)
        self.assertNotIn('会议是周五吗',json.dumps(self.ai.calls[0][0],ensure_ascii=False))

    async def test_invalid_replace_keeps_every_existing_row(self):
        self.record();bundle=self.bot.conversations.export(self.bot,'group',GROUP)
        bundle['records'][0]['created']=float('inf')
        with self.assertRaises(ValueError):self.bot.conversations.restore(self.bot,'group',GROUP,bundle,'replace')
        self.assertEqual(len(self.rows()),1)

    async def test_cross_group_import_is_rejected(self):
        self.record();bundle=self.bot.conversations.export(self.bot,'group',GROUP)
        response=await self.client.post('/api/dialogue/archive/import',json=dict(self.request_body(bundle=bundle),target=OTHER_GROUP))
        self.assertEqual(response.status_code,400)
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.rows(OTHER_GROUP),[])

    async def test_changed_account_mutation_is_rejected(self):
        self.record();key=self.rows()[0]['key']
        response=await self.client.post('/api/dialogue/archive/delete',json=dict(self.request_body(key=key),account='stale'))
        self.assertEqual(response.status_code,409)
        self.assertEqual(len(self.rows()),1)

    async def test_truncated_backup_cannot_replace(self):
        self.record();bundle=self.bot.conversations.export(self.bot,'group',GROUP);bundle['truncated']=True
        with self.assertRaises(ValueError):self.bot.conversations.restore(self.bot,'group',GROUP,bundle,'replace')
        self.assertEqual(len(self.rows()),1)

    async def test_single_delete_is_scoped_and_tombstones_old_history(self):
        self.record();self.record('other',OTHER_GROUP)
        key=self.rows()[0]['key']
        await self.client.post('/api/dialogue/archive/delete',json=self.request_body(key=key))
        self.assertEqual(self.rows(),[])
        self.assertEqual(len(self.rows(OTHER_GROUP)),1)
        self.assertEqual(self.bot.db.execute('SELECT mid FROM conversation_deleted').fetchone()[0],'1')

    async def test_clear_keeps_other_group_and_sets_history_barrier(self):
        self.record();self.record('other',OTHER_GROUP)
        await self.client.post('/api/dialogue/archive/delete',json=self.request_body(clear=True))
        self.assertEqual(self.rows(),[])
        self.assertEqual(len(self.rows(OTHER_GROUP)),1)
        self.assertGreater(self.bot.conversations.reset_time(self.bot,'group',GROUP),time.time()-5)

    async def test_archive_search_and_export_api(self):
        self.record();self.record('2',content='早餐吃面包')
        response=await self.client.get('/api/dialogue/archive',params=dict(kind='group',target=GROUP,q='周五'))
        self.assertEqual(response.json()['total'],1)
        bundle=(await self.client.get('/api/dialogue/archive/export',params=dict(kind='group',target=GROUP))).json()['bundle']
        self.assertEqual(bundle['total'],2)
        self.assertEqual(bundle['schema'],'WhaleGirlWeChat.conversations')

    async def test_summary_uses_only_current_group_with_real_stats(self):
        self.record();self.record('2',content='上午十点开始',sender_id='wxid_b',sender_name='小白')
        self.record('3',OTHER_GROUP,'不应给模型的其他群消息')
        plugin=self.bot.plugins._plugins['group_summary']
        msg=dict(id='command',kind='group',target=GROUP,text='/总结群聊 6',created=time.time())
        self.assertIsNone(plugin.on_ingest(msg,self.bot))
        result=await plugin.generate(msg,self.bot)
        self.assertIn('2 条消息、2 位发言者',result)
        self.assertIn('阿明 1条',result)
        sent=json.dumps(self.ai.calls,ensure_ascii=False)
        self.assertIn('周五',sent)
        self.assertNotIn('其他群消息',sent)
        self.assertEqual(self.ai.calls[0][1]['max_tokens'],1000)

    async def test_summary_empty_and_failed_model_do_not_invent_topics(self):
        plugin=self.bot.plugins._plugins['group_summary']
        msg=dict(id='cmd',kind='group',target=GROUP,text='/总结群聊',created=time.time())
        plugin.on_ingest(msg,self.bot)
        self.assertIn('没有可总结',await plugin.generate(msg,self.bot))
        self.record();self.bot.ai.chat=AsyncMock(return_value='')
        self.assertIn('暂未生成',await plugin.generate(msg,self.bot))

    async def test_summary_private_and_invalid_hours_show_guidance(self):
        plugin=self.bot.plugins._plugins['group_summary']
        self.assertIn('请在',plugin.on_ingest(dict(kind='private',text='/总结群聊'),self.bot))
        for text in ['/总结群聊 0','/总结群聊 999','/总结群聊 abc']:
            self.assertIsInstance(plugin.on_ingest(dict(kind='group',text=text),self.bot),str)

    async def test_summary_at_group_nickname_works_when_keywords_disabled(self):
        self.bot.plugins.update_settings('keyword_trigger',{'keywordsEnabled':False})
        message=dict(id='cmd',kind='group',target=GROUP,text='@群里给机器人的外号\u2005/总结群聊 6',at=True)
        self.bot.plugins._plugins['group_summary'].on_ingest(message,self.bot)
        self.assertEqual(message['_group_summary'],6)
        unaddressed=dict(message,at=False);unaddressed.pop('_group_summary')
        self.bot.plugins._plugins['group_summary'].on_ingest(unaddressed,self.bot)
        self.assertNotIn('_group_summary',unaddressed)

    async def test_summary_model_exception_preserves_real_statistics(self):
        self.record();self.bot.ai.chat=AsyncMock(side_effect=TimeoutError())
        message=dict(id='cmd',kind='group',target=GROUP,text='/总结群聊',created=time.time())
        plugin=self.bot.plugins._plugins['group_summary'];plugin.on_ingest(message,self.bot)
        result=await plugin.generate(message,self.bot)
        self.assertIn('1 条消息',result)
        self.assertIn('暂未生成',result)

    async def test_summary_end_to_end_is_passive_and_not_sentence_truncated(self):
        self.record()
        self.bot.ingest(raw_message('/总结群聊 6',local_id=31002))
        await self.wait_for_send()
        reply=self.inbox_rows()[-1][2]
        self.assertIn('群聊总结',reply)
        self.assertIn('范围：',reply)
        self.assertIn('1000',str(self.ai.calls[0][1]['max_tokens']))

    async def test_paused_summary_does_not_bypass_policy(self):
        self.bot.plugins.update_settings('robot_admin',{'paused':True})
        self.bot.ingest(raw_message('/总结群聊',local_id=31003))
        await asyncio.sleep(.2)
        self.assertFalse(self.bot.bridge.sent_log)
        self.assertFalse(self.ai.calls)

    async def test_locked_persona_activation_is_not_saved_as_success(self):
        old=self.bot.style.active_name
        self.bot.style.save_persona(dict(name='locked150',system_prompt='另一个人设'))
        with patch.object(self.bot.style,'set_active',return_value=False):
            with self.assertRaises(ValueError):self.bot.activate_persona('locked150')
        self.assertEqual(self.bot.style.active_name,old)
        self.assertNotEqual(self.bot.config.get('style.active'),'locked150')

    async def test_custom_persona_does_not_use_hardcoded_whale_greetings(self):
        self.bot.style.save_persona(dict(name='greeting150',system_prompt='我是小猫助手',keywords=['小猫助手']))
        self.bot.activate_persona('greeting150')
        message=dict(id='hi',kind='private',target='wxid_a',sender_id='wxid_a',text='在吗')
        self.assertIsNone(self.bot.plugins._plugins['quick_reply'].on_ingest(message,self.bot))

    async def test_queued_immediate_reply_is_bound_to_original_persona(self):
        msg=dict(id='immediate',kind='private',target='wxid_a',text='在吗',created=time.time())
        self.bot._queue(msg,'旧人设寒暄')
        self.bot.style.save_persona(dict(name='queued150',system_prompt='新的角色'))
        self.bot.activate_persona('queued150')
        self.assertFalse(self.bot.prepared_persona_matches(msg))

    async def test_inflight_old_persona_reply_is_cancelled_before_send(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def chat(*args,**kwargs):
            entered.set();await release.wait();return '旧角色答复，不应发送'
        self.bot.ai.chat=chat
        self.bot.ingest(raw_message('@鲸鱼娘 回答测试',at=True,local_id=31004))
        await asyncio.wait_for(entered.wait(),5)
        self.bot.style.save_persona(dict(name='switch150',system_prompt='我是新的助理'))
        self.bot.activate_persona('switch150');release.set()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and 'cancelled' not in self.states():await asyncio.sleep(.03)
        self.assertIn('cancelled',self.states())
        self.assertFalse(self.bot.bridge.sent_log)

    async def test_current_question_and_persona_take_priority_in_prompt(self):
        self.record(content='之前只是闲聊')
        self.bot.ingest(raw_message('@鲸鱼娘 Python 怎么读取 JSON？',at=True,local_id=31005))
        await self.wait_for_send()
        messages,kwargs=self.ai.calls[0]
        self.assertIn('Python',str(messages[-1]))
        self.assertIn('先直接回答',kwargs['system_prompt'])
        self.assertIn('当前人设',kwargs['system_prompt'])
        self.assertIn('之前只是闲聊',json.dumps(messages,ensure_ascii=False))
