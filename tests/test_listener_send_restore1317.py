"""监听热更新与重连恢复发送；全程替身，无桌面、网络或真实微信。"""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.wechat_app import WeChatRuntime


class Bridge:
    backend='sandbox'
    online=True
    consecutive_fail=0
    last_error=''
    def __init__(self):
        self.stopped=False;self.resumed=0;self.cancelled=0
    def stop_listening(self):self.stopped=True
    def stop_receiving(self):pass
    def start_listening(self,callback):return True,'就绪'
    def reconnect(self):self.stopped=True;self.online=True;return True,'重连就绪'
    def resume_sends(self):self.resumed+=1;self.stopped=False
    def cancel_pending_sends(self):self.cancelled+=1;self.stopped=True


class ListenerSendRestoreTests(unittest.IsolatedAsyncioTestCase):
    def bot(self):
        bridge=Bridge()
        transport=SimpleNamespace(_accept_sends=True,_send_epoch=0,_send_closed=False)
        def reset():transport._send_epoch+=1
        transport.reset_sender=reset
        bot=SimpleNamespace(bridge=bridge,transport=transport,running=True,_stop_requested=False,
            _safety_volatile=False,safety=SimpleNamespace(status=lambda:{}),
            _configured_groups=lambda:[],group_monitoring_enabled=lambda:True,
            cancel_disallowed_pending=Mock(),on_bridge_message=Mock(),note=Mock(),
            connecting=False,reconnecting=False,account=lambda:'离线账号',
            config=SimpleNamespace(get=lambda *args:True))
        return bot

    async def test_monitor_refresh_restores_previously_allowed_send_channel(self):
        bot=self.bot()
        await WeChatRuntime.refresh_monitoring(bot)
        self.assertFalse(bot.bridge.stopped)
        self.assertEqual(bot.bridge.resumed,1)
        self.assertTrue(bot.transport._accept_sends)

    async def test_monitor_refresh_never_unhalts_an_existing_send_pause(self):
        bot=self.bot();bot.transport._accept_sends=False
        await WeChatRuntime.refresh_monitoring(bot)
        self.assertTrue(bot.bridge.stopped)
        self.assertEqual(bot.bridge.resumed,0)

    async def test_health_reconnect_restores_previously_allowed_send_channel(self):
        bot=self.bot();bot.bridge.online=False
        # 旧代码一直不 resume；第二轮 sleep 停止替身以免无限等待。
        steps=0
        async def bounded_sleep(seconds):
            nonlocal steps
            steps+=1
            if steps>2:bot.running=False
        with patch('src.wechat_app.asyncio.sleep',side_effect=bounded_sleep):
            await WeChatRuntime._health(bot)
        self.assertFalse(bot.bridge.stopped)
        self.assertEqual(bot.bridge.resumed,1)

    async def test_refresh_halted_during_cleanup_does_not_restore(self):
        bot=self.bot()
        def stop():
            bot.bridge.stopped=True
            bot.transport._accept_sends=False
            bot.transport._send_epoch+=1
        bot.bridge.stop_listening=stop
        await WeChatRuntime.refresh_monitoring(bot)
        self.assertEqual(bot.bridge.resumed,0)
        self.assertFalse(bot.transport._accept_sends)

    async def test_resume_wait_crossing_stop_keeps_both_gates_stopped(self):
        await self.resume_wait_case('stop')

    async def test_cancelled_resume_wait_still_applies_stop_after_late_rpc(self):
        await self.resume_wait_case('cancel')

    async def test_old_resume_completion_does_not_cancel_an_allowed_new_run(self):
        await self.resume_wait_case('new-run')

    async def test_resume_wait_crossing_safety_hold_remains_stopped(self):
        await self.resume_wait_case('safety')

    async def resume_wait_case(self, action):
        bot=self.bot()
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        callback=WeChatRuntime._receive_callback(bot)
        def resume():
            entered.set()
            if not release.wait(2):raise AssertionError('恢复替身等待未释放')
            bot.bridge.resumed+=1;bot.bridge.stopped=False
        bot.bridge.resume_sends=resume
        original_cancel=bot.bridge.cancel_pending_sends
        def cancel():original_cancel();finished.set()
        bot.bridge.cancel_pending_sends=cancel
        task=asyncio.create_task(WeChatRuntime._restore_listener_sends(
            bot,bot.bridge,bot.transport,0,callback))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            bot.transport._send_epoch+=1
            if action=='new-run':
                WeChatRuntime._receive_callback(bot)
            elif action=='safety':
                bot._safety_volatile=True
            else:
                bot.running=False;bot._stop_requested=True;bot.transport._accept_sends=False
            if action=='cancel':
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            release.set()
            if action=='cancel':
                self.assertTrue(await asyncio.to_thread(finished.wait,1))
            else:
                self.assertFalse(await task)
            self.assertEqual(bot.bridge.resumed,1)
            if action=='new-run':
                self.assertEqual(bot.bridge.cancelled,0)
                self.assertTrue(bot.transport._accept_sends)
            else:
                self.assertTrue(bot.bridge.stopped)
                self.assertEqual(bot.bridge.cancelled,1)
        finally:
            release.set()
            await asyncio.gather(task,return_exceptions=True)

    async def test_health_stop_during_reconnect_never_restarts_listener(self):
        bot=self.bot();bot.bridge.online=False
        def reconnect():
            bot.running=False;bot._stop_requested=True
            bot.transport._accept_sends=False;bot.transport._send_epoch+=1
            return True,'已停止'
        bot.bridge.reconnect=reconnect
        bot.bridge.start_listening=Mock()
        async def sleep(seconds):pass
        with patch('src.wechat_app.asyncio.sleep',side_effect=sleep):
            await WeChatRuntime._health(bot)
        bot.bridge.start_listening.assert_not_called()
        self.assertEqual(bot.bridge.resumed,0)

    async def test_refresh_cleanup_cannot_restart_after_stop_and_new_start(self):
        bot=self.bot()
        def cleanup():bot.transport._send_epoch+=1
        bot.bridge.stop_listening=cleanup
        bot.bridge.start_listening=Mock()
        await WeChatRuntime.refresh_monitoring(bot)
        bot.bridge.start_listening.assert_not_called()

    async def test_refresh_late_start_does_not_stop_the_new_listener(self):
        bot=self.bot()
        def start(callback):
            bot.transport._send_epoch+=1
            bot.bridge._runtime_receive_callback=object()
            return True,'新运行已开始'
        bot.bridge.start_listening=start
        bot.bridge.stop_receiving=Mock()
        await WeChatRuntime.refresh_monitoring(bot)
        bot.bridge.stop_receiving.assert_not_called()
        self.assertEqual(bot.bridge.resumed,0)

    async def test_health_late_start_does_not_stop_the_new_listener(self):
        bot=self.bot();bot.bridge.online=False
        def start(callback):
            bot.transport._send_epoch+=1
            bot.bridge._runtime_receive_callback=object()
            return True,'新运行已开始'
        bot.bridge.start_listening=start
        bot.bridge.stop_receiving=Mock()
        async def sleep(seconds):pass
        with patch('src.wechat_app.asyncio.sleep',side_effect=sleep):
            await WeChatRuntime._health(bot)
        bot.bridge.stop_receiving.assert_not_called()
        self.assertEqual(bot.bridge.resumed,0)

