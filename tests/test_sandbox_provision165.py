"""1.3.65 Windows 沙盒自动启用 + 隔离通道创建：单元测试。

全部用替身：不跑真实 dism、不弹 UAC、不改本机功能状态。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src import sandbox_provision as provision

ENABLED_OUT = ('部署映像服务和管理工具' + chr(13) + chr(10) + '功能信息:' + chr(13) + chr(10) +
               '功能名称 : Containers-DisposableClientVM' + chr(13) + chr(10) + '状态 : 已启用')
DISABLED_OUT = ('部署映像服务和管理工具' + chr(13) + chr(10) + '功能信息:' + chr(13) + chr(10) +
                '功能名称 : Containers-DisposableClientVM' + chr(13) + chr(10) + '状态 : 已禁用')
ABSENT_OUT = '错误: 0x800f0805' + chr(13) + chr(10) + '功能名称未知。'
PRO_EDITION = {'EditionID': 'Professional'}
HOME_EDITION = {'EditionID': 'Core'}


def make_runner(stdout='', returncode=0, stderr='', sink=None):
    """统一替身签名：fn(cmd:list[str], timeout:float) -> dict。"""
    def runner(cmd, timeout):
        if sink is not None:
            sink.append(([str(part) for part in cmd], float(timeout)))
        return {'returncode': returncode, 'stdout': stdout, 'stderr': stderr}
    return runner


def feature_runner(enable_code, sink=None):
    """探测返回“已禁用”，启用命令返回指定退出码。"""
    def runner(cmd, timeout):
        text = ' '.join(str(part) for part in cmd)
        if sink is not None:
            sink.append((text, float(timeout)))
        if 'get-featureinfo' in text:
            return {'returncode': 0, 'stdout': DISABLED_OUT, 'stderr': ''}
        return {'returncode': enable_code, 'stdout': '完成', 'stderr': ''}
    return runner


class FakeElevation:
    def __init__(self, elevated=False, started=True):
        self.elevated = elevated
        self.started = started
        self.calls = []

    def is_elevated(self):
        return self.elevated

    def start(self, executable, arguments, workdir=''):
        self.calls.append((str(executable), str(arguments), str(workdir)))
        return {'started': self.started, 'code': 42, 'message': '已弹出管理员授权窗口'}


class FakeManager:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {
            'success': True, 'sandbox_id': 'sb-test-1', 'channel_id': 'chan-42',
            'stage': 'prepared', 'message': '隔离通道已准备好'}
        self.error = error
        self.calls = []

    def list_sandboxes(self):
        return [{'id': 'sb-test-1'}]

    def prepare_sandbox(self, root, app_dir, sandbox_id):
        self.calls.append((root, app_dir, sandbox_id))
        if self.error is not None:
            raise self.error
        return self.result


class ProvisionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_log(self, text, name='enable.log'):
        path = self.tmp / name
        path.write_text(text, encoding='utf-8')
        return path

    def write_record(self, payload):
        provision._write_record(self.tmp / provision.RECORD_NAME, payload)
        return self.tmp / provision.RECORD_NAME

    def patched_defaults(self):
        return mock.patch.multiple(
            provision,
            _read_edition=mock.Mock(return_value='Professional'),
            _default_log_dir=mock.Mock(return_value=self.tmp),
        )


class DetectTests(ProvisionCase):
    def test_enabled(self):
        result = provision.detect(runner=make_runner(ENABLED_OUT), registry_reader=PRO_EDITION)
        self.assertEqual(result['state'], 'enabled')
        self.assertEqual(result['feature'], 'Enabled')
        self.assertTrue(result['supported'])
        self.assertEqual(result['next_step'], 'launch')
        self.assertIn('已启用', result['message'])

    def test_disabled(self):
        result = provision.detect(runner=make_runner(DISABLED_OUT), registry_reader=PRO_EDITION)
        self.assertEqual(result['state'], 'disabled')
        self.assertEqual(result['feature'], 'Disabled')
        self.assertEqual(result['next_step'], 'enable')
        self.assertIn('关闭', result['message'])

    def test_absent(self):
        result = provision.detect(runner=make_runner(ABSENT_OUT, 0x800F0805),
                                  registry_reader=PRO_EDITION)
        self.assertEqual(result['state'], 'absent')
        self.assertEqual(result['feature'], 'Absent')
        self.assertEqual(result['next_step'], 'enable')

    def test_unsupported_home(self):
        result = provision.detect(runner=make_runner(ENABLED_OUT), registry_reader=HOME_EDITION)
        self.assertEqual(result['state'], 'unsupported')
        self.assertFalse(result['supported'])
        self.assertEqual(result['next_step'], 'none')
        self.assertIn('家庭版', result['message'])
        self.assertIn('替代方案', result['hint'])

    def test_unknown_fallback_without_admin(self):
        runner = make_runner('', 740, '需要提升权限才能运行 DISM')
        result = provision.detect(runner=runner, registry_reader=PRO_EDITION)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['next_step'], 'enable')
        self.assertEqual(result['reason'], 'dism_unavailable')
        self.assertTrue(result['hint'])

    def test_never_raises(self):
        def boom_runner(cmd, timeout):
            raise RuntimeError('runner 爆炸')

        def boom_registry():
            raise OSError('注册表读不了')

        result = provision.detect(runner=boom_runner, registry_reader=boom_registry)
        self.assertIn(result['state'], ('unknown', 'disabled', 'absent'))
        self.assertEqual(result['next_step'], 'enable')
        self.assertTrue(result['message'])
        self.assertIsInstance(result['hint'], str)


class EnableTests(ProvisionCase):
    def test_already_enabled_is_idempotent(self):
        elevation = FakeElevation()
        result = provision.enable(runner=make_runner(ENABLED_OUT), elevation=elevation,
                                  log_dir=self.tmp)
        self.assertEqual(result['state'], 'enabled')
        self.assertTrue(result['already'])
        self.assertEqual(elevation.calls, [])

    def test_unsupported(self):
        with mock.patch.object(provision, '_read_edition', return_value='Core'):
            result = provision.enable(runner=make_runner(DISABLED_OUT),
                                      elevation=FakeElevation(), log_dir=self.tmp)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(result['reason'], 'unsupported_edition')

    def test_uac_started_once(self):
        elevation = FakeElevation()
        runner = make_runner(DISABLED_OUT)
        first = provision.enable(runner=runner, elevation=elevation, log_dir=self.tmp)
        self.assertEqual(first['state'], 'elevation_started')
        self.assertTrue(first['log_path'].endswith('.log'))
        second = provision.enable(runner=runner, elevation=elevation, log_dir=self.tmp)
        self.assertEqual(second['state'], 'elevation_started')
        self.assertTrue(second.get('already'))
        self.assertEqual(len(elevation.calls), 1)
        record = provision._read_record(self.tmp / provision.RECORD_NAME)
        self.assertEqual(record['state'], 'elevation_started')

    def test_elevated_success(self):
        result = provision.enable(runner=feature_runner(0), elevation=FakeElevation(True),
                                  log_dir=self.tmp)
        self.assertEqual(result['state'], 'enabled')
        self.assertFalse(result['already'])
        self.assertEqual(result['exit_code'], 0)

    def test_elevated_needs_reboot(self):
        result = provision.enable(runner=feature_runner(3010), elevation=FakeElevation(True),
                                  log_dir=self.tmp)
        self.assertEqual(result['state'], 'pending_reboot')
        self.assertTrue(result['needs_reboot'])

    def test_elevated_failure_explained(self):
        result = provision.enable(runner=feature_runner(87), elevation=FakeElevation(True),
                                  log_dir=self.tmp)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['exit_code'], 87)
        self.assertIn('参数错误', result['message'])
        self.assertIn('0x00000057', result['message'])


class CheckElevationResultTests(ProvisionCase):
    def test_success(self):
        path = self.write_log('dism 输出若干' + chr(10) + provision.EXIT_MARKER + ' 0' + chr(10))
        result = provision.check_elevation_result(str(path))
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'enabled')

    def test_needs_reboot(self):
        path = self.write_log(provision.EXIT_MARKER + ' 3010')
        result = provision.check_elevation_result(str(path))
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'pending_reboot')
        self.assertTrue(result['needs_reboot'])

    def test_failed(self):
        path = self.write_log(provision.EXIT_MARKER + ' 50')
        result = provision.check_elevation_result(str(path))
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'failed')
        self.assertIn('不支持', result['message'])

    def test_log_missing(self):
        result = provision.check_elevation_result(str(self.tmp / 'nope.log'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['reason'], 'log_missing')
        self.assertIn('授权', result['message'])


class StatusTests(ProvisionCase):
    def test_enabled_next_step_create_channel(self):
        result = provision.status(runner=make_runner(ENABLED_OUT), registry_reader=PRO_EDITION,
                                  log_dir=self.tmp)
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'enabled')
        self.assertEqual(result['next_step'], 'create_channel')

    def test_pending_reboot_still_allows_channel(self):
        self.write_record({'state': 'pending_reboot', 'log_path': '', 'updated': time.time()})
        result = provision.status(runner=make_runner(ENABLED_OUT), registry_reader=PRO_EDITION,
                                  log_dir=self.tmp)
        self.assertTrue(result['needs_reboot'])
        self.assertEqual(result['next_step'], 'create_channel')
        self.assertIn('重启', result['message'])

    def test_rebooted_after_record_clears_pending_flag(self):
        # 记录写着“待重启”，但电脑在那之后已经重启过：标记必须自己失效，否则面板永远卡住。
        self.write_record({'state': 'pending_reboot', 'log_path': '', 'updated': 1.0})
        result = provision.status(runner=make_runner(ENABLED_OUT), registry_reader=PRO_EDITION,
                                  log_dir=self.tmp)
        self.assertFalse(result['needs_reboot'])
        self.assertEqual(result['next_step'], 'create_channel')
        self.assertNotIn('需要重启电脑后才生效', result['message'])

    def test_no_step_ever_asks_for_reboot_only(self):
        # 任何状态都不能把 next_step 设成 'reboot'：那会把启用和建通道两个按钮同时禁掉。
        for runner in (make_runner(ENABLED_OUT), make_runner(DISABLED_OUT),
                       make_runner(ABSENT_OUT), make_runner('', returncode=740)):
            self.write_record({'state': 'pending_reboot', 'log_path': '', 'updated': time.time()})
            result = provision.status(runner=runner, registry_reader=PRO_EDITION, log_dir=self.tmp)
            self.assertNotEqual(result['next_step'], 'reboot', result['state'])

    def test_disabled_next_step_enable(self):
        result = provision.status(runner=make_runner(DISABLED_OUT), registry_reader=PRO_EDITION,
                                  log_dir=self.tmp)
        self.assertEqual(result['state'], 'disabled')
        self.assertEqual(result['next_step'], 'enable')


class CreateChannelTests(ProvisionCase):
    def test_needs_enable(self):
        manager = FakeManager()
        with self.patched_defaults():
            result = provision.create_channel(None, manager=manager,
                                              runner=make_runner(DISABLED_OUT))
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'needs_enable')
        self.assertEqual(manager.calls, [])

    def test_needs_reboot(self):
        # 刚提交启用、还没重启：此时建通道会失败，先拦住。
        self.write_record({'state': 'pending_reboot', 'log_path': '', 'updated': time.time()})
        manager = FakeManager()
        with self.patched_defaults():
            result = provision.create_channel(None, manager=manager,
                                              runner=make_runner(ENABLED_OUT))
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'needs_reboot')
        self.assertEqual(result['reason'], 'needs_reboot')
        self.assertEqual(manager.calls, [])

    def test_stale_reboot_record_does_not_block_channel(self):
        # 记录是上次开机前写的：电脑已经重启过，就不该再拿它挡路。
        self.write_record({'state': 'pending_reboot', 'log_path': '', 'updated': 1.0})
        manager = FakeManager()
        with self.patched_defaults():
            result = provision.create_channel(None, manager=manager,
                                              runner=make_runner(ENABLED_OUT))
        self.assertTrue(result['ok'])
        self.assertEqual(len(manager.calls), 1)

    def test_success_normalised(self):
        manager = FakeManager()
        with self.patched_defaults():
            result = provision.create_channel(None, manager=manager,
                                              runner=make_runner(ENABLED_OUT))
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['sandbox_id'], 'sb-test-1')
        self.assertEqual(result['channel_id'], 'chan-42')
        self.assertEqual(result['stage'], 'prepared')
        self.assertEqual(len(manager.calls), 1)

    def test_manager_raises(self):
        manager = FakeManager(error=RuntimeError('通道目录写不了'))
        with self.patched_defaults():
            result = provision.create_channel(None, manager=manager,
                                              runner=make_runner(ENABLED_OUT))
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'internal')
        self.assertIn('意外错误', result['message'])


class RollbackTextTests(unittest.TestCase):
    def test_rollback_text(self):
        text = provision.rollback_text()
        self.assertIn('dism.exe /online /disable-feature', text)
        self.assertIn(provision.FEATURE_NAME, text)
        self.assertIn('/norestart', text)
        self.assertIn('管理员', text)


class FakeApiService:
    def __init__(self):
        self.enabled = 0

    def status(self):
        return {'ok': True, 'state': 'enabled', 'edition': '专业版',
                'next_step': 'create_channel', 'message': '已启用'}

    def enable(self):
        self.enabled += 1
        return {'state': 'enabled', 'already': True, 'message': '已启用'}

    def check(self, log_path=None):
        return {'ok': True, 'state': 'enabled', 'log_path': log_path or '',
                'message': '日志显示启用成功'}

    def create_channel(self):
        return {'ok': True, 'state': 'ready', 'channel_id': 'chan-42',
                'sandbox_id': 'sb-test-1', 'stage': 'prepared', 'message': '通道已准备'}

    def rollback_text(self):
        return provision.rollback_text()


class ApiTests(unittest.TestCase):
    def make_client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src import sandbox_provision_api as api

        app = FastAPI()
        self.service = FakeApiService()
        app.state.sandbox_provision_service = self.service
        api.register(app, None)
        return TestClient(app)

    def test_status(self):
        client = self.make_client()
        body = client.get('/api/sandbox/provision/status').json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['status']['state'], 'enabled')

    def test_enable(self):
        client = self.make_client()
        body = client.post('/api/sandbox/provision/enable').json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['status']['state'], 'started')

    def test_rollback(self):
        client = self.make_client()
        body = client.get('/api/sandbox/provision/rollback').json()
        self.assertTrue(body['ok'])
        self.assertIn('disable-feature', body['status']['text'])


class CheckFallbackTests(ProvisionCase):
    """没有提权日志时 check() 必须回到真实探测，不能把用户锁死在“请先点启用”里。"""

    def service(self, runner):
        from src import sandbox_provision_api
        return sandbox_provision_api.SandboxProvisionService(runner=runner, log_dir=self.tmp)

    def test_enabled_without_log_reports_ok(self):
        with self.patched_defaults():
            result = self.service(make_runner(ENABLED_OUT)).check()
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'enabled')
        self.assertNotIn('请先点', result['message'])
        self.assertIn('创建隔离通道', result['message'])

    def test_disabled_without_log_asks_to_enable(self):
        with self.patched_defaults():
            result = self.service(make_runner(DISABLED_OUT)).check()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'not_enabled')
        self.assertIn('启用 Windows 沙盒', result['message'])

    def test_unknown_without_log_keeps_enable_available(self):
        with self.patched_defaults():
            result = self.service(make_runner('', returncode=740)).check()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'dism_unavailable')
        self.assertEqual(result['next_step'], 'enable')

    def test_given_log_file_still_wins(self):
        path = self.write_log('完成' + chr(10) + provision.EXIT_MARKER + ' 0')
        with self.patched_defaults():
            result = self.service(make_runner(DISABLED_OUT)).check(str(path))
        self.assertTrue(result['ok'])
        self.assertEqual(result['exit_code'], 0)


def _load_panel():
    try:
        from src.gui.sandbox_provision_panel import SandboxProvisionPanel
    except Exception:
        return None
    return SandboxProvisionPanel


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel_class = _load_panel()
        if cls.panel_class is None:
            raise unittest.SkipTest('没有可用的 Qt 绑定')
        from PyQt6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def make_panel(self):
        return self.panel_class(FakeApiService(), autostart=False)

    def test_buttons_follow_enable_step(self):
        panel = self.make_panel()
        panel.apply_status({'state': 'disabled', 'edition': '专业版', 'next_step': 'enable',
                            'message': '未启用', 'hint': '点启用'})
        self.assertTrue(panel.enable_button.isEnabled())
        # “重新检测”是只读出口，任何状态都必须能点，否则用户会卡在“让你点启用但按钮是灰的”里。
        self.assertTrue(panel.recheck_button.isEnabled())
        self.assertFalse(panel.channel_button.isEnabled())
        self.assertTrue(panel.rollback_button.isEnabled())

    def test_channel_button_enabled_for_launch_step(self):
        panel = self.make_panel()
        panel.apply_status({'state': 'enabled', 'edition': '专业版', 'next_step': 'launch',
                            'message': '已启用', 'hint': '打开沙盒'})
        self.assertTrue(panel.channel_button.isEnabled())
        self.assertFalse(panel.enable_button.isEnabled())

    def test_buttons_follow_create_channel_step(self):
        panel = self.make_panel()
        panel.apply_status({'state': 'enabled', 'edition': '专业版',
                            'next_step': 'create_channel', 'message': '已启用',
                            'hint': '创建通道'})
        self.assertFalse(panel.enable_button.isEnabled())
        self.assertTrue(panel.channel_button.isEnabled())
        self.assertIn('已启用', panel.status_label.text())


if __name__ == '__main__':
    unittest.main()
