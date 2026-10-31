"""沙盒准备/切换互斥与界面真实就绪状态；仅假 API 和离线 Qt。"""
import asyncio
import os
import threading
import unittest
import json
from pathlib import Path

from fastapi import HTTPException

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication

from src.gui.sandbox_panel import SandboxDialog
from tests import test_sandbox_api1315 as api_fixtures
from tests import test_sandbox_manager1315 as manager_fixtures
from src import sandbox_manager as manager

SANDBOX = api_fixtures.SANDBOX
CHANNEL = api_fixtures.CHANNEL


class SandboxConfigApiAuditTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = api_fixtures.SandboxApiTests.asyncSetUp
    asyncTearDown = api_fixtures.SandboxApiTests.asyncTearDown

    async def test_prepare_blocks_both_environment_switches_without_changing_configuration(self):
        entered, release = threading.Event(), threading.Event()
        def prepare(*_args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test preparation was not released')
            return self.prepared
        self.prepare_mock.side_effect = prepare
        task = asyncio.create_task(self.client.post('/api/sandbox/prepare', json={'id': SANDBOX}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            for action, payload in (('activate', {'channel_id': CHANNEL}), ('deactivate', {})):
                with self.subTest(action=action):
                    result = await self.client.post('/api/sandbox/' + action, json=payload)
                    self.assertEqual(result.status_code, 409)
            self.assertEqual(self.switches, [])
            self.assertEqual(self.bot.config['wechat.environment'], 'local')
            self.stop_mock.assert_not_called()
        finally:
            release.set()
            await task

    async def test_prepare_must_not_upgrade_the_sandbox_that_is_running_or_connecting(self):
        self.bot.config.update({'wechat.environment': 'sandbox', 'wechat.sandbox_id': SANDBOX})
        for state in ('running', 'connecting'):
            with self.subTest(state=state):
                self.bot.running = state == 'running'
                self.bot.connecting = state == 'connecting'
                result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX.upper()})
                self.assertEqual(result.status_code, 409)
                self.assertIn('停止', result.json()['detail'])
                self.assertFalse(getattr(self.bot, '_sandbox_preparing', False))
        self.prepare_mock.assert_not_called()
        self.stop_mock.assert_not_called()

    async def test_stopped_current_sandbox_preparation_holds_existing_start_switch_lock(self):
        self.bot.config.update({'wechat.environment': 'sandbox', 'wechat.sandbox_id': SANDBOX})
        entered, release = threading.Event(), threading.Event()
        def prepare(*_args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test preparation was not released')
            return self.prepared
        self.prepare_mock.side_effect = prepare
        task = asyncio.create_task(self.client.post('/api/sandbox/prepare', json={'id': SANDBOX}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertTrue(self.bot.lock.locked())
        finally:
            release.set()
            response = await task
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.bot.lock.locked())

    async def test_preparing_another_sandbox_preserves_a_running_local_bot_and_stop_lock(self):
        self.bot.running = True
        observed = []
        def prepare(*_args):
            observed.append(self.bot.lock.locked())
            return self.prepared
        self.prepare_mock.side_effect = prepare
        result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(self.bot.running)
        self.assertEqual(observed, [False])
        self.assertEqual(self.switches, [])

    async def test_prepare_that_begins_during_activation_invalidates_the_switch_before_commit(self):
        entered, release = threading.Event(), threading.Event()
        def status(*_args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test status was not released')
            return self.pair_status
        self.status_mock.side_effect = status
        switch = asyncio.create_task(self.client.post('/api/sandbox/activate', json={'channel_id': CHANNEL}))
        preparing = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            preparing = asyncio.create_task(self.client.post('/api/sandbox/prepare', json={'id': SANDBOX}))
            for _ in range(20):
                if getattr(self.bot, '_sandbox_preparing', False):
                    break
                await asyncio.sleep(.005)
            self.assertTrue(self.bot._sandbox_preparing)
        finally:
            release.set()
            switched = await switch
            if preparing is not None:
                prepared = await preparing
        self.assertEqual(switched.status_code, 409)
        self.assertEqual(prepared.status_code, 200)
        self.assertEqual(self.switches, [])

    async def test_cancelled_prepare_waiting_for_start_lock_never_creates_or_stops_an_agent(self):
        await self.bot.lock.acquire()
        task = asyncio.create_task(self.client.post('/api/sandbox/prepare', json={'id': SANDBOX}))
        try:
            for _ in range(20):
                if getattr(self.bot, '_sandbox_prepare_task', None) is not None:
                    break
                await asyncio.sleep(.005)
            self.assertTrue(self.bot._sandbox_preparing)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            self.bot.lock.release()
        with self.assertRaises(HTTPException) as caught:
            await self.bot._sandbox_prepare_task
        self.assertEqual(caught.exception.status_code, 409)
        self.prepare_mock.assert_not_called()
        self.stop_mock.assert_not_called()
        self.assertFalse(self.bot._sandbox_preparing)


class SandboxConfigPanelAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        self.dialog = SandboxDialog(lambda *args, **kwargs: self.calls.append((args, kwargs)))
        self.dialog._poll.stop()

    def tearDown(self):
        self.dialog.close()

    def test_online_flag_cannot_enable_a_mismatched_unknown_or_failed_build(self):
        self.calls.pop()[1]['on_done']({'success': True, 'sandboxes': [{'id': SANDBOX}], 'pairs': []})
        for stage in ('build_mismatch', 'prepared', 'unknown_future_stage', 'failed', 'version_mismatch'):
            with self.subTest(stage=stage):
                self.dialog._pairs = [{'channel_id': CHANNEL, 'sandbox_id': SANDBOX,
                                      'stage': stage, 'agent_online': True}]
                self.dialog._selection_changed()
                self.assertFalse(self.dialog.activate_button.isEnabled())
                self.dialog.activate()
                self.assertEqual(self.calls, [])


class SandboxConfigMetadataAuditTests(unittest.TestCase):
    setUp = manager_fixtures.SandboxManagerTests.setUp
    tearDown = manager_fixtures.SandboxManagerTests.tearDown
    fake_cli = manager_fixtures.SandboxManagerTests.fake_cli
    prepare = manager_fixtures.SandboxManagerTests.prepare
    service = manager_fixtures.SandboxManagerTests.service

    def test_valid_legacy_heartbeat_cannot_turn_a_malformed_session_into_a_ready_candidate(self):
        result = self.prepare()
        channel = Path(result['channel_dir'])
        self.service(channel)
        session_path = channel / 'session.json'
        original = json.loads(session_path.read_text('utf-8'))
        self.assertEqual(len(manager.paired_status(self.root)['pairs']), 1)
        for broken in ({'version': True}, {'version': 2}, {'program_key': 'broken'},
                       {'program_mapping': 'unexpected'}):
            with self.subTest(broken=broken):
                session_path.write_text(json.dumps(dict(original, **broken)), 'utf-8')
                self.assertEqual(manager.paired_status(self.root)['pairs'], [])
