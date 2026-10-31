"""并发启动、单次领取和停止收尾的贯通回归；只使用模拟微信。"""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.test_wechat_app_flow import RuntimeTestCase, raw_message
from src.wechat_sender import IsolatedWeChatSender
from src.wechat_send_worker import send_picture, restrict_driver
from src.wechat_bridge import WeChatBridge
from src.wechat_transports import WeChatTransport


class SingleReplyTests(RuntimeTestCase):
    async def test_simultaneous_start_creates_one_listener_and_worker(self):
        bot = await self.start_runtime()
        await bot.stop()
        original = bot.bridge.connect
        def slow_connect():
            time.sleep(0.04)
            return original()
        with patch.object(bot.bridge, 'connect', side_effect=slow_connect) as connect, \
             patch.object(bot.bridge, 'start_listening', wraps=bot.bridge.start_listening) as listen:
            await asyncio.gather(*(bot.start() for _ in range(4)))
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(listen.call_count, 1)
        ai = self.install_ai('这次只回复一遍。')
        bot.ingest(raw_message('鲸鱼娘 你会唱歌吗'))
        await self.wait_for_send()
        await self.wait_for_quiet(0.15)
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(len(bot.bridge.sent_log), 1)

    async def test_two_workers_cannot_generate_for_same_inbox_record(self):
        bot = await self.start_runtime()
        entered, release = asyncio.Event(), asyncio.Event()
        async def ai(*args, **kwargs):
            entered.set()
            await release.wait()
            return '只发一条完整回答。'
        bot.ai.chat = AsyncMock(side_effect=ai)
        bot.ingest(raw_message('鲸鱼娘 解释一下这个问题'))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(self.states(), ['processing'])
        second = asyncio.create_task(bot._work())
        try:
            await asyncio.sleep(0.08)
            self.assertEqual(bot.ai.chat.await_count, 1)
            release.set()
            await self.wait_for_send()
            await self.wait_for_quiet(0.1)
            self.assertEqual(len(bot.bridge.sent_log), 1)
        finally:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)

    async def test_same_event_from_two_shards_is_not_replied_twice(self):
        bot = await self.start_runtime()
        ai = self.install_ai('同一个事件只答一次。')
        first = raw_message('鲸鱼娘 回答这个问题', local_id=1)
        first['sort_seq'] = 987654321
        bot.ingest(first)
        await self.wait_for_send()
        second = dict(first, local_id=77, shard='another-shard')
        bot.bridge.account_name = '改过的昵称'
        bot.ingest(second)
        await self.wait_for_quiet(0.2)
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(len(bot.bridge.sent_log), 1)
        self.assertEqual(len(self.inbox_rows()), 1)

    async def test_stop_cancels_preparation_and_does_not_replay_on_restart(self):
        bot = await self.start_runtime()
        entered = asyncio.Event()
        async def hanging(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        bot.ai.chat = hanging
        bot.ingest(raw_message('鲸鱼娘 解释第一项', local_id=1))
        await asyncio.wait_for(entered.wait(), 2)
        bot.ingest(raw_message('鲸鱼娘 解释第二项', local_id=2))
        await asyncio.wait_for(bot.stop(), 2)
        self.assertEqual(self.states(), ['cancelled', 'cancelled'])
        self.assertFalse(bot.transport._accept_sends)
        self.install_ai('新的问题。')
        await bot.start()
        await self.wait_for_quiet(0.15)
        self.assertFalse(bot.bridge.sent_log)
        bot.ingest(raw_message('鲸鱼娘 新的问题是什么', local_id=3))
        await self.wait_for_send()
        self.assertEqual(len(bot.bridge.sent_log), 1)

    async def test_reply_keeps_at_most_one_picture(self):
        bot = await self.start_runtime()
        one, two = self.root / 'one.png', self.root / 'two.png'
        one.write_bytes(b'picture-one');two.write_bytes(b'picture-two')
        async def append(message, reply, runtime):
            runtime.reply_meta = {'media': [{'type':'image','data':{'file':str(p)}} for p in (one,two)]}
            return reply
        bot.plugins.run_on_post_reply = append
        self.install_ai('这次最多附一张。')
        bot.ingest(raw_message('鲸鱼娘 看看这张表情'))
        await self.wait_for_send(2)
        await self.wait_for_inbox_done()
        self.assertEqual([s['text'] for s in bot.bridge.sent_log], ['这次最多附一张。','[文件]one.png'])

    async def test_untriggered_images_do_not_start_background_downloads(self):
        bot = await self.start_runtime()
        bot.transport.call = AsyncMock()
        bot.ingest(dict(raw_message('', at=False), type='图片'))
        await bot.plugins._plugins['sticker_steal'].tick(bot)
        await asyncio.sleep(0.05)
        bot.transport.call.assert_not_awaited()
        self.assertFalse(bot.plugins._plugins['sticker_steal']._queue)

    async def test_slow_listener_exit_does_not_block_stop_response(self):
        bot = await self.start_runtime()
        release = threading.Event()
        def slow_stop():
            release.wait(2)
        try:
            with patch.object(bot.bridge, 'stop_listening', side_effect=slow_stop), \
                 patch('src.wechat_app.LISTENER_STOP_TIMEOUT', 0.03):
                await asyncio.wait_for(bot.stop(), 1)
            self.assertFalse(bot.running)
            self.assertTrue(bot.bridge._poll_stop.is_set())
            self.assertFalse(bot.transport._accept_sends)
        finally:
            release.set()


class SenderStopTests(unittest.TestCase):
    def test_stop_does_not_wait_for_another_app_delayed_clipboard(self):
        entered, release, restored = threading.Event(), threading.Event(), threading.Event()
        @contextmanager
        def clipboard(path):
            entered.set();release.wait(2)
            try:
                yield SimpleNamespace(sequence=123)
            finally:
                restored.set()
        with tempfile.TemporaryDirectory() as folder:
            sender = IsolatedWeChatSender(Path(folder))
            with patch('src.picture_clipboard.PictureClipboard', clipboard), \
                 patch('src.wechat_sender.subprocess.Popen') as spawn:
                thread = threading.Thread(target=sender._run, args=({'action':'files','as_image':True,'paths':['test.png']},))
                thread.start()
                self.assertTrue(entered.wait(1))
                started = time.monotonic();sender.stop()
                self.assertLess(time.monotonic()-started, 0.2)
                release.set();thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertTrue(restored.is_set())
                spawn.assert_not_called()

    def test_stop_terminates_only_owned_send_process_and_removes_job_files(self):
        with tempfile.TemporaryDirectory() as folder:
            sender = IsolatedWeChatSender(Path(folder))
            entered, ended = threading.Event(), threading.Event()
            process = Mock()
            process.poll.side_effect = lambda: 1 if ended.is_set() else None
            process.wait.side_effect = lambda **kw: (entered.set(), ended.wait(2), 1)[2]
            process.kill.side_effect = ended.set
            with patch('src.wechat_sender.subprocess.Popen', return_value=process):
                thread = threading.Thread(target=sender._run, args=({'action':'self-test'},))
                thread.start()
                self.assertTrue(entered.wait(1))
                sender.stop()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                process.kill.assert_called_once()
                self.assertFalse(list((Path(folder)/'send_jobs').iterdir()))

    def test_stopped_sender_never_starts_late_queued_job(self):
        with tempfile.TemporaryDirectory() as folder:
            sender = IsolatedWeChatSender(Path(folder));sender.stop()
            with patch('src.wechat_sender.subprocess.Popen') as spawn:
                result = sender._run({'action':'self-test'})
            self.assertFalse(result)
            spawn.assert_not_called()

    def test_stopped_listener_drops_late_callbacks_without_reading_names(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = WeChatBridge(Path(folder), backend='mock');bridge.connect()
            callback = Mock();bridge.start_listening(callback);bridge.stop_listening()
            bridge.display_name = Mock(side_effect=AssertionError('停止后不可查库'))
            bridge._db = object();bridge._emit({'username':'late'})
            callback.assert_not_called()


class PictureInputTests(unittest.TestCase):
    def test_driver_cannot_activate_process_memory_or_click_login(self):
        original = Mock(return_value=True)
        driver = SimpleNamespace(ensure_window=original)
        restrict_driver(driver)
        driver.ensure_window(wake=True)
        original.assert_called_once_with(wake=False, timeout=None)
        self.assertFalse(driver._wake_accessibility())
        self.assertFalse(driver._hot_activate_accessibility(123))
        self.assertFalse(driver._auto_login())
        self.assertFalse(driver.ensure_materialized(force=True))

    def driver(self, values):
        driver = Mock();driver.current_chat.return_value = '目标'
        driver._chat_input.return_value.HasKeyboardFocus = True
        driver._read_edit_value.side_effect = values
        return driver, driver._chat_input.return_value

    def test_picture_has_one_paste_and_one_send_with_no_screen_reader(self):
        driver, control = self.driver(['', '\ufffc', ''])
        self.assertTrue(send_picture(driver, '目标')[0])
        self.assertEqual([v.args[0] for v in control.SendKeys.call_args_list], ['{Ctrl}v','{Enter}'])

    def test_uncertain_picture_draft_never_sends_or_pastes_again(self):
        driver, control = self.driver(['', '', ''])
        with patch('src.wechat_send_worker.time.monotonic', side_effect=[0, 2]):
            self.assertFalse(send_picture(driver, '目标')[0])
        self.assertEqual([v.args[0] for v in control.SendKeys.call_args_list], ['{Ctrl}v'])

    def test_existing_draft_is_preserved(self):
        driver, control = self.driver(['用户正在输入'])
        self.assertFalse(send_picture(driver, '目标')[0])
        control.SendKeys.assert_not_called()

    def test_changed_clipboard_never_pastes_user_contents(self):
        driver, control = self.driver([''])
        self.assertFalse(send_picture(driver, '目标', clipboard_ready=lambda:False)[0])
        control.SendKeys.assert_not_called()


class TransportStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_send_interval_prevents_late_send(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = WeChatBridge(Path(folder),backend='mock');bridge.connect()
            transport = WeChatTransport(bridge)
            entered, release = asyncio.Event(), asyncio.Event()
            async def waiting():
                entered.set();await release.wait()
            transport._wait_turn = waiting
            task = asyncio.create_task(transport.send({'target':'g'}, '正文'))
            await entered.wait();transport.halt_sends();release.set()
            self.assertFalse((await task)[0])
            self.assertFalse(bridge.sent_log)
            await transport.close()
