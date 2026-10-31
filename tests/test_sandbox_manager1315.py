"""隔离环境管理只准备专属目录；所有 Windows Sandbox CLI 都使用替身。"""
import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from src import sandbox_manager as manager
from src.isolated_wechat.protocol import load_pairing, read_document, write_document
from src.runtime_identity import APP_VERSION

SANDBOX = '7e2838d2-db90-448c-aced-96ecab3b5f8e'


def session_probe_command(command):
    import base64
    if command[1] != 'exec':
        return False
    text = command[command.index('--command') + 1]
    return ' -EncodedCommand ' in text and 'WHALE_SESSION_PROBE' in base64.b64decode(
        text.split(' -EncodedCommand ', 1)[1]).decode('utf-16le')


def effect_command(command):
    return command[1] == 'share' or (command[1] == 'exec' and not session_probe_command(command))


class SandboxManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'profile'
        self.root.mkdir()
        self.app = Path(self.temp.name) / 'application'
        self.app.mkdir()
        (self.app / 'WhaleGirlWeChat.exe').write_bytes(b'offline executable fixture')
        (self.app / '_internal').mkdir()
        (self.app / '_internal' / 'dependency.bin').write_bytes(b'packaged dependency')
        (self.app / 'config.wechat.yaml').write_text('legacy_api_key: never-map\n', 'utf-8')
        (self.root / 'config.wechat.yaml').write_text('api_key: keep-private\n', 'utf-8')
        self.commands = []
        self.cli_patch = patch.object(manager, 'find_wsb', return_value=Path(self.temp.name) / 'wsb.exe')
        self.run_patch = patch.object(manager.subprocess, 'run', side_effect=self.fake_cli)
        self.version_patch = patch.object(manager, 'packaged_version', return_value=APP_VERSION)
        self.cli_patch.start()
        self.run_patch.start()
        self.version_patch.start()

    def tearDown(self):
        self.run_patch.stop()
        self.cli_patch.stop()
        self.version_patch.stop()
        self.temp.cleanup()

    def fake_cli(self, command, **kwargs):
        self.commands.append(command)
        self.assertFalse(kwargs.get('shell', False))
        self.assertLessEqual(kwargs['timeout'], 25)
        if command[1] == 'list':
            value = {'WindowsSandboxEnvironments': [{'Id': SANDBOX}]}
        elif command[1] == 'share':
            value = None
        elif command[1] == 'exec':
            value = {'ExitCode': 0}
        else:
            raise AssertionError('No install, start, stop, or arbitrary CLI action is permitted')
        # 模拟真实子进程的文件重定向，而不是把 stdout 塞回 CompletedProcess。
        output = '' if value is None else json.dumps(value)
        kwargs['stdout'].write(output.encode('utf-8'))
        kwargs['stdout'].flush()
        return subprocess.CompletedProcess(command, 0, None, None)

    def prepare(self):
        return manager.prepare_sandbox(self.root, self.app, SANDBOX)

    def service(self, channel, **extra):
        pairing = load_pairing(channel)
        value = dict(epoch=uuid.uuid4().hex, app_version=APP_VERSION,
                     heartbeat=time.time(), active=True, status={'online': False})
        value.update(extra)
        write_document(Path(channel) / 'service.json', pairing, value)
        return pairing, value

    def test_prepare_maps_only_program_and_new_channel_without_copying_profile(self):
        result = self.prepare()
        self.assertTrue(result['success'])
        channel = Path(result['channel_dir'])
        self.assertEqual(channel.parent, self.root / 'data' / 'isolated_wechat')
        pairing = load_pairing(channel)
        self.assertEqual(pairing['channel_id'], result['channel_id'])
        self.assertRegex(pairing['secret'], r'^[0-9a-f]{64}$')
        shares = [c for c in self.commands if c[1] == 'share']
        self.assertEqual(len(shares), 2)
        source = Path(shares[0][shares[0].index('--host-path') + 1])
        self.assertTrue(source.is_relative_to(self.root / 'data' / 'isolated_wechat_programs' / APP_VERSION))
        self.assertEqual({file.name for file in source.iterdir()}, {'WhaleGirlWeChat.exe', '_internal', 'runtime.json'})
        self.assertFalse((source / 'config.wechat.yaml').exists())
        self.assertNotIn('--allow-write', shares[0])
        self.assertIn(str(channel), shares[1])
        self.assertIn('--allow-write', shares[1])
        self.assertNotIn(str(self.root), shares[1])
        launch = next(c for c in self.commands if c[1] == 'exec' and not session_probe_command(c))
        self.assertIn('ExistingLogin', launch)
        self.assertIn(r'C:\WhaleApp\WhaleGirlWeChat.exe --sandbox-agent-launch --channel C:\WhaleChannel', launch)
        for file in channel.rglob('*.json'):
            self.assertNotIn('keep-private', file.read_text('utf-8'))
        self.assertNotIn(pairing['secret'], '\n'.join(' '.join(c) for c in self.commands))
        self.assertEqual((self.root / 'config.wechat.yaml').read_text('utf-8'), 'api_key: keep-private\n')

    def test_status_has_no_secret_or_host_path_and_distinguishes_agent_from_wechat(self):
        result = self.prepare()
        pairing, _ = self.service(result['channel_dir'])
        status = manager.paired_status(self.root)
        self.assertEqual(status['pairs'][0]['stage'], 'agent_ready')
        self.assertTrue(status['pairs'][0]['agent_online'])
        self.assertFalse(status['pairs'][0]['wechat_online'])
        raw = json.dumps(status)
        self.assertNotIn(pairing['secret'], raw)
        self.assertNotIn(str(self.root), raw)
        self.assertEqual(status['pairs'][0]['short_id'], result['channel_id'][:12])

    def test_active_pair_is_reused_without_duplicating_mapping_or_launch(self):
        first = self.prepare()
        self.service(first['channel_dir'])
        count = len(self.commands)
        second = self.prepare()
        self.assertEqual(first['channel_id'], second['channel_id'])
        self.assertTrue(all(c[1] == 'list' for c in self.commands[count:]))

    def test_invalid_sandbox_id_never_runs_cli(self):
        for value in ('', 'not-an-id', SANDBOX + ' --command unwanted', '{' + SANDBOX + '}'):
            with self.subTest(value=value), self.assertRaises(manager.SandboxError):
                manager.prepare_sandbox(self.root, self.app, value)
        self.assertEqual(self.commands, [])

    def test_missing_packaged_app_does_not_start_sandbox_or_copy_data(self):
        (self.app / 'WhaleGirlWeChat.exe').unlink()
        with self.assertRaisesRegex(manager.SandboxError, '已构建'):
            self.prepare()
        self.assertEqual(self.commands, [])
        self.assertFalse((self.root / 'data').exists())

    def test_failure_keeps_failed_session_and_does_not_enable_or_retry(self):
        original = self.fake_cli
        def fail(command, **kwargs):
            if command[1] == 'share':
                self.commands.append(command)
                raise subprocess.TimeoutExpired(command, 20)
            return original(command, **kwargs)
        with patch.object(manager.subprocess, 'run', side_effect=fail):
            with self.assertRaisesRegex(manager.SandboxError, '超时'):
                self.prepare()
        sessions = list((self.root / 'data' / 'isolated_wechat').glob('*/session.json'))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(json.loads(sessions[0].read_text('utf-8'))['stage'], 'failed')
        self.assertEqual(sum(c[1] == 'share' for c in self.commands), 1)
        self.assertNotIn('sandbox', (self.root / 'config.wechat.yaml').read_text('utf-8'))

    def test_launch_failure_requests_signed_stop_if_own_agent_did_start(self):
        def launch(sandbox):
            channels = list((self.root / 'data' / 'isolated_wechat').iterdir())
            self.assertEqual(len(channels), 1)
            self.service(channels[0])
            raise manager.SandboxError('启动结果不明确')
        with patch.object(manager, '_launch', side_effect=launch):
            with self.assertRaisesRegex(manager.SandboxError, '启动结果不明确'):
                self.prepare()
        channel = next((self.root / 'data' / 'isolated_wechat').iterdir())
        pairing = load_pairing(channel)
        service = read_document(channel / 'service.json', pairing)
        control = read_document(channel / 'control' / 'stop.json', pairing)
        self.assertEqual(control['action'], 'stop')
        self.assertEqual(control['epoch'], service['epoch'])
        self.assertFalse(any(c[1] in ('stop', 'terminate') for c in self.commands))

    def test_failure_to_record_prepare_failure_does_not_prevent_agent_cleanup(self):
        write = manager._write_session
        def write_session(path, value):
            if value.get('stage') == 'failed':
                raise OSError('fixture write failure')
            return write(path, value)
        with (patch.object(manager, '_launch', side_effect=manager.SandboxError('启动失败')),
                patch.object(manager, '_write_session', side_effect=write_session),
                patch.object(manager, 'stop_guest') as stop):
            with self.assertRaisesRegex(manager.SandboxError, '启动失败'):
                self.prepare()
        stop.assert_called_once()
        channel = stop.call_args.args[0]
        self.assertEqual(channel.parent, self.root / 'data' / 'isolated_wechat')

    def test_channel_path_rejects_traversal_and_mismatched_identity(self):
        result = self.prepare()
        for bad in ('../' + result['channel_id'], 'A' * 32, '/outside'):
            with self.subTest(bad=bad), self.assertRaises(manager.SandboxError):
                manager.channel_path(self.root, bad)
        pair_file = Path(result['channel_dir']) / 'pairing.json'
        data = json.loads(pair_file.read_text('utf-8'))
        data['channel_id'] = uuid.uuid4().hex
        pair_file.write_text(json.dumps(data), 'utf-8')
        with self.assertRaisesRegex(manager.SandboxError, '身份不一致'):
            manager.channel_path(self.root, result['channel_id'])

    def test_symlink_below_root_is_refused(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        try:
            (self.root / 'data').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('当前用户无创建符号链接权限')
        with self.assertRaisesRegex(manager.SandboxError, '链接'):
            self.prepare()
        self.assertEqual(self.commands, [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_stop_uses_signed_epoch_control_and_never_stops_sandbox_or_wechat(self):
        result = self.prepare()
        pairing, service = self.service(result['channel_dir'])
        count = len(self.commands)
        stopped = manager.stop_guest(result['channel_dir'])
        self.assertTrue(stopped['requested'])
        self.assertFalse(stopped['stopped'])
        control = read_document(Path(result['channel_dir']) / 'control' / 'stop.json', pairing)
        self.assertEqual(control['action'], 'stop')
        self.assertEqual(control['epoch'], service['epoch'])
        self.assertLessEqual(control['deadline'] - control['created'], 30)
        self.assertEqual(len(self.commands), count)

    def test_stale_or_wrong_version_service_never_claims_wechat_ready(self):
        result = self.prepare()
        self.service(result['channel_dir'], heartbeat=time.time() - 60)
        status = manager.paired_status(self.root)['pairs'][0]
        self.assertFalse(status['agent_online'])
        self.service(result['channel_dir'], app_version=APP_VERSION + '-old')
        status = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(status['stage'], 'version_mismatch')
        self.assertFalse(status['wechat_online'])
        # 心跳新鲜时版本不符不等于代理离线；但仍不得声称可读取微信。
        self.assertTrue(status['agent_online'])

    def test_online_old_version_agent_is_offered_in_place_upgrade(self):
        result = self.prepare()
        self.service(result['channel_dir'], app_version=APP_VERSION + '.1')
        status = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(status['stage'], 'version_mismatch')
        self.assertTrue(status['agent_online'])
        self.assertFalse(status['wechat_online'])
        self.assertIn('隔离代理在线', status['reason'])
        self.assertIn(APP_VERSION + '.1', status['reason'])
        self.assertIn(APP_VERSION, status['reason'])
        self.assertIn('原地升级', status['reason'])
        self.assertIn('不会启动机器人', status['reason'])

    def test_stale_heartbeat_with_old_version_stays_fail_closed(self):
        result = self.prepare()
        self.service(result['channel_dir'], heartbeat=time.time() - 60, app_version=APP_VERSION + '.1')
        status = manager.paired_status(self.root)['pairs'][0]
        self.assertEqual(status['stage'], 'prepared')
        self.assertFalse(status['agent_online'])
        self.assertFalse(status['wechat_online'])
        self.assertNotIn('原地升级', status['reason'])

    def test_upgrade_wait_defaults_to_45_seconds_and_accepts_override(self):
        self.assertEqual(manager.DEFAULT_UPGRADE_TIMEOUT, 45.0)
        self.assertEqual(manager._upgrade_timeout('WHALE_SANDBOX_UPGRADE_STOP_TIMEOUT', {}), 45.0)
        self.assertEqual(manager._upgrade_timeout('WHALE_SANDBOX_UPGRADE_START_TIMEOUT',
            {'WHALE_SANDBOX_UPGRADE_START_TIMEOUT': '90'}), 90.0)
        for invalid in ('0', '-5', 'unknown'):
            self.assertEqual(manager._upgrade_timeout('WHALE_SANDBOX_UPGRADE_STOP_TIMEOUT',
                {'WHALE_SANDBOX_UPGRADE_STOP_TIMEOUT': invalid}), 45.0)
        names = ('WHALE_SANDBOX_UPGRADE_STOP_TIMEOUT', 'WHALE_SANDBOX_UPGRADE_START_TIMEOUT')
        if not any(os.environ.get(name) for name in names):
            # 未覆盖时等待为 45 秒；既有用例仍用 patch.object 改成更短值。
            self.assertEqual((manager.UPGRADE_STOP_TIMEOUT, manager.UPGRADE_START_TIMEOUT), (45.0, 45.0))

    def test_old_executable_version_never_launches_unknown_flags(self):
        with patch.object(manager, 'packaged_version', return_value='1.3.14'):
            with self.assertRaisesRegex(manager.SandboxError, '版本与控制台不一致'):
                self.prepare()
        self.assertEqual(self.commands, [])

    def test_low_disk_does_not_modify_old_runtime_cache_or_profile(self):
        from types import SimpleNamespace
        with patch.object(manager.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(manager.SandboxError, '磁盘空间不足'):
                self.prepare()
        self.assertEqual(self.commands, [])
        cache = self.root / 'data' / 'isolated_wechat_programs' / APP_VERSION
        self.assertEqual(list(cache.iterdir()), [])
        self.assertEqual((self.root / 'config.wechat.yaml').read_text('utf-8'), 'api_key: keep-private\n')

    def test_runtime_cache_tampering_is_rejected_before_mapping(self):
        self.prepare()
        cached = next((self.root / 'data' / 'isolated_wechat_programs' / APP_VERSION).iterdir())
        (cached / '_internal' / 'dependency.bin').write_bytes(b'changed')
        count = len(self.commands)
        with self.assertRaisesRegex(manager.SandboxError, '缓存内容已改变'):
            self.prepare()
        self.assertEqual(len(self.commands), count)

    def test_runtime_cache_same_size_dependency_tampering_with_timestamp_restored_is_rejected(self):
        self.prepare()
        cached = next((self.root / 'data' / 'isolated_wechat_programs' / APP_VERSION).iterdir())
        dependency = cached / '_internal' / 'dependency.bin'
        stat = dependency.stat()
        data = dependency.read_bytes()
        dependency.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        os.utime(dependency, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        count = len(self.commands)
        with self.assertRaisesRegex(manager.SandboxError, '缓存内容已改变'):
            self.prepare()
        self.assertEqual(len(self.commands), count)

    def test_runtime_cache_extra_profile_file_is_never_mapped(self):
        self.prepare()
        cached = next((self.root / 'data' / 'isolated_wechat_programs' / APP_VERSION).iterdir())
        (cached / 'config.wechat.yaml').write_text('api_key: must-stay-private\n', 'utf-8')
        count = len(self.commands)
        with self.assertRaisesRegex(manager.SandboxError, '额外文件'):
            self.prepare()
        self.assertEqual(len(self.commands), count)

    def test_copy_content_is_verified_before_program_is_mapped(self):
        copy = manager.shutil.copy2
        def corrupt_copy(source, target, *args, **kwargs):
            result = copy(source, target, *args, **kwargs)
            if Path(source).name == 'WhaleGirlWeChat.exe':
                Path(target).write_bytes(b'bad copy')
            return result
        with patch.object(manager.shutil, 'copy2', side_effect=corrupt_copy):
            with self.assertRaisesRegex(manager.SandboxError, '复制核验失败'):
                self.prepare()
        self.assertEqual(self.commands, [])
        cache = self.root / 'data' / 'isolated_wechat_programs' / APP_VERSION
        self.assertEqual(list(cache.iterdir()), [])

    def test_malformed_cli_does_not_invent_a_running_sandbox(self):
        def malformed(command, **kwargs):
            kwargs['stdout'].write(b'[]')
            kwargs['stdout'].flush()
            return subprocess.CompletedProcess(command, 0, None, None)
        with patch.object(manager.subprocess, 'run', side_effect=malformed):
            with self.assertRaises(manager.SandboxError):
                manager.list_sandboxes()


if __name__ == '__main__':
    unittest.main()
