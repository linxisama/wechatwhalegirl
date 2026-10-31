"""插件审计（1.3.47 的 A3）：一行 JSONL 记一次插件调入。

锁的是三件事：能查到（插件/钩子/耗时/成败/异常摘要）、绝不带正文、
写坏了不影响插件链路。另加两条硬约束：单文件到上限滚一次、坏行跳过。
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from src import plugin_audit
from src.plugin_health import PluginHealth


class Demo:
    """最小的"插件"：审计只需要能认出它叫什么。"""
    demo_id = "demo_plugin"

    def on_ingest(self, message, runtime):
        return None

    def boom(self, message, runtime):
        raise ValueError("插件炸了\n第二行不该出现")

    def slow(self, message, runtime):
        time.sleep(0.4)
        return None

    async def a_on_pre_ai(self, message, conversation, runtime):
        return None


class Probe(PluginHealth):
    """绕开落盘的健康状态机，只借用 bounded/bounded_sync 与审计口径。"""

    def __init__(self):
        pass

    def plugin_id(self, plugin):
        return getattr(plugin, "demo_id", "unknown")


class AuditBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="audit147-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "plugin_audit.jsonl"
        plugin_audit.set_path(self.path)
        os.environ.pop(plugin_audit.ENV_SWITCH, None)
        self.addCleanup(plugin_audit.set_path, None)
        self.addCleanup(os.environ.pop, plugin_audit.ENV_SWITCH, None)

    def records(self, limit=50):
        return plugin_audit.read_records(limit, path=self.path)


class RecordTests(AuditBase):
    def test_success_line_has_expected_shape(self):
        plugin_audit.record("demo_plugin", "on_ingest", True, 12.6)
        rows = self.records()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["plugin"], "demo_plugin")
        self.assertEqual(row["hook"], "on_ingest")
        self.assertTrue(row["ok"])
        self.assertEqual(row["ms"], 12)
        self.assertNotIn("error", row)
        self.assertIn("ts", row)

    def test_failure_line_carries_one_line_error(self):
        try:
            raise ValueError("炸了\n带换行")
        except ValueError as exc:
            plugin_audit.record("demo_plugin", "generate", False, 3.2, error=exc)
        raw = self.path.read_text("utf-8")
        self.assertEqual(len(raw.strip().splitlines()), 1)
        row = self.records()[0]
        self.assertFalse(row["ok"])
        self.assertIn("ValueError", row["error"])
        self.assertNotIn("\n", row["error"])

    def test_record_swallows_write_failure(self):
        plugin_audit.set_path(self.root)          # 指向目录：写入必失败
        plugin_audit.record("demo_plugin", "on_ingest", True, 1.0)   # 不许抛
        self.assertEqual(plugin_audit.read_records(10, path=self.root), [])

    def test_switch_off_writes_nothing(self):
        os.environ[plugin_audit.ENV_SWITCH] = "0"
        plugin_audit.record("demo_plugin", "on_ingest", True, 1.0)
        self.assertFalse(self.path.exists())

    def test_error_text_is_capped(self):
        try:
            raise RuntimeError("长" * 500)
        except RuntimeError as exc:
            text = plugin_audit._safe_error(exc)
        self.assertLessEqual(len(text), plugin_audit.ERROR_KEEP)

    def test_hook_label_falls_back(self):
        self.assertEqual(plugin_audit.hook_label(Demo.on_ingest), "on_ingest")
        self.assertEqual(plugin_audit.hook_label(123), "?")


class RotationAndReadTests(AuditBase):
    def test_rotation_keeps_one_previous_file(self):
        old = plugin_audit.MAX_BYTES
        plugin_audit.MAX_BYTES = 200
        try:
            for _ in range(20):
                plugin_audit.record("demo_plugin", "on_ingest", True, 1.0)
        finally:
            plugin_audit.MAX_BYTES = old
        self.assertTrue((self.root / "plugin_audit.jsonl.1").exists(), "应当滚出一份旧文件")

    def test_broken_lines_are_skipped(self):
        self.path.write_text('{"plugin": "a", "hook": "on_ingest", "ok": true, "ms": 1}\n这不是 json\n',
                             "utf-8")
        rows = self.records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["plugin"], "a")

    def test_missing_file_reads_empty(self):
        self.assertEqual(self.records(), [])

    def test_limit_returns_tail(self):
        for index in range(5):
            plugin_audit.record("p" + str(index), "on_ingest", True, index)
        rows = plugin_audit.read_records(2, path=self.path)
        self.assertEqual([row["plugin"] for row in rows], ["p3", "p4"])


class WrapperIntegrationTests(AuditBase):
    def setUp(self):
        super().setUp()
        self.probe = Probe()
        self.demo = Demo()

    def test_sync_success_is_audited(self):
        self.probe.bounded_sync(self.demo.on_ingest, {"text": ""}, None, timeout=5.0)
        row = self.records()[-1]
        self.assertEqual(row["plugin"], "demo_plugin")
        self.assertEqual(row["hook"], "on_ingest")
        self.assertTrue(row["ok"])

    def test_sync_failure_is_audited_and_reraised(self):
        with self.assertRaises(ValueError):
            self.probe.bounded_sync(self.demo.boom, {}, None, timeout=5.0)
        row = self.records()[-1]
        self.assertFalse(row["ok"])
        self.assertIn("ValueError", row["error"])

    def test_sync_timeout_is_audited(self):
        with self.assertRaises(TimeoutError):
            self.probe.bounded_sync(self.demo.slow, {}, None, timeout=0.05)
        row = self.records()[-1]
        self.assertEqual(row["hook"], "slow")
        self.assertFalse(row["ok"])
        self.assertIn("timeout", row["error"])

    def test_async_success_is_audited(self):
        asyncio.run(self.probe.bounded(self.demo.a_on_pre_ai, {}, [], None, timeout=5.0))
        row = self.records()[-1]
        self.assertEqual(row["hook"], "a_on_pre_ai")
        self.assertTrue(row["ok"])

    def test_message_body_never_reaches_the_audit_line(self):
        secret = "主人说：我的银行卡密码是 123456"
        self.probe.bounded_sync(self.demo.on_ingest, {"text": secret}, None, timeout=5.0)
        raw = self.path.read_text("utf-8")
        self.assertNotIn("123456", raw)
        self.assertNotIn(secret, raw)


if __name__ == "__main__":
    unittest.main()
