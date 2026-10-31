"""隔离通道进入真实队列逻辑；桌面、微信和模型均为替身。"""
import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

from src.runtime_identity import APP_VERSION
from src.wechat_bridge import WeChatBridge
from tests.test_wechat_app_flow import RuntimeTestCase, raw_message


class SandboxRuntimeTests(RuntimeTestCase):
    async def wait_state(self, expected, count=1):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            states = self.states()
            if len(states) >= count and states[-1] == expected:
                return
            await asyncio.sleep(.02)
        self.fail(f'队列状态未达预期：{self.states()}')

    async def test_other_open_chat_skips_before_model_without_host_desktop_check(self):
        bot = await self.start_runtime()
        ai = self.install_ai()
        inspect = Mock(return_value={'ready': False, 'waiting_code': 'target_not_open'})
        with patch.object(bot.bridge, 'backend', 'sandbox'), \
             patch.object(bot.bridge, '_chat_name', return_value='目标群'), \
             patch.object(bot.bridge, 'inspect_send_environment', inspect), \
             patch('src.interactive_desktop.interactive_desktop_status', side_effect=AssertionError('宿主桌面不应参与')):
            bot.on_bridge_message(raw_message('鲸鱼娘，帮我分析这个问题'))
            await self.wait_state('skipped')
        self.assertEqual(ai.calls, [])
        self.assertEqual(bot.bridge.sent_log, [])
        inspect.assert_called_with('目标群')

    async def test_bad_target_resolution_does_not_block_next_pending_message(self):
        bot = await self.start_runtime()
        ai = self.install_ai()
        with patch.object(bot.bridge, 'backend', 'sandbox'), \
             patch.object(bot.bridge, '_chat_name', side_effect=[ValueError('目标未知'), '另一群']), \
             patch.object(bot.bridge, 'inspect_send_environment', return_value={'ready': False, 'waiting_code': 'target_not_open'}):
            bot.on_bridge_message(raw_message('鲸鱼娘，帮我分析第一个问题', local_id=50))
            await self.wait_state('failed')
            bot.on_bridge_message(raw_message('鲸鱼娘，帮我分析第二个问题', local_id=51))
            await self.wait_state('skipped', count=2)
        self.assertEqual(self.states(), ['failed', 'skipped'])
        self.assertEqual(ai.calls, [])
        self.assertEqual(bot.bridge.sent_log, [])

    async def test_switch_stops_and_cancels_old_work_but_does_not_connect_or_start_new_bridge(self):
        bot = await self.start_runtime()
        new = WeChatBridge(self.root / 'guest-mock', backend='mock')
        with patch('src.sandbox_manager.channel_path', return_value=self.root), \
             patch('src.isolated_wechat.protocol.load_pairing', return_value={}), \
             patch('src.isolated_wechat.protocol.live_service', return_value={'app_version': APP_VERSION}), \
             patch.object(bot, '_new_bridge', return_value=new), \
             patch.object(new, 'connect') as connect, patch.object(new, 'start_listening') as listen:
            await bot.switch_wechat_environment('sandbox', 'a' * 32, 'sandbox-id')
        self.assertFalse(bot.running)
        self.assertIsNone(bot.worker)
        self.assertFalse(bot.transport._accept_sends)
        self.assertEqual(bot.config.get('wechat.environment'), 'sandbox')
        self.assertFalse(new.auto_open_chat)
        self.assertFalse(new.hot_activate_uia)
        connect.assert_not_called()
        listen.assert_not_called()

    async def test_old_agent_version_rejected_before_stopping_current_runtime(self):
        bot = await self.start_runtime()
        with patch('src.sandbox_manager.channel_path', return_value=self.root), \
             patch('src.isolated_wechat.protocol.load_pairing', return_value={}), \
             patch('src.isolated_wechat.protocol.live_service', return_value={'app_version': '1.3.14'}):
            with self.assertRaisesRegex(ValueError, '版本'):
                await bot.switch_wechat_environment('sandbox', 'a' * 32)
        self.assertTrue(bot.running)
        self.assertEqual(bot.config.get('wechat.environment'), 'local')

    async def test_authority_change_during_stop_does_not_save_new_environment(self):
        bot = await self.start_runtime()
        def revoked():
            raise ValueError('管理员绑定已改变')
        with patch('src.sandbox_manager.channel_path', return_value=self.root), \
             patch('src.isolated_wechat.protocol.load_pairing', return_value={}), \
             patch('src.isolated_wechat.protocol.live_service', return_value={'app_version': APP_VERSION}):
            with self.assertRaisesRegex(ValueError, '绑定'):
                await bot.switch_wechat_environment('sandbox', 'a' * 32, check_access=revoked)
        self.assertFalse(bot.running)
        self.assertEqual(bot.config.get('wechat.environment'), 'local')
        self.assertFalse(bot.transport._accept_sends)

    async def test_cancel_during_close_rebuilds_stopped_sender_and_preserves_private_policy(self):
        bot = await self.start_runtime(wechat_only={'listen_private': False, 'account': 'wxid_selected'},
            wechat={'background_send': True, 'background_submit': 'posted_enter'})
        old_transport = bot.transport
        with patch.object(old_transport, 'close', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await bot.switch_wechat_environment('local')
        self.assertIsNot(bot.transport, old_transport)
        self.assertFalse(bot.running)
        self.assertFalse(bot.transport._accept_sends)
        self.assertFalse(bot.bridge.listen_private)
        self.assertTrue(bot.bridge.background_send)
        self.assertEqual(bot.bridge.background_submit, 'posted_enter')
        self.assertEqual(bot.bridge.configured_account, 'wxid_selected')
        self.assertEqual(bot.config.get('wechat.environment'), 'local')
        # 被取消的测试替身没有实际关闭旧资源；独立收尾，不走真实微信。
        await old_transport.close()
