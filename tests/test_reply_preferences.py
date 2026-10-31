"""昵称回归、真实队列快速路径，以及可切换思考参数的请求验证。"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.ai_engine import AIEngine
from src.chat_state import build_history
from src.plugins.manager import PluginManager
from test_ai_service_compat import _FakeClient, _reply, _error
from test_wechat_app_flow import RuntimeTestCase, raw_message


class PreferenceUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.manager=PluginManager(Path(self.temp.name))
        self.plugin=self.manager._plugins['reply_preferences']

    def tearDown(self):
        self.temp.cleanup()

    def test_current_sender_prefix_removed_but_body_and_other_mentions_kept(self):
        message={'sender_name':'董','sender':'董'}
        self.assertEqual(self.plugin.clean_reply('@董 主人，本鲸鱼娘来了。',message),'主人，本鲸鱼娘来了。')
        self.assertEqual(self.plugin.clean_reply('董，你好',message),'你好')
        self.assertEqual(self.plugin.clean_reply('董的电脑需要更新。',message),'董的电脑需要更新。')
        self.assertEqual(self.plugin.clean_reply('你可以问问董，或者 @小红。',message),'你可以问问董，或者 @小红。')
        self.assertEqual(self.plugin.clean_reply('@董明 你好',message),'@董明 你好')

    def test_preference_can_be_disabled_without_losing_saved_settings(self):
        self.manager.update_settings('reply_preferences',{'hideNicknames':False,'fastMode':False})
        self.assertEqual(self.plugin.clean_reply('@董 你好',{'sender_name':'董'}),'@董 你好')
        self.assertFalse(self.plugin.fast())
        self.manager.reload()
        self.assertFalse(self.manager._plugins['reply_preferences'].fast())

    def test_history_renderer_keeps_persona_filter_and_correct_speaker(self):
        newest={'_persona_key':'other','sender_name':'小红','text':'其他人设消息'}
        older={'_persona_key':'active','sender_name':'董','text':'你好'}
        history=build_history([(json.dumps(newest),'@小红 回答'),(json.dumps(older),'@董 回答')],
                              'active',reply_renderer=self.plugin.clean_reply)
        self.assertEqual(len(history),2)
        self.assertEqual(history[-1]['content'],'回答')


class ThinkingOptionTests(unittest.IsolatedAsyncioTestCase):
    async def engine(self,model,responses):
        client=_FakeClient(responses)
        engine=AIEngine('https://api.test/v1','fake',model=model,max_retries=1)
        engine._get_client=AsyncMock(return_value=client)
        self.addAsyncCleanup(engine.close)
        return engine,client

    async def test_switchable_kimi_closes_thinking_without_temperature_round_trip(self):
        engine,client=await self.engine('kimi-k2.6',[_reply('回答')])
        self.assertEqual(await engine.chat([],thinking='auto_fast'),'回答')
        self.assertEqual(client.payloads[0]['thinking'],{'type':'disabled'})
        self.assertNotIn('temperature',client.payloads[0])

    async def test_unknown_and_forced_thinking_models_keep_defaults(self):
        for model in ('gpt-test','kimi-k2-thinking','kimi-k3','deepseek-reasoner'):
            engine,client=await self.engine(model,[_reply('回答')])
            await engine.chat([],thinking='auto_fast')
            self.assertNotIn('thinking',client.payloads[0])

    async def test_deepseek_switchable_models_use_disabled_flag(self):
        engine,client=await self.engine('deepseek-v4-flash',[_reply('回答')])
        await engine.chat([],thinking='auto_fast')
        self.assertEqual(client.payloads[0]['thinking'],{'type':'disabled'})

    async def test_rejected_auto_parameter_is_learned_and_retried_once(self):
        engine,client=await self.engine('kimi-k2.6',[
            _error(400,'thinking is not supported'),_reply('第一次'),_reply('第二次')])
        self.assertEqual(await engine.chat([],thinking='auto_fast'),'第一次')
        self.assertEqual(await engine.chat([],thinking='auto_fast'),'第二次')
        self.assertEqual(len(client.payloads),3)
        self.assertNotIn('thinking',client.payloads[1])
        self.assertNotIn('thinking',client.payloads[2])

    async def test_other_service_error_is_not_hidden_by_parameter_fallback(self):
        engine,client=await self.engine('kimi-k2.6',[_error(400,'invalid max_tokens')])
        self.assertEqual(await engine.chat([],thinking='auto_fast'),'')
        self.assertEqual(len(client.payloads),1)
        self.assertIn('max_tokens',engine.last_error)


class PreferenceFlowTests(RuntimeTestCase):
    async def test_model_nickname_does_not_leak_into_message(self):
        bot=await self.start_runtime()
        self.install_ai('@董 主人，本鲸鱼娘是小深。')
        raw=raw_message('鲸鱼娘 帮我分析这句话',sender_name='董')
        raw['sender_name']='董'
        bot.ingest(raw)
        await self.wait_for_send()
        self.assertEqual(bot.bridge.sent_log[0]['text'],'主人，本鲸鱼娘是小深。')
        self.assertEqual(bot.bridge.sent_log[0]['at'], [])

    async def test_account_mention_is_only_removed_from_model_turn(self):
        bot=await self.start_runtime()
        ai=self.install_ai('回答')
        name=bot.bridge.account_name
        raw=raw_message('@'+name+' 帮我分析这句话',at=True)
        bot.ingest(raw)
        await self.wait_for_send()
        self.assertNotIn('@'+name,ai.calls[0][0][-1]['content'])
        self.assertIn('@'+name,json.loads(self.inbox_rows()[0][1])['text'])
        self.assertEqual(ai.calls[0][1]['thinking'],'auto_fast')

    async def test_long_user_merge_setting_does_not_hold_short_reply_for_seconds(self):
        bot=await self.start_runtime()
        self.install_ai('快速回答')
        bot.plugins.update_settings('burst_merge',{'capMs':8000})
        started=time.perf_counter()
        bot.ingest(raw_message('鲸鱼娘 帮我分析'))
        await self.wait_for_send(timeout=2)
        self.assertLess(time.perf_counter()-started,1.5)
        self.assertEqual(bot.plugins.get_settings('burst_merge')['capMs'],8000)

    async def test_fast_mode_skips_pacing_but_preserves_plugin_setting(self):
        bot=await self.start_runtime()
        # 本例只验证退役打字插件；新分气泡链路另由 test_takeover_196 完整覆盖。
        bot.plugins.update_settings('chat_controls', {'replySegments': 1})
        self.install_ai('第一句。第二句。')
        # 覆盖旧用户曾开启的参数；精简版不得加载它，但也不能删除用户原设置。
        bot.plugins._settings['send_pacing']={'enabled':True,'minMsPerChar':1000,'maxMsPerChar':1000}
        bot.plugins._save_settings()
        self.assertNotIn('send_pacing',bot.plugins._plugins)
        with self.assertRaises(KeyError):
            bot.plugins.update_settings('send_pacing',{'enabled':True})
        bot.ingest(raw_message('鲸鱼娘 帮我分析'))
        await self.wait_for_send(timeout=2)
        self.assertEqual(len(bot.bridge.sent_log),1)
        self.assertEqual(bot.bridge.sent_log[0]['text'],'第一句。第二句。')
        self.assertTrue(bot.plugins.get_settings('send_pacing')['enabled'])

    async def test_fixed_keyword_text_is_not_rewritten(self):
        bot=await self.start_runtime()
        self.install_ai(boom=True)
        bot.plugins.update_settings('keyword_reply',{'enabled':True,'rules':'暗号=@董 这是固定文案'})
        bot.ingest(raw_message('暗号',sender_name='董'))
        await self.wait_for_send()
        self.assertEqual(bot.bridge.sent_log[0]['text'],'@董 这是固定文案')
        self.assertEqual(bot.bridge.sent_log[0]['at'], [])

    async def test_no_at_settings_off_preserves_transport_default(self):
        bot = await self.start_runtime()
        bot.plugins.update_settings('reply_preferences', {'hideNicknames': False})
        bot.plugins.update_settings('chat_controls', {'noAtInReply': False})
        self.install_ai('清楚了。')
        raw = raw_message('鲸鱼娘 帮我分析')
        raw['sender_name'] = '阿明'
        bot.ingest(raw)
        await self.wait_for_send()
        self.assertEqual(bot.bridge.sent_log[0]['at'], ['阿明'])

    async def test_all_bubbles_do_not_add_sender_mentions(self):
        bot = await self.start_runtime()
        bot.plugins.update_settings('chat_controls', {'replySegments': 3, 'replyMaxChars': 300})
        self.install_ai('第一句话介绍情况。第二句话解释原因。第三句话给出结论。')
        bot.ingest(raw_message('鲸鱼娘 帮我分析'))
        await self.wait_for_send()
        await self.wait_for_inbox_done()
        self.assertGreater(len(bot.bridge.sent_log), 1)
        self.assertTrue(all(not item['at'] for item in bot.bridge.sent_log))

    async def test_switching_to_background_stops_old_sender_before_applying_flag(self):
        from src.wechat_app import Settings
        bot = await self.start_runtime()
        seen = []
        original = bot.bridge.cancel_pending_sends
        def cancel():
            seen.append(bot.bridge.background_send)
            return original()
        draft = bot.public_settings()
        draft.update(send_mode='background', background_send=True, send_compat=False)
        with patch.object(bot.bridge, 'cancel_pending_sends', side_effect=cancel):
            await bot.save(Settings(**draft))
        self.assertTrue(seen, '必须先停止旧发送子进程')
        self.assertIs(seen[0], False)
        self.assertTrue(bot.bridge.background_send)
        self.assertTrue(bot.running)

    async def test_legacy_delays_restored_when_fast_mode_off(self):
        bot=await self.start_runtime()
        bot.plugins.update_settings('reply_preferences',{'fastMode':False})
        self.install_ai('回答')
        bot.plugins.update_settings('burst_merge',{'capMs':700})
        started=time.perf_counter()
        bot.ingest(raw_message('鲸鱼娘 帮我分析'))
        await self.wait_for_send()
        self.assertGreaterEqual(time.perf_counter()-started,.65)


class AdaptiveWorkerTests(unittest.TestCase):
    def test_readback_ready_does_not_sleep_fixed_delays(self):
        from unittest.mock import Mock
        from src.wechat_send_worker import send_text
        uia=Mock()
        uia.current_chat.return_value='群'
        control=uia._chat_input.return_value
        control.GetValuePattern.return_value.IsReadOnly=False
        control.HasKeyboardFocus=True
        uia._read_edit_value.side_effect=['','','正文','']
        uia._compare_norm.side_effect=lambda t:t
        with patch('src.wechat_send_worker.time.sleep') as sleep:
            self.assertTrue(send_text(uia,'群','正文')[0])
        sleep.assert_not_called()
        self.assertEqual(sum(c.args[0]=='{Enter}' for c in control.SendKeys.call_args_list),1)
