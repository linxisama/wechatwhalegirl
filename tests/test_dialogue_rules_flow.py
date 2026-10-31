"""对照 QQ 对话页逐项走真实规则、记忆、模型提示与模拟发送链路。"""
import copy
import json
import unittest
from unittest.mock import patch
import httpx

from test_wechat_app_flow import RuntimeTestCase,raw_message,GROUP
from src.wechat_app import create_app,WeChatRuntime
from src.dialogue_management import snapshot
from src.wechat_management import bind_owner


class DialogueRulesFlowTests(RuntimeTestCase):
    async def asyncSetUp(self):
        bot=await self.start_runtime()
        self.ai=self.install_ai('第一句。第二句。第三句。')
        app=create_app(bot);app.state.bot=bot
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def rules(self,group,plugins):
        response=await self.client.post('/api/dialogue/rules',json={'group':group,'plugins':plugins})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['success'])

    async def test_all_four_rule_groups_save_and_reload(self):
        bot=self.runtime
        data=(await self.client.get('/api/dialogue/rules')).json()
        self.assertEqual(set(data['groups']),{'trigger','followup','isolation','preferences'})
        changes={
            'trigger':{'keyword_trigger':{'mode':'replace','keywords':'用户规则','includePersona':False},
                       'private_replies':{'mode':'allow','contacts':'wxid_allowed'},
                       'mention_gate':{'enabled':True,'user_rules':GROUP+'=wxid_blocked'},
                       'group_monitor':{'mode':'selected','groups':GROUP}},
            'followup':{'short_followup':{'followupWindowSec':60,'maxSentences':1,'strictLimit':True},
                        'chat_controls':{'perMinute':7,'contextChars':3000,'historyTurns':4}},
            'isolation':{'conversation_memory':{'personaIsolation':False,'recallLimit':4,'maxEntries':700}},
            'preferences':{'conversation_memory':{'autoRemember':True,'crossChatPreferences':True,'autoPrefixes':'我偏爱\n本人喜欢'}},
        }
        for group,updates in changes.items():await self.rules(group,updates)
        bot.plugins.reload()
        for updates in changes.values():
            for pid,settings in updates.items():
                for key,value in settings.items():self.assertEqual(bot.plugins.get_settings(pid)[key],value)

    async def test_bad_rules_and_disk_failure_never_partially_save(self):
        bot=self.runtime;before=copy.deepcopy(bot.plugins._settings)
        path=bot.plugins._settings_path;raw=path.read_bytes()
        for body in [
            {'group':'followup','plugins':{'chat_controls':{'perMinute':3},'short_followup':{'maxSentences':0}}},
            {'group':'followup','plugins':{'chat_controls':{'interjectPercent':100}}},
            {'group':'preferences','plugins':{'conversation_memory':{'autoPrefixes':'他们喜欢'}}},
            {'group':'isolation','plugins':{'robot_admin':{'enabled':False}}}]:
            response=await self.client.post('/api/dialogue/rules',json=body)
            self.assertEqual(response.status_code,400)
            self.assertEqual(bot.plugins._settings,before)
            self.assertEqual(path.read_bytes(),raw)
        with patch.object(bot.plugins,'_save_settings',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):bot.plugins.update_many({'short_followup':{'maxSentences':1}})
        self.assertEqual(bot.plugins._settings,before);self.assertEqual(path.read_bytes(),raw)

    async def test_trigger_and_ignore_rules_apply_to_fixed_replies_and_private_messages(self):
        bot=self.runtime
        bot.plugins.update_settings('keyword_reply',{'enabled':True,'rules':'暗号=收到'})
        await self.rules('trigger',{'mention_gate':{'enabled':True,'user_rules':GROUP+'=wxid_blocked'},
                                   'private_replies':{'mode':'allow','contacts':'wxid_allowed'}})
        bot.ingest(raw_message('暗号',sender='wxid_blocked',local_id=1))
        bot.ingest(raw_message('暗号',chat='wxid_other',sender='wxid_other',local_id=2))
        self.assertEqual(self.pending_count(),0)
        bot.ingest(raw_message('暗号',chat='wxid_allowed',sender='wxid_allowed',local_id=3))
        await self.wait_for_send()
        self.assertEqual(bot.bridge.sent_log[-1]['text'],'收到')
        diagnostic=snapshot(bot)['diagnostics']
        self.assertTrue(any(v['state']=='blocked' for v in diagnostic))

    async def test_manual_notes_edit_disable_delete_and_affect_actual_model_prompt(self):
        bot=self.runtime;persona=bot.style.active_name
        response=await self.client.post('/api/dialogue/notes',json={'persona':persona,'content':'谈到加班时先关心状态'})
        self.assertTrue(response.json()['success'])
        note=(await self.client.get('/api/dialogue')).json()['notes'][0]
        bot.ingest(raw_message('鲸鱼娘 解释这条技术方案',local_id=11))
        await self.wait_for_send(1)
        # 多气泡时两次send不等于两轮对话；等当前inbox完成后再切换记忆开关。
        await self.wait_for_inbox_done()
        self.assertIn('谈到加班时先关心状态',self.ai.calls[-1][1]['system_prompt'])
        await self.client.post('/api/dialogue/notes',json={'persona':persona,'id':note['id'],'enabled':False})
        bot.ingest(raw_message('鲸鱼娘 再说明这条技术方案',local_id=12))
        await self.wait_for_send(2)
        await self.wait_for_inbox_done()
        self.assertNotIn('谈到加班时先关心状态',self.ai.calls[-1][1]['system_prompt'])
        response=await self.client.post('/api/dialogue/notes',json={'persona':persona,'id':note['id'],'content':'按步骤讲解','enabled':True})
        self.assertTrue(response.json()['success'])
        self.assertEqual(snapshot(bot)['notes'][0]['content'],'按步骤讲解')
        response=await self.client.post('/api/dialogue/notes',json={'persona':'wrong','id':note['id'],'delete':True})
        self.assertEqual(response.status_code,409)
        response=await self.client.post('/api/dialogue/notes',json={'persona':persona,'id':'missing','delete':True})
        self.assertEqual(response.status_code,404)
        response=await self.client.post('/api/dialogue/notes',json={'persona':persona,'id':note['id'],'delete':True})
        self.assertTrue(response.json()['success']);self.assertEqual(snapshot(bot)['notes'],[])

    async def test_configured_auto_prefix_requires_independent_evidence_and_respects_switch(self):
        bot=self.runtime
        await self.rules('preferences',{'conversation_memory':{'autoPrefixes':'我偏爱','autoRemember':True}})
        bot.ingest(raw_message('我喜欢蓝色',local_id=1))
        self.assertEqual(snapshot(bot)['facts'],[])
        bot.ingest(raw_message('我偏爱蓝色',local_id=2))
        first=snapshot(bot)['facts'][0];self.assertEqual(first['status'],'candidate');self.assertEqual(first['evidence'],1)
        bot.ingest(raw_message('我偏爱蓝色',local_id=2))
        self.assertEqual(snapshot(bot)['facts'][0]['evidence'],1)
        bot.ingest(raw_message('我偏爱蓝色',local_id=3))
        fact=snapshot(bot)['facts'][0];self.assertEqual(fact['status'],'confirmed');self.assertEqual(fact['evidence'],2)
        bot.ingest(raw_message('我偏爱红色？',local_id=4))
        self.assertEqual(len(snapshot(bot)['facts']),1)
        await self.rules('preferences',{'conversation_memory':{'autoRemember':False}})
        bot.ingest(raw_message('我偏爱红色',local_id=5))
        self.assertEqual(len(snapshot(bot)['facts']),1)

    async def test_profiles_add_edit_confirm_search_and_delete(self):
        bot=self.runtime;bot.ingest(raw_message('我喜欢紫色',local_id=1))
        data=(await self.client.get('/api/dialogue')).json();fact=data['facts'][0]
        self.assertEqual(fact['status'],'candidate')
        response=await self.client.post('/api/dialogue/facts',json={'id':fact['id'],'action':'confirm'})
        self.assertTrue(response.json()['success'])
        response=await self.client.post('/api/dialogue/facts',json={'id':fact['id'],'action':'edit','content':'偏好深紫色'})
        self.assertTrue(response.json()['success'])
        searched=(await self.client.get('/api/dialogue?q=深紫')).json()
        self.assertEqual(searched['facts'][0]['content'],'偏好深紫色')
        subject=data['subjects'][0]
        response=await self.client.post('/api/dialogue/facts',json={'action':'add','content':'喜欢吃米饭',**subject})
        self.assertTrue(response.json()['success'],response.text)
        self.assertEqual(len(snapshot(bot)['facts']),2)
        response=await self.client.post('/api/dialogue/facts',json={'id':fact['id'],'action':'delete'})
        self.assertTrue(response.json()['success']);self.assertEqual(len(snapshot(bot)['facts']),1)
        response=await self.client.post('/api/dialogue/facts',json={'id':fact['id'],'action':'delete'})
        self.assertEqual(response.status_code,404)

    async def test_cross_chat_uses_existing_facts_and_isolation_can_be_restored(self):
        bot=self.runtime;memory=bot.plugins._plugins['conversation_memory']
        first={'id':'fact1','kind':'private','target':'wxid_one','sender_id':'wxid_one','text':'/记住 喜欢蓝色'}
        memory.on_ingest(first,bot)
        other=dict(first,id='query',kind='group',target=GROUP,text='/记忆')
        self.assertIn('还没有',memory.on_ingest(other,bot))
        await self.rules('preferences',{'conversation_memory':{'crossChatPreferences':True}})
        self.assertNotIn('喜欢蓝色',memory.on_ingest(other,bot))
        await self.rules('preferences',{'conversation_memory':{'privateToGroup':True}})
        self.assertIn('喜欢蓝色',memory.on_ingest(other,bot))
        self.assertNotIn('喜欢蓝色',memory.on_ingest(dict(other,sender_id='wxid_other'),bot))
        bot.style.save_persona({'name':'other','system_prompt':'另一人设'});bot.activate_persona('other')
        self.assertNotIn('喜欢蓝色',memory.on_ingest(other,bot))
        await self.rules('isolation',{'conversation_memory':{'personaIsolation':False}})
        self.assertIn('喜欢蓝色',memory.on_ingest(other,bot))
        await self.rules('preferences',{'conversation_memory':{'crossChatPreferences':False}})
        self.assertNotIn('喜欢蓝色',memory.on_ingest(other,bot))
        self.assertIn('喜欢蓝色',memory.on_ingest(dict(first,text='/记忆'),bot))

    async def test_followup_window_and_strict_sentence_limit_affect_sent_reply(self):
        bot=self.runtime
        # 固定单气泡以隔离追问句数规则；多气泡发送有独立端到端测试。
        bot.plugins.update_settings('chat_controls', {'replySegments': 1})
        await self.rules('followup',{'short_followup':{'followupWindowSec':60,'maxSentences':1,'strictLimit':True}})
        bot.ingest(raw_message('鲸鱼娘 解释第一项方案',local_id=1));await self.wait_for_send(1)
        bot.ingest(raw_message('鲸鱼娘 解释第二项方案',local_id=2));await self.wait_for_send(2)
        self.assertIn('不超过1句',self.ai.calls[-1][1]['system_prompt'])
        self.assertEqual(bot.bridge.sent_log[-1]['text'],'第一句。')
        bot.ingest(raw_message('未触发的后续讨论',local_id=3))
        self.assertEqual(self.pending_count(),0)
        bot.style.save_persona({'name':'fresh','system_prompt':'新的人设','keywords':['新词']});bot.activate_persona('fresh')
        bot.ingest(raw_message('新词 解释第三项方案',local_id=4));await self.wait_for_send(3)
        self.assertNotIn('不超过1句',self.ai.calls[-1][1]['system_prompt'])

    async def test_diagnostics_survive_restart_and_explain_pending_without_sending(self):
        bot=self.runtime;bot.ingest(raw_message('普通未唤醒群消息',local_id=1))
        await bot.stop();bot.running=True
        bot.bridge.online=False;bot.ingest(raw_message('鲸鱼娘 查询排队原因',local_id=2))
        current=next(v for v in snapshot(bot)['diagnostics'] if v['state']=='pending')
        self.assertIn('离线',current['error'])
        bot.running=False;self.assertIn('未启动',next(v for v in snapshot(bot)['diagnostics'] if v['state']=='pending')['error'])
        db_path=bot.db.execute('PRAGMA database_list').fetchone()[2]
        await bot.close()
        self.runtime=WeChatRuntime(root=self.root,config_path=self.root/'config.wechat.yaml',db_path=db_path)
        data=snapshot(self.runtime)
        self.assertTrue(any(v['state']=='observed' and '未命中' in v['error'] for v in data['diagnostics']))
        self.assertTrue(any(v['state']=='cancelled' and '停止' in v['error'] for v in data['diagnostics']))
        self.assertFalse(self.runtime.bridge.online)

    async def test_sent_callback_uses_generating_persona_after_selection_changes(self):
        from src.chat_state import persona_key
        bot=self.runtime;plugin=bot.plugins._plugins['short_followup']
        previous=bot.style.active_name
        message={'sender_id':'wxid_a','target':GROUP,'kind':'group',
                 '_persona_key':persona_key(bot.style.get().prompt_text())}
        bot.style.save_persona({'name':'callback_new','system_prompt':'切换后的新人设'})
        bot.activate_persona('callback_new')
        plugin.on_sent(message,'旧人设的回复',bot)
        self.assertIsNone(plugin.on_pre_ai(dict(message),[],bot))
        bot.activate_persona(previous)
        self.assertIn('extra_system',plugin.on_pre_ai(dict(message),[],bot))
        plugin._last_replied.clear()
        plugin.on_sent({'sender_id':'wxid_a','target':GROUP},'固定回复',bot)
        self.assertEqual(plugin._last_replied,{})

    async def test_password_is_redacted_in_persistent_diagnostics(self):
        bot=self.runtime;bind_owner(bot,'wxid_admin',manual=True)
        bot.plugins.update_settings('robot_admin',{'enabled':True});bot.admin_sessions.set_password('Pass123')
        bot.ingest(raw_message('进入管理员模式',chat='wxid_admin',sender='wxid_admin',local_id=21));await self.wait_for_send(1)
        bot.ingest(raw_message('Pass123',chat='wxid_admin',sender='wxid_admin',local_id=22));await self.wait_for_send(2)
        self.assertNotIn('Pass123',json.dumps(snapshot(bot),ensure_ascii=False))
        stored=' '.join(row[0] for row in bot.db.execute('SELECT text FROM dialogue_decisions'))
        self.assertNotIn('Pass123',stored);self.assertIn('[管理员验证消息已隐藏]',stored)

    async def test_unconfirmed_fact_is_not_injected_and_confirmed_fact_is(self):
        bot=self.runtime;memory=bot.plugins._plugins['conversation_memory']
        message={'id':'pref1','kind':'private','target':'wxid_one','sender_id':'wxid_one','text':'我喜欢蓝色'}
        memory.on_ingest(dict(message),bot)
        conversation=[];memory.on_pre_ai(message,conversation,bot)
        self.assertEqual(conversation,[])
        memory.on_ingest(dict(message,id='pref2'),bot)
        conversation=[];memory.on_pre_ai(message,conversation,bot)
        self.assertIn('已确认长期记忆',json.dumps(conversation,ensure_ascii=False))
        self.assertIn('我喜欢蓝色',json.dumps(conversation,ensure_ascii=False))
        other=[];memory.on_pre_ai(dict(message,sender_id='wxid_other'),other,bot)
        self.assertEqual(other,[])

    async def test_failed_reply_does_not_open_followup_window(self):
        self.runtime.plugins.update_settings('chat_controls', {'replySegments': 1})
        bot=self.runtime
        await self.rules('followup',{'short_followup':{'maxSentences':1,'strictLimit':True}})
        self.install_ai(boom=True)
        bot.ingest(raw_message('鲸鱼娘 第一条失败请求',local_id=1))
        await self.wait_for_inbox_done()
        fake=self.install_ai('第一句。第二句。')
        bot.ingest(raw_message('鲸鱼娘 第二条正常请求',local_id=2));await self.wait_for_send(1)
        self.assertNotIn('不超过1句',fake.calls[-1][1]['system_prompt'])
        self.assertIn('第二句',bot.bridge.sent_log[-1]['text'])

    async def test_invalid_group_types_and_storage_error_have_clear_feedback(self):
        before=copy.deepcopy(self.runtime.plugins._settings)
        for body in ([],{'group':[]},{'group':'missing','plugins':{}},{'group':'preferences','plugins':[]}):
            response=await self.client.post('/api/dialogue/rules',json=body)
            self.assertEqual(response.status_code,400)
        with patch.object(self.runtime.plugins,'_save_settings',side_effect=OSError('disk full')):
            response=await self.client.post('/api/dialogue/rules',json={'group':'preferences','plugins':{'conversation_memory':{'autoRemember':False}}})
            self.assertEqual(response.status_code,400);self.assertIn('保存失败',response.json()['detail'])
        self.assertEqual(before,self.runtime.plugins._settings)

    async def test_offline_view_does_not_reuse_another_explicitly_selected_account(self):
        bot=self.runtime;known=bot.management_account()
        self.assertTrue(bot._last_account)
        await bot.stop()
        bot.bridge.self_id='';bot.bridge.account_name=''
        bot.bridge.configured_account='wxid_another_selected'
        self.assertNotEqual(bot.management_account(),known)
        self.assertEqual(bot.account(),'微信')

    async def test_display_name_without_group_identity_is_never_used_for_personal_memory(self):
        bot=self.runtime
        for index,group in enumerate((GROUP,'other@chatroom'),1):
            raw=raw_message('我喜欢蓝色',chat=group,local_id=index)
            raw.pop('sender_id');raw.pop('sender_username');raw['sender_name']='同名用户'
            bot.ingest(raw)
        self.assertEqual(snapshot(bot)['facts'],[])
        from src.wechat_transports import message_from_raw
        message=message_from_raw(raw,bot.bridge,bot.aliases())
        self.assertEqual(message['sender_id'],'')
        self.assertIsNone(bot.plugins._plugins['short_followup'].on_pre_ai(message,[],bot))

    async def test_private_identity_falls_back_to_actual_peer_instead_of_display_name(self):
        from src.wechat_transports import message_from_raw
        bot=self.runtime
        raw=raw_message('我喜欢蓝色',chat='wxid_actual_peer',local_id=7)
        raw.pop('sender_id');raw.pop('sender_username');raw['sender_name']='同名用户'
        message=message_from_raw(raw,bot.bridge,bot.aliases())
        self.assertEqual(message['sender_id'],'wxid_actual_peer')
        bot.ingest(raw)
        self.assertEqual(snapshot(bot)['facts'][0]['subject'],'wxid_actual_peer')
