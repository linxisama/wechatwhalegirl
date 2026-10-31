"""不锁真实桌面、不操作真实微信；模拟原生句柄、控件树和桌面状态。"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.wechat_window import prepare_window, NativeWindowAccess
from src.wechat_send_worker import send_text, execute
from tests.test_wechat_app_flow import RuntimeTestCase, raw_message


class WindowDiscoveryTests(unittest.TestCase):
    def objects(self, minimized=False, root=None):
        root = root or SimpleNamespace(ClassName='mmui::MainWindow', GetChildren=lambda: [])
        access = Mock()
        access.candidates.return_value = [{'hwnd': 100, 'pid': 20, 'minimized': minimized}]
        access.restore.return_value = True
        def restored(_hwnd):
            access.candidates.return_value[0]['minimized'] = False
            return True
        access.restore.side_effect = restored
        access.minimized.return_value = False
        access.control.return_value = root
        return Mock(), access

    def test_minimized_main_window_is_restored_once_before_using_controls(self):
        uia, access = self.objects(True)
        order = []
        restored = access.restore.side_effect
        access.restore.side_effect = lambda hwnd: order.append('restore') or restored(hwnd)
        uia._activate.side_effect = lambda win: order.append('activate')
        self.assertTrue(prepare_window(uia, access, lambda: (True, '')))
        self.assertEqual(order, ['restore', 'activate'])
        access.control.assert_called_once_with(100)
        uia._weixin_dll_module.assert_not_called()

    def test_locked_desktop_does_not_enumerate_or_restore_any_window(self):
        uia, access = self.objects(True)
        self.assertFalse(prepare_window(uia, access, lambda: (False, '等待解锁')))
        access.candidates.assert_not_called()
        access.restore.assert_not_called()
        uia._activate.assert_not_called()
        self.assertEqual(uia._window_error, '等待解锁')

    def test_multiple_main_windows_are_not_guessed(self):
        uia, access = self.objects(True)
        access.candidates.return_value *= 2
        self.assertFalse(prepare_window(uia, access, lambda: (True, '')))
        self.assertIn('多个', uia._window_error)
        access.restore.assert_not_called()

    def test_qt_outer_window_is_accepted_only_with_scoped_chat_input(self):
        field = SimpleNamespace(ClassName='mmui::ChatInputField', AutomationId='', GetChildren=lambda: [])
        root = SimpleNamespace(ClassName='Qt51514QWindowIcon', AutomationId='', GetChildren=lambda: [field])
        uia, access = self.objects(root=root)
        self.assertTrue(prepare_window(uia, access, lambda: (True, '')))
        self.assertIs(uia._win, root)

    def test_empty_qt_shell_remains_a_clear_failure_without_accessibility_writes(self):
        root = SimpleNamespace(ClassName='Qt51514QWindowIcon', AutomationId='', GetChildren=lambda: [])
        uia, access = self.objects(root=root)
        self.assertFalse(prepare_window(uia, access, lambda: (True, '')))
        self.assertIn('未暴露', uia._window_error)
        uia._activate.assert_not_called()
        uia._wake_accessibility.assert_not_called()

    def test_failed_restore_is_bounded_and_does_not_type(self):
        uia, access = self.objects(True)
        access.minimized.return_value = True
        with patch('src.wechat_window.time.monotonic', side_effect=[0, 2]):
            self.assertFalse(prepare_window(uia, access, lambda: (True, '')))
        access.control.assert_not_called()

    def test_executable_query_does_not_require_process_modules(self):
        adapter = object.__new__(NativeWindowAccess)
        adapter.gui = Mock()
        adapter.gui.EnumWindows.side_effect = lambda callback, arg: callback(100, arg)
        adapter.gui.IsIconic.return_value = True
        adapter.gui.GetWindowText.return_value = '微信'
        adapter.gui.GetClassName.return_value = 'Qt51514QWindowIcon'
        adapter.gui.GetWindow.return_value = 0
        adapter.gui.GetWindowLong.side_effect = lambda hwnd, index: 0x96c70000 if index == -16 else 0
        adapter.process = Mock()
        adapter.process.GetWindowThreadProcessId.return_value = (1, 20)
        adapter.psutil = Mock()
        adapter.psutil.Process.return_value.name.return_value = 'weixin.exe'
        adapter.psutil.Process.return_value.create_time.return_value = 100.25
        self.assertEqual(adapter.candidates()[0]['hwnd'], 100)
        adapter.psutil.Process.return_value.memory_maps.assert_not_called()
        adapter.process.EnumProcessModules.assert_not_called()

    def test_worker_fails_before_loading_wechat_when_desktop_is_locked(self):
        with patch('src.interactive_desktop.interactive_desktop_status', return_value=(False, '等待解锁')):
            self.assertEqual(execute({'action': 'text', 'who': '测试群', 'text': '正文'}), (False, '等待解锁'))

    def test_window_reason_is_preserved_in_send_result(self):
        driver = Mock(_window_error='微信已收起到托盘，请手动打开')
        driver.ensure_window.return_value = False
        ok, reason = send_text(driver, '测试群', '正文')
        self.assertFalse(ok)
        self.assertIn('托盘', reason)
        driver._chat_input.assert_not_called()

    def test_wrong_conversation_never_searches_or_switches_chats(self):
        driver = Mock()
        driver.ensure_window.return_value = True
        driver.current_chat.return_value = '其他会话'
        ok, reason = send_text(driver, '目标会话', '正文')
        self.assertFalse(ok)
        self.assertIn('手动打开', reason)
        driver.open_chat.assert_not_called()
        driver._chat_input.assert_not_called()


class DesktopQueueTests(RuntimeTestCase):
    async def begin(self, initially_locked):
        runtime = await self.start_runtime()
        # 仅在已建立的 mock 运行时测试真实后端的桌面闸门，发送接口仍完全模拟。
        runtime.bridge.backend = 'wechatauto'
        self.locked = initially_locked
        status = patch('src.interactive_desktop.interactive_desktop_status',
                       side_effect=lambda: (not self.locked, '等待解锁'))
        status.start()
        self.addCleanup(status.stop)
        sender = patch.object(runtime.transport, 'send', new=AsyncMock(return_value=(True, '模拟发送')))
        self.sender = sender.start()
        self.addCleanup(sender.stop)
        self.ai = AsyncMock(return_value='鲸鱼每年沿着海岸迁徙。')
        runtime.ai.chat = self.ai
        return runtime

    async def until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('等待模拟状态超时')
            await asyncio.sleep(0.02)

    async def test_locked_queue_waits_without_ai_then_sends_once_after_unlock(self):
        runtime = await self.begin(True)
        runtime.ingest(raw_message('鲸鱼娘 介绍鲸鱼迁徙路线', local_id=2401))
        await self.until(lambda: runtime.pipeline['stage'] == 'waiting_desktop')
        self.assertEqual(self.states(), ['pending'])
        self.ai.assert_not_awaited()
        self.sender.assert_not_awaited()
        self.locked = False
        await self.until(lambda: self.states() == ['sent'])
        self.assertEqual(self.ai.await_count, 1)
        self.assertEqual(self.sender.await_count, 1)

    async def test_lock_during_model_keeps_reply_without_calling_model_again(self):
        runtime = await self.begin(False)
        async def model(*args, **kwargs):
            self.locked = True
            return '鲸鱼沿海岸迁徙。'
        self.ai.side_effect = model
        runtime.ingest(raw_message('鲸鱼娘 介绍鲸鱼迁徙路线', local_id=2402))
        await self.until(lambda: runtime.pipeline['stage'] == 'waiting_desktop')
        self.assertEqual(self.states(), ['processing'])
        self.assertTrue(self.inbox_rows()[0][2])
        self.sender.assert_not_awaited()
        self.locked = False
        await self.until(lambda: self.states() == ['sent'])
        self.assertEqual(self.ai.await_count, 1)
        self.assertEqual(self.sender.await_count, 1)

    async def test_stopping_while_locked_cancels_pending_without_sending(self):
        runtime = await self.begin(True)
        runtime.ingest(raw_message('鲸鱼娘 介绍鲸鱼迁徙路线', local_id=2403))
        await self.until(lambda: runtime.pipeline['stage'] == 'waiting_desktop')
        await runtime.stop()
        self.assertEqual(self.states(), ['cancelled'])
        self.ai.assert_not_awaited()
        self.sender.assert_not_awaited()

    async def test_expired_waiting_message_is_not_replied_after_unlock(self):
        runtime = await self.begin(True)
        runtime.ingest(raw_message('鲸鱼娘 介绍鲸鱼迁徙路线', local_id=2404))
        await self.until(lambda: runtime.pipeline['stage'] == 'waiting_desktop')
        runtime.db.execute('UPDATE inbox SET created=?', (time.time() - 1000,))
        runtime.db.commit()
        self.locked = False
        await self.until(lambda: self.states() == ['expired'])
        self.ai.assert_not_awaited()
        self.sender.assert_not_awaited()
