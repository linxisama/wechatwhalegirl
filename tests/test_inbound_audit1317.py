"""停止/重启后的旧入站不能跨代投递；纯替身，不访问微信或执行发送。"""
import asyncio
import tempfile
import threading
import unittest
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from src.wechat_app import WeChatRuntime
from src.wechat_bridge import WeChatBridge
from src.isolated_wechat.host_bridge import SandboxBridge
from tests.test_isolated_transport1315 import ChannelCase


class RuntimeInboundGenerationTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, backend='wechatauto'):
        bridge=SimpleNamespace(backend=backend)
        bot=SimpleNamespace(bridge=bridge,running=True,_loop=asyncio.get_running_loop(),
                            ingest=Mock(),note=Mock())
        bot.on_bridge_message=MethodType(WeChatRuntime.on_bridge_message,bot)
        bot._ingest_threadsafe=MethodType(WeChatRuntime._ingest_threadsafe,bot)
        return bot

    async def test_native_old_callback_cannot_enter_the_next_run(self):
        bot=self.runtime()
        old=WeChatRuntime._receive_callback(bot)
        current=WeChatRuntime._receive_callback(bot)
        old({'content':'旧监听'})
        current({'content':'当前监听'})
        await asyncio.sleep(0)
        self.assertEqual(bot.ingest.call_count,1)
        self.assertEqual(bot.ingest.call_args.args[0]['content'],'当前监听')

    async def test_queued_native_callback_is_rejected_after_bridge_replacement(self):
        bot=self.runtime()
        old=WeChatRuntime._receive_callback(bot)
        old({'content':'旧账号尚未入库'})
        bot.bridge=SimpleNamespace(backend='wechatauto')
        await asyncio.sleep(0)
        bot.ingest.assert_not_called()

    async def test_native_callback_does_not_require_protocol_ack(self):
        bot=self.runtime()
        current=WeChatRuntime._receive_callback(bot)
        current({'content':'正常消息'})
        await asyncio.sleep(0)
        bot.ingest.assert_called_once()
        bot.note.assert_not_called()

    async def test_replacing_runtime_callback_preserves_other_receivers_without_growth(self):
        bot=self.runtime()
        other=Mock()
        bot.bridge._callbacks=[other]
        for _ in range(5):
            callback=WeChatRuntime._receive_callback(bot)
            bot.bridge._callbacks.append(callback)
        self.assertEqual(bot.bridge._callbacks,[other,callback])

    async def test_monitor_refresh_uses_a_bound_generation_for_queued_messages(self):
        bot=self.runtime()
        bot._stop_requested=False
        bot._configured_groups=lambda:[]
        bot.group_monitoring_enabled=lambda:True
        bot.cancel_disallowed_pending=Mock()
        callbacks=[]
        bot.bridge.stop_listening=Mock()
        bot.bridge.start_listening=lambda callback:(callbacks.append(callback) or True,'就绪')
        await WeChatRuntime.refresh_monitoring(bot)
        callbacks[0]({'content':'监控更新后已排队的旧消息'})
        # 事件循环尚未入库时停止并恢复；旧闭包不得借新一轮 running=True 复活。
        bot.bridge._runtime_receive_epoch=None
        WeChatRuntime._receive_callback(bot)
        await asyncio.sleep(0)
        bot.ingest.assert_not_called()

    async def test_monitor_refresh_stopped_during_listener_cleanup_does_not_restart(self):
        bot=self.runtime()
        bot._stop_requested=False
        bot._configured_groups=lambda:[]
        bot.group_monitoring_enabled=lambda:True
        bot.cancel_disallowed_pending=Mock()
        def stop():
            bot.running=False
            bot._stop_requested=True
        bot.bridge.stop_listening=stop
        bot.bridge.start_listening=Mock()
        await WeChatRuntime.refresh_monitoring(bot)
        bot.bridge.start_listening.assert_not_called()


class NativeInboundGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.bridge=WeChatBridge(Path(self.tmp.name),'mock')
        self.bridge.connect()

    def tearDown(self):
        self.bridge.stop_listening()
        self.tmp.cleanup()

    def test_new_start_never_clears_the_previous_listener_stop_event(self):
        self.bridge.start_listening(Mock())
        previous=self.bridge._poll_stop
        self.bridge.stop_receiving()
        self.bridge.start_listening(Mock())
        self.assertIsNot(self.bridge._poll_stop,previous)
        self.assertTrue(previous.is_set())

    def test_stop_revokes_runtime_generation_before_another_start(self):
        self.bridge._runtime_receive_epoch=object()
        self.bridge.start_listening(Mock())
        self.bridge.stop_receiving()
        self.assertIsNone(self.bridge._runtime_receive_epoch)

    def test_message_blocked_in_contact_lookup_cannot_reach_restarted_callback(self):
        old,new=Mock(),Mock()
        self.bridge.start_listening(old)
        self.bridge._db=object()
        entered,release=threading.Event(),threading.Event()
        errors=[]
        def lookup(value,*args):
            entered.set()
            if not release.wait(2):raise AssertionError('只读联系人替身未释放')
            return value
        self.bridge.display_name=lookup
        def emit():
            try:self.bridge._emit({'username':'old-chat','content':'旧监听消息'})
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=emit)
        try:
            thread.start()
            self.assertTrue(entered.wait(1))
            self.bridge.stop_receiving()
            self.bridge.start_listening(new)
        finally:
            release.set();thread.join(2)
        self.assertEqual(errors,[])
        self.assertFalse(thread.is_alive())
        old.assert_not_called()
        new.assert_not_called()
        self.assertEqual(self.bridge.recv_count,0)

    def test_wxauto4_registered_callback_from_previous_run_is_rejected(self):
        callbacks=[]
        self.bridge.backend='wxauto4'
        self.bridge.target_groups=['测试群']
        self.bridge._wx=SimpleNamespace(AddListenChat=lambda group,callback:callbacks.append(callback),
            StartListening=lambda:None,StopListening=lambda **kwargs:None)
        old,new=Mock(),Mock()
        self.bridge.start_listening(old)
        self.bridge.stop_receiving()
        self.bridge.start_listening(new)
        callbacks[0](SimpleNamespace(sender='member',content='旧监听消息'))
        old.assert_not_called();new.assert_not_called()
        callbacks[1](SimpleNamespace(sender='member',content='当前监听消息'))
        self.assertEqual(new.call_count,1)

    def test_classic_poll_keeps_the_old_client_and_cannot_resume_after_restart(self):
        entered,release=threading.Event(),threading.Event()
        def old_read():
            entered.set()
            if not release.wait(2):raise AssertionError('旧轮询未释放')
            return {'旧会话':[SimpleNamespace(content='旧消息',sender='member')]}
        self.bridge.backend='wxauto'
        self.bridge._wx=SimpleNamespace(GetListenMessage=old_read,StopListening=lambda **kwargs:None)
        old,new=Mock(),Mock()
        self.bridge.start_listening(old)
        thread=self.bridge._poll_thread
        try:
            self.assertTrue(entered.wait(1))
            self.bridge.stop_receiving()
            # 当前轮无需新消息；若旧轮询误用新 self._wx，其线程身份会留下证据。
            readers=[]
            def current_read():
                readers.append(threading.current_thread())
                return {}
            self.bridge._wx=SimpleNamespace(GetListenMessage=current_read,StopListening=lambda **kwargs:None)
            self.bridge.start_listening(new)
            release.set()
            thread.join(1.5)
            self.assertFalse(thread.is_alive())
            self.assertNotIn(thread,readers)
            old.assert_not_called();new.assert_not_called()
        finally:
            release.set();self.bridge.stop_receiving();thread.join(2)

    def test_late_native_listener_constructor_cannot_replace_the_new_listener(self):
        entered,release=threading.Event(),threading.Event()
        instances=[]
        class Listener:
            def __init__(self,*args,**kwargs):
                self.started=False;self.callback=None
                instances.append(self)
                if len(instances)==1:
                    entered.set()
                    if not release.wait(2):raise AssertionError('旧监听构造未释放')
            def add_all(self,callback,**kwargs):self.callback=callback
            def start(self):self.started=True
            def stop(self):self.started=False
        self.bridge.backend='wechatauto'
        errors=[]
        def start():
            try:self.bridge.start_listening(Mock())
            except BaseException as error:errors.append(error)
        with patch.dict(sys.modules,{'wechatauto.db':SimpleNamespace(Listener=Listener)}), \
             patch('src.wechat_listener.ListenerDatabase',return_value=object()):
            thread=threading.Thread(target=start)
            try:
                thread.start();self.assertTrue(entered.wait(1))
                self.bridge.stop_receiving()
                self.assertTrue(self.bridge.start_listening(Mock())[0])
            finally:
                release.set();thread.join(2)
        self.assertEqual(errors,[])
        self.assertFalse(thread.is_alive())
        self.assertFalse(instances[0].started)
        self.assertIs(self.bridge._listener,instances[1])


class SandboxStoppedAcknowledgementTests(ChannelCase):
    def host(self):
        service=self.service()
        host=SandboxBridge(self.root/'host',self.channel)
        host._pairing=self.pairing;host._epoch=service.epoch
        host._load_ack()
        return host

    def test_callback_that_stops_the_listener_cannot_publish_ack_or_receive_count(self):
        host=self.host()
        stop=host._poll_stop
        host._callbacks=[lambda raw:stop.set()]
        host._receive_event({'event_id':1,'message':{'content':'已停止的旧消息'}},stop)
        self.assertEqual(host._event_ack,0)
        self.assertFalse(host._ack_file.exists())
        self.assertEqual(host.recv_count,0)

    def test_late_async_completion_after_stop_keeps_event_pending(self):
        host=self.host()
        host._callbacks=[lambda raw:False]
        host._receive_event({'event_id':1,'message':{'content':'尚未确认'}},host._poll_stop)
        host.stop_receiving()
        host.ack_inbound(1)
        self.assertEqual(host._event_ack,0)
        self.assertFalse(host._ack_file.exists())
        self.assertIn(1,host._event_pending)

    def test_late_start_rpc_cannot_replace_restarted_listener(self):
        host=self.host()
        entered,release=threading.Event(),threading.Event()
        calls=[]
        def rpc(*args,**kwargs):
            calls.append(args[0])
            if len(calls)==1:
                entered.set()
                if not release.wait(2):raise AssertionError('旧监听 RPC 未释放')
            return {'ok':True}
        host._rpc=rpc
        old,new=Mock(),Mock()
        results=[]
        thread=threading.Thread(target=lambda:results.append(host.start_listening(old)))
        with patch('src.isolated_wechat.host_bridge.threading.Thread') as polling:
            try:
                thread.start();self.assertTrue(entered.wait(1))
                host.stop_receiving()
                self.assertTrue(host.start_listening(new)[0])
                current_stop=host._poll_stop
            finally:
                release.set();thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(results[0][0])
        self.assertEqual(host._callbacks,[new])
        self.assertIs(host._poll_stop,current_stop)
        self.assertFalse(current_stop.is_set())
        self.assertEqual(polling.call_count,1)

