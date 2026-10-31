"""目录草稿、重连配置、托盘和严格后台的行为回归；不向真实微信发送。"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src.wechat_background import inspect_background, send_background
from src.wechat_bridge import WeChatBridge, describe_accounts
from src.wechat_sender import IsolatedWeChatSender


class DirectoryAndReconnectTests(unittest.TestCase):
    def test_account_list_uses_same_fallback_as_connect(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / '其他盘 中文 空格' / 'xwechat_files'
            storage = root / 'wxid_test_abcd' / 'db_storage'
            storage.mkdir(parents=True)
            with patch('wechatauto.db.auto_detect_db_dir', return_value=None), \
                 patch('src.wechat_paths._known_candidates', return_value=iter([str(root)])), \
                 patch('src.wechat_bridge.active_database_account', return_value=''), \
                 patch('wechatauto.db.WeChatDB', side_effect=RuntimeError('no nickname')):
                self.assertEqual(describe_accounts('', strict=True)[0]['account'], 'wxid_test_abcd')
                for directory in (root, storage.parent, storage):
                    self.assertEqual(describe_accounts(str(directory), strict=True)[0]['account'], 'wxid_test_abcd')
                with self.assertRaisesRegex(ValueError, '指定目录'):
                    describe_accounts(str(root / 'missing'), strict=True)

    def test_connect_and_reconnect_do_not_reset_saved_send_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'xwechat_files'
            (root / 'wxid_test_abcd' / 'db_storage').mkdir(parents=True)
            bridge = WeChatBridge(Path(folder) / 'data', db_dir=str(root))
            bridge.send_compat = True
            bridge.auto_open_chat = False
            bridge.background_send = True
            db = SimpleNamespace(db_dir=str(root), account='wxid_test_abcd', wxid='wxid_test',
                                 get_self_info=lambda: {'nick_name': 'test'})
            with patch('wechatauto.db.WeChatDB', return_value=db), \
                 patch.object(bridge, 'choose_account', return_value=('wxid_test_abcd', '设置里指定的账号')):
                for _ in range(3):
                    self.assertTrue(bridge._connect_wechatauto()[0])
                    job = bridge._wx._prepare_job({'action': 'window-check'})
                    self.assertTrue(job['send_compat'])
                    self.assertTrue(job['background_send'])
                    self.assertFalse(job['auto_open_chat'])

    def test_http_directory_preview_ignores_stale_hint_and_does_not_save(self):
        from fastapi.testclient import TestClient
        from src.wechat_app import WeChatRuntime, create_app
        from tests.test_wechat_recovery import _write_config
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            config = _write_config(root)
            runtime = WeChatRuntime(root=root, config_path=config, db_path=root/'test.db')
            runtime.bridge.db_dir = 'saved'
            runtime.bridge._db_dir_hint = 'stale'
            before = config.read_bytes()
            with TestClient(create_app(runtime)) as client, patch('src.wechat_bridge.describe_accounts', return_value=[]) as discover:
                client.get('/api/wechat/accounts', params={'db_dir': 'E:/中文 空格/xwechat_files/test/db_storage'})
                discover.assert_called_with('E:/中文 空格/xwechat_files/test/db_storage', strict=True)
                client.get('/api/wechat/accounts')
                discover.assert_called_with('saved', strict=True)
                client.get('/api/wechat/accounts', params={'db_dir': ''})
                discover.assert_called_with('', strict=True)
                discover.side_effect = ValueError('指定目录无法读取')
                result = client.get('/api/wechat/accounts', params={'db_dir': 'bad'}).json()
                self.assertFalse(result['success'])
                self.assertIn('指定目录无法读取', result['message'])
            self.assertEqual(config.read_bytes(), before)


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.value = SimpleNamespace(Value='', IsReadOnly=False)
        self.value.SetValue = Mock(side_effect=lambda text: setattr(self.value, 'Value', text))
        self.invoke = SimpleNamespace(Invoke=Mock(side_effect=lambda: setattr(self.value, 'Value', '')))
        self.field = SimpleNamespace(ClassName='mmui::ChatInputField', AutomationId='', Name='测试群',
            ControlTypeName='EditControl', GetChildren=lambda: [], GetValuePattern=lambda: self.value)
        self.button = SimpleNamespace(ClassName='mmui::XButton', AutomationId='', Name='发送', IsEnabled=True,
            ControlTypeName='ButtonControl', GetChildren=lambda: [], GetInvokePattern=lambda: self.invoke)
        self.root = SimpleNamespace(ClassName='mmui::MainWindow', AutomationId='', Name='微信',
            ControlTypeName='WindowControl', GetChildren=lambda: [self.field, self.button])
        self.row = {'hwnd': 100, 'pid': 200, 'minimized': False}
        self.access = SimpleNamespace(candidates=lambda: [self.row], matches_account=lambda pid, path: True,
                                      control=lambda hwnd: self.root)
        self.desktop = lambda: (True, '')
        self.job = {'account_path': 'test', 'who': '测试群', 'text': '中文 {Ctrl}a\nEnglish'}
        self.phase = Mock()

    def inspect(self, who='测试群'):
        return inspect_background('test', who, self.access, self.desktop)

    def send(self):
        return send_background(self.job, self.phase, self.access, self.desktop)

    def test_background_sends_literal_text_once_without_keyboard_or_mouse_methods(self):
        self.assertTrue(self.send()[0])
        self.value.SetValue.assert_called_once_with(self.job['text'])
        self.invoke.Invoke.assert_called_once()
        self.assertEqual(self.value.Value, '')

    def test_inspection_never_writes_or_submits(self):
        self.assertTrue(self.inspect()[0]['ready'])
        self.value.SetValue.assert_not_called()
        self.invoke.Invoke.assert_not_called()

    def test_other_chat_waits_without_writing(self):
        self.field.Name = '其他群'
        self.assertIn('手动打开', self.send()[1])
        self.value.SetValue.assert_not_called()

    def test_minimized_window_is_not_restored(self):
        self.row['minimized'] = True
        self.assertIn('最小化', self.send()[1])
        self.value.SetValue.assert_not_called()

    def test_existing_draft_is_preserved(self):
        self.value.Value = '用户草稿'
        self.assertIn('草稿', self.send()[1])
        self.assertEqual(self.value.Value, '用户草稿')
        self.value.SetValue.assert_not_called()

    def test_no_send_button_no_write_and_no_keyboard_fallback(self):
        self.root.GetChildren = lambda: [self.field]
        self.assertIn('发送按钮', self.send()[1])
        self.value.SetValue.assert_not_called()

    def test_ambiguous_send_buttons_never_write_or_submit(self):
        self.root.GetChildren = lambda: [self.field, self.button, self.button]
        self.assertIn('唯一确认', self.send()[1])
        self.value.SetValue.assert_not_called()
        self.invoke.Invoke.assert_not_called()

    def test_readonly_and_missing_invoke_wait(self):
        self.value.IsReadOnly = True
        self.assertFalse(self.inspect()[0]['ready'])
        self.value.IsReadOnly = False
        self.button.GetInvokePattern = lambda: None
        self.assertFalse(self.inspect()[0]['ready'])
        self.value.SetValue.assert_not_called()

    def test_partial_write_is_not_submitted(self):
        self.value.SetValue.side_effect = lambda text: setattr(self.value, 'Value', text[:2])
        self.assertIn('回读不一致', self.send()[1])
        self.invoke.Invoke.assert_not_called()

    def test_chat_changed_after_write_is_not_submitted(self):
        def write(text):
            self.value.Value = text
            self.field.Name = '另外的聊天'
        self.value.SetValue.side_effect = write
        self.assertIn('会话或状态改变', self.send()[1])
        self.invoke.Invoke.assert_not_called()

    def test_unconfirmed_submission_is_not_retried(self):
        self.invoke.Invoke.side_effect = None
        self.assertIn('结果待核实', self.send()[1])
        self.invoke.Invoke.assert_called_once()

    def test_background_supplier_failure_cannot_fall_back_to_foreground(self):
        sender = IsolatedWeChatSender(Path('.'), background=Mock(side_effect=ValueError('invalid')))
        self.assertTrue(sender._prepare_job({})['background_send'])

    def test_dispatch_does_not_enter_compat_activation(self):
        from src.wechat_send_worker import execute
        with patch('src.wechat_background.inspect_background', return_value=({'ready': False, 'reason': 'waiting'}, None)), \
             patch('src.wechat_send_worker.enable_send_compat', side_effect=AssertionError('must not activate')):
            self.assertEqual(execute({'action': 'window-check', 'background_send': True, 'send_compat': True}), (False, 'waiting'))

    def test_old_backend_never_falls_back_to_foreground_or_restores_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = WeChatBridge(Path(folder), backend='wxauto4')
            bridge.background_send = True
            with patch.object(bridge, '_connect_wxauto4') as connect:
                self.assertFalse(bridge.connect()[0])
                connect.assert_not_called()
            bridge.online = True
            bridge._wx = Mock()
            with patch.object(bridge, '_chat_name', return_value='测试群'), \
                 patch.object(bridge, '_restore_minimized_windows') as restore:
                self.assertFalse(bridge.send_text('test', 'hello')[0])
                self.assertFalse(bridge.send_files('test', ['unused.png'], as_image=True)[0])
                self.assertFalse(bridge.inspect_send_environment()['ready'])
                restore.assert_not_called()
            bridge._wx.SendMsg.assert_not_called()
            bridge._wx.SendFiles.assert_not_called()


class TrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_menu_controls_existing_bot_and_hidden_window_can_return(self):
        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QWidget, QSystemTrayIcon
        from src.gui.tray import RobotTray
        window = QWidget()
        window._action = Mock()
        window._request_exit = Mock()
        window._busy = False
        window._quitting = False
        with patch.object(QSystemTrayIcon, 'show'), patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True):
            tray = RobotTray(window, QIcon(), available=True)
            window.show()
            tray.update({'running': False})
            tray.start_action.trigger()
            window._action.assert_called_once_with('start')
            self.assertFalse(tray.stop_action.isEnabled())
            tray.update({'running': True})
            tray.stop_action.trigger()
            self.assertEqual(window._action.call_args.args, ('stop',))
            self.assertTrue(tray.hide_window())
            self.assertFalse(window.isVisible())
            tray._activated(QSystemTrayIcon.ActivationReason.DoubleClick)
            self.assertTrue(window.isVisible())
            tray.exit_action.trigger()
            window._request_exit.assert_called_once()
            window._busy = True
            tray.refresh_actions()
            self.assertFalse(tray.stop_action.isEnabled())
            tray.icon.hide()
            window.close()

    def test_gui_directory_preview_encodes_unsaved_path(self):
        import wechat_gui as gui
        from urllib.parse import parse_qs, urlsplit
        tab = gui.SettingsTab()
        tab.db_dir.setText('E:/中文 空格/xwechat_files/test/db_storage')
        with patch.object(gui, 'call_api') as request:
            tab._load_accounts()
        path = request.call_args.args[2]
        self.assertEqual(parse_qs(urlsplit(path).query)['db_dir'], [tab.db_dir.text()])
        tab.close()

    def test_close_to_tray_keeps_backend_even_when_request_is_running(self):
        import wechat_gui as gui
        from PyQt6.QtCore import QRect
        window = SimpleNamespace(_quitting=False, _tray=SimpleNamespace(hide_window=lambda: True),
            _cfg={}, geometry=lambda: QRect(1, 2, 900, 600), _backend_proc=Mock(),
            _action_worker=Mock())
        event = Mock()
        with patch.object(gui, 'save_gui_config'):
            gui.MainWindow.closeEvent(window, event)
        event.ignore.assert_called_once()
        window._backend_proc.terminate.assert_not_called()
        window._action_worker.isRunning.assert_not_called()

    def test_unsaved_directory_change_discards_old_account_response(self):
        import wechat_gui as gui
        tab = gui.SettingsTab()
        tab.db_dir.setText('D:/old')
        with patch.object(gui, 'call_api') as request:
            tab._load_accounts()
        tab.db_dir.setText('E:/new')
        request.call_args.kwargs['on_done']({'success': True, 'accounts': [{'account': 'old'}]})
        self.assertEqual(tab.account.findData('old'), -1)
        self.assertIn('目录已改变', tab.account_hint.text())
        tab.close()

    def test_exit_waits_for_requests_then_stops_backend_before_close(self):
        import wechat_gui as gui
        queued = []
        window = SimpleNamespace(_quitting=False, _quit_stop_done=False, _timer=Mock(), _polling=True,
            _action_worker=None, _api_jobs=set(), _run_api=Mock(), _backend_proc=Mock(), close=Mock())
        window._finish_exit = lambda: gui.MainWindow._finish_exit(window)
        with patch.object(gui.QTimer, 'singleShot', side_effect=lambda ms, callback: queued.append(callback)), \
             patch.object(gui.QApplication, 'instance', return_value=Mock()) as app:
            gui.MainWindow._request_exit(window)
            self.assertTrue(window._quitting)
            window._run_api.assert_not_called()
            window._polling = False
            queued.pop(0)()
            self.assertEqual(window._run_api.call_args.args, ('POST', 'stop'))
            window.close.assert_not_called()
            window._run_api.call_args.kwargs['on_done']({'success': True})
            queued.pop(0)()
            window.close.assert_called_once()
            app.return_value.quit.assert_called_once()

    def test_delayed_start_and_settings_callbacks_do_not_restart_during_exit(self):
        import wechat_gui as gui
        window = SimpleNamespace(_quitting=True, _run_task=Mock())
        with patch.object(gui, 'ensure_backend') as start, patch.object(gui, 'call_api') as request:
            gui.MainWindow._start_backend(window)
            gui.MainWindow._load_settings(window)
        start.assert_not_called()
        request.assert_not_called()
        window._run_task.assert_not_called()

    def test_failed_external_backend_stop_keeps_window_and_tray_usable(self):
        import wechat_gui as gui
        window = SimpleNamespace(_quitting=True, _quit_stop_done=False, _timer=Mock(), _polling=False,
            _action_worker=None, _api_jobs=set(), _run_api=Mock(), _backend_proc=None,
            console_tab=Mock(), showNormal=Mock(), close=Mock())
        with patch.object(gui.QApplication, 'instance') as app:
            gui.MainWindow._finish_exit(window)
            window._run_api.call_args.kwargs['on_done']({'success': False, 'message': '连接超时'})
        self.assertFalse(window._quitting)
        self.assertFalse(window._quit_stop_done)
        window.showNormal.assert_called_once()
        window._timer.start.assert_called_once_with(3000)
        window.close.assert_not_called()
        app.assert_not_called()


from tests import test_wechat_app_flow as flow


class BackgroundQueueTests(flow.RuntimeTestCase):
    async def test_strict_text_success_is_preserved_but_image_only_is_not_reported_as_sent(self):
        runtime = await self.start_runtime()
        runtime.bridge.background_send = True
        message = {'kind': 'private', 'target': 'test', 'target_name': '测试联系人'}
        meta = {'media': [{'type': 'image', 'data': {'file': 'not-opened.png'}}]}
        with patch.object(runtime.bridge, 'send_files') as files:
            ok, _ = await runtime.transport.send(message, '文字回复', meta)
            self.assertTrue(ok)
            self.assertEqual(len(runtime.bridge.sent_log), 1)
            ok, reason = await runtime.transport.send(message, '', meta)
            self.assertFalse(ok)
            self.assertIn('未发送任何内容', reason)
            self.assertEqual(len(runtime.bridge.sent_log), 1)
            files.assert_not_called()

    async def test_legacy_backend_wait_is_cancelled_without_touching_its_driver(self):
        runtime = await self.start_runtime()
        runtime.bridge.backend = 'wxauto4'
        runtime.bridge.background_send = True
        driver = runtime.bridge._wx = Mock()
        with patch('src.interactive_desktop.interactive_desktop_status', return_value=(True, '')):
            task = asyncio.create_task(runtime._wait_for_desktop('legacy-wait'))
            await asyncio.sleep(.1)
            self.assertEqual(runtime.pipeline['stage'], 'waiting_wechat_input')
            runtime.running = False
            self.assertFalse(await asyncio.wait_for(task, 1))
        driver.assert_not_called()
        self.assertEqual(driver.method_calls, [])

    async def test_wait_then_ready_generates_and_sends_only_once(self):
        from unittest.mock import AsyncMock
        from src.wechat_app import WeChatRuntime
        runtime = await self.start_runtime()
        runtime.bridge.backend = 'wechatauto'
        runtime.bridge.background_send = True
        runtime.bridge._chat_name = Mock(return_value='测试群')
        current = {'ready': False, 'reason': '严格后台等待：请手动打开目标聊天', 'minimized': True}
        runtime.bridge.inspect_send_environment = Mock(side_effect=lambda who='': dict(current))
        runtime.ai.chat = AsyncMock(return_value='一次回答。')
        sender = AsyncMock(return_value=(True, '模拟后台发送'))
        with patch.object(runtime.transport, 'send', sender), \
             patch('src.interactive_desktop.interactive_desktop_status', return_value=(True, '')):
            runtime.ingest(flow.raw_message('鲸鱼娘 介绍鲸鱼', local_id=4001))
            for _ in range(100):
                if runtime.pipeline['stage'] == 'waiting_wechat_input':
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(runtime.pipeline['stage'], 'waiting_wechat_input')
            runtime.ai.chat.assert_not_awaited()
            sender.assert_not_awaited()
            current.update(ready=True, minimized=False)
            for _ in range(350):
                if self.states() == ['sent']:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(self.states(), ['sent'])
            self.assertEqual(runtime.ai.chat.await_count, 1)
            self.assertEqual(sender.await_count, 1)

    async def test_stop_cancels_strict_background_wait(self):
        from src.wechat_app import WeChatRuntime
        runtime = await self.start_runtime()
        runtime.bridge.backend = 'wechatauto'
        runtime.bridge.background_send = True
        runtime.bridge.inspect_send_environment = Mock(return_value={'ready': False, 'reason': '后台等待'})
        with patch('src.interactive_desktop.interactive_desktop_status', return_value=(True, '')):
            task = asyncio.create_task(runtime._wait_for_desktop('waiting'))
            await asyncio.sleep(.1)
            runtime.running = False
            self.assertFalse(await asyncio.wait_for(task, 1))
