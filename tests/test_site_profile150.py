"""机器/环境特征探测（GET /api/env + 面板「兼容性自检」）的离线测试。

覆盖七项 key 的顺序、status 三值、单项探测崩了必须降级成 unknown、源码里不许出现
DPI 感知设置函数、窗口标题/用户名不外泄，以及路由真的挂在 app 上。
"""
import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

from src import site_profile
from src.runtime_identity import APP_VERSION, PORT
from src.wechat_app import WeChatRuntime, create_app


class ProfileShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = site_profile.collect()

    # ── 九项 key 齐全、顺序固定（1.3.60 增加权限与桌面DSH两项）────────────────
    def test_items_keep_fixed_keys_and_order(self):
        keys = [item["key"] for item in self.profile["items"]]
        self.assertEqual(keys, list(site_profile.ITEM_ORDER))
        self.assertEqual(keys, ["scale", "screen", "wechat", "ocr",
                                "windows_sandbox", "port", "privilege", "dsh", "data_root"])

    # ── status 只能是三值之一，detail/data 形状固定 ──────────────────────────
    def test_status_is_one_of_three_values(self):
        for item in self.profile["items"]:
            self.assertIn(item["status"], ("ok", "warn", "unknown"), item["key"])
            self.assertIsInstance(item["detail"], str, item["key"])
            self.assertTrue(item["detail"].strip(), item["key"])
            self.assertIsInstance(item["data"], dict, item["key"])
        self.assertEqual(self.profile["ok"],
                         not any(item["status"] == "unknown" for item in self.profile["items"]))

    def test_summary_and_generated_at_are_usable(self):
        self.assertTrue(str(self.profile.get("summary") or "").strip())
        datetime.fromisoformat(self.profile["generated_at"])  # 必须是标准库能解析的 ISO8601
        self.assertEqual(self.profile["platform"]["app_version"], APP_VERSION)
        self.assertEqual(self.profile["platform"]["port"], PORT)

    # ── 探测块异常 → 该块 unknown，collect() 不抛 ────────────────────────────
    def test_each_probe_failure_degrades_to_unknown(self):
        for key in site_profile.ITEM_ORDER:
            with patch.object(site_profile, site_profile._PROBE_NAMES[key],
                              side_effect=RuntimeError("boom")):
                try:
                    result = site_profile.collect()
                except Exception as exc:  # noqa: BLE001 - 这里就是要它别抛
                    self.fail(f"{key} 探测异常被抛到调用方：{exc!r}")
            items = {item["key"]: item for item in result["items"]}
            self.assertEqual(items[key]["status"], "unknown", key)
            self.assertIn("boom", items[key]["detail"])
            self.assertEqual([item["key"] for item in result["items"]],
                             list(site_profile.ITEM_ORDER))
            self.assertFalse(result["ok"])

    def test_collect_finishes_within_budget(self):
        started = time.monotonic()
        site_profile.collect()
        self.assertLess(time.monotonic() - started, 3.0)

    def test_repeated_collect_leaves_no_temporary_probe_file(self):
        root = site_profile.data_root()
        site_profile.collect()
        site_profile.collect()
        self.assertEqual(list(root.glob(".site_profile_*.tmp")), [])


class PrivacyTests(unittest.TestCase):
    # ── 假数据注入：窗口标题是联系人昵称，exe 路径带用户名，都不许出现 ────────
    def test_window_title_and_home_path_never_reach_the_result(self):
        home = Path.home()
        processes = [{"pid": 4242, "name": "Weixin.exe", "exe": str(home / "Weixin.exe")}]
        window = {"width": 800, "height": 600, "left": 0, "top": 0, "title": "张三"}
        with patch.object(site_profile, "_wechat_processes", return_value=processes), \
                patch.object(site_profile, "_find_window", return_value=window), \
                patch.object(site_profile, "_file_version", return_value="4.1.15.56"):
            result = site_profile.collect()
        raw = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("张三", raw)
        self.assertNotIn(str(home), raw)
        item = {entry["key"]: entry for entry in result["items"]}["wechat"]
        self.assertEqual(item["status"], "ok")
        self.assertTrue(item["data"]["window"]["title_hidden"])
        self.assertNotIn("张三", item["data"]["window"]["title"])
        self.assertIn("~", item["data"]["exe"])

    # ── 只许读 DPI，源码里不许出现设置进程 DPI 感知的函数名 ──────────────────
    def test_dpi_awareness_setters_never_appear_in_source(self):
        source = Path(site_profile.__file__).read_text(encoding="utf-8")
        for name in ("SetProcessDpiAwarenessContext", "SetProcessDPIAware",
                     "SetProcessDpiAwareness"):
            self.assertNotIn(name, source)


class EnvRouteTests(unittest.TestCase):
    # ── GET /api/env 挂在 app 上，返回的就是 collect() 的形状 ────────────────
    def test_env_route_returns_the_profile(self):
        # 不用 TemporaryDirectory：后台仍开着 messages.sqlite3，Windows 上清理会报 WinError 32
        root = Path(tempfile.mkdtemp(prefix="env-route-"))
        self.addCleanup(shutil.rmtree, root, True)
        config = root / "config.wechat.yaml"
        config.write_text(yaml.safe_dump({
            "ai": {"api_url": "https://example.test/v1", "api_key": "fake", "model": "m"},
            "wechat": {"backend": "mock"}, "wechat_only": {"backend": "mock"},
        }), encoding="utf-8")
        bot = WeChatRuntime(root=root, config_path=config)
        client = TestClient(create_app(bot))
        try:
            response = client.get("/api/env")
        finally:
            client.close()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([item["key"] for item in body["items"]],
                         list(site_profile.ITEM_ORDER))
        self.assertIn("summary", body)
        self.assertIn("platform", body)
