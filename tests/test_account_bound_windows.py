"""双账号句柄绑定、只读发送体检和连接分类回归，不接触真实微信。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.wechat_bridge import WeChatBridge
from src.wechat_sender import IsolatedWeChatSender, SendResult
from src.wechat_window import select_window, inspect_window, prepare_window, NativeWindowAccess
from tests.test_minimized_desktop import DesktopQueueTests


class AccountWindowTests(unittest.TestCase):
    def access(self):
        access = Mock()
        access.candidates.return_value = [
            {'hwnd': 100, 'pid': 1, 'minimized': False},
            {'hwnd': 200, 'pid': 2, 'minimized': False}]
        access.matches_account.side_effect = lambda pid, path: pid == 2
        field = SimpleNamespace(ClassName='mmui::ChatInputField', AutomationId='', GetChildren=lambda: [])
        access.control.return_value = SimpleNamespace(ClassName='Qt51514QWindowIcon', AutomationId='', GetChildren=lambda: [field])
        return access

    def test_two_accounts_selects_bound_pid_instead_of_first_window(self):
        access = self.access()
        row, diagnostic = select_window(access, 'C:/wx/bot')
        self.assertEqual(row['hwnd'], 200)
        self.assertEqual(diagnostic['candidate_count'], 2)
        self.assertEqual(diagnostic['matched_count'], 1)
        access.restore.assert_not_called()

    def test_two_matching_windows_remain_ambiguous(self):
        access = self.access()
        access.matches_account.return_value = True
        access.matches_account.side_effect = None
        row, diagnostic = select_window(access, 'C:/wx/bot')
        self.assertIsNone(row)
        self.assertIn('多个', diagnostic['reason'])

    def test_single_window_without_account_match_is_rejected(self):
        """不能用同名会话替代账号核验，未匹配的单窗口也必须拒绝。"""
        access = self.access()
        access.candidates.return_value = access.candidates.return_value[:1]
        row, diagnostic = select_window(access, 'C:/wx/bot')
        self.assertIsNone(row)
        self.assertEqual(diagnostic['waiting_code'], 'account_unverified')

    def test_multiple_windows_without_match_still_refuses(self):
        access = self.access()
        access.candidates.return_value = [{'hwnd': 100, 'pid': 1, 'minimized': False},
                                          {'hwnd': 200, 'pid': 2, 'minimized': False}]
        # fixture 用的是 side_effect，return_value 覆盖不了它
        access.matches_account.side_effect = lambda pid, path: False
        row, diagnostic = select_window(access, 'C:/wx/bot')
        self.assertIsNone(row)
        self.assertIn('无法把已选账号与微信窗口对应', diagnostic['reason'])

    def test_denied_file_query_does_not_choose_a_window_by_order(self):
        access = self.access()
        access.matches_account.side_effect = PermissionError('denied')
        row, diagnostic = select_window(access, 'C:/wx/bot')
        self.assertIsNone(row)
        self.assertEqual(diagnostic['account_checks_failed'], 2)

    def test_account_path_requires_actual_db_storage_not_name_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            account = Path(folder) / 'bot'
            adapter = object.__new__(NativeWindowAccess)
            adapter.psutil = Mock()
            files = adapter.psutil.Process.return_value.open_files
            files.return_value = [SimpleNamespace(path=str(Path(folder) / 'bot_old/db_storage/message.db'))]
            self.assertFalse(adapter.matches_account(1, str(account)))
            files.return_value = [SimpleNamespace(path=str(account / 'db_storage/message/message_0.db'))]
            self.assertTrue(adapter.matches_account(1, str(account)))

    def test_inspection_finds_input_without_restore_activation_or_focus(self):
        access = self.access()
        info = inspect_window('C:/wx/bot', access, lambda: (True, ''))
        self.assertTrue(info['ready'])
        self.assertEqual(info['pid'], 2)
        access.control.assert_called_once_with(200)
        access.restore.assert_not_called()
        access._activate.assert_not_called()

    def test_inspection_reports_empty_tree_as_unavailable(self):
        access = self.access()
        access.control.return_value.GetChildren = lambda: []
        info = inspect_window('C:/wx/bot', access, lambda: (True, ''))
        self.assertFalse(info['ready'])
        self.assertEqual(info['matched_count'], 1)
        self.assertFalse(info['input_available'])

    def test_prepare_activates_only_bound_account_window(self):
        access = self.access()
        uia = Mock()
        self.assertTrue(prepare_window(uia, access, lambda: (True, ''), 'C:/wx/bot'))
        access.control.assert_called_once_with(200)
        uia._activate.assert_called_once_with(access.control.return_value)


class SenderAccountTests(unittest.TestCase):
    def test_account_binding_and_diagnostic_survive_child_protocol(self):
        with tempfile.TemporaryDirectory() as folder:
            seen = []
            process = Mock()
            process.wait.return_value = 0
            process.poll.return_value = 0
            def start(command, **kwargs):
                request = Path(command[-1])
                seen.append(json.loads(request.read_text(encoding='utf-8')))
                request.with_suffix('.result.json').write_text(json.dumps({
                    'ok': False, 'message': '无输入控件', 'requires_reconnect': False,
                    'diagnostic': {'ready': False, 'matched_count': 1, 'pid': 2}}), encoding='utf-8')
                return process
            with patch('src.wechat_sender.subprocess.Popen', side_effect=start):
                sender = IsolatedWeChatSender(Path(folder), 'C:/wx/bot')
                result = sender.SendMsg('你好', '测试群')
                sender.stop()
                probe = sender.inspect_environment()
            self.assertFalse(result.requires_reconnect)
            self.assertEqual(probe.diagnostic['pid'], 2)
            self.assertEqual([v['account_path'] for v in seen], ['C:/wx/bot'] * 2)
            self.assertEqual(seen[1]['action'], 'window-check')
            self.assertEqual(list((Path(folder) / 'send_jobs').iterdir()), [])

    def test_local_control_failures_do_not_trigger_database_reconnect(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = WeChatBridge(Path(folder), backend='wechatauto')
            bridge.backend, bridge.online = 'wechatauto', True
            bridge._wx = Mock()
            bridge._wx.SendMsg.return_value = SendResult(False, '没有控件', False, {'ready': False})
            for _ in range(4):
                self.assertFalse(bridge.send_text('测试群', '你好')[0])
            self.assertEqual(bridge.send_fail, 4)
            self.assertEqual(bridge.consecutive_fail, 0)
            self.assertTrue(bridge.online)
            self.assertFalse(bridge.status()['send_environment']['ready'])


class BoundQueueTests(DesktopQueueTests):
    async def test_minimized_window_still_reaches_existing_send_restore_path(self):
        runtime = await self.begin(False)
        runtime.bridge.send_environment = {'ready': False, 'minimized': True, 'reason': '最小化'}
        from tests.test_wechat_app_flow import raw_message
        runtime.ingest(raw_message('鲸鱼娘 介绍鲸鱼迁徙', local_id=2502))
        await self.until(lambda: self.states() == ['sent'])
        self.assertEqual(self.sender.await_count, 1)

    async def test_unavailable_input_waits_before_ai_and_recovers_once(self):
        runtime = await self.begin(False)
        runtime.bridge.send_environment = {'ready': False, 'reason': '没有控件'}
        with patch.object(runtime.bridge, 'inspect_send_environment', return_value=runtime.bridge.send_environment):
            from tests.test_wechat_app_flow import raw_message
            runtime.ingest(raw_message('鲸鱼娘 解释鲸鱼迁徙', local_id=2501))
            await self.until(lambda: runtime.pipeline['stage'] == 'waiting_wechat_input')
            self.ai.assert_not_awaited()
            self.sender.assert_not_awaited()
            runtime.bridge.send_environment = {'ready': True}
            await self.until(lambda: self.states() == ['sent'])
        self.assertEqual(self.ai.await_count, 1)
        self.assertEqual(self.sender.await_count, 1)

    async def test_test_wechat_reports_database_online_but_send_unavailable(self):
        runtime = await self.start_runtime()
        from src.wechat_app import create_app
        app = create_app(runtime)
        app.state.bot = runtime
        route = next(r.endpoint for r in app.routes if r.path == '/api/test-wechat')
        with patch.object(runtime.bridge, 'inspect_send_environment', return_value={'ready': False, 'reason': '没有控件'}):
            response = await route()
        self.assertTrue(response['connection_ok'])
        self.assertFalse(response['success'])
        self.assertFalse(response['send_ready'])
