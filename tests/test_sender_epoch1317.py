"""停止/恢复不能让旧发送复活；全部替身，不操作微信或真实剪贴板。"""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from src.wechat_sender import IsolatedWeChatSender
from src.wechat_transports import WeChatTransport
from tests.test_wechat_transport import make_transport,group_message


class TransportEpochTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp,self.bridge,self.transport=make_transport()

    async def asyncTearDown(self):
        await self.transport.close()
        self.tmp.cleanup()

    async def test_interval_waiter_from_before_stop_is_not_revived_by_resume(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def wait():
            entered.set();await release.wait()
        self.transport._wait_turn=wait
        pending=asyncio.create_task(self.transport.send(group_message(),'旧正文'))
        await entered.wait()
        self.transport.halt_sends();self.transport.resume_sends();release.set()
        result=await pending
        self.assertFalse(result[0])
        self.assertEqual(self.bridge.sent_log,[])
        self.assertEqual(result.diagnostic['submission_state'],'not_submitted')
        self.assertTrue((await self.transport.send(group_message(),'新正文'))[0])
        self.assertEqual([row['text'] for row in self.bridge.sent_log],['新正文'])

    async def test_stale_executor_queue_entry_is_rejected_before_calling_bridge(self):
        entered,release=threading.Event(),threading.Event()
        def busy():entered.set();release.wait(2)
        blocker=self.transport._sender.submit(busy)
        await asyncio.to_thread(entered.wait,1)
        pending=asyncio.create_task(self.transport.send(group_message(),'旧正文'))
        await asyncio.sleep(.01)
        self.transport.halt_sends();self.transport.resume_sends();release.set()
        await asyncio.wrap_future(blocker)
        self.assertFalse((await pending)[0])
        self.assertEqual(self.bridge.sent_log,[])

    async def test_resolve_session_waiter_does_not_send_after_stop_resume(self):
        entered,release=threading.Event(),threading.Event()
        def resolve(target):entered.set();release.wait(2);return target
        self.bridge.resolve_session=resolve
        pending=asyncio.create_task(self.transport.send_to('filehelper','旧正文'))
        await asyncio.to_thread(entered.wait,1)
        self.transport.halt_sends();self.transport.resume_sends();release.set()
        self.assertFalse((await pending)[0])
        self.assertEqual(self.bridge.sent_log,[])

    async def test_close_blocks_new_send_without_executor_exception(self):
        await self.transport.close()
        self.transport.resume_sends()
        result=await self.transport.send(group_message(),'关闭后正文')
        self.assertFalse(result[0])
        self.assertEqual(self.bridge.sent_log,[])

    async def test_cancelling_old_future_does_not_cancel_resumed_generation(self):
        entered,release=threading.Event(),threading.Event()
        cancelled=Mock()
        self.bridge.cancel_pending_sends=cancelled
        def old_call():entered.set();release.wait(2);return True,'old finished'
        task=asyncio.create_task(self.transport._run_send(old_call))
        await asyncio.to_thread(entered.wait,1)
        self.transport.halt_sends();self.transport.resume_sends()
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(cancelled.call_count,1)
        finally:release.set()

    async def test_old_timeout_cannot_mark_or_cancel_the_new_generation(self):
        entered,release=threading.Event(),threading.Event()
        self.transport._timeout=.08
        self.bridge.cancel_pending_sends=Mock()
        self.bridge.mark_send_timeout=Mock()
        def old_call():entered.set();release.wait(2);return True,'old finished'
        task=asyncio.create_task(self.transport._run_send(old_call))
        await asyncio.to_thread(entered.wait,1)
        self.transport.halt_sends();self.transport.resume_sends()
        try:
            result=await task
            self.assertFalse(result[0])
            self.assertEqual(self.bridge.cancel_pending_sends.call_count,1)
            self.bridge.mark_send_timeout.assert_not_called()
        finally:release.set()


class NativeSenderEpochTests(unittest.TestCase):
    def test_slow_prepare_from_before_stop_resume_cannot_launch_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            sender=IsolatedWeChatSender(Path(folder))
            entered,release=threading.Event(),threading.Event()
            original=sender._prepare_job
            def prepare(job):entered.set();release.wait(2);return original(job)
            sender._prepare_job=prepare
            result=[]
            with patch('src.wechat_sender.subprocess.Popen',side_effect=AssertionError('旧任务不可启动子进程')) as popen:
                task=threading.Thread(target=lambda:result.append(sender.SendMsg('旧正文','测试群')))
                task.start();self.assertTrue(entered.wait(1))
                sender.stop();sender.resume();release.set();task.join(2)
            self.assertFalse(task.is_alive())
            popen.assert_not_called()
            self.assertEqual(len(result),1)
            self.assertFalse(result[0].ok)
            self.assertEqual(result[0].diagnostic['submission_state'],'not_submitted')
