"""Health keeps monitoring a normal receiver takeover, while old task owners exit."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from src.wechat_app import WeChatRuntime


class HealthTakeoverTests(unittest.IsolatedAsyncioTestCase):
    def bot(self,alive=False):
        bridge=SimpleNamespace(backend='sandbox',online=True,consecutive_fail=0,last_error='',
            _receive_generation=1,_runtime_receive_epoch=object(),_runtime_receive_callback=object(),alive=alive)
        bridge.is_receiving=Mock(side_effect=lambda:bridge.alive)
        bridge.reconnect=Mock(return_value=(True,'offline reconnected'))
        def start(callback):
            bridge.alive=True
            return True,'offline listener alive'
        bridge.start_listening=Mock(side_effect=start)
        bridge.stop_receiving=Mock();bridge.resume_sends=Mock();bridge.cancel_pending_sends=Mock()
        transport=SimpleNamespace(_send_epoch=3,_accept_sends=False,_send_closed=False)
        def reset():transport._send_epoch+=1
        transport.reset_sender=Mock(side_effect=reset)
        bot=SimpleNamespace(bridge=bridge,transport=transport,running=True,connecting=False,reconnecting=False,
            _stop_requested=False,_safety_volatile=False,safety=SimpleNamespace(status=lambda:{}),
            _configured_groups=lambda:[],group_monitoring_enabled=lambda:True,
            config=SimpleNamespace(get=lambda *args:True),note=Mock(),account=lambda:'offline',on_bridge_message=Mock())
        bot.health=asyncio.current_task()
        bot._refresh_background_readiness=AsyncMock()
        return bot

    async def drive(self,bot,on_sleep=None):
        ticks=[]
        async def sleep(seconds):
            ticks.append(seconds)
            if len(ticks)>8:
                bot.running=False
                raise AssertionError('offline health loop failed to stop')
            if on_sleep:on_sleep(seconds,ticks)
        with patch('src.wechat_app.asyncio.sleep',side_effect=sleep):
            await WeChatRuntime._health(bot)
        return ticks

    async def test_reconnect_source_takeover_still_monitors_next_dead_receiver(self):
        bot=self.bot();bridge=bot.bridge;old_callback=bridge._runtime_receive_callback
        replacement=object();calls=[]
        def reconnect():
            calls.append(1)
            if len(calls)==1:
                bridge._runtime_receive_callback=replacement
                bridge._runtime_receive_epoch=object();bridge._receive_generation+=1;bridge.alive=True
            return True,'offline reconnect'
        bridge.reconnect.side_effect=reconnect
        def tick(seconds,ticks):
            if len(ticks)==3:bridge.alive=False
            if len(ticks)==5:bot.running=False
        ticks=await self.drive(bot,tick)
        self.assertEqual(ticks,[15,5,15,5,15])
        self.assertEqual(bridge.reconnect.call_count,2)
        bridge.start_listening.assert_called_once()
        attached=bridge.start_listening.call_args.args[0]
        self.assertIsNot(attached,old_callback);self.assertIsNot(attached,replacement)
        bridge.stop_receiving.assert_not_called();bridge.resume_sends.assert_not_called()
        bot.transport.reset_sender.assert_called_once()

    async def test_start_source_takeover_keeps_new_callback_and_next_recovery(self):
        bot=self.bot();bridge=bot.bridge;replacement=object();attached=[]
        def start(callback):
            attached.append(callback);bridge.alive=True
            if len(attached)==1:
                bridge._runtime_receive_callback=replacement
                bridge._runtime_receive_epoch=object();bridge._receive_generation+=1
            return True,'offline source ownership'
        bridge.start_listening.side_effect=start
        def tick(seconds,ticks):
            if len(ticks)==3:bridge.alive=False
            if len(ticks)==5:bot.running=False
        await self.drive(bot,tick)
        self.assertEqual(bridge.reconnect.call_count,2)
        self.assertEqual(len(attached),2);self.assertIsNot(attached[0],attached[1])
        self.assertIsNot(attached[1],replacement)
        bridge.stop_receiving.assert_not_called();bridge.resume_sends.assert_not_called()
        bot.transport.reset_sender.assert_called_once()

    async def test_old_owner_exits_before_any_sleep_or_reconnect(self):
        bot=self.bot();bot.health=object()
        ticks=await self.drive(bot)
        self.assertEqual(ticks,[])
        bot.bridge.reconnect.assert_not_called();bot.bridge.start_listening.assert_not_called()
        bot.transport.reset_sender.assert_not_called()

    async def test_owner_takeover_during_backoff_never_reconnects(self):
        bot=self.bot()
        def takeover(seconds,ticks):
            if seconds==5:bot.health=object()
        self.assertEqual(await self.drive(bot,takeover),[15,5])
        bot.bridge.reconnect.assert_not_called();bot.bridge.start_listening.assert_not_called()
        bot.transport.reset_sender.assert_not_called();bot.bridge.resume_sends.assert_not_called()

    async def test_old_reconnect_finally_does_not_clear_new_owner_flag(self):
        bot=self.bot()
        def takeover():
            bot.health=object();bot.reconnecting=True
            return True,'new owner is reconnecting'
        bot.bridge.reconnect.side_effect=takeover
        await self.drive(bot)
        self.assertTrue(bot.reconnecting)
        bot.bridge.start_listening.assert_not_called();bot.transport.reset_sender.assert_not_called()
        bot.bridge.resume_sends.assert_not_called()

    async def test_stop_and_each_safety_hold_during_reconnect_exit_without_reviving(self):
        for mode in ('stop','volatile','persisted'):
            with self.subTest(mode=mode):
                bot=self.bot()
                def finish():
                    if mode=='stop':bot._stop_requested=True;bot.running=False
                    if mode=='volatile':bot._safety_volatile=True
                    if mode=='persisted':bot.safety.status=lambda:{'active':True}
                    return True,'offline stopped'
                bot.bridge.reconnect.side_effect=finish
                await self.drive(bot)
                bot.bridge.start_listening.assert_not_called();bot.transport.reset_sender.assert_not_called()
                bot.bridge.resume_sends.assert_not_called()
                self.assertFalse(bot.reconnecting)

    async def test_owner_change_after_readiness_cannot_replay_new_hook_source(self):
        bot=self.bot(alive=True);bot.bridge.backend='hook';bot.bridge.replay_pending=Mock()
        async def readiness():bot.health=object()
        bot._refresh_background_readiness=AsyncMock(side_effect=readiness)
        self.assertEqual(await self.drive(bot),[15])
        bot.bridge.replay_pending.assert_not_called();bot.bridge.reconnect.assert_not_called()

    async def test_safety_hold_at_entry_never_queries_or_recovers(self):
        bot=self.bot();bot._safety_volatile=True
        self.assertEqual(await self.drive(bot),[])
        bot.bridge.is_receiving.assert_not_called();bot.bridge.reconnect.assert_not_called()

    async def test_native_account_probe_keeps_owner_predicate_callable(self):
        bot=self.bot(alive=True)
        bot.bridge.backend='wechatauto';bot.bridge._db_dir_hint='offline-only'
        bot.bridge._db=SimpleNamespace(account='wxid_offline_fixture')
        bot._note_account_switch=Mock(return_value=False)
        async def readiness():bot.running=False
        bot._refresh_background_readiness=AsyncMock(side_effect=readiness)
        with patch('src.wechat_bridge.active_database_account',return_value='wxid_offline_fixture') as probe:
            self.assertEqual(await self.drive(bot),[15])
        probe.assert_called_once_with('offline-only')
        bot._note_account_switch.assert_called_once_with('wxid_offline_fixture','wxid_offline_fixture')
        bot.bridge.reconnect.assert_not_called();bot._refresh_background_readiness.assert_awaited_once()

    async def test_new_sender_epoch_is_a_new_run_and_old_health_returns(self):
        bot=self.bot()
        def reconnect():
            bot.transport._send_epoch+=1
            bot.bridge._runtime_receive_callback=object()
            return True,'new runtime sender owns source'
        bot.bridge.reconnect.side_effect=reconnect
        self.assertEqual(await self.drive(bot),[15,5])
        bot.bridge.start_listening.assert_not_called();bot.transport.reset_sender.assert_not_called()
        bot.bridge.resume_sends.assert_not_called();self.assertTrue(bot.running)

    async def test_new_bridge_is_a_new_run_and_old_health_does_not_touch_it(self):
        bot=self.bot();old_bridge=bot.bridge
        replacement=SimpleNamespace(reconnect=Mock(),start_listening=Mock(),stop_receiving=Mock())
        def reconnect():
            bot.bridge=replacement
            return True,'new bridge owns runtime'
        old_bridge.reconnect.side_effect=reconnect
        self.assertEqual(await self.drive(bot),[15,5])
        old_bridge.start_listening.assert_not_called()
        replacement.reconnect.assert_not_called();replacement.start_listening.assert_not_called()
        replacement.stop_receiving.assert_not_called();self.assertTrue(bot.running)


if __name__=='__main__':unittest.main()
