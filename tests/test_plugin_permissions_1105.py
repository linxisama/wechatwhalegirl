"""1.3.105 权限五项：声明、审计、强制。

为什么需要（用户 2026-11-08 的第三条判定标准）：
  「不靠改原版内部实现活着。靠了也能装，但必须标出来，把代价写在明面上。」
清单里的 permissions 字段过去就是那个「明面上」，但它只是被解析成列表，
从来没有被检查过 —— 声明 [] 和声明 ["net","fs_write"] 在运行期完全一样。

运行：./.venv-wechat/Scripts/python.exe -m unittest tests.test_plugin_permissions_1105
"""
from __future__ import annotations

import contextlib
import types
import unittest

from src import plugin_guard
from src.plugin_guard import (
    PERMISSION_NAMES,
    PERMISSIONS,
    PermissionDenied,
    PermissionGuard,
    normalize,
    unknown,
)
from src.plugin_packs import validate_manifest
from src.plugins import PluginBase, PluginMeta
from src.runtime_identity import APP_VERSION

HOST = APP_VERSION


def _strict(**overrides):
    data = {
        "id": "demo_pack",
        "name": "测试包",
        "version": "1.0.0",
        "entry": "plugin.py",
        "author": "测试",
        "description": "权限测试用",
        "api_version": 2,
        "requires": "1.3.0",
        "data_policy": "只写自己的设置",
        "native": "纯 Python",
        "side": "本机侧运行",
        "upstream": "本地测试",
        "capabilities": ["on_ingest"],
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


class VocabularyTests(unittest.TestCase):
    """五项权限的规范名必须稳定，改名等于破坏已有清单。"""

    def test_five_permissions(self):
        self.assertEqual(PERMISSION_NAMES,
                         ("net", "fs_read", "fs_write", "exec", "wechat_send"))

    def test_every_permission_has_chinese_label(self):
        labels = dict(PERMISSIONS)
        for name in PERMISSION_NAMES:
            with self.subTest(name=name):
                self.assertTrue(labels.get(name), name + " 必须有中文说明")

    def test_normalize_lowercases_and_dedupes(self):
        self.assertEqual(normalize(["net", "NET", " net ", "fs_read"]), ("net", "fs_read"))

    def test_normalize_drops_unknown(self):
        self.assertEqual(normalize(["net", "bogus"]), ("net",))

    def test_unknown_reports_only_bad_names(self):
        self.assertEqual(unknown(["net", "bogus", "x"]), ("bogus", "x"))


class ManifestTests(unittest.TestCase):
    """清单侧的声明与校验。"""

    def test_missing_permissions_defaults_to_empty(self):
        ok, errors, meta = validate_manifest(_strict(), HOST)
        self.assertTrue(ok, errors)
        self.assertEqual(meta["permissions"], [])

    def test_declared_permissions_are_kept(self):
        ok, errors, meta = validate_manifest(_strict(permissions=["net", "fs_write"]), HOST)
        self.assertTrue(ok, errors)
        self.assertEqual(meta["permissions"], ["net", "fs_write"])

    def test_unknown_permission_is_rejected(self):
        ok, errors, _meta = validate_manifest(_strict(permissions=["net", "hack"]), HOST)
        self.assertFalse(ok)
        self.assertTrue(any("hack" in item for item in errors), errors)

    def test_non_list_permissions_rejected(self):
        ok, errors, _meta = validate_manifest(_strict(permissions="net"), HOST)
        self.assertFalse(ok)
        self.assertTrue(any("permissions" in item for item in errors), errors)

    def test_permissions_are_lowercased_in_meta(self):
        ok, errors, meta = validate_manifest(_strict(permissions=["NET", "FS_Read"]), HOST)
        self.assertTrue(ok, errors)
        self.assertEqual(meta["permissions"], ["net", "fs_read"])


class AuditModeTests(unittest.TestCase):
    """enforce=False（默认）：只审计，不拦截。存量插件靠这条活下来。"""

    def test_require_does_not_raise_in_audit_mode(self):
        guard = PermissionGuard("demo", ["net"], enforce=False)
        guard.require("fs_write")   # 不抛

    def test_violation_is_recorded(self):
        sink = []
        guard = PermissionGuard("demo", ["net"], enforce=False, sink=sink.append)
        self.assertFalse(guard.check("exec"))
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0]["plugin"], "demo")
        self.assertEqual(sink[0]["permission"], "exec")
        self.assertFalse(sink[0]["enforced"])

    def test_granted_permission_is_not_recorded(self):
        sink = []
        guard = PermissionGuard("demo", ["net"], enforce=False, sink=sink.append)
        self.assertTrue(guard.check("net"))
        self.assertEqual(sink, [])

    def test_sink_failure_does_not_break_the_plugin(self):
        def boom(_entry):
            raise RuntimeError("sink 坏了")
        guard = PermissionGuard("demo", [], enforce=False, sink=boom)
        self.assertFalse(guard.check("net"))   # 不抛


class EnforceModeTests(unittest.TestCase):
    """enforce=True：缺声明直接拒。"""

    def test_require_raises_for_undeclared(self):
        guard = PermissionGuard("demo", ["net"], enforce=True)
        with self.assertRaises(PermissionDenied) as caught:
            guard.require("exec")
        self.assertEqual(caught.exception.plugin_id, "demo")
        self.assertEqual(caught.exception.permission, "exec")
        self.assertIn("exec", str(caught.exception))
        self.assertIn("起子进程", str(caught.exception))

    def test_require_passes_for_declared(self):
        guard = PermissionGuard("demo", ["net"], enforce=True)
        guard.require("net")   # 不抛

    def test_permission_denied_is_a_permission_error(self):
        """继承 PermissionError，既有 except 也能接住。"""
        self.assertTrue(issubclass(PermissionDenied, PermissionError))

    def test_violation_still_recorded_when_enforcing(self):
        sink = []
        guard = PermissionGuard("demo", [], enforce=True, sink=sink.append)
        with self.assertRaises(PermissionDenied):
            guard.require("wechat_send")
        self.assertEqual(len(sink), 1)
        self.assertTrue(sink[0]["enforced"])


class DispatchIntegrationTests(unittest.TestCase):
    """PluginDispatch 每次调用前必须换上新插件的权限视图。"""

    def _dispatch(self, plugins, enforce=False):
        from src.plugin_dispatch import PluginDispatch

        class FakeManager:
            def get_settings(self, plugin_id):
                return {"enabled": True}

            def is_quarantined(self, plugin_id):
                return False

        for plugin in plugins:
            plugin._manager = FakeManager()

        class Dispatch(PluginDispatch):
            permission_enforce = enforce

            def __init__(self):
                self._plugins = {p.META.id: p for p in plugins}
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

        return Dispatch()

    def test_guard_is_attached_with_declared_permissions(self):
        seen = {}

        class Probe(PluginBase):
            def on_ingest(self, message, runtime):
                seen["guard"] = getattr(runtime, "permission_guard", None)
                return None

        Probe.META = PluginMeta(id="probe", name="probe", description="",
                                permissions=("net", "fs_read"))
        runtime = types.SimpleNamespace(permission_audit=[])
        self._dispatch([Probe(None)]).run_on_ingest({}, runtime)
        guard = seen["guard"]
        self.assertIsNotNone(guard)
        self.assertEqual(guard.plugin_id, "probe")
        self.assertEqual(guard.granted, ("net", "fs_read"))
        self.assertFalse(guard.enforce, "默认必须是审计档")

    def test_guard_is_replaced_between_plugins(self):
        """runtime 是共享对象，插件 A 的视图不能留给插件 B。"""
        seen = []

        def make(plugin_id, perms):
            class Probe(PluginBase):
                def on_observed(self, message, runtime):
                    seen.append((plugin_id, runtime.permission_guard.granted))
            Probe.META = PluginMeta(id=plugin_id, name=plugin_id, description="",
                                    permissions=perms)
            return Probe(None)

        runtime = types.SimpleNamespace(permission_audit=[])
        self._dispatch([make("alpha", ("net",)), make("beta", ("exec",))]) \
            .run_on_observed({}, runtime)
        self.assertEqual(seen, [("alpha", ("net",)), ("beta", ("exec",))])

    def test_audit_trace_is_bounded(self):
        class Probe(PluginBase):
            def on_observed(self, message, runtime):
                for _ in range(80):
                    runtime.permission_guard.check("exec")
        Probe.META = PluginMeta(id="probe", name="probe", description="", permissions=())
        runtime = types.SimpleNamespace(permission_audit=[])
        self._dispatch([Probe(None)]).run_on_observed({}, runtime)
        self.assertEqual(len(runtime.permission_audit), 50)

    def test_runtime_without_audit_list_still_works(self):
        class Probe(PluginBase):
            def on_observed(self, message, runtime):
                runtime.permission_guard.check("net")
        Probe.META = PluginMeta(id="probe", name="probe", description="", permissions=())
        self._dispatch([Probe(None)]).run_on_observed({}, types.SimpleNamespace())


class BuiltinDeclarationsTests(unittest.TestCase):
    """内置插件该声明的必须声明 —— 否则审计日志会一直报同一批。"""

    def test_network_plugins_declare_net(self):
        from src.plugins import web_context, web_lookup
        for module in (web_context, web_lookup):
            with self.subTest(module=module.__name__):
                self.assertIn("net", tuple(module.__dict__[self._plugin_class(module)].META.permissions))

    def _plugin_class(self, module):
        for value in vars(module).values():
            if (isinstance(value, type) and issubclass(value, PluginBase)
                    and value is not PluginBase and getattr(value, "META", None) is not None):
                return value.__name__
        raise AssertionError(module.__name__ + " 里没有插件类")


if __name__ == "__main__":
    unittest.main()