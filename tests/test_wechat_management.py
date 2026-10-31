"""管理策略通过实际的离线入站、队列和发送链路验证。"""
import asyncio
import json
import unittest
from unittest.mock import patch
import httpx

from test_wechat_app_flow import RuntimeTestCase, raw_message, GROUP, OTHER_GROUP
from src.wechat_app import create_app
from src.wechat_management import bind_owner, sessions
from src.plugins.robot_admin import clean_log
from src.wechat_listener import ListenerDatabase


class ManagementFlowTests(RuntimeTestCase):
    async def prepare_admin(self):
        bot = await self.start_runtime()
        bot.plugins.update_settings('private_replies',{'mode':'off'})
        bot.ingest(raw_message('绑定前观察',chat='wxid_owner',sender='wxid_owner',local_id=900))
        bind_owner(bot,'wxid_owner')
        bot.plugins.update_settings('robot_admin',{'enabled':True})
        self.install_ai(boom=True)
        return bot

    async def test_private_off_blocks_fixed_reply_before_queue(self):
        bot = await self.start_runtime()
        bot.plugins.update_settings('keyword_reply',{'enabled':True,'rules':'暗号=收到'})
        bot.plugins.update_settings('private_replies',{'mode':'off'})
        bot.ingest(raw_message('暗号',chat='wxid_friend',sender='wxid_friend'))
        self.assertEqual(self.pending_count(),0)
        self.assertEqual(sessions(bot)['sessions'][0]['received'],1)
        self.assertFalse(sessions(bot)['sessions'][0]['reply_allowed'])

    async def test_private_allowlist_and_exact_fixed_keyword(self):
        bot = await self.start_runtime()
        self.install_ai(boom=True)
        bot.plugins.update_settings('private_replies',{'mode':'allow','contacts':'wxid_yes'})
        bot.plugins.update_settings('keyword_reply',{'enabled':True,'rules':'DeepSeek=收到','scope':'private','match':'exact'})
        bot.ingest(raw_message('ＤＥＥＰＳＥＥＫ',chat='wxid_no',sender='wxid_no'))
        bot.ingest(raw_message('ＤＥＥＰＳＥＥＫ',chat='wxid_yes',sender='wxid_yes',local_id=2))
        await self.wait_for_send()
        self.assertEqual(len(bot.bridge.sent_log),1)

    async def test_group_observe_only_blocks_fixed_and_ai(self):
        bot = await self.start_runtime()
        bot.plugins.update_settings('keyword_reply',{'enabled':True,'rules':'暗号=收到'})
        bot.plugins.update_settings('group_monitor',{'replyEnabled':False})
        bot.ingest(raw_message('暗号 鲸鱼娘'))
        self.assertEqual(self.pending_count(),0)
        self.assertEqual(sessions(bot)['sessions'][0]['received'],1)

    async def test_empty_selected_and_off_do_not_observe_any_group(self):
        bot = await self.start_runtime()
        for mode in ('selected','off'):
            bot.plugins.update_settings('group_monitor',{'mode':mode,'groups':''})
            await bot.refresh_monitoring()
            bot.ingest(raw_message('鲸鱼娘',local_id=1 if mode=='selected' else 2))
            self.assertFalse(bot.bridge.listen_groups)
            self.assertEqual(sessions(bot)['sessions'],[])

    async def test_group_selected_name_or_id_hot_applies_without_reconnect(self):
        bot = await self.start_runtime()
        bot.plugins.update_settings('group_monitor',{'mode':'selected','groups':'我的群'})
        with patch.object(bot.bridge,'connect',side_effect=AssertionError('不应重新连接微信')):
            await bot.refresh_monitoring()
        message=raw_message('只观察',local_id=1)
        message['target_name']='我的群'
        bot.ingest(message)
        bot.ingest(raw_message('只观察',chat=OTHER_GROUP,local_id=2))
        self.assertEqual([v['target'] for v in sessions(bot)['sessions']],[GROUP])

    async def test_group_plugin_does_not_overwrite_original_settings_list(self):
        bot = await self.start_runtime(wechat_only={'groups':[GROUP]})
        bot.plugins.update_settings('group_monitor',{'mode':'selected','groups':OTHER_GROUP})
        self.assertEqual(bot._configured_groups(),[OTHER_GROUP])
        self.assertEqual(bot.public_settings()['groups'],[GROUP])

    async def test_replaced_wakewords_use_normalized_match(self):
        bot=await self.start_runtime()
        ai=self.install_ai()
        bot.plugins.update_settings('keyword_trigger',{'mode':'replace','includePersona':False,'keywords':'DeepSeek'})
        bot.ingest(raw_message('鲸鱼娘',local_id=1))
        self.assertEqual(self.pending_count(),0)
        bot.ingest(raw_message('ＤＥＥＰ\u200bＳＥＥＫ 帮我分析',local_id=2))
        await self.wait_for_send()
        self.assertEqual(len(ai.calls),1)
        self.assertEqual(bot.aliases(),['DeepSeek'])

    async def test_at_only_still_accepts_actual_at(self):
        bot=await self.start_runtime()
        self.install_ai()
        bot.plugins.update_settings('keyword_trigger',{'keywordsEnabled':False})
        bot.plugins.update_settings('chat_controls',{'aliases':'特别昵称','interjectPercent':100})
        bot.ingest(raw_message('鲸鱼娘 特别昵称 帮忙分析',local_id=1))
        self.assertEqual(self.pending_count(),0)
        bot.ingest(raw_message('请分析这个问题',at=True,local_id=2))
        await self.wait_for_send()

    async def test_admin_binding_requires_received_private_and_cannot_edit_owner_field(self):
        bot=await self.start_runtime()
        with self.assertRaises(ValueError):
            bind_owner(bot,'wxid_unseen')
        with self.assertRaises(ValueError):
            bot.plugins.update_settings('robot_admin',{'ownerIds':'wxid_unseen'})
        bot.ingest(raw_message('群里的成员',sender='wxid_owner'))
        with self.assertRaises(ValueError):
            bind_owner(bot,'wxid_owner')

    async def test_admin_can_query_despite_private_off_and_no_model(self):
        bot=await self.prepare_admin()
        bot.ingest(raw_message('机器人 状态',chat='wxid_owner',sender='wxid_owner',local_id=901))
        await self.wait_for_send()
        self.assertIn('微信',self.inbox_rows()[0][2])
        self.assertTrue(json.loads(self.inbox_rows()[0][1])['_robot_admin'])

    async def test_spoofed_sender_and_group_do_not_get_admin_access(self):
        bot=await self.prepare_admin()
        bot.ingest(raw_message('机器人 暂停回复',chat='wxid_owner',sender='wxid_intruder',local_id=902))
        bot.ingest(raw_message('机器人 暂停回复',sender='wxid_owner',local_id=903))
        self.assertFalse(bot.plugins._plugins['robot_admin'].get('paused'))
        self.assertFalse(any(json.loads(row[1]).get('_robot_admin') for row in self.inbox_rows()))

    async def test_pause_resume_preserves_admin_and_drops_waiting_replies(self):
        bot=await self.prepare_admin()
        bot.plugins.update_settings('private_replies',{'mode':'all'})
        bot.plugins.update_settings('keyword_reply',{'enabled':True,'rules':'暗号=自动回复'})
        bot.ingest(raw_message('暗号',chat='wxid_friend',sender='wxid_friend',local_id=904))
        bot.ingest(raw_message('机器人 暂停回复',chat='wxid_owner',sender='wxid_owner',local_id=905))
        await self.wait_for_send()
        self.assertIn('cancelled',self.states())
        bot.ingest(raw_message('暗号',chat='wxid_friend',sender='wxid_friend',local_id=906))
        bot.ingest(raw_message('机器人 恢复回复',chat='wxid_owner',sender='wxid_owner',local_id=907))
        await self.wait_for_send(2)
        self.assertEqual(len(bot.bridge.sent_log),2)
        self.assertFalse(bot.plugins._plugins['robot_admin'].get('paused'))

    async def test_policy_changed_during_generation_stops_send(self):
        bot=await self.start_runtime()
        entered=asyncio.Event()
        finish=asyncio.Event()
        async def chat(*args,**kwargs):
            entered.set()
            await finish.wait()
            return '不应发出'
        bot.ai.chat=chat
        bot.ingest(raw_message('请分析这条消息',chat='wxid_friend',sender='wxid_friend'))
        await asyncio.wait_for(entered.wait(),2)
        bot.plugins.update_settings('private_replies',{'mode':'off'})
        bot.cancel_disallowed_pending()
        finish.set()
        for _ in range(50):
            if bot.pipeline['stage']=='idle':
                break
            await asyncio.sleep(.02)
        self.assertEqual(bot.bridge.sent_log,[])
        self.assertEqual(self.states(),['cancelled'])

    async def test_unbinding_cancels_queued_admin_response(self):
        bot=await self.prepare_admin()
        bot.ingest(raw_message('机器人 日志',chat='wxid_owner',sender='wxid_owner',local_id=908))
        bind_owner(bot,'wxid_owner',False)
        await asyncio.sleep(.05)
        self.assertEqual(bot.bridge.sent_log,[])
        self.assertEqual(self.states(),['cancelled'])

    async def test_binding_and_pause_survive_plugin_reload(self):
        bot=await self.prepare_admin()
        bot.plugins.update_settings('robot_admin',{'paused':True})
        bot.plugins.reload()
        self.assertIn('wxid_owner',bot.plugins._plugins['robot_admin'].owners(bot.management_account()))
        self.assertFalse(bot.reply_allowed({'kind':'group','target':GROUP}))

    async def test_switch_account_cannot_reuse_binding_or_observed_contacts(self):
        bot=await self.prepare_admin()
        with patch.object(bot,'management_account',return_value='wxid_other_account'):
            self.assertEqual(sessions(bot)['sessions'],[])
            self.assertFalse(bot.plugins._plugins['robot_admin'].is_admin({'kind':'private','target':'wxid_owner','sender_id':'wxid_owner'},bot))
            with self.assertRaises(ValueError):
                bind_owner(bot,'wxid_owner')

    async def test_admin_hourly_limit_prevents_more_commands(self):
        bot=await self.prepare_admin()
        bot.plugins.update_settings('robot_admin',{'maxPerHour':1})
        bot.ingest(raw_message('机器人 状态',chat='wxid_owner',sender='wxid_owner',local_id=910))
        bot.ingest(raw_message('机器人 暂停回复',chat='wxid_owner',sender='wxid_owner',local_id=911))
        await self.wait_for_send()
        self.assertEqual(len(self.inbox_rows()),1)
        self.assertFalse(bot.plugins._plugins['robot_admin'].get('paused'))

    async def test_http_monitor_save_and_admin_bind_unbind(self):
        bot=await self.prepare_admin()
        app=create_app(bot)
        app.state.bot=bot
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
            result=await client.get('/api/management/sessions')
            self.assertTrue(result.json()['sessions'][0]['owner'])
            result=await client.post('/api/management/owner',json={'target':'wxid_owner','bind':False})
            self.assertTrue(result.json()['success'])
            result=await client.post('/api/management/owner',json={'target':'wxid_no','bind':True})
            self.assertEqual(result.status_code,400)
            result=await client.post('/api/plugins/group_monitor',json={'mode':'selected','groups':''})
            self.assertTrue(result.json()['success'])
            self.assertFalse(bot.bridge.listen_groups)

    async def test_log_sanitization(self):
        self.assertNotIn('secretvalue',clean_log('api_key=secretvalue https://key.example/?token=another'))
        self.assertNotIn('another',clean_log('https://key.example/?token=another'))
        self.assertNotIn('plainsecret',clean_log('Authorization: Bearer plainsecret'))


class ListenerOffTests(unittest.TestCase):
    def test_no_groups_still_allows_private_listener(self):
        class DB:
            def get_sessions(self,limit=500):
                return [{'username':GROUP},{'username':'wxid_friend'}]
        self.assertEqual(ListenerDatabase(DB(),[],True,False).get_sessions(),[{'username':'wxid_friend'}])
