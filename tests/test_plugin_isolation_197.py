"""真正的损坏插件、持久隔离和运行故障注入；不连接微信、不调用模型。"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.plugins import PluginBase, PluginMeta
from src.plugins.manager import PluginManager


def source(pid, body='    pass\n', schema='{}'):
    return ("from . import PluginBase, PluginMeta, FieldSchema\n"
            "class Demo(PluginBase):\n"
            f"    META = PluginMeta(id={pid!r},name='测试插件',description='',config_schema={schema})\n" + body)


class LoadingIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = PluginManager(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, pid, code):
        self.manager.custom_plugin_path(pid).write_text(code, 'utf-8')
        self.manager.register_custom(pid)

    def test_syntax_dependency_exit_metadata_constructor_and_postload_are_isolated(self):
        cases = {
            'bad_syntax': 'not python(\n',
            'bad_dependency': 'import missing_dependency_for_isolation_197\n',
            'bad_exit': 'raise SystemExit(17)\n',
            'bad_constructor': source('bad_constructor', "    def __init__(self, manager):\n        raise RuntimeError('PRIVATE_KEY_MUST_NOT_LEAK')\n"),
            'bad_postload': source('bad_postload', "    def post_load(self):\n        raise ValueError('failed')\n"),
            'bad_schema': source('bad_schema', schema="{'x': FieldSchema(type='int', label='x', min='broken')}"),
        }
        for pid, code in cases.items():
            self.install(pid, code)
        self.install('good_plugin', source('good_plugin'))
        self.manager.reload()
        self.assertIn('good_plugin', self.manager._plugins)
        self.assertIn('chat_controls', self.manager._plugins)
        for pid in cases:
            self.assertNotIn(pid, self.manager._plugins)
            self.assertTrue(self.manager.is_quarantined(pid))
            self.assertNotIn('src_custom_plugin_' + pid, sys.modules if pid in {'bad_syntax', 'bad_dependency', 'bad_exit'} else {})
        rows = self.manager.list_plugins()
        self.assertEqual(sum(bool(row.get('faulted')) for row in rows), len(cases))
        self.assertNotIn('PRIVATE_KEY_MUST_NOT_LEAK', json.dumps(self.manager.health()))
        self.assertNotIn('PRIVATE_KEY_MUST_NOT_LEAK', self.manager._fault_path.read_text('utf-8'))
        again = PluginManager(self.root)
        self.assertEqual(len(again.health()), len(cases))
        self.assertIn('good_plugin', again._plugins)

    def test_broken_plugin_cannot_replace_builtin_id(self):
        self.install('impostor', source('robot_admin'))
        self.manager.reload()
        self.assertTrue(self.manager.is_quarantined('impostor'))
        self.assertEqual(type(self.manager._plugins['robot_admin']).__module__, 'src.plugins.robot_admin')

    def test_same_fingerprint_is_skipped_but_changed_file_is_retried(self):
        body = "    def __init__(self, manager):\n        manager.attempts = getattr(manager, 'attempts', 0) + 1\n        raise ValueError('x')\n"
        self.install('repair_me', source('repair_me', body))
        self.manager.reload()
        self.assertEqual(self.manager.attempts, 1)
        self.manager.reload()
        self.assertEqual(self.manager.attempts, 1)
        path = self.manager.custom_plugin_path('repair_me')
        old_stat = path.stat()
        old = path.read_text('utf-8')
        replacement = 'self.ok = True'.ljust(len("raise ValueError('x')"))
        path.write_text(old.replace("raise ValueError('x')", replacement), 'utf-8')
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        self.manager.reload()
        self.assertIn('repair_me', self.manager._plugins)
        self.assertTrue(self.manager._plugins['repair_me'].ok)
        self.assertFalse(self.manager.is_quarantined('repair_me'))

    def test_failed_postload_cannot_change_other_settings(self):
        original = self.manager.get_settings('chat_controls')
        self.install('bad_migration', source('bad_migration', "    def post_load(self):\n        self._manager.update_settings('chat_controls', {'replySegments': 4})\n        raise ValueError()\n"))
        self.manager.reload()
        self.assertEqual(self.manager.get_settings('chat_controls'), original)
        self.assertEqual(json.loads(self.manager._settings_path.read_text('utf-8'))['chat_controls'], original)

    def test_corrupt_fault_file_and_unwritable_fault_log_do_not_break_startup(self):
        self.manager._fault_path.write_text('{bad', 'utf-8')
        again = PluginManager(self.root)
        self.assertEqual(again.health(), [])
        self.assertGreater(len(again.list_plugins()), 20)
        with patch('src.persistence.atomic_text', side_effect=OSError('disk full')):
            again._fault('broken_test', 'load', ValueError())
        self.assertTrue(again.is_quarantined('broken_test'))


class HookIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.temp.name))
        self.runtime = SimpleNamespace(reply_meta=None, note=lambda value: None)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def plugins(self, **methods):
        values = {}
        for pid, hooks in methods.items():
            cls = type(pid, (PluginBase,), dict(META=PluginMeta(pid, pid, ''), **hooks))
            values[pid] = cls(self.manager)
        self.manager._plugins = values
        return values

    async def test_failed_sync_changes_are_rolled_back_and_next_plugin_runs(self):
        def broken(plugin, message, runtime):
            message['text'] = 'corrupted'
            message['nested'].append('bad')
            runtime.reply_meta = {'media': ['bad']}
            raise ValueError()
        def good(plugin, message, runtime):
            message['healthy'] = True
        self.plugins(broken={'on_ingest': broken}, good={'on_ingest': good})
        msg = {'text': 'original', 'nested': ['safe']}
        self.manager.run_on_ingest(msg, self.runtime)
        self.assertEqual(msg, {'text': 'original', 'nested': ['safe'], 'healthy': True})
        self.assertIsNone(self.runtime.reply_meta)
        self.assertTrue(self.manager.is_quarantined('broken'))

    async def test_pre_ai_failure_cannot_leak_conversation_or_partial_hints(self):
        def broken(plugin, msg, turns, runtime):
            turns.clear()
            return {'extra_system': 123}
        def good(plugin, msg, turns, runtime):
            return {'extra_system': 'healthy'}
        self.plugins(broken={'on_pre_ai': broken}, good={'on_pre_ai': good})
        turns = [{'role': 'user', 'content': 'question'}]
        hints = self.manager.run_on_pre_ai({}, turns, self.runtime)
        self.assertEqual(turns, [{'role': 'user', 'content': 'question'}])
        self.assertEqual(hints['extra_system'].strip(), 'healthy')

    async def test_async_systemexit_does_not_exit_event_loop(self):
        async def broken(plugin, msg, runtime):
            raise SystemExit(9)
        async def good(plugin, msg, runtime):
            msg['healthy'] = True
        self.plugins(broken={'on_prepare': broken}, good={'on_prepare': good})
        msg = {}
        await self.manager.run_on_prepare(msg, self.runtime)
        self.assertTrue(msg['healthy'])
        self.assertEqual(self.manager.health()[0]['error_type'], 'SystemExit')

    async def test_tick_timeout_continues_next_plugin_and_cancellation_is_not_a_fault(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def slow(plugin, runtime):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()
        seen = []
        async def good(plugin, runtime):
            seen.append(True)
        self.plugins(slow={'tick': slow}, good={'tick': good})
        self.manager.TICK_TIMEOUT = .02
        await self.manager.run_tick(self.runtime)
        self.assertTrue(stopped.is_set())
        self.assertEqual(seen, [True])
        self.assertEqual(self.manager.health()[0]['error_type'], 'TimeoutError')
        self.manager._faults.clear()
        self.manager.TICK_TIMEOUT = 60
        started.clear()
        job = asyncio.create_task(self.manager.run_tick(self.runtime))
        await started.wait()
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertEqual(self.manager.health(), [])

    async def test_invalid_reply_metadata_is_rolled_back(self):
        async def broken(plugin, msg, reply, runtime):
            msg['bad'] = True
            return 'corrupted', {'media': 123}
        async def good(plugin, msg, reply, runtime):
            return reply + 'healthy'
        self.plugins(broken={'on_post_reply': broken}, good={'on_post_reply': good})
        msg = {}
        reply = await self.manager.run_on_post_reply(msg, 'original', self.runtime)
        self.assertEqual(reply, 'originalhealthy')
        self.assertNotIn('bad', msg)
        self.assertIsNone(self.runtime.reply_meta)

    async def test_policy_plugin_failure_blocks_outgoing_but_not_plugin_listing(self):
        self.manager._fault('robot_admin', 'load', ValueError())
        self.assertIn('robot_admin', self.manager.blocked_policies())
        self.assertFalse(self.manager._plugins['robot_admin'].enabled)
        self.assertGreater(len(self.manager.list_plugins()), 20)

    async def test_vision_result_can_be_observed_during_ocr_local_transaction(self):
        from src.plugin_health import live_message
        msg = {'text': 'picture'}
        with self.manager.local_changes(msg) as (working, _):
            msg['vision_description'] = 'completed concurrently'
            self.assertEqual(live_message(working)['vision_description'], 'completed concurrently')
            working['vision_text'] = 'OCR text'
        self.assertEqual(msg['vision_description'], 'completed concurrently')
        self.assertEqual(msg['vision_text'], 'OCR text')

    async def test_successful_context_edit_preserves_live_current_turn(self):
        current = {'role': 'user', 'content': 'question'}
        conversation = [current]
        with self.manager.local_changes({}, conversation) as (_, turns):
            turns.insert(0, {'role': 'system', 'content': 'reference'})
            turns[-1]['content'] += ' prepared'
        self.assertIs(conversation[-1], current)
        current['content'] += ' image annotation'
        self.assertEqual(conversation[-1]['content'], 'question prepared image annotation')

    async def test_model_failure_does_not_quarantine_forwarding_plugin(self):
        class Engine:
            async def chat(self, *args, **kwargs):
                raise RuntimeError('temporary model outage')
        engine = Engine()
        with self.assertRaisesRegex(RuntimeError, 'temporary model outage'):
            await self.manager.generate_reply(engine, [])
        self.assertFalse(self.manager.is_quarantined('chat_controls'))
        self.assertEqual(self.manager.blocked_policies(), [])
        async def recovered(*args, **kwargs):
            return 'recovered answer'
        engine.chat = recovered
        self.assertEqual(await self.manager.generate_reply(engine, []), 'recovered answer')

    async def test_fault_inside_generator_is_isolated_without_model_retry(self):
        calls = []
        class Engine:
            async def chat(self, *args, **kwargs):
                calls.append(True)
                return 'answer'
        async def broken(plugin, engine, turns, **kwargs):
            raise ValueError('plugin defect before model call')
        self.plugins(chat_controls={'generate': broken})
        self.assertEqual(await self.manager.generate_reply(Engine(), []), '')
        self.assertEqual(calls, [])
        self.assertTrue(self.manager.is_quarantined('chat_controls'))


class RecoveryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_and_retry_api_preserve_configuration_and_policy_fail_closed(self):
        import shutil
        import httpx
        from src.wechat_app import WeChatRuntime, create_app
        from tests.test_wechat_app_flow import write_config, ROOT
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(ROOT/'styles', root/'styles')
            bot = WeChatRuntime(root=root, config_path=write_config(root), db_path=root/'test.sqlite')
            try:
                manager = bot.plugins
                path = manager.custom_plugin_path('bad_recover')
                path.write_text(source('bad_recover', '    def post_load(self):\n        raise ValueError()\n'), 'utf-8')
                manager.register_custom('bad_recover')
                manager.reload()
                app = create_app(bot)
                app.state.bot = bot
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
                    health = (await client.get('/api/plugins/health')).json()
                    self.assertEqual(health['faults'][0]['id'], 'bad_recover')
                    async def fake_chat(*args, **kwargs):
                        return '基础回复正常。'
                    bot.ai.chat = fake_chat
                    await bot.start()
                    from tests.test_wechat_app_flow import raw_message
                    bot.ingest(raw_message('测试故障隔离后的正常回复', chat='wxid_test', kind='private'))
                    async with asyncio.timeout(3):
                        while not bot.bridge.sent_log:
                            await asyncio.sleep(.02)
                    self.assertEqual(bot.bridge.sent_log[0]['text'], '基础回复正常。')
                    self.assertEqual((await client.post('/api/plugins/retry-failed')).status_code, 409)
                    await bot.stop()
                    original = manager.get_settings('chat_controls')
                    path.write_text(source('bad_recover'), 'utf-8')
                    response = await client.post('/api/plugins/retry-failed')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['faults'], [])
                    self.assertEqual(manager.get_settings('chat_controls'), original)
                    manager._plugins.pop('robot_admin')
                    manager._fault('robot_admin', 'constructor', ValueError())
                    state = (await client.get('/api/status')).json()
                    self.assertTrue(state['reply_paused'])
                    self.assertFalse(bot.reply_decision({'kind': 'private', 'target': 'user'})[0])
                    self.assertEqual((await client.get('/api/plugins')).status_code, 200)
            finally:
                await bot.close()


class FaultUITests(unittest.TestCase):
    def test_fault_card_is_disabled_and_retry_has_explicit_entry(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication, QLabel
        import wechat_gui as gui
        app = QApplication.instance() or QApplication([])
        tab = gui.PluginTab()
        try:
            frame = tab._make_row({'id': 'fault_test', 'name': '损坏插件', 'enabled': False,
                'faulted': True, 'fault': {'message': '已隔离：constructor（ValueError）'}})
            self.assertFalse(tab._rows['fault_test']['toggle'].isEnabled())
            self.assertTrue(any('已隔离' in label.text() for label in frame.findChildren(QLabel)))
            self.assertIn('停止机器人', tab.btn_retry_plugins.toolTip())
        finally:
            tab.close()
            app.processEvents()
