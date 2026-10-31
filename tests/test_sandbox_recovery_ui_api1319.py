"""修复入口沿用准备的权限和取消保护，不能顺带启动或启用机器人。"""
import asyncio
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from src import sandbox_api as api
import test_sandbox_api1315 as existing_api
import test_sandbox_panel1315 as existing_panel

SANDBOX = existing_api.SANDBOX


class RecoveryApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = existing_api.SandboxApiTests.asyncSetUp
    asyncTearDown = existing_api.SandboxApiTests.asyncTearDown

    async def test_recovery_uses_separate_action_and_does_not_expose_private_result(self):
        with patch.object(api.manager, 'recover_sandbox', return_value=self.prepared) as recover:
            result = await self.client.post('/api/sandbox/recover', json={'id': SANDBOX})
        self.assertEqual(result.status_code, 200)
        recover.assert_called_once()
        self.assertEqual(recover.call_args.args[0], self.bot.root)
        self.assertEqual(recover.call_args.args[2], SANDBOX)
        self.prepare_mock.assert_not_called()
        self.assertNotIn('secret', result.text)
        self.assertNotIn('private', result.text)
        self.assertEqual(self.switches, [])
        self.assertFalse(self.bot.running)
        self.assertFalse(self.bot._sandbox_preparing)

    async def test_recovery_requires_bound_actor_and_valid_selected_id(self):
        with patch.object(api.manager, 'recover_sandbox') as recover:
            self.bound = False
            result = await self.client.post('/api/sandbox/recover', json={'id': SANDBOX})
            self.assertEqual(result.status_code, 403)
            self.bound = True
            result = await self.client.post('/api/sandbox/recover', json={'id': SANDBOX, 'command': 'no'})
            self.assertEqual(result.status_code, 422)
            recover.assert_not_called()

    async def test_recovery_rejects_running_or_reconnecting_even_in_local_environment(self):
        with patch.object(api.manager, 'recover_sandbox') as recover:
            for field in ('running', 'connecting', 'reconnecting'):
                with self.subTest(field=field):
                    setattr(self.bot, field, True)
                    result = await self.client.post('/api/sandbox/recover', json={'id': SANDBOX})
                    self.assertEqual(result.status_code, 409)
                    setattr(self.bot, field, False)
                    self.assertFalse(self.bot._sandbox_preparing)
            recover.assert_not_called()

    async def test_recovery_keeps_runtime_lock_and_busy_gate_during_slow_mapping(self):
        entered, release = threading.Event(), threading.Event()
        def recover(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('worker not released')
            return self.prepared
        with patch.object(api.manager, 'recover_sandbox', side_effect=recover):
            task = asyncio.create_task(self.client.post('/api/sandbox/recover', json={'id': SANDBOX}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assertTrue(self.bot.lock.locked())
                self.assertTrue(self.bot._sandbox_preparing)
                result = await self.client.post('/api/sandbox/prepare', json={'id': SANDBOX})
                self.assertEqual(result.status_code, 409)
                result = await self.client.post('/api/sandbox/deactivate', json={})
                self.assertEqual(result.status_code, 409)
            finally:
                release.set()
            self.assertEqual((await task).status_code, 200)
        self.assertFalse(self.bot.lock.locked())

    async def test_cancelled_recovery_keeps_gate_until_worker_and_stops_only_own_agent(self):
        entered, release = threading.Event(), threading.Event()
        def recover(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('worker not released')
            return self.prepared
        with patch.object(api.manager, 'recover_sandbox', side_effect=recover):
            task = asyncio.create_task(self.client.post('/api/sandbox/recover', json={'id': SANDBOX}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(self.bot._sandbox_preparing)
            finally:
                release.set()
            with self.assertRaises(HTTPException) as caught:
                await self.bot._sandbox_prepare_task
        self.assertEqual(caught.exception.status_code, 409)
        self.stop_mock.assert_called_once()
        self.assertFalse(self.bot._sandbox_preparing)
        self.assertEqual(self.switches, [])


class RecoveryPanelTests(unittest.TestCase):
    setUpClass = classmethod(existing_panel.SandboxPanelTests.setUpClass.__func__)
    setUp = existing_panel.SandboxPanelTests.setUp
    tearDown = existing_panel.SandboxPanelTests.tearDown
    deliver = existing_panel.SandboxPanelTests.deliver

    def failed_pair(self):
        self.deliver({'success': True, 'sandboxes': [{'id': SANDBOX}], 'pairs': [{
            'channel_id': existing_api.CHANNEL, 'sandbox_id': SANDBOX, 'stage': 'failed',
            'agent_online': False, 'reason': '配对未完成'}]})

    def test_failed_pair_exposes_explicit_repair_and_never_activates_or_starts(self):
        self.failed_pair()
        self.assertTrue(self.dialog.recover_button.isEnabled())
        self.assertFalse(self.dialog.activate_button.isEnabled())
        self.dialog.recover()
        args, kwargs = self.calls[0]
        self.assertEqual(args[1:4], ('POST', 'sandbox/recover', {'id': SANDBOX}))
        self.assertEqual(kwargs['timeout'], 180)
        self.assertFalse(self.dialog.recover_button.isEnabled())
        self.dialog.recover()
        self.assertEqual(len(self.calls), 1)
        self.deliver({'success': True, 'message': '修复成功，尚未启用'})
        self.assertIn('尚未启用', self.dialog.status.text())
        self.assertEqual(self.calls[0][0][1:3], ('GET', 'sandbox/status'))

    def test_missing_or_healthy_pair_never_sends_repair(self):
        for stage in (None, 'prepared', 'agent_ready', 'ready', 'sandbox_closed'):
            with self.subTest(stage=stage):
                self.dialog.refresh()
                self.deliver({'success': True, 'sandboxes': [{'id': SANDBOX}], 'pairs': [] if stage is None else [{
                    'channel_id': existing_api.CHANNEL, 'sandbox_id': SANDBOX, 'stage': stage}]})
                self.assertFalse(self.dialog.recover_button.isEnabled())
                self.dialog.recover()
                self.assertEqual(self.calls, [])

    def test_failed_refresh_invalidates_old_repair_candidate(self):
        self.failed_pair()
        self.dialog.refresh()
        self.deliver({'success': False, 'message': '读取失败'})
        self.assertFalse(self.dialog.recover_button.isEnabled())
        self.dialog.recover()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
