"""Offline local liveness and runtime health; no service, database, UI or sends."""
import ast
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

WORK=Path(__file__).resolve().parent
ROOT=WORK.parent
sys.path.insert(0,str(ROOT))

def load(name, file):
    spec=importlib.util.spec_from_file_location(name,WORK/file)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module

from src import wechat_bridge as base
from src.isolated_wechat import host_bridge as host
from src import wechat_app as runtime

class ThreadFlag:
    def __init__(self,alive=True):self.alive=alive
    def is_alive(self):return self.alive

class LocalLivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='listener-health-offline-')
        self.addCleanup(self.temp.cleanup)
        self.bridge=base.WeChatBridge(Path(self.temp.name)/'data')
        self.bridge.backend='wechatauto'
        self.bridge.online=True
        self.bridge._listener_epoch=object()

    def listener(self,alive=True,stopped=False):
        event=threading.Event()
        if stopped:event.set()
        self.bridge._listener=SimpleNamespace(_thread=ThreadFlag(alive),_stop=event)

    def test_native_actual_thread_and_stop_metadata_required(self):
        self.listener();self.assertTrue(self.bridge.is_receiving())
        self.assertTrue(self.bridge.status()['listening'])
        self.bridge._listener._thread.alive=False
        self.assertFalse(self.bridge.is_receiving())
        self.assertEqual(self.bridge.status()['read_state'],'connected')

    def test_native_stop_or_bridge_stop_cannot_claim_receiving(self):
        self.listener(stopped=True);self.assertFalse(self.bridge.is_receiving())
        self.listener();self.bridge.stop_receiving()
        self.assertFalse(self.bridge.is_receiving())

    def test_unknown_native_listener_version_fails_closed(self):
        self.bridge._listener=SimpleNamespace()
        self.assertFalse(self.bridge.is_receiving())
        self.bridge._listener=SimpleNamespace(_thread=ThreadFlag(),_stop=None)
        self.assertFalse(self.bridge.is_receiving())

    def test_sandbox_local_helper_never_reads_service_or_rpc(self):
        bridge=host.SandboxBridge(Path(self.temp.name)/'host',Path(self.temp.name)/'absent')
        bridge.online=bridge._listening=True
        bridge._poll_thread=ThreadFlag()
        bridge._service=Mock(side_effect=AssertionError('no service file read'))
        bridge._rpc=Mock(side_effect=AssertionError('no RPC'))
        self.assertTrue(bridge.is_receiving())
        bridge._poll_thread.alive=False
        self.assertFalse(bridge.is_receiving())
        bridge._poll_thread.alive=True;bridge._poll_stop.set()
        self.assertFalse(bridge.is_receiving())
        bridge._service.assert_not_called();bridge._rpc.assert_not_called()

class HealthRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def bot(self,alive):
        bridge=SimpleNamespace(backend='sandbox',online=True,consecutive_fail=0,last_error='',
            _receive_generation=1,_runtime_receive_epoch=object(),_runtime_receive_callback=object())
        bridge.alive=alive
        bridge.is_receiving=Mock(side_effect=lambda:bridge.alive)
        bridge.reconnect=Mock(return_value=(True,'offline stub reconnected'))
        def start(callback):
            bridge.alive=True
            return True,'offline stub listening'
        bridge.start_listening=Mock(side_effect=start)
        bridge.stop_receiving=Mock()
        bridge.resume_sends=Mock()
        bridge.cancel_pending_sends=Mock()
        transport=SimpleNamespace(_send_epoch=3,_accept_sends=False,_send_closed=False)
        def reset():transport._send_epoch+=1
        transport.reset_sender=Mock(side_effect=reset)
        bot=SimpleNamespace(bridge=bridge,transport=transport,running=True,connecting=False,reconnecting=False,
            _stop_requested=False,_safety_volatile=False,safety=SimpleNamespace(status=lambda:{}),
            _configured_groups=lambda:[],group_monitoring_enabled=lambda:True,
            config=SimpleNamespace(get=lambda *args:True),note=Mock(),account=lambda:'offline fixture',
            on_bridge_message=Mock())
        async def readiness():bot.running=False
        bot._refresh_background_readiness=AsyncMock(side_effect=readiness)
        return bot

    async def run_bounded(self,bot,on_delay=None):
        ticks=[]
        async def sleep(seconds):
            ticks.append(seconds)
            if seconds==5 and on_delay:on_delay()
            if len(ticks)>=3:bot.running=False
        with patch.object(runtime.asyncio,'sleep',side_effect=sleep):
            await runtime.WeChatRuntime._health(bot)
        return ticks

    async def test_alive_online_receiver_does_not_reconnect(self):
        bot=self.bot(True);await self.run_bounded(bot)
        bot.bridge.reconnect.assert_not_called()
        bot._refresh_background_readiness.assert_awaited_once()

    async def test_dead_receiver_online_from_other_rpc_reconnects_once_without_send_resume(self):
        bot=self.bot(False);await self.run_bounded(bot)
        bot.bridge.reconnect.assert_called_once()
        bot.bridge.start_listening.assert_called_once()
        self.assertTrue(bot.bridge.alive)
        bot.transport.reset_sender.assert_called_once()
        bot.bridge.resume_sends.assert_not_called()
        self.assertFalse(bot.transport._accept_sends)
        self.assertTrue(any('消息监听已停止' in str(c) for c in bot.note.call_args_list))

    async def test_stop_during_backoff_does_not_revive_or_touch_queues(self):
        bot=self.bot(False)
        def stop():bot.running=False;bot._stop_requested=True
        await self.run_bounded(bot,stop)
        bot.bridge.reconnect.assert_not_called();bot.bridge.start_listening.assert_not_called()
        bot.transport.reset_sender.assert_not_called();bot.bridge.resume_sends.assert_not_called()

    async def test_stale_generation_during_backoff_does_not_reconnect_new_generation(self):
        bot=self.bot(False)
        await self.run_bounded(bot,lambda:setattr(bot.bridge,'_receive_generation',2))
        bot.bridge.reconnect.assert_not_called();bot.bridge.start_listening.assert_not_called()
        self.assertEqual(bot.bridge._receive_generation,2)

    async def test_replaced_bridge_during_backoff_is_untouched(self):
        bot=self.bot(False);old=bot.bridge
        replacement=SimpleNamespace(backend='sandbox',online=True,consecutive_fail=0)
        await self.run_bounded(bot,lambda:setattr(bot,'bridge',replacement))
        old.reconnect.assert_not_called();old.start_listening.assert_not_called()

    async def test_safety_pause_during_backoff_does_not_reconnect(self):
        bot=self.bot(False)
        await self.run_bounded(bot,lambda:setattr(bot,'_safety_volatile',True))
        bot.bridge.reconnect.assert_not_called();bot.bridge.start_listening.assert_not_called()

    async def test_late_start_success_with_dead_thread_is_not_reported_healthy(self):
        bot=self.bot(False)
        bot.bridge.start_listening.side_effect=lambda callback:(True,'claimed only')
        await self.run_bounded(bot)
        bot.bridge.reconnect.assert_called_once()
        self.assertFalse(bot.bridge.online)
        self.assertIn('接收线程未存活',bot.bridge.last_error)
        bot.transport.reset_sender.assert_not_called();bot.bridge.resume_sends.assert_not_called()

    async def test_receive_source_changed_inside_reconnect_does_not_attach_old_callbacks(self):
        bot=self.bot(False)
        def changed():
            bot.bridge._runtime_receive_callback=object()
            return True,'new listener source'
        bot.bridge.reconnect.side_effect=changed
        await self.run_bounded(bot)
        bot.bridge.start_listening.assert_not_called();bot.bridge.resume_sends.assert_not_called()

if __name__=='__main__':unittest.main()
