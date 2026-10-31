"""真实目录结构、模拟数据库/控件与隔离子进程验证；不连接微信。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.wechat_bridge import WeChatBridge
from src.wechat_paths import account_root, clean_path, resolve_database_dir, _config_paths
from src.wechat_sender import IsolatedWeChatSender
from src.wechat_send_worker import send_text, send_picture, execute
from src.wechat_transports import message_from_raw


class DataPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / '中文 空格' / 'xwechat_files'
        self.storage = self.root / 'custom_account' / 'db_storage'
        self.storage.mkdir(parents=True)

    def test_root_parent_account_and_storage_normalize_identically(self):
        for value in (self.root, self.root.parent, self.storage.parent, self.storage):
            with self.subTest(path=value):
                self.assertEqual(account_root(str(value)), str(self.root))

    def test_explicit_invalid_path_does_not_switch_to_detected_account(self):
        detector = Mock(return_value=str(self.root))
        with self.assertRaisesRegex(ValueError, '指定目录'):
            resolve_database_dir(str(self.root / 'missing'), detector)
        detector.assert_not_called()

    def test_environment_expansion_preserves_spaces(self):
        with patch.dict(os.environ, {'WECHAT_TEST_PATH': str(self.root)}):
            self.assertEqual(account_root('"%WECHAT_TEST_PATH%"'), str(self.root))

    def test_drive_root_is_not_converted_to_drive_relative(self):
        if os.name != 'nt':
            self.skipTest('Windows 路径')
        self.assertEqual(str(clean_path('D:')), 'D:\\')
        with self.assertRaisesRegex(ValueError, '绝对路径'):
            clean_path('D:relative')

    def test_configuration_path_parser_keeps_chinese_spaces(self):
        value = str(self.root)
        for text in (value, 'FileSavePath=' + value, json.dumps({'storage': {'path': value}})):
            self.assertIn(value, list(_config_paths(text)))

    def test_legacy_utf16_wechat_configuration_finds_custom_drive_location(self):
        settings = Path(self.tmp.name) / 'profile' / 'Tencent/WeChat/All Users/config'
        settings.mkdir(parents=True)
        (settings / '3ebffe94.ini').write_text(str(self.root.parent), encoding='utf-16')
        with patch.dict(os.environ, {'APPDATA': str(settings.parents[3]), 'LOCALAPPDATA': '',
                                     'USERPROFILE': self.tmp.name, 'WECHATAUTO_DB_DIR': ''}), \
                patch('src.wechat_paths.os.name', 'nt'):
            self.assertEqual(resolve_database_dir(detector=lambda: None), str(self.root))

    def test_cached_environment_never_overrides_explicit_directory(self):
        with patch.dict(os.environ, {'WECHATAUTO_DB_DIR': str(self.root / 'missing')}):
            self.assertEqual(resolve_database_dir(str(self.root)), str(self.root))

    def test_custom_drive_path_works_from_a_different_working_directory(self):
        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=workspace / '.tmp', prefix='data-path-') as folder:
            root = Path(folder) / '微信 数据' / 'xwechat_files'
            (root / 'account' / 'db_storage').mkdir(parents=True)
            command = [sys.executable, '-c',
                       'import sys,json;sys.path.insert(0,sys.argv[1]);'
                       'from src.wechat_paths import account_root;'
                       'print(json.dumps(account_root(sys.argv[2])))', str(workspace), str(root)]
            result = subprocess.run(command, cwd=self.tmp.name, capture_output=True, timeout=5, check=True)
            self.assertEqual(json.loads(result.stdout), str(root))


class TargetResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = WeChatBridge(Path(self.tmp.name), backend='wechatauto')
        self.bridge.backend, self.bridge.online = 'wechatauto', True
        self.bridge._wx = Mock()
        self.bridge._wx.SendMsg.return_value = True
        self.names = {'123@chatroom': '群聊备注', 'wxid_friend': '好友备注'}
        self.bridge._db = SimpleNamespace(nickname_map=lambda refresh=False: dict(self.names),
                                          group_id_to_name=lambda _: None,
                                          get_nickname=lambda value: value)

    def test_current_group_remark_replaces_raw_id_and_stale_name(self):
        for old_name in ('', '123@chatroom', '过期群名', '最后发言者'):
            self.assertTrue(self.bridge.send_text('123@chatroom', '你好', old_name)[0])
            self.assertEqual(self.bridge._wx.SendMsg.call_args.kwargs['who'], '群聊备注')
        self.names['123@chatroom'] = '更新备注'
        self.bridge.send_text('123@chatroom', '你好', '群聊备注')
        self.assertEqual(self.bridge._wx.SendMsg.call_args.kwargs['who'], '更新备注')

    def test_unmapped_stable_id_never_starts_automation(self):
        ok, detail = self.bridge.send_text('missing@chatroom', '你好')
        self.assertFalse(ok)
        self.assertIn('未解析', detail)
        self.bridge._wx.SendMsg.assert_not_called()

    def test_duplicate_names_fail_without_choosing_first_contact(self):
        self.names['other@chatroom'] = '群聊备注'
        ok, detail = self.bridge.send_text('123@chatroom', '你好')
        self.assertFalse(ok)
        self.assertIn('显示名重复', detail)
        self.bridge._wx.SendMsg.assert_not_called()

    def test_account_change_does_not_reuse_previous_mapping(self):
        self.assertEqual(self.bridge.display_name('123@chatroom'), '群聊备注')
        self.bridge._db = SimpleNamespace(nickname_map=lambda refresh=False: {},
                                         group_id_to_name=lambda _: None)
        self.assertFalse(self.bridge.send_text('123@chatroom', '你好')[0])
        self.bridge._wx.SendMsg.assert_not_called()

    def test_unknown_group_does_not_trust_an_unverified_sender_display_name(self):
        ok, detail = self.bridge.send_text('unknown@chatroom', '你好', '最后发言者')
        self.assertFalse(ok)
        self.assertIn('未解析', detail)
        self.bridge._wx.SendMsg.assert_not_called()

    def test_last_group_speaker_is_never_used_as_conversation_name(self):
        self.bridge._db.get_sessions = lambda limit: [{'username': 'unknown@chatroom', 'last_sender': '小明'}]
        self.assertEqual(self.bridge.list_sessions()[0]['name'], 'unknown@chatroom')
        msg = message_from_raw({'username': 'unknown@chatroom', 'sender_username': 'wxid_friend',
                                'type': '文本', 'content': '你好'}, self.bridge)
        self.assertEqual(msg['target_name'], 'unknown@chatroom')


class SenderDiagnosticsTests(unittest.TestCase):
    def test_internal_identifiers_are_rejected_before_window_or_wechat_import(self):
        for name in ('123@chatroom', 'wxid_person', 'wechat|bot|group:123', 'private:123'):
            driver = Mock()
            self.assertFalse(send_text(driver, name, '你好')[0])
            self.assertFalse(send_picture(driver, name)[0])
            self.assertFalse(execute({'action': 'text', 'who': name, 'text': '你好'})[0])
            driver.ensure_window.assert_not_called()

    def test_timeout_reports_last_phase_and_cleans_payloads_without_resending(self):
        with tempfile.TemporaryDirectory() as folder:
            process = Mock()
            process.wait.side_effect = [subprocess.TimeoutExpired('worker', 24), 0]
            process.poll.return_value = 0
            def start(command, **kwargs):
                request = Path(command[-1])
                request.with_suffix('.status.json').write_text(
                    json.dumps({'stage': 'conversation', 'send_key': False}), encoding='utf-8')
                return process
            with patch('src.wechat_sender.subprocess.Popen', side_effect=start) as popen:
                result = IsolatedWeChatSender(Path(folder)).SendMsg('秘密正文', '测试群')
            self.assertFalse(result)
            self.assertIn('核对会话', str(result))
            self.assertIn('未执行', str(result))
            self.assertNotIn('秘密正文', str(result))
            self.assertEqual(popen.call_count, 1)
            process.kill.assert_called_once()
            self.assertEqual(list((Path(folder) / 'send_jobs').iterdir()), [])

    def test_real_child_reports_invalid_target_without_touching_wechat(self):
        with tempfile.TemporaryDirectory() as folder:
            result = IsolatedWeChatSender(Path(folder)).SendMsg('秘密正文', 'invalid@chatroom')
            self.assertFalse(result)
            self.assertIn('内部会话标识', str(result))
            self.assertNotIn('秘密正文', str(result))
            self.assertEqual(list((Path(folder) / 'send_jobs').iterdir()), [])


class DependencyDiagnosticsTests(unittest.TestCase):
    def test_source_and_installer_errors_offer_different_repairs(self):
        from src.native_dependencies import dependency_error_hint
        failure = ImportError('DLL load failed while importing win32ui')
        with patch('sys.frozen', True, create=True):
            self.assertIn('覆盖安装', dependency_error_hint(failure))
        with patch('sys.frozen', False, create=True):
            self.assertIn('同一 Python', dependency_error_hint(failure))
        self.assertEqual(dependency_error_hint(ValueError('密码不正确')), '')
