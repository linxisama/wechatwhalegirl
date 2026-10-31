"""失败恢复的有证据续接；CLI、进程与微信全为假对象。"""
import base64
import json
import subprocess
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from src import sandbox_manager as manager
from tests import test_sandbox_recovery1319 as fixtures

SANDBOX = fixtures.SANDBOX


class RecoveryResumeTests(unittest.TestCase):
    setUp = fixtures.SandboxRecoveryTests.setUp
    tearDown = fixtures.SandboxRecoveryTests.tearDown
    fake_cli = fixtures.SandboxRecoveryTests.fake_cli
    prepare = fixtures.SandboxRecoveryTests.prepare
    service = fixtures.SandboxRecoveryTests.service
    failed_pair = fixtures.SandboxRecoveryTests.failed_pair
    recover = fixtures.SandboxRecoveryTests.recover

    def recovery_cli(self, command, **kwargs):
        if command[1] == 'exec':
            text = command[command.index('--command') + 1]
            if ' -EncodedCommand ' in text:
                code = base64.b64decode(text.split(' -EncodedCommand ', 1)[1]).decode('utf-16le')
                if 'WHALE_SESSION_PROBE' in code:
                    return self.fake_cli(command, **kwargs)
        return fixtures.SandboxRecoveryTests.recovery_cli(self, command, **kwargs)

    def _mapped_failure(self):
        self.failed_pair()
        self.proof_extra = {'agent_process_count': 1}
        with self.assertRaises(manager.SandboxError):
            self.recover()
        self.proof_extra = {}
        return (self.channel / 'recovery.json').read_bytes()

    def test_confirmed_mappings_resume_without_sharing_channel_or_changing_identity(self):
        original = self._mapped_failure()
        start = len(self.commands)
        result = self.recover()
        self.assertTrue(result['success'])
        self.assertEqual((self.channel / 'recovery.json').read_bytes(), original)
        self.assertEqual((self.channel / 'pairing.json').read_bytes(), self.original_pairing)
        self.assertFalse(any(c[1] == 'share' for c in self.commands[start:]))
        self.assertEqual(sum('--sandbox-agent-launch' in ' '.join(c) for c in self.commands[start:]), 1)
        self.assertEqual(json.loads((self.channel / 'session.json').read_text())['stage'], 'prepared')
        journals = list(self.channel.glob('resume-*.json'))
        self.assertEqual(len(journals), 1)
        self.assertEqual(json.loads(journals[0].read_text())['stage'], 'ready')

    def test_process_probe_failure_can_be_rechecked_without_new_shares(self):
        original = self._mapped_failure()
        self.proof_extra = {'agent_process_count': 1}
        with self.assertRaises(manager.SandboxError):
            self.recover()
        self.proof_extra = {}
        start = len(self.commands)
        self.assertTrue(self.recover()['success'])
        self.assertEqual((self.channel / 'recovery.json').read_bytes(), original)
        self.assertEqual(len(list(self.channel.glob('resume-*.json'))), 2)
        self.assertFalse(any(c[1] == 'share' for c in self.commands[start:]))

    def test_existing_recovery_with_uncertain_mapping_is_not_resumed(self):
        self._mapped_failure()
        file = self.channel / 'recovery.json'
        record = json.loads(file.read_text())
        record.update(channel_mapping='uncertain', failed_operation='share_channel')
        file.write_text(json.dumps(record))
        start = len(self.commands)
        with self.assertRaises(manager.SandboxError):
            self.recover()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[start:]))

    def test_launched_or_heartbeat_unknown_journal_is_not_resumed(self):
        for operation in ('launch', 'verify_heartbeat', 'commit_session'):
            with self.subTest(operation=operation):
                self._mapped_failure()
                record = json.loads((self.channel / 'recovery.json').read_text())
                record.update(stage='failed', failed_operation=operation)
                (self.channel / 'recovery.json').write_text(json.dumps(record))
                with self.assertRaises(manager.SandboxError):
                    self.recover()
                self.root = self.root.parent / __import__('uuid').uuid4().hex
                self.root.mkdir()
                (self.root / 'config.wechat.yaml').write_text('api_key: keep-private\n')

    def test_recovery_diagnostic_exports_finite_failure_codes(self):
        self._mapped_failure()
        record_path = self.channel / 'recovery.json'
        record = json.loads(record_path.read_text())
        record['failure'] = {'operation': 'exec', 'hresult': '0x80070520', 'returncode': 2147943712,
                             'secret': 'never-export', 'raw': 'never-export'}
        record_path.write_text(json.dumps(record))
        result = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(result['recovery_state'], 'resumable')
        self.assertEqual(result['recovery_failed_operation'], 'process_probe')
        self.assertEqual(result['recovery_hresult'], '0x80070520')
        self.assertNotIn('never-export', json.dumps(result))

    def test_changed_original_session_cannot_resume(self):
        self._mapped_failure()
        session = json.loads((self.channel / 'session.json').read_text())
        session['created'] += 1
        (self.channel / 'session.json').write_text(json.dumps(session))
        with self.assertRaises(manager.SandboxError):
            self.recover()

    def test_changed_full_program_fingerprint_cannot_resume(self):
        self._mapped_failure()
        file = self.channel / 'program-mappings.json'
        value = json.loads(file.read_text())
        record = next(iter(value['mappings'].values()))
        record['fingerprint'] = record['fingerprint'][:20] + 'f' * 44
        file.write_text(json.dumps(value))
        with self.assertRaises(manager.SandboxError):
            self.recover()

    def test_no_login_session_precheck_does_not_reserve_pair_or_share(self):
        def no_session(command, **kwargs):
            if command[1] == 'exec':
                self.commands.append(command)
                kwargs['stderr'].write(b'0x80070520')
                return subprocess.CompletedProcess(command, 2147943712)
            return self.fake_cli(command, **kwargs)
        with patch.object(manager.subprocess, 'run', side_effect=no_session):
            with self.assertRaisesRegex(manager.SandboxError, '沙盒桌面'):
                self.prepare()
        folder = self.root / 'data/isolated_wechat'
        self.assertFalse(folder.exists() and any(folder.iterdir()))
        self.assertFalse(any(c[1] == 'share' for c in self.commands))

    def test_resume_precheck_failure_preserves_record_and_can_be_retried(self):
        old = self._mapped_failure()
        with patch.object(manager, '_session_probe', side_effect=manager.SandboxError('先打开沙盒桌面')):
            with self.assertRaisesRegex(manager.SandboxError, '沙盒桌面'):
                self.recover()
        self.assertEqual((self.channel / 'recovery.json').read_bytes(), old)
        self.assertTrue(self.recover()['success'])

    def test_new_build_only_adds_readonly_program_mapping_and_preserves_old_record(self):
        old = self._mapped_failure()
        (self.app / '_internal/dependency.bin').write_bytes(b'new build for existing sandbox')
        start = len(self.commands)
        self.assertTrue(self.recover()['success'])
        shares = [c for c in self.commands[start:] if c[1] == 'share']
        self.assertEqual(len(shares), 1)
        self.assertNotIn('--allow-write', shares[0])
        self.assertIn('C:\\WhaleApp-', shares[0][shares[0].index('--sandbox-path') + 1])
        self.assertEqual((self.channel / 'recovery.json').read_bytes(), old)

    def test_unknown_new_build_mapping_never_reposts_share_or_launch(self):
        self._mapped_failure()
        (self.app / '_internal/dependency.bin').write_bytes(b'new build')
        original = self.recovery_cli
        def uncertain(command, **kwargs):
            if command[1] == 'share':
                self.commands.append(command)
                raise subprocess.TimeoutExpired(command, 20)
            return original(command, **kwargs)
        with patch.object(manager.subprocess, 'run', side_effect=uncertain):
            with self.assertRaises(manager.SandboxError):
                manager.recover_sandbox(self.root, self.app, SANDBOX)
        start = len(self.commands)
        with self.assertRaises(manager.SandboxError):
            self.recover()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[start:]))
        self.assertEqual(manager.paired_status(self.root)['pairs'][0]['recovery_state'], 'blocked')

    def test_latest_launch_failure_is_visible_while_continuation_stays_blocked(self):
        self._mapped_failure()
        with patch.object(manager, '_launch', side_effect=manager.SandboxError(
                'arbitrary-private-text', operation='exec', returncode=5, hresult='0x80070005')):
            with self.assertRaises(manager.SandboxError):
                self.recover()
        status = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(status['recovery_state'], 'blocked')
        self.assertEqual(status['recovery_failed_operation'], 'launch')
        self.assertEqual(status['recovery_hresult'], '0x80070005')
        self.assertNotIn('arbitrary-private-text', json.dumps(status))

    def test_completed_resume_diagnostic_is_ready_only_after_session_commit(self):
        self._mapped_failure()
        self.recover()
        self.assertEqual(manager.paired_status(self.root)['pairs'][0]['recovery_state'], 'ready')

    def test_recovery_without_service_refuses_new_business_evidence_on_resume(self):
        self._mapped_failure()
        (self.channel / 'consumed/protected.json').write_text('{}')
        before = len(self.commands)
        with self.assertRaisesRegex(manager.SandboxError, '业务或消费'):
            self.recover()
        self.assertFalse(any('--sandbox-agent-launch' in ' '.join(c) or c[1] == 'share'
                             for c in self.commands[before:]))

    def test_signed_clean_exit_does_not_allow_resume_across_business_evidence(self):
        self._mapped_failure()
        self.service(self.channel, active=False)
        (self.channel / 'consumed/protected.json').write_text('{}')
        before = len(self.commands)
        with self.assertRaisesRegex(manager.SandboxError, '业务或消费'):
            self.recover()
        self.assertFalse(any('--sandbox-agent-launch' in ' '.join(c) or c[1] == 'share'
                             for c in self.commands[before:]))

    def test_business_evidence_appearing_during_signed_inactive_probe_prevents_launch(self):
        self._mapped_failure()
        self.service(self.channel, active=False)
        probe = manager._recovery_process_probe
        def evidence_arrives(pairing, sandbox):
            result = probe(pairing, sandbox)
            (self.channel / 'consumed/protected.json').write_text('{}')
            return result
        before = len(self.commands)
        with patch.object(manager, '_recovery_process_probe', side_effect=evidence_arrives):
            with self.assertRaisesRegex(manager.SandboxError, '业务或消费'):
                self.recover()
        self.assertFalse(any('--sandbox-agent-launch' in ' '.join(c) or c[1] == 'share'
                             for c in self.commands[before:]))

    def test_preflight_failure_does_not_cancel_an_agent_this_operation_never_started(self):
        self._mapped_failure()
        with patch.object(manager, '_session_probe', side_effect=manager.SandboxError('no desktop')), \
             patch.object(manager, '_cancel_upgrade_candidate') as cancel:
            with self.assertRaises(manager.SandboxError):
                self.recover()
        cancel.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows named preparation mutex')
    def test_another_process_cannot_enter_same_profile_and_sandbox_during_prepare(self):
        script = '''
import sys
from src import sandbox_manager as manager
try:
    with manager._prepare_process_lock(sys.argv[1], sys.argv[2]):
        raise SystemExit(3)
except manager.SandboxError:
    print('concurrent_prepare_denied')
'''
        # 只调用原生互斥锁，不运行CLI、微信、代理或任何配对副作用。
        with manager._prepare_process_lock(self.root, SANDBOX):
            outcome = fixtures._OWNED_PROCESS_RUN([sys.executable, '-c', script, str(self.root), SANDBOX],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        self.assertIn(b'concurrent_prepare_denied', outcome.stdout)
        with manager._prepare_process_lock(self.root, SANDBOX):
            pass  # 原操作退出后可重新取得；不留永久文件锁。


