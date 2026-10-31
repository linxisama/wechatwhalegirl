"""已复现日志清空/截断与导出凭据泄漏；临时目录、假凭据、无真实客户端。"""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from src.diagnostic_redaction import REDACTED, redact_diagnostic_text
from src.diagnostics_bundle import BOM, build_bundle, write_text_with_bom
from src.logging_setup import read_new_errors


def header(second, text):
    return f'2026-10-27 12:00:{second:02d},000 [MainThread] ERROR {text}\n'


class DiagnosticCopyTests(unittest.TestCase):
    def test_sensitive_field_values_are_removed_but_nonsecret_counters_remain(self):
        names = ('api_key', 'apiKey', 'ai.api_key_encrypted', 'x-api-key', 'access_token',
                 'refresh_token', 'api_token', 'token', 'secret', 'secret_key_encrypted',
                 'password', 'robot_admin.password_hash', 'password_salt', 'client_secret',
                 'credential', 'credentials', 'private_key', 'Cookie')
        for index, name in enumerate(names):
            value = f'FAKE-CREDENTIAL-{index}-ONLY'
            for raw in (f'{name}={value} status=401', json.dumps({name:value}), f"'{name}': '{value}'"):
                with self.subTest(name=name, raw=raw):
                    result = redact_diagnostic_text(raw)
                    self.assertNotIn(value, result)
                    self.assertIn(REDACTED, result)
        normal = 'max_tokens=400 token_count=20 api_key_set=true prompt_tokens=15 phase=check_chat HTTP 401'
        self.assertEqual(redact_diagnostic_text(normal), normal)

    def test_authorization_callback_urls_and_unlabelled_known_keys_are_removed(self):
        values = ('FAKE-BEARER-ONLY-1316', 'FAKE-BASIC-ONLY-1316', 'f' * 64,
                  'FAKE-QUERY-ONLY-1316', 'FAKE-PASSWORD-ONLY-1316', 'sk-audit-ONLY-NOT-A-REAL-KEY-1316')
        raw = ('Authorization: Bearer ' + values[0] + '\nBasic ' + values[1] + '\n'
               'callback=http://127.0.0.1:8091/api/wechat/hook/callback/' + values[2] + '\n'
               'url=https://host.invalid/path?token=' + values[3] + '&status=401\n'
               'proxy=https://user:' + values[4] + '@host.invalid\n'
               'Incorrect API key provided: ' + values[5] + '\nphase=check_chat reason=chat_not_open')
        result = redact_diagnostic_text(raw)
        for value in values:
            self.assertNotIn(value, result)
        self.assertIn('status=401', result)
        self.assertIn('phase=check_chat reason=chat_not_open', result)
        self.assertIn('/api/wechat/hook/callback/' + REDACTED, result)

    def test_quote_escapes_and_missing_quote_do_not_leak_or_eat_traceback(self):
        raw = 'password="FAKE\\\"SPACE SECRET" status=403\nsecret=\'FAKE UNFINISHED\nTraceback (most recent call last):\n  File "sender.py", line 7, in send\nValueError: boom\n'
        result = redact_diagnostic_text(raw)
        self.assertNotIn('FAKE', result)
        self.assertIn('status=403', result)
        self.assertIn('Traceback (most recent call last):', result)
        self.assertIn('File "sender.py", line 7, in send', result)
        self.assertIn('ValueError: boom', result)

    def test_complete_private_key_block_is_redacted_without_hiding_next_error(self):
        raw = '-----BEGIN RSA PRIVATE KEY-----\nFAKE-PRIVATE-KEY-ONLY\n-----END RSA PRIVATE KEY-----\nValueError: boom'
        result = redact_diagnostic_text(raw)
        self.assertNotIn('FAKE-PRIVATE', result)
        self.assertIn('ValueError: boom', result)

    def test_nested_credential_values_and_whole_cookie_headers_are_not_partially_exposed(self):
        values = ('FAKE-USER-CREDENTIAL', 'FAKE-PASSWORD-CREDENTIAL', 'FAKE-COOKIE-CREDENTIAL')
        raw = (json.dumps({'credentials': {'user':values[0], 'nested':[values[1]]}}, indent=2)
               + '\nCookie: session=' + values[2] + '; another=' + values[0]
               + '\nValueError: boom')
        result = redact_diagnostic_text(raw)
        for value in values:
            self.assertNotIn(value, result)
        self.assertIn('ValueError: boom', result)
        self.assertEqual(redact_diagnostic_text(result), result)

    def test_yaml_credential_block_is_hidden_without_hiding_following_config_or_stack(self):
        raw = ('ai:\n  secret_key: |-\n    FAKE-YAML-BLOCK-ONLY\n    SECOND-FAKE-SECRET-LINE\n'
               '  max_tokens: 400\nTraceback (most recent call last):\nValueError: boom\n')
        result = redact_diagnostic_text(raw)
        self.assertNotIn('FAKE-YAML', result)
        self.assertNotIn('SECOND-FAKE', result)
        self.assertIn('max_tokens: 400', result)
        self.assertIn('\nTraceback (most recent call last):', result)

    def test_report_and_log_copies_are_sanitized_and_original_files_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA':directory}):
            root = Path(directory)
            (root/'logs').mkdir()
            credential = 'FAKE-PRIVATE-API-KEY-ONLY-1316'
            body = 'api_key="' + credential + '"\nTraceback (most recent call last):\n  File "send.py", line 7\nValueError: 中文错误\n'
            originals = {'backend.log':body.encode(), 'startup.log':BOM+body.encode(),
                         'crash.log':body.encode('utf-16'), 'driver.log':body.encode('gb18030')}
            for name, raw in originals.items():
                (root/'logs'/name).write_bytes(raw)
            result = build_bundle(root/'result.zip', root=root,
                                  notice='password=' + credential, events='token=' + credential)
            with zipfile.ZipFile(result) as archive:
                self.assertIn('logs/backend.log', archive.namelist())
                for name in archive.namelist():
                    decoded = archive.read(name).decode('utf-8-sig')
                    self.assertNotIn(credential, decoded, name)
                    if name.startswith('logs/'):
                        self.assertIn('ValueError: 中文错误', decoded)
                        self.assertIn('File "send.py", line 7', decoded)
            for name, raw in originals.items():
                self.assertEqual((root/'logs'/name).read_bytes(), raw)

    def test_txt_export_also_redacts_copy_and_keeps_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            target = write_text_with_bom(Path(directory)/'events.txt', 'token=FAKE-TXT-ONLY\nValueError: boom')
            self.assertTrue(target.read_bytes().startswith(BOM))
            text = target.read_text('utf-8-sig')
            self.assertNotIn('FAKE-TXT-ONLY', text)
            self.assertIn('ValueError: boom', text)


class RewrittenLogIdentityTests(unittest.TestCase):
    def test_truncate_starts_new_identity_while_partial_updates_keep_new_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'backend.log'
            path.write_text(header(0, 'old-error'), 'utf-8')
            first, cursor = read_new_errors(path)
            path.write_text(header(1, 'new-error-after-truncate') + 'Traceback (most recent call last):\n', 'utf-8')
            second, cursor = read_new_errors(path, cursor)
            with path.open('a', encoding='utf-8') as stream:
                stream.write('  File "send.py", line 7\nValueError: split\n')
            third, cursor = read_new_errors(path, cursor)
            self.assertNotEqual(first[0].record_id, second[-1].record_id)
            self.assertEqual(second[-1].record_id, third[0].record_id)
            self.assertIn('ValueError: split', third[0])
            self.assertEqual(read_new_errors(path, cursor)[0], [])


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    import wechat_gui
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, '需要 PyQt6')
class ClearedLogGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_clear_does_not_hide_a_new_error_after_same_file_truncate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'logs').mkdir()
            log = root/'logs/backend.log'
            log.write_text(header(0, 'old-error'), 'utf-8')
            with patch.object(wechat_gui, 'ROOT', root):
                tab = wechat_gui.ConsoleTab()
                tab._tail_timer.stop()
                try:
                    original = log.read_bytes()
                    tab._clear_log_display()
                    self.assertEqual(log.read_bytes(), original)
                    tab._poll_log_errors()
                    self.assertEqual(tab.evt_view.toPlainText(), '暂无记录')
                    log.write_text(header(1, 'new-error-after-truncate'), 'utf-8')
                    tab._poll_log_errors()
                    self.assertIn('new-error-after-truncate', tab.evt_view.toPlainText())
                    tab._poll_log_errors()
                    self.assertEqual(tab.evt_view.toPlainText().count('new-error-after-truncate'), 1)
                    self.assertNotIn('old-error', tab.evt_view.toPlainText())
                finally:
                    tab.deleteLater()
