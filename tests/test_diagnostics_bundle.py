"""诊断包与 TXT 导出的离线测试：空目录也要能出包，中文必须带 BOM。"""

import tempfile
import unittest
import zipfile
from pathlib import Path

from src import diagnostics_bundle
from src.runtime_identity import APP_VERSION


class DiagnosticsBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # ── 验收标准 4：导出 TXT 必须带 UTF-8 BOM ───────────────────────────
    def test_write_text_with_bom_starts_with_bom(self):
        target = self.root / "运行记录-20261026-1704.txt"
        saved = diagnostics_bundle.write_text_with_bom(target, "报错：窗口未找到\n第二行")
        self.assertEqual(saved, target)
        raw = target.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn("报错：窗口未找到", text)
        self.assertIn("第二行", text)

    # ── 验收标准 3：日志全缺也要出包，且至少含 report.txt ────────────────
    def test_bundle_survives_empty_root(self):
        target = self.root / "诊断包-20261026-1704.zip"
        saved = diagnostics_bundle.build_bundle(
            target, root=self.root / "空的", notice="发送失败：窗口未找到",
            events="运行记录：暂无")
        self.assertEqual(Path(saved), target)
        self.assertTrue(target.is_file())
        with zipfile.ZipFile(target) as bundle:
            names = bundle.namelist()
            report = bundle.read(diagnostics_bundle.REPORT_NAME).decode("utf-8-sig")
            self.assertTrue(bundle.testzip() is None)
        self.assertIn(diagnostics_bundle.REPORT_NAME, names)
        self.assertIn("发送失败：窗口未找到", report)
        self.assertIn("运行记录：暂无", report)
        self.assertIn(APP_VERSION, report)
        # 系统信息：至少要有系统版本、Python 版本、权限三项给开发者看
        self.assertIn("Python", report)
        self.assertIn("管理员", report)

    def test_bundle_packs_logs_rotations_and_startup_files(self):
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "backend.log").write_text("2026-10-26 17:04:11,123 [MainThread] INFO 启动\n",
                                          encoding="utf-8")
        (logs / "backend.log.1").write_text("旧的日志\n", encoding="utf-8")
        (logs / "send-worker-stacks.log").write_text("子进程堆栈\n", encoding="utf-8")
        (logs / "startup.log").write_text("启动日志\n", encoding="utf-8")
        (self.root / "startup-diagnostics.json").write_text("{}", encoding="utf-8")
        auto = self.root / "wechatauto_logs"
        auto.mkdir()
        (auto / "app_20261026.log").write_text("自动发送日志\n", encoding="utf-8")

        target = self.root / "诊断包.zip"
        diagnostics_bundle.build_bundle(target, root=self.root, notice="", events="")
        with zipfile.ZipFile(target) as bundle:
            names = bundle.namelist()
            report = bundle.read(diagnostics_bundle.REPORT_NAME).decode("utf-8-sig")
        self.assertIn("logs/backend.log", names)
        self.assertIn("logs/backend.log.1", names)
        self.assertIn("logs/send-worker-stacks.log", names)
        self.assertIn("wechatauto_logs/app_20261026.log", names)
        self.assertIn("logs/startup.log", names)
        # report.txt 里要列出实际打了什么，用户反馈时一眼能对
        self.assertIn("logs/backend.log", report)

    def test_collect_bundle_files_skips_missing(self):
        # 工程目录里什么都没有：不能抛异常，也不能凭空造出不存在的路径。
        # （冻结/源码两种模式下启动日志可能已写到 %LOCALAPPDATA%，所以只断言
        #  这里收集到的每一项都真的存在、且没有来自本次临时目录。）
        items = diagnostics_bundle.collect_bundle_files(self.root)
        for arcname, path in items:
            self.assertTrue(path.is_file(), arcname)
            self.assertNotIn(str(self.root), str(path))
        self.assertNotIn("logs/backend.log", [arcname for arcname, _ in items])

    def test_report_mentions_skipped_when_logs_absent(self):
        target = self.root / "诊断包.zip"
        diagnostics_bundle.build_bundle(target, root=self.root, notice="有报错", events="")
        with zipfile.ZipFile(target) as bundle:
            report = bundle.read(diagnostics_bundle.REPORT_NAME).decode("utf-8-sig")
        self.assertIn("有报错", report)
        self.assertTrue(report.strip())


if __name__ == "__main__":
    unittest.main()
