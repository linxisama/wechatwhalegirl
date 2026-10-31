"""失败配对的显式恢复；签名文件真实、CLI 全替身，不接触微信。"""
import base64
import json
import os
import re
import subprocess
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from src import sandbox_manager as manager
from src.isolated_wechat import protocol
from src.isolated_wechat.protocol import load_pairing, read_document, write_document
from src.runtime_identity import APP_VERSION
from tests import test_sandbox_manager1315 as fixtures

SANDBOX = fixtures.SANDBOX
_OWNED_PROCESS_RUN = subprocess.run


class SandboxRecoveryTests(unittest.TestCase):
    setUp = fixtures.SandboxManagerTests.setUp
    tearDown = fixtures.SandboxManagerTests.tearDown
    fake_cli = fixtures.SandboxManagerTests.fake_cli
    prepare = fixtures.SandboxManagerTests.prepare
    service = fixtures.SandboxManagerTests.service

    def failed_pair(self, *, service=None):
        result = self.prepare()
        self.channel = Path(result['channel_dir'])
        value = json.loads((self.channel / 'session.json').read_text('utf-8'))
        value['stage'] = 'failed'
        (self.channel / 'session.json').write_text(json.dumps(value), 'utf-8')
        self.original_session = (self.channel / 'session.json').read_bytes()
        self.original_pairing = (self.channel / 'pairing.json').read_bytes()
        self.pairing = load_pairing(self.channel)
        if service is not None:
            self.pairing, self.old_service = self.service(self.channel, **service)
        self.start_index = len(self.commands)
        return result

    def recovery_cli(self, command, **kwargs):
        result = self.fake_cli(command, **kwargs)
        if command[1] == 'exec':
            text = command[command.index('--command') + 1]
            if fixtures.session_probe_command(command):
                return result
            if ' -EncodedCommand ' in text:
                code = base64.b64decode(text.split(' -EncodedCommand ', 1)[1]).decode('utf-16le')
                self.assertNotIn(self.pairing['secret'], code)
                self.assertNotIn('Stop-Process', code)
                self.assertNotIn('Weixin.exe', code)
                nonce = re.search("nonce='([0-9a-f]{32})'", code)[1]
                self.assertIn(manager._build_guest_channel(self.channel.name), code)
                value = {'nonce': nonce, 'agent_process_count': 0, 'process_query_ok': True}
                value.update(getattr(self, 'proof_extra', {}))
                proof = self.channel / ('recovery-proof-' + nonce + '.json')
                write_document(proof, self.pairing, value)
                if getattr(self, 'tamper_proof', False):
                    proof.write_text(proof.read_text('utf-8').replace('"signature":"', '"signature":"00'), 'utf-8')
            else:
                key = re.search(r'C:\\WhaleApp-([0-9a-f]{20})\\WhaleGirlWeChat\.exe ', text)[1]
                self.assertTrue(text.endswith('--channel ' + manager._build_guest_channel(self.channel.name)))
                extra = dict(program_key=key)
                extra.update(getattr(self, 'service_extra', {}))
                self.service(self.channel, **extra)
        return result

    def recover(self):
        with patch.object(manager.subprocess, 'run', side_effect=self.recovery_cli):
            return manager.recover_sandbox(self.root, self.app, SANDBOX)

    def assert_original(self):
        self.assertEqual((self.channel / 'session.json').read_bytes(), self.original_session)
        self.assertEqual((self.channel / 'pairing.json').read_bytes(), self.original_pairing)

    def test_recovery_preserves_identity_and_login_and_uses_new_derived_paths(self):
        first = self.failed_pair()
        self.assertTrue(self.recover()['success'])
        value = json.loads((self.channel / 'session.json').read_text('utf-8'))
        self.assertEqual(value['channel_id'], first['channel_id'])
        self.assertEqual(value['channel_mapping'], 'by_channel')
        self.assertEqual(value['program_mapping'], 'by_build')
        self.assertEqual(value['stage'], 'prepared')
        self.assertEqual((self.channel / 'pairing.json').read_bytes(), self.original_pairing)
        record = json.loads((self.channel / 'recovery.json').read_text('utf-8'))
        self.assertEqual(record['original_session'], json.loads(self.original_session))
        self.assertEqual(record['stage'], 'ready')
        self.assertEqual(record['rpc_requests'], 0)
        self.assertEqual(record['original_service_evidence'], 'no_service_no_business')
        shares = [c for c in self.commands[self.start_index:] if c[1] == 'share']
        self.assertEqual([c[c.index('--sandbox-path') + 1] for c in shares], [
            manager._build_guest_app(value['program_key']), manager._build_guest_channel(first['channel_id'])])
        self.assertTrue(all(c[1] in ('list', 'share', 'exec') for c in self.commands))
        for name in ('requests', 'responses', 'consumed', 'events'):
            self.assertEqual(list((self.channel / name).iterdir()), [])
        self.assertEqual((self.root / 'config.wechat.yaml').read_text('utf-8'), 'api_key: keep-private\n')
        self.assertEqual(manager.paired_status(self.root)['pairs'][0]['stage'], 'agent_ready')

    def test_normal_prepare_failed_guard_is_unchanged(self):
        self.failed_pair()
        with self.assertRaisesRegex(manager.SandboxError, '未完成的目录映射'):
            self.prepare()
        self.assert_original()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[self.start_index:]))

    def test_no_service_with_any_business_evidence_refuses_recovery(self):
        self.failed_pair()
        for name in ('requests', 'responses', 'consumed', 'events', 'ocr'):
            with self.subTest(name=name):
                folder = self.channel / name
                folder.mkdir(exist_ok=True)
                marker = folder / 'evidence.json'
                marker.write_text('{}', 'utf-8')
                with self.assertRaisesRegex(manager.SandboxError, '业务或消费记录'):
                    self.recover()
                self.assert_original()
                self.assertFalse((self.channel / 'recovery.json').exists())
                marker.unlink()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[self.start_index:]))

    def test_bad_original_service_signature_cannot_use_absent_service_path(self):
        self.failed_pair(service={'active': False})
        path = self.channel / 'service.json'
        path.write_text(path.read_text('utf-8').replace('"signature":"', '"signature":"00'), 'utf-8')
        with self.assertRaisesRegex(manager.SandboxError, '无法核验原配对'):
            self.recover()
        self.assert_original()
        self.assertFalse((self.channel / 'recovery.json').exists())

    def test_live_old_service_must_confirm_signed_cleanup_before_any_mapping(self):
        self.failed_pair(service={'active': True})
        def stop_ack(path, pairing, payload, **kwargs):
            result = write_document(path, pairing, payload, **kwargs)
            if Path(path).name == 'stop.json':
                value = read_document(self.channel / 'service.json', pairing)
                value.pop('version', None);value.pop('channel_id', None)
                value.update(active=False, heartbeat=time.time())
                write_document(self.channel / 'service.json', pairing, value)
            return result
        original_cli = self.recovery_cli
        def checked_cli(command, **kwargs):
            if command[1] == 'share':
                self.assertFalse(read_document(self.channel / 'service.json', self.pairing)['active'])
            return original_cli(command, **kwargs)
        with patch.object(protocol, 'write_document', side_effect=stop_ack), \
             patch.object(manager.subprocess, 'run', side_effect=checked_cli):
            self.assertTrue(manager.recover_sandbox(self.root, self.app, SANDBOX)['success'])
        record = json.loads((self.channel / 'recovery.json').read_text('utf-8'))
        self.assertEqual(record['original_service_evidence'], 'signed_inactive')
        self.assertNotEqual(read_document(self.channel / 'service.json', self.pairing)['epoch'], self.old_service['epoch'])

    def test_stale_active_old_service_is_not_a_clean_exit(self):
        self.failed_pair(service={'active': True, 'heartbeat': time.time() - 60})
        with patch.object(manager, 'UPGRADE_STOP_TIMEOUT', .01):
            with self.assertRaisesRegex(manager.SandboxError, '尚未确认'):
                self.recover()
        self.assert_original()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[self.start_index:]))

    def test_guest_process_count_and_nonce_must_be_verified(self):
        for extra in ({'agent_process_count': 1}, {'agent_process_count': False},
                      {'nonce': 'f' * 32}, {'process_query_ok': False}):
            with self.subTest(extra=extra):
                self.failed_pair()
                self.proof_extra = extra
                with self.assertRaisesRegex(manager.SandboxError, '只读证据不足'):
                    self.recover()
                self.assert_original()
                self.assertFalse(any('--sandbox-agent-launch' in ' '.join(c) for c in self.commands[self.start_index:]))
                # 每个子用例使用自己的profile，保留上一轮失败证据在原临时树中。
                self.root = self.root.parent / uuid.uuid4().hex
                self.root.mkdir()
                (self.root / 'config.wechat.yaml').write_text('api_key: keep-private\n', 'utf-8')

    def test_tampered_guest_probe_receipt_is_rejected(self):
        self.failed_pair();self.tamper_proof = True
        with self.assertRaisesRegex(manager.SandboxError, '进程回执无法核验'):
            self.recover()
        self.assert_original()

    def test_mapping_timeout_retains_original_and_never_reposts(self):
        self.failed_pair()
        def uncertain(command, **kwargs):
            if command[1] == 'share' and '--allow-write' in command:
                self.commands.append(command)
                raise subprocess.TimeoutExpired(command, 20)
            return self.recovery_cli(command, **kwargs)
        with patch.object(manager.subprocess, 'run', side_effect=uncertain):
            with self.assertRaisesRegex(manager.SandboxError, '超时'):
                manager.recover_sandbox(self.root, self.app, SANDBOX)
        self.assert_original()
        record = json.loads((self.channel / 'recovery.json').read_text('utf-8'))
        self.assertEqual(record['failed_operation'], 'share_channel')
        self.assertEqual(record['channel_mapping'], 'uncertain')
        before = len(self.commands)
        with self.assertRaisesRegex(manager.SandboxError, '已有恢复记录'):
            self.recover()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))

    def test_wrong_build_or_old_epoch_never_commits_session(self):
        self.failed_pair(service={'active': False})
        self.service_extra = {'epoch': self.old_service['epoch']}
        with patch.object(manager, 'UPGRADE_START_TIMEOUT', .01):
            with self.assertRaisesRegex(manager.SandboxError, '未确认新构建'):
                self.recover()
        self.assert_original()

    def test_fresh_epoch_with_wrong_build_never_commits_or_claims_ready(self):
        self.failed_pair()
        self.service_extra = {'program_key': 'f' * 20}
        with self.assertRaisesRegex(manager.SandboxError, '身份或心跳不符'):
            self.recover()
        self.assert_original()
        self.assertEqual(json.loads((self.channel / 'recovery.json').read_text('utf-8'))['failed_operation'],
                         'verify_heartbeat')

    def test_missing_or_corrupt_pairing_cannot_create_a_second_identity(self):
        self.failed_pair()
        path = self.channel / 'pairing.json'
        original = path.read_bytes()
        for malformed in (None, b'{}'):
            with self.subTest(missing=malformed is None):
                if malformed is None:
                    path.unlink()
                else:
                    path.write_bytes(malformed)
                with self.assertRaisesRegex(manager.SandboxError, '元数据无法核验'):
                    self.recover()
                self.assertEqual((self.channel / 'session.json').read_bytes(), self.original_session)
                self.assertEqual(len(list((self.root / 'data/isolated_wechat').iterdir())), 1)
                path.write_bytes(original)
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[self.start_index:]))

    def test_business_evidence_arriving_during_probe_prevents_launch(self):
        self.failed_pair()
        original_cli = self.recovery_cli
        def racing_cli(command, **kwargs):
            result = original_cli(command, **kwargs)
            if command[1] == 'exec' and not fixtures.session_probe_command(command) and ' -EncodedCommand ' in command[command.index('--command') + 1]:
                (self.channel / 'consumed/late.json').write_text('{}', 'utf-8')
            return result
        with patch.object(manager.subprocess, 'run', side_effect=racing_cli):
            with self.assertRaisesRegex(manager.SandboxError, '业务或消费记录'):
                manager.recover_sandbox(self.root, self.app, SANDBOX)
        self.assert_original()
        self.assertFalse(any('--sandbox-agent-launch' in ' '.join(c) for c in self.commands[self.start_index:]))

    def test_recovered_build_upgrade_preserves_channel_mapping(self):
        self.failed_pair();self.recover()
        self.service(self.channel, active=False)
        (self.app / '_internal/dependency.bin').write_bytes(b'next immutable dependency')
        before = len(self.commands)
        with patch.object(manager.subprocess, 'run', side_effect=self.recovery_cli):
            self.assertTrue(self.prepare()['success'])
        launches = [c for c in self.commands[before:] if c[1] == 'exec' and not fixtures.session_probe_command(c)]
        self.assertEqual(len(launches), 1)
        self.assertIn('--channel ' + manager._build_guest_channel(self.channel.name), ' '.join(launches[0]))
        self.assertEqual(json.loads((self.channel / 'session.json').read_text('utf-8'))['channel_mapping'], 'by_channel')

    def test_uncommitted_online_candidate_is_never_an_enable_candidate(self):
        self.failed_pair()
        write = manager._write_session
        def fail_commit(path, value):
            if Path(path).name == 'session.json' and value.get('channel_mapping') == 'by_channel':
                raise OSError('fixture commit failure')
            return write(path, value)
        with patch.object(manager, '_write_session', side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, 'commit failure'):
                self.recover()
        self.assert_original()
        self.assertTrue(read_document(self.channel / 'service.json', self.pairing)['active'])
        status = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(status['stage'], 'failed')
        self.assertFalse(status['agent_online'])
        with self.assertRaisesRegex(manager.SandboxError, '未完成的目录映射'):
            self.prepare()

    def test_preparing_record_cannot_be_promoted_by_live_heartbeat(self):
        self.failed_pair(service={'active': True})
        value = json.loads(self.original_session)
        value['stage'] = 'preparing'
        (self.channel / 'session.json').write_text(json.dumps(value), 'utf-8')
        status = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(status['stage'], 'preparing')
        self.assertFalse(status['agent_online'])
        with self.assertRaisesRegex(manager.SandboxError, '未完成的目录映射'):
            self.prepare()

    def test_fresh_new_epoch_uses_same_clock_skew_tolerance_as_protocol(self):
        self.failed_pair()
        self.service_extra = {'heartbeat': time.time() - .5}
        self.assertTrue(self.recover()['success'])

    def test_recovered_session_relaunch_keeps_derived_channel_path(self):
        self.failed_pair();self.recover()
        self.service(self.channel, active=False)
        before = len(self.commands)
        self.prepare()
        commands = self.commands[before:]
        self.assertFalse(any(c[1] == 'share' for c in commands))
        launch = next(c for c in commands if c[1] == 'exec' and not fixtures.session_probe_command(c))
        self.assertIn('--channel ' + manager._build_guest_channel(self.channel.name), ' '.join(launch))

    def test_unknown_channel_mapping_and_invalid_id_never_form_commands(self):
        for channel_id in ('', '../x', 'A' * 32, 'a' * 32 + ' --command bad'):
            with self.subTest(channel_id=channel_id), self.assertRaises(manager.SandboxError):
                manager._launch(SANDBOX, program_key='a' * 20, channel_id=channel_id)
        self.assertEqual(self.commands, [])
        with self.assertRaises(manager.SandboxError):
            manager._session_channel_key({'channel_mapping': 'arbitrary', 'channel_id': 'a' * 32})

    def test_cli_failure_only_exposes_numeric_diagnostics(self):
        def rejected(command, **kwargs):
            kwargs['stderr'].write(b'PRIVATE-SECRET host-private-path error 0x80070005')
            return subprocess.CompletedProcess(command, 7, None, None)
        with patch.object(manager.subprocess, 'run', side_effect=rejected):
            with self.assertRaisesRegex(manager.SandboxError, '0x80070005') as caught:
                manager._run_wsb(['share', '--id', SANDBOX])
        error = caught.exception
        self.assertEqual(manager._error_scalars(error), {'operation': 'share', 'returncode': 7, 'hresult': '0x80070005'})
        self.assertNotIn('PRIVATE', str(error))
        self.assertNotIn('host-private-path', str(error))

    @unittest.skipUnless(os.name == 'nt', '签名互通使用本测试拥有的 Windows PowerShell 子进程')
    def test_powershell51_signature_is_verified_by_python_protocol(self):
        # 只替换用户/进程枚举夹具及本测试目录；真实执行原脚本的JSON和HMAC部分。
        # 不运行wsb，不查询应用进程，不读取真实配对，也不改变主机环境。
        self.failed_pair()
        executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        if not executable.is_file():
            self.skipTest('Windows PowerShell 不可用')
        runner = _OWNED_PROCESS_RUN
        def owned_crypto_process(arguments, **kwargs):
            command = arguments[arguments.index('--command') + 1]
            code = base64.b64decode(command.split(' -EncodedCommand ', 1)[1]).decode('utf-16le')
            code = code.replace("if ($env:USERNAME -ne 'WDAGUtilityAccount') { exit 2 }", '')
            code = code.replace("$rows=@(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)",
                                "$rows=@([pscustomobject]@{Name='offline-fixture.exe';CommandLine='fixture'})")
            code = code.replace(manager._build_guest_channel(self.channel.name), str(self.channel))
            encoded = base64.b64encode(code.encode('utf-16le')).decode('ascii')
            result = runner([str(executable), '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                             '-EncodedCommand', encoded], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
            return {'ExitCode': result.returncode}
        with patch.object(manager, '_run_wsb', side_effect=owned_crypto_process):
            proof = manager._recovery_process_probe(self.pairing, SANDBOX)
        self.assertEqual(proof['agent_process_count'], 0)
        self.assertTrue(proof['process_query_ok'])


if __name__ == '__main__':
    unittest.main()
