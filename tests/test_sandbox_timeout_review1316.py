"""长只读连接/定位的事件循环与停止边界；不访问微信或执行发送。"""
import asyncio
import threading
from unittest.mock import Mock, patch

from src.wechat_app import WeChatRuntime
from tests.test_wechat_app_flow import RuntimeTestCase, write_config


class SandboxReadOnlyWaitTests(RuntimeTestCase):
    async def _stop_during_readonly_wait(self, operation):
        config = write_config(self.root)
        bot = self.runtime = WeChatRuntime(root=self.root, config_path=config,
                                           db_path=self.root / 'messages.sqlite3')
        entered, release = threading.Event(), threading.Event()
        loop_was_responsive = asyncio.Event()

        def blocked(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError('测试没有释放只读替身')
            return (True, '假读取已连接') if operation == 'connect' else {'ready': True}

        connect = blocked if operation == 'connect' else Mock(return_value=(True, '假读取已连接'))
        inspect = blocked if operation == 'inspect_send_environment' else Mock(return_value={'ready': True})
        with patch.object(bot.bridge, 'backend', 'sandbox'), \
             patch.object(bot.bridge, 'connect', side_effect=connect), \
             patch.object(bot.bridge, 'inspect_send_environment', side_effect=inspect), \
             patch.object(bot.bridge, 'start_listening') as listen:
            start = asyncio.create_task(bot.start())
            stop = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                asyncio.get_running_loop().call_soon(loop_was_responsive.set)
                await asyncio.wait_for(loop_was_responsive.wait(), .5)
                stop = asyncio.create_task(bot.stop())
                # 停止意图必须在只读线程返回前生效，不能晚返回后再启动监听。
                await asyncio.sleep(0)
                self.assertTrue(bot._stop_requested)
                self.assertFalse(bot.running)
                self.assertFalse(bot.transport._accept_sends)
            finally:
                release.set()
                await asyncio.wait_for(start, 2)
                if stop is not None:
                    await asyncio.wait_for(stop, 2)
            listen.assert_not_called()
        self.assertFalse(bot.running)
        self.assertFalse(bot.transport._accept_sends)

    async def test_cold_readonly_connect_keeps_loop_alive_and_cannot_start_after_stop(self):
        await self._stop_during_readonly_wait('connect')

    async def test_readonly_inspection_keeps_loop_alive_and_cannot_start_after_stop(self):
        await self._stop_during_readonly_wait('inspect_send_environment')
