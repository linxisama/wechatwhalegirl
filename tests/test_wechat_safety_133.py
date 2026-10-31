"""账号安全暂停回归；不连接微信、不触发登录或真实发送。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from src.wechat_safety import SafetyHold, RESET_PHRASE, service_target, security_notice
from src.wechat_bridge import WeChatBridge
from src.wechat_send_worker import enable_send_compat,execute
from tests.test_wechat_app_flow import RuntimeTestCase,raw_message
from tests.test_agent_api import AgentApiTests


class SafetyTests(unittest.TestCase):
    def test_service_id_is_authoritative_not_display_name(self):
        for who in ('weixin','newsapp','gh_officialtest'):
            self.assertTrue(service_target({'target':who}))
        self.assertFalse(service_target({'target':'wxid_friend','target_name':'微信团队'}))
        self.assertFalse(service_target({'target':'friend@chatroom'}))

    def test_security_text_only_accepted_from_weixin_not_self_or_group(self):
        text='该微信号因使用行为或设备网络环境异常，当前无法使用所有社交场景'
        self.assertTrue(security_notice({'username':'weixin','content':text}))
        self.assertTrue(security_notice({'username':'weixin','content':('<msg><des><![CDATA['+text+']]></des></msg>').encode(), 'is_send':'0'}))
        self.assertFalse(security_notice({'username':'user','content':text}))
        self.assertFalse(security_notice({'username':'weixin','content':text,'attr':'self'}))
        self.assertFalse(security_notice({'username':'g@chatroom','content':text}))

    def test_persistent_hold_corruption_and_explicit_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'hold.json';hold=SafetyHold(path)
            self.assertFalse(hold.status()['active'])
            hold.activate('test')
            self.assertTrue(SafetyHold(path).status()['active'])
            with self.assertRaises(ValueError):hold.clear('随便确认')
            self.assertTrue(hold.status()['active'])
            hold.clear(RESET_PHRASE);self.assertFalse(hold.status()['active'])
            path.write_text('{broken','utf-8')
            self.assertTrue(hold.status()['active'])

    def test_bridge_hold_prevents_backend_detection_and_old_official_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            bridge=WeChatBridge(data_dir=root/'data/wechat-only')
            SafetyHold(root/'data/wechat_safety_hold.json').activate('test')
            with patch.object(bridge,'detect_backend',side_effect=AssertionError('must not touch backend')):
                self.assertFalse(bridge.connect()[0])
            bridge._wx=SimpleNamespace(inspect_environment=Mock(side_effect=AssertionError('must not inspect')))
            self.assertFalse(bridge.start_listening(Mock())[0])
            self.assertIsNone(bridge.download_media('wxid_user',1,'image'))
            self.assertFalse(bridge.inspect_send_environment()['ready'])
            bridge._wx.inspect_environment.assert_not_called()
            bridge.backend='mock';bridge.online=True
            self.assertFalse(bridge.send_text('wxid_user','不会发出')[0])
            SafetyHold(root/'data/wechat_safety_hold.json').clear(RESET_PHRASE)
            with patch.object(bridge,'_record_mock_send',side_effect=AssertionError('must not submit')):
                self.assertFalse(bridge.send_text('weixin','不能回复通知')[0])
                self.assertFalse(bridge.send_files('weixin',['not-opened.png'],as_image=True)[0])

    def test_legacy_callback_cannot_reenable_process_write_even_with_forged_entrance(self):
        wake=Mock(side_effect=AssertionError('write process'))
        hot=Mock(side_effect=AssertionError('write process'))
        self.assertFalse(enable_send_compat(SimpleNamespace(_hot_activate_accessibility=hot),{'wake':wake,'hot':hot}))
        wake.assert_not_called();hot.assert_not_called()

    def test_window_check_does_not_construct_compat_driver(self):
        job={'action':'window-check','send_compat':True,'auto_open_chat':True}
        with patch('wechatauto.uia_driver.WeChatUIA',side_effect=AssertionError('driver not allowed')), \
             patch('src.wechat_window.inspect_window',return_value={'ready':False,'reason':'缺控件'}):
            self.assertFalse(execute(job)[0])
        self.assertTrue(job['_diagnostic']['gate']['process_write_blocked'])


class SafetyFlowTests(RuntimeTestCase):
    async def test_regular_official_notice_never_uses_ai_archive_learning_or_queue(self):
        bot=await self.start_runtime()
        self.install_ai('不应调用',boom=True)
        bot.ingest(raw_message('如果遇到问题可反馈给我们',chat='weixin',kind='private'))
        await asyncio.sleep(.03)
        self.assertFalse(bot.bridge.sent_log)
        self.assertEqual(bot.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0],0)
        self.assertFalse(bot.safety.status()['active'])

    async def test_security_notification_stops_and_persists_before_any_model_call(self):
        bot=await self.start_runtime()
        self.install_ai('不应调用',boom=True)
        bot.ingest(raw_message('该微信号因使用行为或设备网络环境异常，当前无法使用所有社交场景',chat='weixin',kind='private'))
        await asyncio.sleep(.05)
        self.assertFalse(bot.running)
        self.assertTrue(SafetyHold(bot.root/'data/wechat_safety_hold.json').status()['active'])
        self.assertFalse(bot.bridge.sent_log)
        with patch.object(bot.bridge,'connect',side_effect=AssertionError('must not reconnect')):
            with self.assertRaisesRegex(ValueError,'账号安全暂停'):
                await bot.start()

    async def test_inflight_generation_cannot_send_after_security_notice(self):
        bot=await self.start_runtime()
        entered=asyncio.Event()
        async def chat(*args,**kwargs):
            entered.set();await asyncio.sleep(30);return '不能迟发'
        bot.ai.chat=chat
        bot.ingest(raw_message('鲸鱼娘 请解释缓存一致性',local_id=91))
        await asyncio.wait_for(entered.wait(),3)
        bot.ingest(raw_message('使用行为或设备网络环境异常',chat='weixin',kind='private',local_id=92))
        await asyncio.sleep(.1)
        self.assertFalse(bot.running)
        self.assertFalse(bot.bridge.sent_log)


class SafetyApiTests(AgentApiTests):
    def test_reset_does_not_connect_start_or_accept_bridge_token(self):
        self.runtime.safety.activate('test')
        self.assertTrue(self.client.get('/api/status').json()['safety']['active'])
        self.assertEqual(self.client.post('/api/start',json={}).status_code,400)
        self.assertEqual(self.client.post('/api/wechat/safety/reset',json={'confirmation':'确定'}).status_code,400)
        from src.agent_authority import authority
        key=authority(self.runtime).issue('dsh',['manage_console'])['token']
        self.assertEqual(self.client.post('/api/wechat/safety/reset',json={'confirmation':RESET_PHRASE},headers={'Authorization':'Bearer '+key}).status_code,403)
        with patch.object(self.runtime.bridge,'connect',side_effect=AssertionError('must not connect')):
            self.assertTrue(self.client.post('/api/wechat/safety/reset',json={'confirmation':RESET_PHRASE}).json()['success'])
        self.assertFalse(self.runtime.running)
        self.assertFalse(self.runtime.safety.status()['active'])
