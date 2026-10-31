"""Hook 入站必须先持久入库再确认，旧监听轮次不能穿过停止/重启。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.hook_gateway import CallbackToken
from tests.test_wechat_app_flow import RuntimeTestCase, raw_message


class HookRuntimeTests(RuntimeTestCase):
    async def start_runtime(self, **overrides):
        bot = await super().start_runtime(**overrides)
        # 此组仅验证收件入口；真实发送队列另有端到端覆盖。
        bot.worker.cancel()
        await asyncio.gather(bot.worker, return_exceptions=True)
        bot.worker = None
        return bot

    async def test_ack_follows_ingest_and_stopped_or_replaced_bridge_is_not_acked(self):
        bot = await self.start_runtime()
        origin = SimpleNamespace(ack_inbound=Mock(), callback_token=CallbackToken())
        bot.bridge = origin
        raw = dict(raw_message('test'), hook_msgid='7654321')
        order = []
        origin.ack_inbound.side_effect = lambda value: order.append(('ack', value))
        with patch.object(bot, 'ingest', side_effect=lambda value: order.append(('ingest', value))):
            bot._ingest_threadsafe(raw, origin, origin.callback_token.path)
            self.assertEqual([item[0] for item in order], ['ingest', 'ack'])
            self.assertEqual(order[-1][1], '7654321')
            bot.running = False
            bot._ingest_threadsafe(raw, origin, origin.callback_token.path)
            self.assertEqual(len(order), 2)
        # 还原真实 mock 桥以供 RuntimeTestCase 收尾。
        bot.bridge = bot.transport.bridge

    async def test_restart_rotates_nonce_and_old_listener_cannot_enter_new_run(self):
        bot = await self.start_runtime()
        original = bot.bridge
        bridge = SimpleNamespace(backend='hook', callback_token=CallbackToken(),
            set_callback_url=Mock(), ack_inbound=Mock())
        bot.bridge = bridge
        first = bot._receive_callback()
        first_path = bridge.callback_token.path
        second = bot._receive_callback()
        self.assertNotEqual(first_path, bridge.callback_token.path)
        with patch.object(bot, 'ingest') as ingest:
            first(dict(raw_message('old'), hook_msgid='1'))
            second(dict(raw_message('new'), hook_msgid='2'))
            await asyncio.sleep(.02)
            self.assertEqual(ingest.call_count, 1)
            self.assertEqual(ingest.call_args.args[0]['hook_msgid'], '2')
            bridge.ack_inbound.assert_called_once_with('2')
        bot.bridge = original

    async def test_ingest_failure_keeps_journal_unacked_and_pauses_receiving_and_sends(self):
        bot = await self.start_runtime()
        original = bot.bridge
        bridge = SimpleNamespace(ack_inbound=Mock(), cancel_pending_sends=Mock(),
            stop_receiving=Mock(), callback_token=CallbackToken())
        bot.bridge = bridge
        with patch.object(bot, 'ingest', side_effect=OSError('disk full')), \
             patch.object(bot, 'stop', new=Mock(return_value=asyncio.sleep(0))):
            bot._ingest_threadsafe(dict(raw_message('test'), hook_msgid='1'),
                                   bridge, bridge.callback_token.path)
            await asyncio.sleep(.01)
        self.assertFalse(bot.running)
        self.assertTrue(bot._stop_requested)
        self.assertFalse(bot.transport._accept_sends)
        bridge.ack_inbound.assert_not_called()
        bridge.cancel_pending_sends.assert_called_once()
        bridge.stop_receiving.assert_called_once()
        bot.bridge = original

    async def test_sandbox_callback_defers_ack_until_commit_and_ignores_old_run(self):
        bot = await self.start_runtime()
        original = bot.bridge
        bridge = SimpleNamespace(backend='sandbox', ack_inbound=Mock())
        bot.bridge = bridge
        try:
            old = bot._receive_callback()
            current = bot._receive_callback()
            order = []
            bridge.ack_inbound.side_effect = lambda value: order.append(('ack', value))
            with patch.object(bot, 'ingest', side_effect=lambda value: order.append(('commit', value))):
                self.assertIs(old(dict(raw_message('old'), _sandbox_event_id=1)), False)
                self.assertIs(current(dict(raw_message('new'), _sandbox_event_id=2)), False)
                self.assertEqual(order, [])
                await asyncio.sleep(.02)
            self.assertEqual([entry[0] for entry in order], ['commit', 'ack'])
            self.assertEqual(order[-1][1], 2)
            self.assertEqual(order[0][1]['_sandbox_event_id'], 2)
        finally:
            bot.bridge = original

