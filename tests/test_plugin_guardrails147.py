"""插件钩子的有界执行、故障可诊断与"连续失败才隔离"（1.3.47-A1）。

全部用假插件直接塞进 manager._plugins：不写插件文件、不连微信、不调模型。
超时用注入的短上限证明（manager.SYNC_TIMEOUT 等是实例属性，覆盖即生效）。
"""
import asyncio
import json
import tempfile
import time
import types
import unittest
from pathlib import Path

from src.plugin_health import FAILURE_LIMIT
from src.plugins.manager import PluginManager
from src.plugin_dispatch import PluginDispatch


class FakePlugin:
    """只带 META.id / enabled 和用得到的钩子，足够走完派发路径。"""

    SYNC_HOOKS = ('on_ingest', 'on_observed', 'on_block', 'on_pre_ai', 'on_sent')

    def __init__(self, pid, **hooks):
        self.META = types.SimpleNamespace(id=pid, name=pid, description='测试用', category='测试')
        self.enabled = True
        self.calls = 0
        # 派发会挨个问每个插件要钩子；缺哪个就该"没实现"，而不是抛 AttributeError
        for name in self.SYNC_HOOKS:
            setattr(self, name, lambda *a, **k: None)
        self.describe = self._noop_async
        for name, hook in hooks.items():
            setattr(self, name, hook)

    async def _noop_async(self, *args, **kwargs):
        return None


class Runtime:
    def __init__(self):
        self.reply_meta = {}
        self.ai_lock = asyncio.Lock()


class Engine:
    """替身模型：插件超时后主流程要能退回到它。"""

    def __init__(self):
        self.replied = []

    async def chat(self, conversation, **kwargs):
        self.replied.append(conversation)
        return '兜底回复'


def use(manager, *fakes):
    """让派发只看见这些假插件，断言不受内置插件影响。"""
    manager._plugins = {fake.META.id: fake for fake in fakes}
    manager._plugin_ids = {id(fake): fake.META.id for fake in fakes}
    return fakes[0] if fakes else None


def sleeping(seconds=12):
    def hook(*args):
        args[0]['slow_started'] = True   # 先写一笔：超时后这笔提交必须被回滚
        time.sleep(seconds)
        return '迟到'
    return hook


class GuardrailBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = PluginManager(self.root)
        self.runtime = Runtime()

    def tearDown(self):
        self.temp.cleanup()

    def message(self):
        return {'id': 'm1', 'text': '你好', 'media': []}


class SyncHookGuardrailTests(GuardrailBase):
    def test_constants_are_configured(self):
        self.assertEqual(PluginDispatch.SYNC_TIMEOUT, 8.0)
        self.assertEqual(PluginDispatch.DESCRIBE_TIMEOUT, 30.0)
        self.assertEqual(PluginDispatch.GENERATE_TIMEOUT, 60.0)

    def test_on_ingest_timeout_returns_at_the_timeout(self):
        plugin = FakePlugin('slow_ingest', on_ingest=sleeping())
        use(self.manager, plugin)
        self.manager.SYNC_TIMEOUT = 0.2
        message = self.message()
        started = time.monotonic()
        self.assertIsNone(self.manager.run_on_ingest(message, self.runtime))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0)               # 12 秒的钩子不再拖住整条链路
        self.assertNotIn('slow_started', message)   # 超时那一次的写入被回滚

    def test_timeout_is_recorded_with_traceback_and_flag(self):
        use(self.manager, FakePlugin('slow_ingest', on_ingest=sleeping()))
        self.manager.SYNC_TIMEOUT = 0.2
        self.manager.run_on_ingest(self.message(), self.runtime)
        row = self.manager.health()[0]
        self.assertEqual(row['stage'], 'on_ingest')
        self.assertEqual(row['error_type'], 'TimeoutError')
        self.assertTrue(row['timed_out'])
        self.assertIn('bounded_sync', row['traceback'])
        saved = json.loads(self.manager._fault_path.read_text('utf-8'))
        self.assertTrue(saved['slow_ingest']['timed_out'])
        self.assertIn('bounded_sync', saved['slow_ingest']['traceback'])

    def test_ingest_string_short_circuits_and_is_committed(self):
        def first(message, runtime):
            message['seen'] = 'first'
            return '立即回复'

        def second(message, runtime):
            message['seen'] = 'second'

        use(self.manager, FakePlugin('first_hook', on_ingest=first),
            FakePlugin('second_hook', on_ingest=second))
        message = self.message()
        self.assertEqual(self.manager.run_on_ingest(message, self.runtime), '立即回复')
        self.assertEqual(message.get('seen'), 'first')          # 跨线程写回仍提交
        self.assertEqual(message.get('_immediate_plugin'), 'first_hook')

    def test_block_true_still_blocks(self):
        def block(message, runtime):
            return True
        use(self.manager, FakePlugin('blocker', on_block=block))
        self.assertTrue(self.manager.run_on_block(self.message(), self.runtime))

    def test_pre_ai_merge_rules_unchanged(self):
        def first(message, turns, runtime):
            return {'delay': 1.5, 'extra_system': '甲', 'max_tokens': 300}

        def second(message, turns, runtime):
            return {'extra_system': '乙', 'max_tokens': 200}

        use(self.manager, FakePlugin('hint_a', on_pre_ai=first),
            FakePlugin('hint_b', on_pre_ai=second))
        result = self.manager.run_on_pre_ai(self.message(), [], self.runtime)
        self.assertEqual(result['delay'], 1.5)
        self.assertEqual(result['max_tokens'], 200)            # 取更小的上限
        self.assertEqual(result['extra_system'], '\n甲\n乙')
        self.assertEqual(result['extra_system_parts'], [('hint_a', '甲'), ('hint_b', '乙')])
        self.assertFalse(result['block'])

    def test_invalid_delay_is_recorded_and_other_plugins_continue(self):
        def bad(message, turns, runtime):
            return {'delay': 90}

        def good(message, turns, runtime):
            return {'extra_system': '仍然生效'}

        use(self.manager, FakePlugin('bad_hint', on_pre_ai=bad),
            FakePlugin('good_hint', on_pre_ai=good))
        result = self.manager.run_on_pre_ai(self.message(), [], self.runtime)
        self.assertEqual(result['extra_system'], '\n仍然生效')
        self.assertEqual(self.manager.health()[0]['error_type'], 'ValueError')

    def test_observed_and_sent_hooks_are_bounded(self):
        # 一次成功会清掉该插件的故障记录（"恢复即消失"是既有语义），所以两条钩子
        # 必须各用一个干净管理器，否则前一条会被后一条的正常返回抹掉。
        cases = [('on_observed', lambda m: m.run_on_observed(self.message(), self.runtime)),
                 ('on_sent', lambda m: m.run_on_sent(self.message(), '回复', self.runtime))]
        for stage, call in cases:
            with self.subTest(stage=stage):
                manager = PluginManager(self.root / stage)
                use(manager, FakePlugin('slow_' + stage, **{stage: sleeping()}))
                manager.SYNC_TIMEOUT = 0.2
                started = time.monotonic()
                call(manager)
                self.assertLess(time.monotonic() - started, 3.0)
                self.assertEqual(manager.health()[0]['stage'], stage)


class StrikeQuarantineTests(GuardrailBase):
    def flaky(self, pid="flaky"):
        state = {'fail': True, 'calls': 0}

        def hook(message, runtime):
            state['calls'] += 1
            if state['fail']:
                raise ValueError('boom')

        plugin = FakePlugin(pid, on_ingest=hook)
        plugin.state = state
        return plugin

    def test_single_failure_keeps_plugin_enabled(self):
        plugin = self.flaky()
        use(self.manager, plugin)
        self.manager.run_on_ingest(self.message(), self.runtime)
        self.assertTrue(self.manager.is_quarantined('flaky'))    # 故障照旧记录
        self.assertTrue(self.manager._enabled(plugin))           # 但没到隔离线
        self.manager.run_on_ingest(self.message(), self.runtime)
        self.assertEqual(plugin.state['calls'], 2)               # 第 2 次确实又被调用
        self.assertEqual(self.manager.health()[0]['failures'], 2)

    def test_three_consecutive_failures_quarantine(self):
        plugin = self.flaky()
        use(self.manager, plugin)
        for _ in range(FAILURE_LIMIT):
            self.manager.run_on_ingest(self.message(), self.runtime)
        self.assertFalse(self.manager._enabled(plugin))
        self.manager.run_on_ingest(self.message(), self.runtime)
        self.assertEqual(plugin.state['calls'], FAILURE_LIMIT)   # 隔离后不再调用

    def test_success_resets_the_count(self):
        plugin = self.flaky()
        use(self.manager, plugin)
        for _ in range(2):
            self.manager.run_on_ingest(self.message(), self.runtime)
        plugin.state['fail'] = False
        self.manager.run_on_ingest(self.message(), self.runtime)
        self.assertEqual(self.manager._strikes['flaky']['on_ingest'], 0)   # 成功一次即清零
        self.assertFalse(self.manager._blocked('flaky'))   # 记录留着，但没到隔离线
        plugin.state['fail'] = True
        for _ in range(FAILURE_LIMIT - 1):
            self.manager.run_on_ingest(self.message(), self.runtime)
        self.assertTrue(self.manager._enabled(plugin))
        self.manager.run_on_ingest(self.message(), self.runtime)
        self.assertFalse(self.manager._enabled(plugin))

    def test_failure_count_survives_restart(self):
        plugin = self.flaky()
        use(self.manager, plugin)
        for _ in range(2):
            self.manager.run_on_ingest(self.message(), self.runtime)
        again = PluginManager(self.root)
        self.assertEqual(again._strikes['flaky']['on_ingest'], 2)
        self.assertFalse(again._blocked('flaky'))
        self.assertEqual(again.health()[0]['failures'], 2)

    def test_retry_failed_clears_counts(self):
        plugin = self.flaky()
        use(self.manager, plugin)
        for _ in range(FAILURE_LIMIT):
            self.manager.run_on_ingest(self.message(), self.runtime)
        self.assertFalse(self.manager._enabled(plugin))
        self.manager.retry_failed()
        self.assertEqual(self.manager._strikes, {})
        self.assertFalse(self.manager.is_quarantined('flaky'))
        self.assertTrue(self.manager._enabled(plugin))

    def test_old_fault_file_without_counts_is_still_read(self):
        payload = {'legacy': {'id': 'legacy', 'stage': 'on_ingest', 'error_type': 'ValueError',
                             'time': 1.0, 'fingerprint': 'abc', 'message': '已隔离：on_ingest（ValueError）'}}
        self.manager._fault_path.write_text(json.dumps(payload, ensure_ascii=False), 'utf-8')
        again = PluginManager(self.root)
        self.assertTrue(again.is_quarantined('legacy'))
        self.assertTrue(again._blocked('legacy'))    # 旧记录的语义就是已隔离
        self.assertEqual(again._strikes, {})

    def test_load_failure_still_quarantines_at_once(self):
        code = ("from . import PluginBase, PluginMeta\n"
                "class Demo(PluginBase):\n"
                "    META = PluginMeta(id='broken147', name='坏插件', description='')\n"
                "    def __init__(self, manager):\n"
                "        raise RuntimeError('boom')\n")
        self.manager.custom_plugin_path('broken147').write_text(code, 'utf-8')
        self.manager.register_custom('broken147')
        self.manager.reload()
        self.assertTrue(self.manager.is_quarantined('broken147'))
        self.assertTrue(self.manager._blocked('broken147'))
        self.assertEqual(self.manager._strikes['broken147']['constructor'], 1)

    def test_traceback_does_not_carry_plugin_message(self):
        def leak(message, runtime):
            raise ValueError('PRIVATE_KEY_MUST_NOT_LEAK')
        use(self.manager, FakePlugin('leaky', on_ingest=leak))
        self.manager.run_on_ingest(self.message(), self.runtime)
        self.assertNotIn('PRIVATE_KEY_MUST_NOT_LEAK', self.manager._fault_path.read_text('utf-8'))
        self.assertNotIn('PRIVATE_KEY_MUST_NOT_LEAK', json.dumps(self.manager.health(), ensure_ascii=False))
        self.assertIn('test_plugin_guardrails147', self.manager.health()[0]['traceback'])


class AsyncHookGuardrailTests(GuardrailBase):
    def test_describe_timeout_keeps_flow_and_releases_lock(self):
        async def describe(message, runtime):
            await asyncio.sleep(12)
            message['described'] = True

        use(self.manager, FakePlugin('image_understanding', describe=describe))
        self.manager.DESCRIBE_TIMEOUT = 0.1
        message = self.message()

        async def flow():
            runtime = Runtime()
            async with runtime.ai_lock:
                await self.manager.describe_images(message, runtime)
            return await asyncio.wait_for(runtime.ai_lock.acquire(), 1)

        self.assertTrue(asyncio.run(flow()))
        self.assertNotIn('described', message)
        row = self.manager.health()[0]
        self.assertEqual(row['stage'], 'describe')
        self.assertTrue(row['timed_out'])

    def test_generate_timeout_keeps_flow_and_releases_lock(self):
        async def generate(calls, conversation, **kwargs):
            await asyncio.sleep(12)
            return '插件生成的回复'

        use(self.manager, FakePlugin('chat_controls', generate=generate))
        self.manager.GENERATE_TIMEOUT = 0.1
        engine = Engine()

        async def flow():
            runtime = Runtime()
            async with runtime.ai_lock:
                reply = await self.manager.generate_reply(engine, [{'role': 'user', 'content': 'hi'}], system_prompt='s')
            return reply, await asyncio.wait_for(runtime.ai_lock.acquire(), 1)

        reply, acquired = asyncio.run(flow())
        self.assertEqual(reply, '')        # 插件超时按阻断处理，本轮不发
        self.assertTrue(acquired)          # 模型锁没有被永久占住
        row = self.manager.health()[0]
        self.assertEqual(row['stage'], 'generate')
        self.assertTrue(row['timed_out'])


if __name__ == "__main__":
    unittest.main()
