"""1.3.39：读不到的旧通道目录不再阻断准备；能读到却结构不符仍整体失败。"""
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI

from src import sandbox_api as api
from src import sandbox_manager as manager
from src.runtime_identity import APP_VERSION

SANDBOX = 'd4254f07-930a-4602-ac01-96d6adffbf4a'
BROKEN = '5d78451fff6f4f3b987e6b84525b983e'
OTHER = '8ed4ba6a574a4aac807b06cc82986983'
CHANNEL = 'a' * 32


class PrepareSkippedChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'profile'
        self.root.mkdir()
        self.app = Path(self.temp.name) / 'application'
        self.app.mkdir()
        (self.app / 'WhaleGirlWeChat.exe').write_bytes(b'offline executable fixture')
        (self.app / '_internal').mkdir()
        (self.app / '_internal' / 'dependency.bin').write_bytes(b'packaged dependency')
        self.commands = []
        self.patches = [
            patch.object(manager, 'find_wsb', return_value=Path(self.temp.name) / 'wsb.exe'),
            patch.object(manager.subprocess, 'run', side_effect=self.fake_cli),
            patch.object(manager, 'packaged_version', return_value=APP_VERSION),
        ]
        # start() 返回的是替身本身，stop() 必须落在 _patcher 上；存替身会让补丁永久生效。
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def fake_cli(self, command, **kwargs):
        self.commands.append(command)
        self.assertFalse(kwargs.get('shell', False))
        if command[1] == 'list':
            value = {'WindowsSandboxEnvironments': [{'Id': SANDBOX}]}
        elif command[1] == 'share':
            value = None
        else:
            value = {'ExitCode': 0}
        output = '' if value is None else json.dumps(value)
        kwargs['stdout'].write(output.encode('utf-8'))
        kwargs['stdout'].flush()
        return subprocess.CompletedProcess(command, 0, None, None)

    def prepare(self):
        return manager.prepare_sandbox(self.root, self.app, SANDBOX)

    def channels(self):
        return self.root / 'data' / 'isolated_wechat'

    def readable_channel(self, ident):
        """建一条结构完整但可以任意改写的旧通道目录。"""
        path = self.channels() / ident
        path.mkdir(parents=True)
        (path / 'pairing.json').write_text(json.dumps(
            {'version': 1, 'channel_id': ident, 'secret': '0' * 64}), 'utf-8')
        return path

    def unreadable_channel(self, ident, point):
        """模拟 1.3.18 遗留的 ACL 损坏目录：目录能列出，读取一律拒绝访问。"""
        if point == 'channel_path':
            (self.channels() / ident).mkdir(parents=True)
            original = manager.channel_path

            def channel_path(root, channel_id):
                if channel_id == ident:
                    raise PermissionError(13, '拒绝访问', str(root))
                return original(root, channel_id)
            return patch.object(manager, 'channel_path', side_effect=channel_path)
        original = manager._read_small

        def read_small(path, limit=8192):
            if Path(path).parent.name == ident:
                raise PermissionError(13, '拒绝访问', str(path))
            return original(path, limit)
        return patch.object(manager, '_read_small', side_effect=read_small)

    def test_unreadable_channel_directories_are_skipped_and_reported(self):
        self.readable_channel(OTHER)  # 只有 session.json 拒绝访问，目录与配对本身可读
        with self.unreadable_channel(BROKEN, 'channel_path'), \
                self.unreadable_channel(OTHER, '_read_small'):
            result = self.prepare()
        self.assertTrue(result['success'])
        self.assertEqual(sorted(result['skipped_channels']), sorted((BROKEN, OTHER)))
        self.assertNotIn(result['channel_id'], (BROKEN, OTHER))
        self.assertTrue((self.channels() / result['channel_id'] / 'session.json').is_file())
        shares = [c for c in self.commands if c[1] == 'share']
        self.assertEqual(len(shares), 2)
        self.assertIn(str(self.channels() / result['channel_id']), shares[1])

    def test_readable_invalid_metadata_still_aborts_every_mapping(self):
        path = self.readable_channel(BROKEN)
        for value, text in ((2, 'version 2'), (True, 'version true')):
            with self.subTest(case=text):
                session = {'version': value, 'channel_id': BROKEN, 'sandbox_id': SANDBOX,
                           'app_version': APP_VERSION, 'stage': 'prepared'}
                (path / 'session.json').write_text(json.dumps(session), 'utf-8')
                with self.assertRaisesRegex(manager.SandboxError, '元数据无法核验'):
                    self.prepare()
                self.assertFalse([c for c in self.commands if c[1] == 'share'])

    def test_readable_channel_of_another_identity_still_aborts(self):
        path = self.readable_channel(BROKEN)
        session = {'version': 1, 'channel_id': OTHER, 'sandbox_id': SANDBOX,
                   'app_version': APP_VERSION, 'stage': 'prepared'}
        (path / 'session.json').write_text(json.dumps(session), 'utf-8')
        with self.assertRaisesRegex(manager.SandboxError, '元数据无法核验'):
            self.prepare()
        self.assertFalse([c for c in self.commands if c[1] == 'share'])

    def test_prepare_without_bad_channel_reports_empty_skipped_list(self):
        result = self.prepare()
        self.assertEqual(result['skipped_channels'], [])

    def test_broken_foreign_channel_does_not_disturb_the_target_pair(self):
        first = self.prepare()
        original = (self.channels() / first['channel_id'] / 'session.json').read_bytes()
        with self.unreadable_channel(BROKEN, 'channel_path'):
            second = self.prepare()
        self.assertEqual(second['channel_id'], first['channel_id'])
        self.assertEqual(second['skipped_channels'], [BROKEN])
        self.assertEqual(
            (self.channels() / first['channel_id'] / 'session.json').read_bytes(), original)
        self.assertEqual(sorted(item.name for item in self.channels().iterdir()),
                         sorted((first['channel_id'], BROKEN)))

    def test_recovery_entry_still_fails_closed_on_unreadable_channel(self):
        with self.unreadable_channel(BROKEN, 'channel_path'):
            with self.assertRaisesRegex(manager.SandboxError, '元数据无法核验'):
                manager._existing_pairs_for_prepare(
                    self.root, SANDBOX, {SANDBOX})


class PrepareApiSkippedChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = SimpleNamespace(root=Path(self.temp.name), lock=asyncio.Lock(),
            config={'wechat.environment': 'local', 'wechat.sandbox_channel_id': ''},
            running=False, note=Mock())
        self.result = {'success': True, 'channel_id': CHANNEL, 'sandbox_id': SANDBOX,
            'stage': 'prepared', 'message': '已准备', 'channel_dir': r'C:\private'}
        patches = [
            patch.object(api, 'require_bound', return_value={'revision': 1}),
            patch.object(api.manager, 'prepare_sandbox', side_effect=lambda *args: self.result),
        ]
        self.patches = [item.start() for item in patches]
        for item in reversed(patches):
            self.addCleanup(item.stop)
        app = FastAPI()
        app.state.bot = self.bot
        api.add_routes(app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url='http://testserver')

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    async def test_response_exposes_skipped_channels(self):
        self.result['skipped_channels'] = [BROKEN]
        result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['skipped_channels'], [BROKEN])
        self.assertNotIn('private', result.text)

    async def test_response_without_skipped_list_stays_unchanged(self):
        result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX})
        self.assertEqual(set(result.json()),
            {'success', 'channel_id', 'sandbox_id', 'stage', 'message'})


if __name__ == '__main__':
    unittest.main()
