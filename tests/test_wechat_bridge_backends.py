"""`WeChatBridge.detect_backend()` 的离线测试（后端探测与连接）。

这条逻辑决定了"到底连微信还是进模拟模式"，判错的代价很实在：
- 明明什么都探测不到却报了个真后端 → 用户以为连上了，消息其实发不出去；
- 低权限进程里枚举不到 `weixin.exe` 就改配置 → 明明配好了却被静默降级。

所以策略是：**显式配置不做探测**，只有 `auto` 才按「客户端进程 → 可用包」挑；
什么都探测不到时返回 `mock`（不假装连上了微信）。

全程离线：`_client_executable` 与 `importlib.util.find_spec` 都被替换成桩，
不会真的去看进程表或导入微信库。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.wechat_bridge import (
    BACKEND_AUTO,
    BACKEND_MOCK,
    BACKEND_WECHATAUTO,
    BACKEND_WXAUTO,
    BACKEND_WXAUTO4,
    SUPPORTED_BACKENDS,
    WeChatBridge,
)


def make_bridge(backend=BACKEND_AUTO):
    folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    bridge = WeChatBridge(data_dir=Path(folder.name), backend=backend)
    return folder, bridge


def availability(*, wechatauto=False, wxauto4=False, wxauto=False):
    """构造 `_module_available` 的替身：只认给定的包名。"""
    available = {name for name, flag in (("wechatauto", wechatauto),
                                         ("wxauto4", wxauto4),
                                         ("wxauto", wxauto)) if flag}
    return lambda name: name in available


class ExplicitBackendTests(unittest.TestCase):
    def test_explicit_backends_are_returned_as_is(self):
        for backend in SUPPORTED_BACKENDS:
            with self.subTest(backend=backend):
                folder, bridge = make_bridge(backend)
                try:
                    with patch("src.wechat_bridge._client_executable") as probe, \
                            patch("src.wechat_bridge._module_available") as modules:
                        self.assertEqual(bridge.detect_backend(), backend)
                    # 显式配置下连探测都不该发生
                    probe.assert_not_called()
                    modules.assert_not_called()
                finally:
                    folder.cleanup()

    def test_backend_name_is_normalised(self):
        for value, expected in (("WECHATAUTO", BACKEND_WECHATAUTO),
                                (" mock ", BACKEND_MOCK),
                                ("wechatauto", BACKEND_WECHATAUTO)):
            with self.subTest(value=value):
                folder, bridge = make_bridge(value)
                try:
                    self.assertEqual(bridge.requested_backend, expected)
                    self.assertEqual(bridge.detect_backend(), expected)
                finally:
                    folder.cleanup()


class AutoDetectionTests(unittest.TestCase):
    def setUp(self):
        self._folder, self.bridge = make_bridge(BACKEND_AUTO)

    def tearDown(self):
        self._folder.cleanup()

    def detect(self, client="", **packages):
        with patch("src.wechat_bridge._client_executable", return_value=client), \
                patch("src.wechat_bridge._module_available",
                      side_effect=availability(**packages)):
            return self.bridge.detect_backend()

    def test_weixin_client_prefers_wechatauto(self):
        self.assertEqual(self.detect("weixin.exe", wechatauto=True, wxauto4=True),
                         BACKEND_WECHATAUTO)

    def test_weixin_client_falls_back_to_wxauto4(self):
        self.assertEqual(self.detect("weixin.exe", wxauto4=True), BACKEND_WXAUTO4)

    def test_weixin_client_without_packages_goes_mock(self):
        # 有微信在跑但没有任何可用库：只能进模拟模式，不能假装连上了
        self.assertEqual(self.detect("weixin.exe"), BACKEND_MOCK)

    def test_legacy_wechat_client_prefers_classic_wxauto(self):
        self.assertEqual(self.detect("wechat.exe", wxauto=True, wechatauto=True),
                         BACKEND_WXAUTO)

    def test_legacy_wechat_client_can_use_wechatauto(self):
        self.assertEqual(self.detect("wechat.exe", wechatauto=True), BACKEND_WECHATAUTO)

    def test_legacy_wechat_client_without_packages_goes_mock(self):
        self.assertEqual(self.detect("wechat.exe"), BACKEND_MOCK)

    def test_no_client_process_uses_available_package(self):
        self.assertEqual(self.detect(wechatauto=True), BACKEND_WECHATAUTO)
        self.assertEqual(self.detect(wxauto4=True), BACKEND_WXAUTO4)
        self.assertEqual(self.detect(wxauto=True), BACKEND_WXAUTO)

    def test_nothing_detected_returns_mock(self):
        # 这是最关键的一条：不能假装连上了微信
        self.assertEqual(self.detect(), BACKEND_MOCK)


class ModuleAvailabilityTests(unittest.TestCase):
    def test_missing_module_is_reported_as_unavailable(self):
        from src.wechat_bridge import _module_available

        self.assertFalse(_module_available("definitely_not_installed_zzz"))

    def test_stdlib_module_is_available(self):
        from src.wechat_bridge import _module_available

        self.assertTrue(_module_available("json"))

    def test_broken_spec_lookup_does_not_raise(self):
        from src.wechat_bridge import _module_available

        with patch("importlib.util.find_spec", side_effect=ValueError("bad name")):
            self.assertFalse(_module_available("wechatauto"))


class MockConnectTests(unittest.TestCase):
    def test_mock_connect_is_online_with_an_account_name(self):
        folder, bridge = make_bridge(BACKEND_MOCK)
        try:
            ok, detail = bridge.connect()
            self.assertTrue(ok, detail)
            self.assertTrue(bridge.online)
            self.assertEqual(bridge.backend, BACKEND_MOCK)
            self.assertTrue(bridge.account_name, "模拟模式的账号名不能是空串")
            self.assertTrue(bridge.self_id)
            self.assertIn("模拟", bridge.degraded)
        finally:
            folder.cleanup()

    def test_auto_with_nothing_available_connects_as_mock(self):
        folder, bridge = make_bridge(BACKEND_AUTO)
        try:
            with patch("src.wechat_bridge._client_executable", return_value=""), \
                    patch("src.wechat_bridge._module_available", return_value=False):
                ok, detail = bridge.connect()
            self.assertTrue(ok, detail)
            self.assertEqual(bridge.backend, BACKEND_MOCK)
            self.assertTrue(bridge.online)
        finally:
            folder.cleanup()

    def test_mock_backend_send_is_recorded_not_sent(self):
        folder, bridge = make_bridge(BACKEND_MOCK)
        try:
            bridge.connect()
            ok, detail = bridge.send_text("12345678@chatroom", "在的", "测试群", ["阿明"])
            self.assertTrue(ok)
            self.assertIn("模拟", detail)
            self.assertEqual(bridge.sent_log[-1]["text"], "在的")
            self.assertEqual(bridge.sent_log[-1]["at"], ["阿明"])
        finally:
            folder.cleanup()

    def test_status_reports_backend_and_online(self):
        folder, bridge = make_bridge(BACKEND_MOCK)
        try:
            bridge.connect()
            status = bridge.status()
            self.assertEqual(status["backend"], BACKEND_MOCK)
            self.assertTrue(status["online"])
            self.assertTrue(status["account"])
        finally:
            folder.cleanup()

    def test_disconnect_marks_offline(self):
        folder, bridge = make_bridge(BACKEND_MOCK)
        try:
            bridge.connect()
            bridge.disconnect()
            self.assertFalse(bridge.online)
        finally:
            folder.cleanup()


if __name__ == "__main__":
    unittest.main()
