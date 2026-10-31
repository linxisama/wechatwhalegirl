"""存活沙盒的构建升级：真实签名文件、替身 CLI；不运行微信或任何外部程序。"""
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
from src.isolated_wechat import launcher
from src.isolated_wechat.guest_service import GuestService
from src.isolated_wechat.protocol import read_document, load_pairing, write_document
from src.runtime_identity import APP_VERSION
from tests.test_isolated_transport1315 import FakeNative
from tests import test_sandbox_manager1315 as manager_fixtures

SANDBOX = manager_fixtures.SANDBOX


class SandboxUpgradeTests(unittest.TestCase):
    # 只复用离线夹具，不继承旧测试，避免把同一批测试重复计算成新增覆盖。
    setUp = manager_fixtures.SandboxManagerTests.setUp
    tearDown = manager_fixtures.SandboxManagerTests.tearDown
    fake_cli = manager_fixtures.SandboxManagerTests.fake_cli
    prepare = manager_fixtures.SandboxManagerTests.prepare
    service = manager_fixtures.SandboxManagerTests.service

    def changed_pair(self, **extra):
        first = self.prepare()
        self.channel = Path(first['channel_dir'])
        self.pairing, self.old_service = self.service(self.channel, **extra)
        self.original_session = (self.channel / 'session.json').read_bytes()
        self.original_pairing = (self.channel / 'pairing.json').read_bytes()
        self.start_index = len(self.commands)
        self.old_cache = next((self.root / 'data/isolated_wechat_programs' / APP_VERSION).iterdir())
        self.old_dependency = (self.old_cache / '_internal/dependency.bin').read_bytes()
        (self.app / '_internal/dependency.bin').write_bytes(b'new immutable build dependency')
        return first

    def stop_ack(self, path, pairing, payload, **kwargs):
        result = write_document(path, pairing, payload, **kwargs)
        if Path(path).name == 'stop.json' and payload.get('action') == 'stop':
            value = read_document(self.channel / 'service.json', self.pairing)
            if value['epoch'] == payload['epoch']:
                value.pop('version', None)
                value.pop('channel_id', None)
                value.update(active=False, heartbeat=time.time())
                write_document(self.channel / 'service.json', self.pairing, value)
        return result

    def upgrade_cli(self, command, **kwargs):
        if command[1] == 'share':
            # 新映射真正发生前，旧 producer 必须已签名确认清理结束。
            self.assertFalse(read_document(self.channel / 'service.json', self.pairing)['active'])
        result = self.fake_cli(command, **kwargs)
        if command[1] == 'exec':
            command_text = command[command.index('--command') + 1]
            match = re.search(r'C:\\WhaleApp-([0-9a-f]{20})\\WhaleGirlWeChat\.exe ', command_text)
            if match:
                extra = dict(program_key=match[1])
                extra.update(getattr(self, 'new_service_extra', {}))
                self.service(self.channel, **extra)
        return result

    def no_session_change(self):
        self.assertEqual((self.channel / 'session.json').read_bytes(), self.original_session)
        self.assertEqual((self.channel / 'pairing.json').read_bytes(), self.original_pairing)
        self.assertEqual((self.old_cache / '_internal/dependency.bin').read_bytes(), self.old_dependency)

    def test_upgrade_stops_then_maps_exact_build_without_changing_login_or_old_cache(self):
        first = self.changed_pair()
        with patch.object(protocol, 'write_document', side_effect=self.stop_ack), \
             patch.object(manager.subprocess, 'run', side_effect=self.upgrade_cli):
            result = self.prepare()
        self.assertEqual(result['channel_id'], first['channel_id'])
        session = json.loads((self.channel / 'session.json').read_text('utf-8'))
        self.assertEqual(session['program_mapping'], 'by_build')
        self.assertNotEqual(session['program_key'], json.loads(self.original_session)['program_key'])
        shares = [c for c in self.commands[self.start_index:] if c[1] == 'share']
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0][shares[0].index('--sandbox-path') + 1],
                         r'C:\WhaleApp-' + session['program_key'])
        self.assertNotIn('--allow-write', shares[0])
        self.assertEqual((self.channel / 'pairing.json').read_bytes(), self.original_pairing)
        self.assertEqual((self.old_cache / '_internal/dependency.bin').read_bytes(), self.old_dependency)
        self.assertTrue(all(c[1] in ('list', 'share', 'exec') for c in self.commands))
        self.assertEqual(manager.paired_status(self.root)['pairs'][0]['stage'], 'agent_ready')

    def test_explicit_signed_clean_exit_can_be_reused_without_a_fresh_heartbeat(self):
        self.changed_pair(active=False, heartbeat=time.time() - 60)
        with patch.object(protocol, 'write_document', wraps=write_document) as writer, \
             patch.object(manager.subprocess, 'run', side_effect=self.upgrade_cli):
            self.assertTrue(self.prepare()['success'])
        self.assertFalse(any(Path(call.args[0]).name == 'stop.json' for call in writer.call_args_list))

    def test_stale_active_record_is_not_treated_as_clean_exit(self):
        self.changed_pair(heartbeat=time.time() - 60)
        with patch.object(manager, 'UPGRADE_STOP_TIMEOUT', .01):
            with self.assertRaisesRegex(manager.SandboxError, '尚未确认'):
                self.prepare()
        self.no_session_change()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[self.start_index:]))

    def test_missing_or_bad_signed_heartbeat_cannot_authorize_new_mapping(self):
        self.changed_pair()
        heartbeat = self.channel / 'service.json'
        original = heartbeat.read_bytes()
        for bad in (None, original.replace(b'"signature":"', b'"signature":"00')):
            with self.subTest(missing=bad is None):
                if bad is None:
                    heartbeat.unlink()
                else:
                    heartbeat.write_bytes(bad)
                with self.assertRaisesRegex(manager.SandboxError, '无法核验原配对'):
                    self.prepare()
                self.no_session_change()
                heartbeat.write_bytes(original)
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[self.start_index:]))

    def test_damaged_existing_session_is_not_hidden_to_create_a_second_mapping(self):
        self.changed_pair()
        session = self.channel / 'session.json'
        session.write_text('{', 'utf-8')
        before = len(self.commands)
        with self.assertRaisesRegex(manager.SandboxError, '元数据无法核验'):
            self.prepare()
        self.assertEqual(session.read_text('utf-8'), '{')
        self.assertEqual(len(list((self.root / 'data/isolated_wechat').iterdir())), 1)
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[before:]))

    def test_session_owner_change_after_listing_cannot_redirect_upgrade_to_another_sandbox(self):
        self.changed_pair()
        original = manager._existing_pairs_for_prepare
        def changed(*args):
            pairs = original(*args)
            value = json.loads(self.original_session)
            value['sandbox_id'] = str(uuid.uuid4())
            (self.channel / 'session.json').write_text(json.dumps(value), 'utf-8')
            return pairs
        with patch.object(manager, '_existing_pairs_for_prepare', side_effect=changed):
            with self.assertRaisesRegex(manager.SandboxError, '归属已改变'):
                self.prepare()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[self.start_index:]))

    def test_unknown_session_version_cannot_be_hidden_or_upgrade_an_existing_pair(self):
        self.changed_pair()
        for version in (True, 2, '1'):
            with self.subTest(version=version):
                value = json.loads(self.original_session)
                value['version'] = version
                (self.channel / 'session.json').write_text(json.dumps(value), 'utf-8')
                with self.assertRaisesRegex(manager.SandboxError, '元数据无法核验'):
                    self.prepare()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[self.start_index:]))

    def test_null_mapping_record_is_not_overwritten_as_if_it_were_absent(self):
        self.changed_pair(active=False)
        _, key = manager._prepare_program(self.root, self.app)
        value = {'version': 1, 'channel_id': self.channel.name, 'sandbox_id': SANDBOX,
                 'mappings': {key: None}}
        registry = self.channel / 'program-mappings.json'
        registry.write_text(json.dumps(value), 'utf-8')
        original = registry.read_bytes()
        with self.assertRaisesRegex(manager.SandboxError, '映射记录无效'):
            self.prepare()
        self.no_session_change()
        self.assertEqual(registry.read_bytes(), original)
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[self.start_index:]))

    def test_full_bounded_mapping_registry_can_still_reuse_confirmed_target(self):
        self.changed_pair(active=False)
        app, key = manager._prepare_program(self.root, self.app)
        manifest = json.loads((app / 'runtime.json').read_text('utf-8'))
        mappings = {f'{index:020x}': {'app_version': APP_VERSION,
                    'fingerprint': f'{index:020x}' + '0' * 44, 'stage': 'mapped'}
                    for index in range(99)}
        mappings[key] = {'app_version': APP_VERSION, 'fingerprint': manifest['fingerprint'], 'stage': 'mapped'}
        value = {'version': 1, 'channel_id': self.channel.name, 'sandbox_id': SANDBOX, 'mappings': mappings}
        registry = self.channel / 'program-mappings.json'
        registry.write_text(json.dumps(value), 'utf-8')
        self.assertGreater(registry.stat().st_size, 8192)
        with patch.object(manager.subprocess, 'run', side_effect=self.upgrade_cli):
            self.assertTrue(self.prepare()['success'])
        self.assertFalse(any(c[1] == 'share' for c in self.commands[self.start_index:]))

    def test_epoch_change_during_stop_is_rejected_without_mapping(self):
        self.changed_pair()
        def changed(path, pairing, payload, **kwargs):
            result = write_document(path, pairing, payload, **kwargs)
            if Path(path).name == 'stop.json':
                self.service(self.channel, active=False)
            return result
        with patch.object(protocol, 'write_document', side_effect=changed):
            with self.assertRaisesRegex(manager.SandboxError, '身份已改变'):
                self.prepare()
        self.no_session_change()
        self.assertFalse(any(c[1] in ('share', 'exec') for c in self.commands[self.start_index:]))

    def test_uncertain_share_preserves_session_and_is_never_automatically_reposted(self):
        self.changed_pair()
        def uncertain(command, **kwargs):
            if command[1] == 'share':
                self.commands.append(command)
                raise subprocess.TimeoutExpired(command, 20)
            return self.fake_cli(command, **kwargs)
        with patch.object(protocol, 'write_document', side_effect=self.stop_ack), \
             patch.object(manager.subprocess, 'run', side_effect=uncertain):
            with self.assertRaisesRegex(manager.SandboxError, '超时'):
                self.prepare()
        self.no_session_change()
        with self.assertRaisesRegex(manager.SandboxError, '尚未确认'):
            self.prepare()
        self.no_session_change()
        self.assertEqual(sum(c[1] == 'share' for c in self.commands[self.start_index:]), 1)
        self.assertFalse(any(c[1] == 'exec' and not manager_fixtures.session_probe_command(c) for c in self.commands[self.start_index:]))

    def test_ambiguous_launch_stops_only_matching_candidate_and_reuses_confirmed_mapping(self):
        self.changed_pair()
        def ambiguous(command, **kwargs):
            result = self.upgrade_cli(command, **kwargs)
            if command[1] == 'exec' and not manager_fixtures.session_probe_command(command):
                raise subprocess.TimeoutExpired(command, 25)
            return result
        with patch.object(protocol, 'write_document', side_effect=self.stop_ack), \
             patch.object(manager.subprocess, 'run', side_effect=ambiguous):
            with self.assertRaisesRegex(manager.SandboxError, '超时'):
                self.prepare()
        self.no_session_change()
        candidate = read_document(self.channel / 'service.json', self.pairing)
        control = read_document(self.channel / 'control/stop.json', self.pairing)
        self.assertNotEqual(candidate['epoch'], self.old_service['epoch'])
        self.assertEqual(candidate['epoch'], control['epoch'])
        self.assertFalse(candidate['active'])
        with patch.object(protocol, 'write_document', side_effect=self.stop_ack), \
             patch.object(manager.subprocess, 'run', side_effect=self.upgrade_cli):
            self.assertTrue(self.prepare()['success'])
        self.assertEqual(sum(c[1] == 'share' for c in self.commands[self.start_index:]), 1)

    def reject_new_agent(self, extra):
        self.changed_pair()
        self.new_service_extra = extra
        with patch.object(protocol, 'write_document', side_effect=self.stop_ack), \
             patch.object(manager.subprocess, 'run', side_effect=self.upgrade_cli):
            with self.assertRaisesRegex(manager.SandboxError, '身份或心跳不符'):
                self.prepare()
        self.no_session_change()

    def test_new_agent_wrong_build_proof_never_commits_session(self):
        self.reject_new_agent({'program_key': 'f' * 20})

    def test_new_agent_missing_build_proof_never_commits_session(self):
        self.reject_new_agent({'program_key': ''})

    def test_new_agent_stale_heartbeat_never_commits_session(self):
        self.reject_new_agent({'heartbeat': time.time() - 60})

    def test_new_agent_wrong_version_never_commits_session(self):
        self.reject_new_agent({'app_version': '1.3.15'})

    def test_old_epoch_with_new_build_label_cannot_be_replayed_as_upgrade(self):
        self.changed_pair()
        self.new_service_extra = {'epoch': self.old_service['epoch']}
        with patch.object(protocol, 'write_document', side_effect=self.stop_ack), \
             patch.object(manager.subprocess, 'run', side_effect=self.upgrade_cli), \
             patch.object(manager, 'UPGRADE_START_TIMEOUT', .01):
            with self.assertRaisesRegex(manager.SandboxError, '未确认新构建'):
                self.prepare()
        self.no_session_change()

    def test_reusing_upgraded_build_launches_derived_path_without_remapping(self):
        self.changed_pair()
        with patch.object(protocol, 'write_document', side_effect=self.stop_ack), \
             patch.object(manager.subprocess, 'run', side_effect=self.upgrade_cli):
            upgraded = self.prepare()
            self.service(self.channel, active=False)
            before = len(self.commands)
            result = self.prepare()
        self.assertEqual(result['channel_id'], upgraded['channel_id'])
        self.assertEqual(sum(c[1] == 'share' for c in self.commands[before:]), 0)
        launch = next(c for c in self.commands[before:] if c[1] == 'exec' and not manager_fixtures.session_probe_command(c))
        self.assertIn('C:\\WhaleApp-', launch[launch.index('--command') + 1])

    def test_build_mismatch_status_is_not_an_enable_candidate(self):
        first = self.prepare()
        self.service(first['channel_dir'], program_key='f' * 20)
        pair = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(pair['stage'], 'build_mismatch')
        self.assertFalse(pair['agent_online'])

    def test_launch_key_cannot_inject_a_path_or_command(self):
        for key in ('../outside', 'A' * 20, 'a' * 20 + ' --command bad', ''):
            with self.subTest(key=key), self.assertRaises(manager.SandboxError):
                manager._launch(SANDBOX, program_key=key)
        self.assertEqual(self.commands, [])


class FrozenBuildIdentityTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exe = self.root / 'WhaleGirlWeChat.exe'
        self.exe.write_bytes(b'offline frozen fixture')
        self.manifest = self.root / 'runtime.json'
        self.fingerprint = '0123456789abcdef' * 4
        self.manifest.write_text(json.dumps({'app_version': APP_VERSION,
                                            'fingerprint': self.fingerprint}), 'utf-8')
        self.frozen = patch.object(launcher.sys, 'frozen', True, create=True)
        self.executable = patch.object(launcher.sys, 'executable', str(self.exe))
        self.frozen.start()
        self.executable.start()
        self.environment = patch.dict(os.environ)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.executable.stop()
        self.frozen.stop()
        self.temp.cleanup()

    def test_frozen_build_identity_is_derived_from_adjacent_manifest(self):
        self.assertEqual(launcher.runtime_program_key(), self.fingerprint[:20])

    def test_linked_manifest_is_rejected_without_reading_it(self):
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == self.manifest or original(path)):
            with self.assertRaises(protocol.ProtocolError):
                launcher.runtime_program_key()

    def test_missing_bad_version_hash_or_oversized_manifest_is_rejected(self):
        for raw in (None, '{}', json.dumps({'app_version': '1.3.15', 'fingerprint': self.fingerprint}),
                    json.dumps({'app_version': APP_VERSION, 'fingerprint': 'A' * 64}), ' ' * 8193):
            with self.subTest(missing=raw is None):
                if raw is None:
                    self.manifest.unlink(missing_ok=True)
                else:
                    self.manifest.write_text(raw, 'utf-8')
                with self.assertRaises(protocol.ProtocolError):
                    launcher.runtime_program_key()

    def test_same_version_live_legacy_or_other_build_cannot_shortcut_new_launch(self):
        channel = self.root / 'channel'
        channel.mkdir()
        (channel / 'pairing.json').write_text(json.dumps({'version': 1,
            'channel_id': uuid.uuid4().hex, 'secret': 'ab' * 32}), 'utf-8')
        pairing = load_pairing(channel)
        for key in (None, 'f' * 20, self.fingerprint[:20]):
            service = {'epoch': uuid.uuid4().hex, 'app_version': APP_VERSION,
                       'heartbeat': time.time(), 'active': True}
            if key is not None:
                service['program_key'] = key
            write_document(channel / 'service.json', pairing, service)
            if key == self.fingerprint[:20]:
                self.assertTrue(launcher._already_running(pairing))
            else:
                with self.assertRaisesRegex(protocol.ProtocolError, '另一构建'):
                    launcher._already_running(pairing)

    def test_guest_signed_heartbeat_contains_derived_build_identity(self):
        channel = self.root / 'channel'
        channel.mkdir()
        (channel / 'pairing.json').write_text(json.dumps({'version': 1,
            'channel_id': uuid.uuid4().hex, 'secret': 'ab' * 32}), 'utf-8')
        service = GuestService(channel, self.root / 'guest-private', bridge_factory=FakeNative)
        service.heartbeat()
        value = read_document(channel / 'service.json', service.pairing)
        self.assertEqual(value['program_key'], self.fingerprint[:20])
        self.assertFalse(service.bridge.online)
