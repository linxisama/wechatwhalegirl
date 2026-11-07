"""1.3.104 竞标-仲裁：排他钩子按权重决定谁说话，其余记进 trace。

为什么需要（用户 2026-11-08 的原话）：
  「一个回话关联到四五个插件，最后只调用最主要的那个，那其他插件真的有用吗」
过去 on_ingest / on_block 按注册顺序先到先得 —— 谁先被 import 谁赢，
用户完全无从判断为什么是它在回。现在按权重降序问，第一个给结果的赢，
被挤掉的记进 runtime.bid_trace。

运行：./.venv-wechat/Scripts/python.exe -m unittest tests.test_plugin_bid_1104
"""
from __future__ import annotations

import contextlib
import types
import unittest

from src.plugin_dispatch import PluginDispatch
from src.plugins import PluginBase, PluginMeta


class FakeManager:
    """插件只用到 get_settings / is_quarantined，其余不碰。"""

    def __init__(self, settings=None):
        self._settings = settings or {}

    def get_settings(self, plugin_id):
        return dict(self._settings.get(plugin_id, {"enabled": True}))

    def is_quarantined(self, plugin_id):
        return False


def make_plugin(plugin_id, weight=0, ingest=None, block=None, observed=None):
    namespace = {}
    if ingest is not None:
        namespace["on_ingest"] = lambda self, message, runtime, _v=ingest: _v
    if block is not None:
        namespace["on_block"] = lambda self, message, runtime, _v=block: _v
    if observed is not None:
        namespace["on_observed"] = lambda self, message, runtime, _f=observed: _f(plugin_id)
    cls = type("Probe_" + plugin_id, (PluginBase,), namespace)
    cls.META = PluginMeta(id=plugin_id, name=plugin_id, description="测试用",
                          capability_weight=weight)
    return cls(FakeManager())


class Dispatch(PluginDispatch):
    """把保护机制换成直通，只测仲裁本身。"""

    def __init__(self, plugins):
        self._plugins = {plugin.META.id: plugin for plugin in plugins}
        self._removed = set()

    def plugin_id(self, plugin):
        return plugin.META.id

    def _blocked(self, plugin_id):
        return False

    def is_quarantined(self, plugin_id):
        return False

    def protected(self, plugin, hook):
        return contextlib.nullcontext()

    def local_changes(self, *args, **kwargs):
        return contextlib.nullcontext((args[0] if args else {}, None))

    def bounded_sync(self, function, *args, timeout=0):
        return function(*args)

    def blocked_policies(self):
        return []


def runtime():
    return types.SimpleNamespace(bid_trace=[])


class ExclusiveHookTests(unittest.TestCase):
    """只有 on_ingest / on_block 是排他的。"""

    def test_exclusive_hooks_are_declared(self):
        self.assertEqual(set(PluginDispatch.EXCLUSIVE_HOOKS), {"on_ingest", "on_block"})

    def test_higher_weight_wins_ingest(self):
        low = make_plugin("low", weight=0, ingest="from-low")
        high = make_plugin("high", weight=5, ingest="from-high")
        rt = runtime()
        # 注意注册顺序是 low 在前：过去先到先得会让 low 赢。
        self.assertEqual(Dispatch([low, high]).run_on_ingest({}, rt), "from-high")

    def test_equal_weight_keeps_registration_order(self):
        """不填权重的存量插件行为必须完全不变。"""
        first = make_plugin("first", ingest="from-first")
        second = make_plugin("second", ingest="from-second")
        self.assertEqual(Dispatch([first, second]).run_on_ingest({}, runtime()), "from-first")

    def test_lower_weight_takes_over_when_higher_has_nothing_to_say(self):
        low = make_plugin("low", weight=0, ingest="from-low")
        high = make_plugin("high", weight=9, ingest=None)
        self.assertEqual(Dispatch([low, high]).run_on_ingest({}, runtime()), "from-low")

    def test_negative_weight_is_allowed(self):
        low = make_plugin("low", weight=-3, ingest="from-low")
        normal = make_plugin("normal", weight=0, ingest="from-normal")
        self.assertEqual(Dispatch([low, normal]).run_on_ingest({}, runtime()), "from-normal")


class TraceTests(unittest.TestCase):
    """仲裁必须留痕，否则就是黑箱。"""

    def test_trace_records_winner_and_losers(self):
        low = make_plugin("low", weight=0, ingest="from-low")
        high = make_plugin("high", weight=5, ingest="from-high")
        rt = runtime()
        Dispatch([low, high]).run_on_ingest({}, rt)
        self.assertEqual(len(rt.bid_trace), 1)
        entry = rt.bid_trace[0]
        self.assertEqual(entry["hook"], "on_ingest")
        self.assertEqual(entry["winner"], "high")
        self.assertEqual(entry["weight"], 5)
        self.assertEqual([item["id"] for item in entry["losers"]], ["low"])
        self.assertEqual(entry["reason"], "weight")

    def test_no_trace_when_nothing_was_crowded_out(self):
        """唯一候选不叫仲裁，记一条 losers=[] 只是噪声。"""
        rt = runtime()
        Dispatch([make_plugin("solo", ingest="x")]).run_on_ingest({}, rt)
        self.assertEqual(rt.bid_trace, [])

    def test_no_trace_when_higher_weight_simply_had_nothing_to_say(self):
        rt = runtime()
        low = make_plugin("low", weight=0, ingest="from-low")
        high = make_plugin("high", weight=9, ingest=None)
        Dispatch([low, high]).run_on_ingest({}, rt)
        self.assertEqual(rt.bid_trace, [], "没人被挤掉就不该留痕")

    def test_trace_is_bounded(self):
        rt = runtime()
        for index in range(120):
            low = make_plugin("low", weight=0, ingest="a")
            high = make_plugin("high", weight=1, ingest="b")
            Dispatch([low, high]).run_on_ingest({}, rt)
        self.assertEqual(len(rt.bid_trace), 50, "长跑时 trace 必须有界")

    def test_runtime_without_trace_does_not_break(self):
        """没有 bid_trace 的 runtime（老测试、老调用点）不能让主链路炸。"""
        low = make_plugin("low", weight=0, ingest="from-low")
        high = make_plugin("high", weight=5, ingest="from-high")
        rt = types.SimpleNamespace()
        self.assertEqual(Dispatch([low, high]).run_on_ingest({}, rt), "from-high")


class BlockHookTests(unittest.TestCase):
    """on_block 返回 False 是明确的「不拦」，必须继续问下一个。"""

    def test_high_weight_false_still_asks_next(self):
        high = make_plugin("high", weight=9, block=False)
        low = make_plugin("low", weight=0, block=True)
        self.assertTrue(Dispatch([high, low]).run_on_block({}, runtime()))

    def test_high_weight_true_stops_immediately(self):
        rt = runtime()
        high = make_plugin("high", weight=9, block=True)
        low = make_plugin("low", weight=0, block=True)
        self.assertTrue(Dispatch([high, low]).run_on_block({}, rt))
        self.assertEqual(rt.bid_trace[0]["winner"], "high")
        self.assertEqual([item["id"] for item in rt.bid_trace[0]["losers"]], ["low"])

    def test_block_trace_only_when_someone_actually_blocks(self):
        rt = runtime()
        a = make_plugin("a", weight=0, block=False)
        b = make_plugin("b", weight=0, block=False)
        self.assertFalse(Dispatch([a, b]).run_on_block({}, rt))
        self.assertEqual(rt.bid_trace, [])


class NonExclusiveHookTests(unittest.TestCase):
    """其余钩子是累积/变换，必须全部执行，不能被仲裁掉。"""

    def test_on_observed_runs_every_plugin_in_registration_order(self):
        called = []
        low = make_plugin("low", weight=0, observed=lambda pid: called.append(pid))
        high = make_plugin("high", weight=99, observed=lambda pid: called.append(pid))
        Dispatch([low, high]).run_on_observed({}, runtime())
        self.assertEqual(called, ["low", "high"], "非排他钩子不能按权重重排")

    def test_exclusive_hooks_list_matches_dispatch_usage(self):
        """EXCLUSIVE_HOOKS 与实际用 _bidders 的钩子必须一致。"""
        import inspect
        source = inspect.getsource(PluginDispatch)
        self.assertIn("_bidders('on_ingest')", source)
        self.assertIn("_bidders('on_block')", source)
        for hook in ("on_observed", "on_pre_ai", "on_prepare", "on_sent", "on_post_reply", "tick"):
            with self.subTest(hook=hook):
                self.assertNotIn("_bidders('%s')" % hook, source,
                                 hook + " 不是排他钩子，不能走仲裁")


if __name__ == "__main__":
    unittest.main()