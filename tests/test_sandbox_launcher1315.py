"""来宾入口只验证启动边界；不启动实际程序、驱动或微信。"""
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.isolated_wechat import launcher
from src.isolated_wechat.protocol import ProtocolError, load_pairing, write_document
from src.runtime_identity import APP_VERSION


class SandboxLauncherTests(unittest.TestCase):
    def mutex_api(self, kernel, error):
        # 仅替换 launcher 的互斥接口，不能改全局 ctypes 并污染协议真实文件读取。
        api = SimpleNamespace(WinDLL=Mock(return_value=kernel),
            get_last_error=Mock(return_value=error), set_last_error=Mock(),
            c_void_p=launcher.ctypes.c_void_p)
        return patch.object(launcher, 'ctypes', api)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.channel = self.root / 'mapped-channel'
        self.channel.mkdir()
        (self.channel / 'pairing.json').write_text(json.dumps({
            'version': 1, 'channel_id': uuid.uuid4().hex, 'secret': 'ab' * 32}), 'utf-8')
        self.pairing = load_pairing(self.channel)
        self.private = self.root / 'guest-local' / 'WhaleGirlWeChatGuest'
        self.guard = patch.object(launcher, 'require_sandbox_user')
        self.guard.start()
        self.env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.private.parent)})
        self.env.start()
        self.old_cwd = Path.cwd()

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.env.stop()
        self.guard.stop()
        self.temp.cleanup()

    def call(self, flag='--sandbox-agent-launch'):
        return launcher.main([flag, '--channel', str(self.channel)])

    def service(self, **extra):
        value = dict(epoch=uuid.uuid4().hex, active=True, heartbeat=time.time(), app_version=APP_VERSION)
        value.update(extra)
        write_document(self.channel / 'service.json', self.pairing, value)

    def test_launch_sets_child_private_cwd_and_keeps_the_exact_channel(self):
        observed = []
        with patch.object(launcher, 'worker_command', return_value=['frozen.exe', '--sandbox-agent']) as command, \
             patch.object(launcher.subprocess, 'Popen', side_effect=lambda *a, **kw: observed.append((a, kw, Path.cwd()))):
            self.assertEqual(self.call(), 0)
        command.assert_called_once_with('--sandbox-agent', '--channel', str(self.channel))
        self.assertEqual(observed[0][1]['cwd'], str(self.private))
        self.assertEqual(observed[0][2], self.private)
        self.assertFalse(observed[0][1].get('shell', False))
        self.assertEqual(Path.cwd(), self.old_cwd)

    def test_current_live_service_does_not_launch_or_initialize_data(self):
        self.service()
        with patch.object(launcher.subprocess, 'Popen') as process:
            self.assertEqual(self.call(), 0)
        process.assert_not_called()
        self.assertFalse(self.private.exists())

    def test_old_live_service_is_rejected_before_spawn(self):
        self.service(app_version='1.3.14')
        with patch.object(launcher.subprocess, 'Popen') as process:
            with self.assertRaises(ProtocolError):
                self.call()
        process.assert_not_called()

    def test_valid_stale_or_stopped_service_allows_new_agent(self):
        for extra in ({'heartbeat': time.time() - 30}, {'active': False}):
            self.service(**extra)
            with patch.object(launcher.subprocess, 'Popen') as process:
                self.assertEqual(self.call(), 0)
            process.assert_called_once()

    def test_invalid_signature_or_shape_is_not_treated_as_stale(self):
        self.service(heartbeat=time.time() - 30)
        path = self.channel / 'service.json'
        value = json.loads(path.read_text('utf-8'))
        value['signature'] = '00' * 32
        path.write_text(json.dumps(value), 'utf-8')
        with patch.object(launcher.subprocess, 'Popen') as process:
            with self.assertRaises(ProtocolError):
                self.call()
        process.assert_not_called()
        self.service(heartbeat='invalid')
        with patch.object(launcher.subprocess, 'Popen') as process:
            with self.assertRaises(ProtocolError):
                self.call()
        process.assert_not_called()

    def test_relative_or_shared_data_root_is_rejected(self):
        for local in ('relative/path', str(self.channel)):
            with patch.dict(os.environ, {'LOCALAPPDATA': local}), \
                 patch.object(launcher.subprocess, 'Popen') as process:
                with self.assertRaises(ProtocolError):
                    self.call()
            process.assert_not_called()

    def test_frozen_program_directory_cannot_become_private_data(self):
        app = self.root / 'mapped-program'
        app.mkdir()
        with patch.object(launcher.sys, 'frozen', True, create=True), \
             patch.object(launcher.sys, 'executable', str(app / 'WhaleGirlWeChat.exe')), \
             patch.dict(os.environ, {'LOCALAPPDATA': str(app)}):
            with self.assertRaises(ProtocolError):
                self.call()

    def test_frozen_launch_uses_the_same_exe_agent_flag_without_supervisor(self):
        executable = self.root / 'mapped-program' / 'WhaleGirlWeChat.exe'
        with patch.object(launcher.sys, 'frozen', True, create=True), \
             patch.object(launcher.sys, 'executable', str(executable)), \
             patch.object(launcher.subprocess, 'Popen') as process:
            self.assertEqual(self.call(), 0)
        self.assertEqual(process.call_args.args[0],
                         [str(executable), '--sandbox-agent', '--channel', str(self.channel)])

    def test_private_data_parent_cannot_be_a_link(self):
        self.private.parent.mkdir()
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == self.private.parent or original(path)):
            with self.assertRaises(ProtocolError):
                launcher._private_root(self.pairing)

    def test_host_identity_is_rejected_before_pairing_or_spawn(self):
        with patch.object(launcher, 'require_sandbox_user', side_effect=ProtocolError('host user')), \
             patch.object(launcher, 'load_pairing') as pairing, \
             patch.object(launcher.subprocess, 'Popen') as process:
            with self.assertRaises(ProtocolError):
                self.call()
        pairing.assert_not_called()
        process.assert_not_called()

    def test_agent_logging_and_lazy_service_run_in_private_cwd(self):
        observed = []
        kernel = SimpleNamespace(CreateMutexW=Mock(return_value=42), CloseHandle=Mock(), ReleaseMutex=Mock())
        with self.mutex_api(kernel, 0), \
             patch.object(launcher, 'setup_logging', side_effect=lambda root: observed.append(('log', root, Path.cwd()))), \
             patch.object(launcher, 'serve', side_effect=lambda channel, root: observed.append(('serve', root, Path.cwd())) or 17):
            self.assertEqual(self.call('--sandbox-agent'), 17)
        self.assertEqual(observed, [('log', self.private / 'logs', self.private),
                                    ('serve', self.private, self.private)])
        kernel.ReleaseMutex.assert_called_once_with(42)
        kernel.CloseHandle.assert_called_once_with(42)
        self.assertEqual(Path.cwd(), self.old_cwd)

    def test_duplicate_mutex_closes_only_own_handle_without_serving(self):
        kernel = SimpleNamespace(CreateMutexW=Mock(return_value=42), CloseHandle=Mock(), ReleaseMutex=Mock())
        with self.mutex_api(kernel, 183), \
             patch.object(launcher, 'setup_logging') as setup, patch.object(launcher, 'serve') as serve:
            self.assertEqual(self.call('--sandbox-agent'), 0)
        setup.assert_not_called()
        serve.assert_not_called()
        kernel.ReleaseMutex.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(42)

    def test_mutex_creation_failure_is_reported_instead_of_fake_success(self):
        kernel = SimpleNamespace(CreateMutexW=Mock(return_value=None), CloseHandle=Mock(), ReleaseMutex=Mock())
        with self.mutex_api(kernel, 5), \
             patch.object(launcher, 'serve') as serve:
            with self.assertRaises(ProtocolError):
                self.call('--sandbox-agent')
        serve.assert_not_called()
        kernel.CloseHandle.assert_not_called()

    def test_agent_failure_releases_mutex_and_restores_caller_cwd(self):
        kernel = SimpleNamespace(CreateMutexW=Mock(return_value=42), CloseHandle=Mock(), ReleaseMutex=Mock())
        with self.mutex_api(kernel, 0), \
             patch.object(launcher, 'setup_logging'), patch.object(launcher, 'serve', side_effect=RuntimeError('mock failure')):
            with self.assertRaisesRegex(RuntimeError, 'mock failure'):
                self.call('--sandbox-agent')
        kernel.ReleaseMutex.assert_called_once_with(42)
        kernel.CloseHandle.assert_called_once_with(42)
        self.assertEqual(Path.cwd(), self.old_cwd)

    def test_invalid_mode_has_no_pairing_or_process_effect(self):
        for argv in ([], ['--unknown']):
            with patch.object(launcher, 'load_pairing') as pairing, \
                 patch.object(launcher.subprocess, 'Popen') as process:
                with self.assertRaises(ProtocolError):
                    launcher.main(argv)
            pairing.assert_not_called()
            process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
