"""1.3.39：沙盒内安装微信的宿主侧路径——映射、客体脚本与轮询探针全部走替身。
真机安装路径已由用户在 d4254f07 上验证，本文件不执行任何 wsb 命令。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI, HTTPException

from src import sandbox_api as api
from src import sandbox_manager as manager

SANDBOX = 'd4254f07-930a-4602-ac01-96d6adffbf4a'
OTHER = '11111111-2222-3333-4444-555555555555'
INSTALLER = 'WeChatSetup-test.exe'


def _script(root):
    return Path(root) / 'data' / 'sandbox_install' / 'install.ps1'


class InstallClientTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        bundle = self.root / 'bundle'
        bundle.mkdir()
        self.installer = bundle / INSTALLER
        self.installer.write_bytes(b'MZ fake installer')
        self.calls = []
        self.present = False

    def fake_wsb(self, arguments, *, timeout=20):
        """探针在装好前回 7、装好后回 0；脚本执行本身只回退出码。"""
        self.calls.append(list(arguments))
        if arguments[0] == 'share':
            return {}
        if manager.CLIENT_PROBE in arguments[-1]:
            return {'ExitCode': 0 if self.present else 7}
        self.present = True
        return {'ExitCode': 0}

    def install(self, installer=None, ident=SANDBOX, fake=None):
        with patch.object(manager, 'list_sandboxes', return_value=[{'id': SANDBOX}]), \
                patch.object(manager, '_run_wsb', side_effect=fake or self.fake_wsb):
            return manager.install_client(self.root, ident,
                str(self.installer if installer is None else installer))

    def commands(self, kind):
        return [call for call in self.calls if call[0] == kind]

    def test_installs_and_returns_executable(self):
        result = self.install()
        self.assertTrue(result['success'])
        self.assertTrue(result['installed'])
        self.assertEqual(result['executable'], manager.CLIENT_EXECUTABLE)
        shares = self.commands('share')
        self.assertEqual(len(shares), 2)
        self.assertIn(str(self.installer.parent), shares[0])
        self.assertIn(manager.GUEST_INSTALL, shares[0])
        self.assertNotIn('--allow-write', shares[0])
        self.assertIn(manager.GUEST_OUT, shares[1])
        self.assertIn('--allow-write', shares[1])

    def test_already_installed_short_circuits_before_installer(self):
        self.present = True
        result = self.install(installer=self.root / 'missing.exe')
        self.assertTrue(result['installed'])
        self.assertEqual(result['reason'], 'already_installed')
        self.assertEqual(self.commands('share'), [])
        self.assertEqual([call for call in self.calls if 'install.ps1' in call[-1]], [])
        self.assertFalse(_script(self.root).exists())

    def test_missing_installer_reports_installer_missing(self):
        with self.assertRaises(manager.SandboxInstallError) as caught:
            self.install(installer=self.root / 'missing.exe')
        self.assertEqual(caught.exception.reason, 'installer_missing')
        self.assertEqual(self.commands('share'), [])

    def test_sandbox_not_running_reports_reason(self):
        with patch.object(manager, 'list_sandboxes', return_value=[{'id': OTHER}]):
            with self.assertRaises(manager.SandboxInstallError) as caught:
                manager.install_client(self.root, SANDBOX, str(self.installer))
        self.assertEqual(caught.exception.reason, 'sandbox_not_running')
        self.assertEqual(self.calls, [])

    def never_ready(self, arguments, *, timeout=20):
        self.calls.append(list(arguments))
        if arguments[0] == 'share':
            return {}
        return {'ExitCode': 7 if manager.CLIENT_PROBE in arguments[-1] else 0}

    def test_timeout_after_three_hundred_seconds(self):
        clock = [0.0]
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with patch.object(manager, 'list_sandboxes', return_value=[{'id': SANDBOX}]), \
                patch.object(manager, '_run_wsb', side_effect=self.never_ready), \
                patch.object(manager.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(manager.time, 'sleep', side_effect=sleep):
            with self.assertRaises(manager.SandboxInstallError) as caught:
                manager.install_client(self.root, SANDBOX, str(self.installer))
        self.assertEqual(caught.exception.reason, 'install_timeout')
        self.assertEqual(clock[0], manager.INSTALL_TIMEOUT_SECONDS)
        self.assertEqual(set(sleeps), {manager.INSTALL_POLL_SECONDS})
        self.assertEqual(len(sleeps), 60)

    def test_written_install_script_and_exec_command(self):
        self.assertTrue(self.install()['installed'])
        raw = _script(self.root).read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        text = raw.decode('utf-8-sig')
        self.assertTrue(text.isascii())
        self.assertIn("$ErrorActionPreference = 'Stop'", text)
        self.assertIn(manager.GUEST_INSTALL + '\\' + INSTALLER, text)
        self.assertIn("Start-Process -FilePath $installer -ArgumentList '/S'", text)
        self.assertIn(manager.GUEST_OUT + '\\install.log', text)
        self.assertIn("Add-Content -LiteralPath $log", text)
        runs = [call for call in self.calls if call[0] == 'exec' and 'install.ps1' in call[-1]]
        self.assertEqual(len(runs), 1)
        self.assertIn('-ExecutionPolicy Bypass -File ' + manager.GUEST_OUT + '\\install.ps1',
            runs[0][-1])


class InstallClientRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = SimpleNamespace(root=Path(self.temp.name), config={}, running=False,
            lock=asyncio.Lock(), note=Mock())
        patch.object(api, 'require_bound',
            side_effect=lambda bot: {'revision': 1, 'peer': 'local_console'}).start()
        self.addCleanup(patch.stopall)
        app = FastAPI()
        app.state.bot = self.bot
        api.add_routes(app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url='http://testserver')

    async def post(self, **kwargs):
        payload = {'id': SANDBOX, 'installer': r'C:\Downloads\WeChatSetup-4.1.8.exe'}
        payload.update(kwargs)
        return await self.client.post('/api/sandbox/install-client', json=payload)

    async def test_success_returns_install_result_and_clears_flag(self):
        with patch.object(api.manager, 'install_client',
                return_value={'success': True, 'installed': True, 'executable': 'x',
                    'message': '已装好'}):
            response = await self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['installed'], True)
        self.assertFalse(getattr(self.bot, '_sandbox_installing', False))

    async def test_failure_reports_stable_reason(self):
        with patch.object(api.manager, 'install_client',
                side_effect=manager.SandboxInstallError('install_timeout', '等待超时')):
            response = await self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
            {'success': False, 'installed': False, 'reason': 'install_timeout',
             'message': '等待超时'})
        self.assertFalse(getattr(self.bot, '_sandbox_installing', False))

    async def test_busy_flags_are_rejected(self):
        for flag, value in (('_sandbox_preparing', True), ('_sandbox_launching', True),
                ('_sandbox_installing', True)):
            setattr(self.bot, flag, value)
            with self.subTest(flag=flag):
                self.assertEqual((await self.post()).status_code, 409)
            delattr(self.bot, flag)

    async def asyncTearDown(self):
        await self.client.aclose()
