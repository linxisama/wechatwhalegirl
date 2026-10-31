"""仅在离线临时资料内模拟 05 首次共享失败，验证归档与崩溃守卫。"""
import base64
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src import sandbox_manager as manager
from src import sandbox_acl_recovery as repair
from src.isolated_wechat.protocol import load_pairing
from src.runtime_identity import APP_VERSION
from tests import test_sandbox_recovery1319 as fixtures

SANDBOX = fixtures.SANDBOX


class AclRecoveryTests(unittest.TestCase):
    setUp = fixtures.SandboxRecoveryTests.setUp
    tearDown = fixtures.SandboxRecoveryTests.tearDown
    fake_cli = fixtures.SandboxRecoveryTests.fake_cli
    service = fixtures.SandboxRecoveryTests.service

    def denied(self, command, **kwargs):
        if command[1] == 'share':
            self.commands.append(command)
            kwargs['stderr'].write(b'0x80070005')
            return subprocess.CompletedProcess(command, 2147942405)
        return self.fake_cli(command, **kwargs)

    def old_denied_pair(self):
        old = '1.3.24'
        with patch.object(manager, 'APP_VERSION', old), \
             patch.object(manager, 'packaged_version', return_value=old), \
             patch.object(manager.subprocess, 'run', side_effect=self.denied):
            with self.assertRaises(manager.SandboxError):
                manager.prepare_sandbox(self.root, self.app, SANDBOX)
            self.channel = next((self.root / 'data/isolated_wechat').iterdir())
            self.pairing = load_pairing(self.channel)
            with self.assertRaises(manager.SandboxError):
                manager.recover_sandbox(self.root, self.app, SANDBOX)
        self.original = json.loads((self.channel / 'session.json').read_text())
        self.snapshot = repair._snapshot(self.channel)
        # 当前构建必须是未尝试的新路径；不能重试旧 uncertain 程序 share。
        (self.app / '_internal/dependency.bin').write_bytes(b'current 1325 private ACL build')
        self.start_index = len(self.commands)

    def cli(self, command, **kwargs):
        if command[1] == 'exec':
            text = command[command.index('--command') + 1]
            if ' -EncodedCommand ' in text:
                code = base64.b64decode(text.split(' -EncodedCommand ', 1)[1]).decode('utf-16le')
                if 'WHALE_ACL_REPAIR_PREFLIGHT' in code:
                    self.assertNotIn(self.pairing['secret'], code)
                    self.assertNotIn('Stop-Process', code)
                    self.assertNotIn('SendInput', code)
                    return self.fake_cli(command, **kwargs)
                if 'WHALE_SESSION_PROBE' in code:
                    return self.fake_cli(command, **kwargs)
        return fixtures.SandboxRecoveryTests.recovery_cli(self, command, **kwargs)

    def recover(self):
        with patch.object(manager.subprocess, 'run', side_effect=self.cli):
            return manager.recover_sandbox(self.root, self.app, SANDBOX)

    def journal(self):
        path = repair._journal_path(self.root, self.channel.name)
        return path, json.loads(path.read_text())

    def test_exact_pre_channel_denial_repairs_same_identity_and_archives_original_bytes(self):
        self.old_denied_pair()
        self.assertEqual(manager.paired_status(self.root)['pairs'][0]['recovery_state'], 'repairable')
        result = self.recover()
        self.assertTrue(result['success'])
        self.assertEqual(result['channel_id'], self.channel.name)
        self.assertEqual(dict(load_pairing(self.channel)), dict(self.pairing))
        journal_path, journal = self.journal()
        self.assertEqual(journal['stage'], 'ready')
        self.assertEqual(journal['rpc_requests'], 0)
        self.assertNotIn(self.pairing['secret'], journal_path.read_text())
        archive = self.channel.parent / journal['archive_name']
        self.assertEqual(repair._snapshot(archive), self.snapshot)
        for name in ('pairing.json', 'recovery.json'):
            self.assertEqual((self.channel / name).read_bytes(), self.snapshot[name])
        session = json.loads((self.channel / 'session.json').read_text())
        self.assertEqual(session['stage'], 'prepared')
        self.assertEqual(session['app_version'], APP_VERSION)
        self.assertEqual(session['channel_mapping'], 'by_channel')
        shares = [c for c in self.commands[self.start_index:] if c[1] == 'share']
        self.assertEqual(len(shares), 2)
        self.assertEqual(shares[0][shares[0].index('--sandbox-path') + 1], manager._build_guest_app(session['program_key']))
        self.assertNotIn('--allow-write', shares[0])
        self.assertEqual(shares[1][shares[1].index('--sandbox-path') + 1], manager._build_guest_channel(self.channel.name))
        self.assertIn('--allow-write', shares[1])
        self.assertEqual(sum('--sandbox-agent-launch' in ' '.join(c) for c in self.commands[self.start_index:]), 1)
        self.assertEqual(manager.channel_path(self.root, self.channel.name), self.channel)
        self.assertEqual(manager.paired_status(self.root)['pairs'][0]['recovery_state'], 'ready')
        self.assertEqual((self.root / 'config.wechat.yaml').read_text(), 'api_key: keep-private\n')

    def test_unknown_mapping_and_any_prior_launch_are_never_repaired(self):
        self.old_denied_pair()
        path = self.channel / 'recovery.json'
        raw = path.read_bytes()
        original = json.loads(raw)
        for extra in ({'channel_mapping': 'uncertain'}, {'launch_started': 1},
                      {'failed_operation': 'share_channel'}, {'failure': {'operation': 'share',
                       'returncode': 2147942405, 'hresult': '0x80070006'}}):
            with self.subTest(extra=extra):
                path.write_text(json.dumps(dict(original, **extra)))
                before = len(self.commands)
                with self.assertRaises(manager.SandboxError):
                    self.recover()
                self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))
                self.assertFalse(repair._journal_path(self.root, self.channel.name).parent.exists())
        path.write_bytes(raw)

    def test_any_business_unknown_file_or_resume_journal_prevents_archive(self):
        self.old_denied_pair()
        for name in ('requests/old.json', 'service.json', 'extra-secret.txt', 'resume-' + 'f' * 32 + '.json', 'control/stop.json'):
            with self.subTest(name=name):
                file = self.channel / name
                file.parent.mkdir(exist_ok=True)
                file.write_text('{}')
                before = len(self.commands)
                with self.assertRaises(manager.SandboxError):
                    self.recover()
                self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))
                file.unlink()

    def test_new_key_cannot_reuse_old_uncertain_program_mapping(self):
        self.old_denied_pair()
        old_key = self.original['program_key']
        with patch.object(manager, '_prepare_program', return_value=(self.app, old_key)):
            with self.assertRaisesRegex(manager.SandboxError, '从未映射的新构建'):
                self.recover()
        self.assertFalse(repair._journal_path(self.root, self.channel.name).parent.exists())

    def test_guest_presence_or_failed_process_query_prevents_any_relocation(self):
        self.old_denied_pair()
        original = self.cli
        def bad_probe(command, **kwargs):
            if command[1] == 'exec':
                text = command[command.index('--command') + 1]
                if ' -EncodedCommand ' in text:
                    code = base64.b64decode(text.split(' -EncodedCommand ', 1)[1]).decode('utf-16le')
                    if 'WHALE_ACL_REPAIR_PREFLIGHT' in code:
                        self.commands.append(command)
                        kwargs['stdout'].write(b'{"ExitCode":5}')
                        return subprocess.CompletedProcess(command, 0)
            return original(command, **kwargs)
        with patch.object(manager.subprocess, 'run', side_effect=bad_probe):
            with self.assertRaisesRegex(manager.SandboxError, '只读证据不足'):
                manager.recover_sandbox(self.root, self.app, SANDBOX)
        self.assertEqual(repair._snapshot(self.channel), self.snapshot)
        self.assertFalse(repair._journal_path(self.root, self.channel.name).parent.exists())

    def test_archive_gap_crash_guard_blocks_prepare_recover_activation_and_status_stays_visible(self):
        self.old_denied_pair()
        real_rename = Path.rename
        def fail_install(path, target):
            if path.name.startswith('.acl-stage-'):
                raise OSError('simulated crash after archive')
            return real_rename(path, target)
        with patch.object(Path, 'rename', autospec=True, side_effect=fail_install):
            with self.assertRaises(OSError):
                self.recover()
        self.assertFalse(self.channel.exists())
        journal_path, journal = self.journal()
        archive = self.channel.parent / journal['archive_name']
        self.assertEqual(repair._snapshot(archive), self.snapshot)
        self.assertEqual(journal['failed_operation'], 'install_channel')
        before = len(self.commands)
        for action in (lambda: manager.prepare_sandbox(self.root, self.app, SANDBOX),
                       lambda: manager.recover_sandbox(self.root, self.app, SANDBOX),
                       lambda: manager.channel_path(self.root, self.channel.name)):
            with self.assertRaisesRegex(manager.SandboxError, '目录修复尚未确认'):
                action()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))
        pairs = manager.paired_status(self.root)['pairs']
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['channel_id'], self.channel.name)
        self.assertEqual(pairs[0]['recovery_state'], 'blocked')
        self.assertNotIn(self.pairing['secret'], json.dumps(pairs))

    def test_empty_external_guard_directory_is_already_fail_closed(self):
        self.old_denied_pair()
        path = repair._journal_path(self.root, self.channel.name)
        path.parent.mkdir(parents=True)
        before = len(self.commands)
        for action in (lambda: manager.prepare_sandbox(self.root, self.app, SANDBOX),
                       lambda: manager.recover_sandbox(self.root, self.app, SANDBOX)):
            with self.assertRaises(manager.SandboxError):
                action()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))

    def test_channel_share_timeout_never_reposts_and_preserves_original_archive(self):
        self.old_denied_pair()
        original = self.cli
        def timeout(command, **kwargs):
            if command[1] == 'share' and '--allow-write' in command:
                self.commands.append(command)
                raise subprocess.TimeoutExpired(command, 20)
            return original(command, **kwargs)
        with patch.object(manager.subprocess, 'run', side_effect=timeout):
            with self.assertRaises(manager.SandboxError):
                manager.recover_sandbox(self.root, self.app, SANDBOX)
        _, journal = self.journal()
        self.assertEqual(journal['channel_mapping'], 'uncertain')
        self.assertEqual(repair._snapshot(self.channel.parent / journal['archive_name']), self.snapshot)
        before = len(self.commands)
        with self.assertRaises(manager.SandboxError):
            self.recover()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))

    def test_failed_session_commit_cannot_claim_ready_or_relaunch(self):
        self.old_denied_pair()
        write = manager._write_session
        def fail_commit(path, value):
            if Path(path) == self.channel / 'session.json' and value.get('stage') == 'prepared':
                raise OSError('simulated disk write failure')
            return write(path, value)
        with patch.object(manager, '_write_session', side_effect=fail_commit):
            with self.assertRaises(OSError):
                self.recover()
        self.assertEqual(json.loads((self.channel / 'session.json').read_text()), self.original)
        self.assertEqual(self.journal()[1]['stage'], 'failed')
        with self.assertRaises(manager.SandboxError):
            manager.channel_path(self.root, self.channel.name)
        self.assertEqual(manager.paired_status(self.root)['pairs'][0]['recovery_state'], 'blocked')

    def test_ready_guard_with_missing_canonical_blocks_creation_of_second_identity(self):
        self.old_denied_pair()
        self.recover()
        self.channel.rename(self.channel.parent / '.manually-moved')
        before = len(self.commands)
        with self.assertRaises(manager.SandboxError):
            manager.prepare_sandbox(self.root, self.app, SANDBOX)
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))

    def test_canonical_installed_but_program_share_failure_never_retries(self):
        self.old_denied_pair()
        original = self.cli
        def denied_program(command, **kwargs):
            if command[1] == 'share' and '--allow-write' not in command:
                return self.denied(command, **kwargs)
            return original(command, **kwargs)
        with patch.object(manager.subprocess, 'run', side_effect=denied_program):
            with self.assertRaises(manager.SandboxError):
                manager.recover_sandbox(self.root, self.app, SANDBOX)
        self.assertTrue(self.channel.is_dir())
        self.assertEqual(self.journal()[1]['failed_operation'], 'share_program')
        before = len(self.commands)
        with self.assertRaises(manager.SandboxError):
            self.recover()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))
        status = manager.paired_status(self.root)
        self.assertFalse(status['available'])
        self.assertEqual(status['pairs'][0]['initial_hresult'], '0x80070005')
        self.assertEqual(status['pairs'][0]['recovery_hresult'], '0x80070005')
        self.assertEqual(status['pairs'][0]['recovery_returncode'], 2147942405)

    def test_session_commit_before_failed_ready_guard_is_still_blocked(self):
        self.old_denied_pair()
        write = manager._write_session
        def fail_final_guard(path, value):
            if Path(path).name == 'journal.json' and value.get('stage') == 'ready':
                raise OSError('simulated crash before final guard publish')
            return write(path, value)
        with patch.object(manager, '_write_session', side_effect=fail_final_guard):
            with self.assertRaises(OSError):
                self.recover()
        self.assertEqual(json.loads((self.channel / 'session.json').read_text())['stage'], 'prepared')
        self.assertEqual(self.journal()[1]['stage'], 'failed')
        before = len(self.commands)
        with self.assertRaises(manager.SandboxError):
            manager.channel_path(self.root, self.channel.name)
        with self.assertRaises(manager.SandboxError):
            self.recover()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))
        status = manager.paired_status(self.root)
        self.assertFalse(status['available'])
        self.assertEqual(status['pairs'][0]['recovery_state'], 'blocked')

    def test_corrupt_guard_types_are_finite_blocked_instead_of_status_exception(self):
        self.old_denied_pair()
        self.recover()
        file, journal = self.journal()
        for extra in ({'stage': []}, {'stage': {}}, {'current_operation': []},
                      {'original_sha256': []}, {'rpc_requests': False}):
            with self.subTest(extra=extra):
                file.write_text(json.dumps(dict(journal, **extra)))
                status = manager.paired_status(self.root)
                self.assertFalse(status['available'])
                self.assertEqual(status['message'], repair.BLOCKED_REASON)
                with self.assertRaises(manager.SandboxError):
                    manager.channel_path(self.root, self.channel.name)
        file.write_text(json.dumps(journal))

    def test_corrupt_guard_and_missing_canonical_never_look_like_new_pair(self):
        self.old_denied_pair()
        self.recover()
        file, _ = self.journal()
        file.write_text('{broken')
        self.channel.rename(self.channel.parent / '.manually-moved')
        status = manager.paired_status(self.root)
        self.assertFalse(status['available'])
        self.assertEqual(status['message'], repair.BLOCKED_REASON)
        before = len(self.commands)
        with self.assertRaises(manager.SandboxError):
            manager.prepare_sandbox(self.root, self.app, SANDBOX)
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))

    def test_business_arriving_during_staging_prevents_archive(self):
        self.old_denied_pair()
        create = repair.create_private_directory
        def business_arrives(path):
            result = create(path)
            if Path(path).name.startswith('.acl-stage-'):
                (self.channel / 'requests/arrived.json').write_text('{}')
            return result
        with patch.object(repair, 'create_private_directory', side_effect=business_arrives):
            with self.assertRaises(manager.SandboxError):
                self.recover()
        self.assertTrue(self.channel.is_dir())
        self.assertFalse(list(self.channel.parent.glob('.acl-archive-*')))
        self.assertTrue((self.channel / 'requests/arrived.json').exists())
        self.assertFalse(any(c[1] == 'share' for c in self.commands[self.start_index:]))


if __name__ == '__main__':
    unittest.main()
