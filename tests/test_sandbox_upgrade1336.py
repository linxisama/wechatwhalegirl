"""原地升级须有界等待且先核对原配对构建；签名文件与 CLI 均为离线夹具。"""
import json
import math
import unittest
from pathlib import Path
from unittest.mock import patch

from src import sandbox_manager as manager
from src.isolated_wechat import protocol
from src.runtime_identity import APP_VERSION
from tests import test_sandbox_manager1315 as manager_fixtures
from tests import test_sandbox_upgrade1316 as upgrade_fixtures


class UpgradeTimeout1336Tests(unittest.TestCase):
    def test_nonfinite_timeout_override_falls_back_to_finite_default(self):
        for value in ('inf', 'Infinity', '+inf', '-inf', 'nan', 'NaN', '1e999', float('inf')):
            with self.subTest(value=value):
                result = manager._upgrade_timeout('TEST_TIMEOUT', {'TEST_TIMEOUT': value})
                self.assertEqual(result, manager.DEFAULT_UPGRADE_TIMEOUT)
                self.assertTrue(math.isfinite(result))

    def test_positive_finite_override_remains_supported(self):
        for value, expected in (('0.01', .01), ('90', 90.), ('1e3', 1000.), (120, 120.)):
            with self.subTest(value=value):
                self.assertEqual(manager._upgrade_timeout('TEST_TIMEOUT',
                    {'TEST_TIMEOUT': value}), expected)


class UpgradeBuildIdentity1336Tests(unittest.TestCase):
    # 仅复用替身和签名夹具，不继承旧 TestCase 或重复运行旧用例。
    setUp = manager_fixtures.SandboxManagerTests.setUp
    tearDown = manager_fixtures.SandboxManagerTests.tearDown
    fake_cli = manager_fixtures.SandboxManagerTests.fake_cli
    prepare = manager_fixtures.SandboxManagerTests.prepare
    service = manager_fixtures.SandboxManagerTests.service
    changed_pair = upgrade_fixtures.SandboxUpgradeTests.changed_pair
    stop_ack = upgrade_fixtures.SandboxUpgradeTests.stop_ack
    upgrade_cli = upgrade_fixtures.SandboxUpgradeTests.upgrade_cli

    def old_session(self, channel, *, mapping='legacy'):
        path = Path(channel) / 'session.json'
        value = json.loads(path.read_text('utf-8'))
        value.update(app_version='1.3.34', program_mapping=mapping)
        path.write_text(json.dumps(value), 'utf-8')
        return value

    def test_old_version_and_wrong_build_reports_build_mismatch_before_upgrade_hint(self):
        first = self.prepare()
        for mapping in ('legacy', 'by_build'):
            with self.subTest(mapping=mapping):
                self.old_session(first['channel_dir'], mapping=mapping)
                self.service(first['channel_dir'], app_version='1.3.34', program_key='f' * 20)
                pair = manager.paired_status(self.root)['pairs'][0]
                self.assertEqual(pair['stage'], 'build_mismatch')
                self.assertFalse(pair['agent_online'])
                self.assertFalse(pair['wechat_online'])
                self.assertNotIn('原地升级', pair['reason'])

    def test_old_version_missing_derived_build_proof_stays_blocked(self):
        first = self.prepare()
        self.old_session(first['channel_dir'], mapping='by_build')
        self.service(first['channel_dir'], app_version='1.3.34')
        pair = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(pair['stage'], 'build_mismatch')
        self.assertFalse(pair['agent_online'])

    def test_current_session_old_service_wrong_build_stays_blocked(self):
        first = self.prepare()
        self.service(first['channel_dir'], app_version='1.3.34', program_key='f' * 20)
        pair = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(pair['stage'], 'build_mismatch')
        self.assertFalse(pair['agent_online'])

    def test_legacy_old_version_without_program_key_remains_upgrade_candidate(self):
        first = self.prepare()
        self.old_session(first['channel_dir'])
        self.service(first['channel_dir'], app_version='1.3.34')
        pair = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(pair['stage'], 'version_mismatch')
        self.assertTrue(pair['agent_online'])
        self.assertFalse(pair['wechat_online'])

    def test_old_version_matching_derived_build_remains_upgrade_candidate(self):
        first = self.prepare()
        session = self.old_session(first['channel_dir'], mapping='by_build')
        self.service(first['channel_dir'], app_version='1.3.34', program_key=session['program_key'])
        pair = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(pair['stage'], 'version_mismatch')
        self.assertTrue(pair['agent_online'])
        self.assertFalse(pair['wechat_online'])

    def test_prepare_wrong_build_never_stops_maps_or_launches_any_agent(self):
        first = self.prepare()
        channel = Path(first['channel_dir'])
        for version in ('1.3.34', APP_VERSION):
            with self.subTest(version=version):
                session = json.loads((channel / 'session.json').read_text('utf-8'))
                session['app_version'] = version
                (channel / 'session.json').write_text(json.dumps(session), 'utf-8')
                self.service(channel, app_version=version, program_key='f' * 20)
                evidence = {path.relative_to(channel): path.read_bytes()
                            for path in channel.rglob('*') if path.is_file()}
                command_count = len(self.commands)
                with patch.object(manager, '_upgrade_pair') as upgrade:
                    with self.assertRaisesRegex(manager.SandboxError, '构建与原配对不一致'):
                        self.prepare()
                    upgrade.assert_not_called()
                self.assertTrue(all(command[1] == 'list' for command in self.commands[command_count:]))
                self.assertEqual({path.relative_to(channel): path.read_bytes()
                                  for path in channel.rglob('*') if path.is_file()}, evidence)

    def test_legacy_old_version_can_still_complete_same_channel_upgrade(self):
        first = self.changed_pair()
        self.old_session(self.channel)
        self.service(self.channel, app_version='1.3.34')
        original_pairing = (self.channel / 'pairing.json').read_bytes()
        with (patch.object(protocol, 'write_document', side_effect=self.stop_ack),
              patch.object(manager.subprocess, 'run', side_effect=self.upgrade_cli)):
            result = self.prepare()
        self.assertEqual(result['channel_id'], first['channel_id'])
        self.assertEqual(result['stage'], 'agent_ready')
        self.assertEqual((self.channel / 'pairing.json').read_bytes(), original_pairing)
        self.assertEqual(json.loads((self.channel / 'session.json').read_text('utf-8'))['app_version'],
                         APP_VERSION)
        shares = [command for command in self.commands[self.start_index:] if command[1] == 'share']
        self.assertEqual(len(shares), 1)

    def test_matching_derived_old_version_can_complete_same_channel_upgrade(self):
        first = self.changed_pair()
        session = self.old_session(self.channel, mapping='by_build')
        self.service(self.channel, app_version='1.3.34', program_key=session['program_key'])
        original_pairing = (self.channel / 'pairing.json').read_bytes()
        with (patch.object(protocol, 'write_document', side_effect=self.stop_ack),
              patch.object(manager.subprocess, 'run', side_effect=self.upgrade_cli)):
            result = self.prepare()
        self.assertEqual(result['channel_id'], first['channel_id'])
        self.assertEqual(result['stage'], 'agent_ready')
        self.assertEqual((self.channel / 'pairing.json').read_bytes(), original_pairing)
        self.assertEqual(manager.paired_status(self.root)['pairs'][0]['stage'], 'agent_ready')


if __name__ == '__main__':
    unittest.main()
