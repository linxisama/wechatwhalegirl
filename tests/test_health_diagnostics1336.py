"""Offline health diagnostics and logging failures; no service/UI/message access."""
import asyncio
from contextlib import redirect_stdout
import json
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import yaml

from src.wechat_app import WeChatRuntime
from tests import test_health_takeover1334 as fixtures


class BrokenWriter:
    def write(self, text):
        raise BrokenPipeError('private/path/closed-stdout')

    def flush(self):
        pass


class HealthDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def bot(self):
        return fixtures.HealthTakeoverTests().bot(alive=True)

    async def drive(self, bot, tick=None):
        sleeps = []
        async def sleep(seconds):
            sleeps.append(seconds)
            if getattr(bot, '_offline_ready', False):
                bot.running = False
            if len(sleeps) > 8:
                self.fail('offline bounded loop exceeded')
            if tick:
                tick(seconds)
        with patch('src.wechat_app.asyncio.sleep', side_effect=sleep):
            await WeChatRuntime._health(bot)
        return sleeps

    def recovery(self, bot, error):
        bot.bridge.is_receiving.side_effect = [error, True]
        async def ready():
            bot._offline_ready = True
        bot._refresh_background_readiness = AsyncMock(side_effect=ready)

    async def test_failure_is_visible_then_normal_check_recovers_and_retains_failure(self):
        bot = self.bot()
        self.recovery(bot, RuntimeError('private-message /private/path'))
        failed = []
        def tick(seconds):
            if seconds == 10:
                failed.append(WeChatRuntime.health_status(bot))
        self.assertEqual(await self.drive(bot, tick), [15, 10, 15, 15])
        self.assertTrue(failed[0]['task_alive'])
        self.assertEqual(failed[0]['check_state'], 'error')
        self.assertEqual(failed[0]['last_failure_type'], 'RuntimeError')
        current = WeChatRuntime.health_status(bot)
        self.assertEqual(current['check_state'], 'ok')
        self.assertGreaterEqual(current['last_check_at'], current['last_failure_at'])
        self.assertEqual(current['last_failure_type'], 'RuntimeError')
        self.assertEqual(current['last_failure_at'], failed[0]['last_failure_at'])
        exported = json.dumps(current) + str(bot.note.call_args_list)
        self.assertNotIn('private-message', exported)
        self.assertNotIn('/private/path', exported)
        self.assertEqual(bot.bridge.is_receiving.call_count, 2)

    async def test_error_logging_exception_does_not_kill_health(self):
        bot = self.bot()
        bot.note = Mock(side_effect=OSError('private/path/log-error'))
        self.recovery(bot, ValueError('private-message'))
        self.assertEqual(await self.drive(bot), [15, 10, 15, 15])
        state = WeChatRuntime.health_status(bot)
        self.assertEqual(state['check_state'], 'ok')
        self.assertEqual(state['last_failure_type'], 'ValueError')
        bot._refresh_background_readiness.assert_awaited_once()
        bot.bridge.reconnect.assert_not_called()

    async def test_closed_stdout_with_real_note_does_not_stop_guard(self):
        bot = self.bot()
        bot.events = []
        bot._runtime_log = Mock()
        bot.note = MethodType(WeChatRuntime.note, bot)
        self.recovery(bot, RuntimeError('private-message'))
        with redirect_stdout(BrokenWriter()):
            self.assertEqual(await self.drive(bot), [15, 10, 15, 15])
        self.assertEqual(bot.events[0]['text'], '连接守护异常：RuntimeError')
        bot._runtime_log.write.assert_called_once_with('连接守护异常：RuntimeError')
        self.assertEqual(WeChatRuntime.health_status(bot)['check_state'], 'ok')

    async def test_unbounded_or_non_identifier_exception_name_is_not_exported(self):
        for name in ('private/path-secret', 'A' * 65):
            with self.subTest(name=name):
                bot = self.bot()
                error = type(name, (RuntimeError,), {})('private-message')
                self.recovery(bot, error)
                await self.drive(bot)
                self.assertEqual(WeChatRuntime.health_status(bot)['last_failure_type'], 'Exception')
                self.assertNotIn(name, str(bot.note.call_args_list))

    async def test_old_owner_error_cannot_replace_new_owner_diagnostics(self):
        bot = self.bot()
        new = {'check_state': 'ok', 'last_check_at': 100.0,
               'last_failure_type': 'NewOwnerError', 'last_failure_at': 90.0}
        bot._health_diagnostics = new
        def takeover():
            bot.health = object()
            raise RuntimeError('old owner failure')
        bot.bridge.is_receiving.side_effect = takeover
        self.assertEqual(await self.drive(bot), [15, 10])
        self.assertIs(bot._health_diagnostics, new)
        bot.bridge.reconnect.assert_not_called()
        bot.bridge.start_listening.assert_not_called()
        bot.transport.reset_sender.assert_not_called()

    async def test_cancelled_error_propagates_without_failure_or_recovery(self):
        bot = self.bot()
        bot.bridge.is_receiving.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.drive(bot)
        self.assertEqual(WeChatRuntime.health_status(bot)['check_state'], 'not_checked')
        bot.note.assert_not_called()
        bot.bridge.reconnect.assert_not_called()

    async def test_stop_or_safety_during_error_backoff_never_recovers_receiver(self):
        for mode in ('stop', 'volatile', 'persisted'):
            with self.subTest(mode=mode):
                bot = self.bot()
                bot.bridge.is_receiving.side_effect = RuntimeError('offline failure')
                def tick(seconds):
                    if seconds != 10:
                        return
                    if mode == 'stop':
                        bot._stop_requested = True
                        bot.running = False
                    elif mode == 'volatile':
                        bot._safety_volatile = True
                    else:
                        bot.safety.status = lambda: {'active': True}
                self.assertEqual(await self.drive(bot, tick), [15, 10])
                self.assertEqual(bot.bridge.is_receiving.call_count, 1)
                bot.bridge.reconnect.assert_not_called()
                bot.bridge.start_listening.assert_not_called()
                bot.bridge.resume_sends.assert_not_called()
                bot.transport.reset_sender.assert_not_called()

    async def test_task_liveness_is_independent_from_last_check_result(self):
        bot = self.bot()
        self.assertEqual(WeChatRuntime.health_status(bot), {
            'task_alive': True, 'check_state': 'not_checked', 'last_check_at': 0.0,
            'last_failure_type': '', 'last_failure_at': 0.0})
        task = asyncio.create_task(asyncio.sleep(0))
        bot.health = task
        await task
        bot._health_diagnostics = {'check_state': 'ok', 'last_check_at': 12.0,
                                   'last_failure_type': 'RuntimeError', 'last_failure_at': 10.0}
        state = WeChatRuntime.health_status(bot)
        self.assertFalse(state['task_alive'])
        self.assertEqual(state['check_state'], 'ok')
        self.assertEqual(state['last_failure_type'], 'RuntimeError')
        bot.health = None
        self.assertFalse(WeChatRuntime.health_status(bot)['task_alive'])

    async def test_actual_runtime_status_exports_separate_health_not_bridge_reason(self):
        with tempfile.TemporaryDirectory(prefix='health-diagnostics-offline-') as tmp:
            root = Path(tmp)
            config = root / 'config.wechat.yaml'
            config.write_text(yaml.safe_dump({'wechat': {'backend': 'mock'},
                                             'wechat_only': {'backend': 'mock'}}), encoding='utf-8')
            # 不把进程全局日志 handler 指向即将清理的临时目录。
            log = SimpleNamespace(path=root / 'logs/backend.log', write=Mock())
            with patch('src.wechat_app.RuntimeLog', return_value=log):
                bot = WeChatRuntime(root=root, config_path=config)
            try:
                bot._health_diagnostics = {'check_state': 'error', 'last_check_at': 12.0,
                                           'last_failure_type': 'RuntimeError', 'last_failure_at': 12.0}
                before = bot.bridge.last_error
                status = bot.status()
                self.assertEqual(set(status['health']), {'task_alive', 'check_state', 'last_check_at',
                                                        'last_failure_type', 'last_failure_at'})
                self.assertEqual(status['health']['last_failure_type'], 'RuntimeError')
                self.assertFalse(status['health']['task_alive'])
                self.assertEqual(bot.bridge.last_error, before)
            finally:
                bot.db.close()


if __name__ == '__main__':
    unittest.main()
