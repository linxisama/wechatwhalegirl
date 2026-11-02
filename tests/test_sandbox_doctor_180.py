# -*- coding: utf-8 -*-
"""1.3.80：容器管理器服务坏掉的机器要给出准确结论，而不是笼统说「服务没启动」。

对方那台 Win10 19045 精简版：CmService 卡在 START_PENDING、组件存储缺 BaseImages、
事件 7023 报「找不到文件」。这里钉住三件事：证据怎么采、结论怎么判、界面怎么措辞。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import sandbox_doctor
from src import sandbox_provision as provision
from src.sandbox_provision_api import SandboxProvisionService

START_PENDING = "STATE              : 2  START_PENDING"
RUNNING = "STATE              : 4  RUNNING"
STOPPED = "STATE              : 1  STOPPED"


def _store(base_images=False, portable_base_layer=False, utility_vm=False):
    return {"base_images": base_images, "portable_base_layer": portable_base_layer,
            "utility_vm": utility_vm}


class ContainerStoreTests(unittest.TestCase):
    def test_reports_missing_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = sandbox_doctor.container_store_info(root=tmp, reg=lambda path, names: {})
        self.assertFalse(info["base_images"])
        self.assertFalse(info["portable_base_layer"])
        self.assertFalse(info["utility_vm"])
        self.assertEqual(info["root"], tmp)

    def test_reports_existing_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "BaseImages").mkdir()
            (Path(tmp) / "PortableBaseLayer").mkdir()
            info = sandbox_doctor.container_store_info(root=tmp,
                                                      reg=lambda path, names: {"*": "x"})
        self.assertTrue(info["base_images"])
        self.assertTrue(info["portable_base_layer"])
        self.assertTrue(info["utility_vm"])

    def test_registry_reader_failure_is_not_fatal(self):
        def boom(path, names):
            raise OSError("no key")
        with tempfile.TemporaryDirectory() as tmp:
            info = sandbox_doctor.container_store_info(root=tmp, reg=boom)
        self.assertFalse(info["utility_vm"])


class ServiceEventTests(unittest.TestCase):
    def test_parses_file_not_found(self):
        text = "\r\n".join([
            "Event[0]:",
             "  日志名称: System",
            "  日期: 2026/11/2 8:02:54",
            "  事件 ID: 7023",
            "  描述:",
            "容器管理器服务 服务因下列错误而停止:",
            "The system cannot find the file specified.",
        ])

        def runner(command, timeout=None):
            self.assertIn("7023", " ".join(command))
            return 0, text

        events = sandbox_doctor.service_events(runner=runner)
        self.assertTrue(events["file_not_found"])
        self.assertIn("容器管理器服务", events["tail"])
        self.assertEqual(events["exit"], 0)

    def test_runner_failure_is_not_fatal(self):
        def runner(command, timeout=None):
            raise OSError("wevtutil missing")

        events = sandbox_doctor.service_events(runner=runner)
        self.assertEqual(events["exit"], -1)
        self.assertFalse(events["file_not_found"])

    def test_clean_log_is_not_file_not_found(self):
        def runner(command, timeout=None):
            return 0, "事件 ID: 7023\r\n容器管理器服务 服务已停止。\r\n"

        events = sandbox_doctor.service_events(runner=runner)
        self.assertFalse(events["file_not_found"])


class BrokenVerdictTests(unittest.TestCase):
    def _report(self, *, cm=START_PENDING, store=None, events=None):
        return {
            "binaries": {"sandbox_app": {"exists": True}, "wsb": {"exists": True}},
            "services": {"CmService": {"state": sandbox_doctor._sc_state(cm)}},
            "container_store": store if store is not None else _store(),
            "events": events if events is not None else {"file_not_found": False},
        }

    def test_start_pending_with_missing_store_is_broken(self):
        code, hint = sandbox_doctor.verdict(self._report())
        self.assertEqual(code, "container_service_broken")
        self.assertIn("本机模式", hint)
        self.assertIn("CmService", hint)

    def test_event_7023_alone_is_broken(self):
        code, _ = sandbox_doctor.verdict(
            self._report(store=_store(base_images=True, portable_base_layer=True),
                         events={"file_not_found": True}))
        self.assertEqual(code, "container_service_broken")

    def test_stopped_with_healthy_store_stays_container_service_down(self):
        code, hint = sandbox_doctor.verdict(
            self._report(cm=STOPPED, store=_store(base_images=True, portable_base_layer=True)))
        self.assertEqual(code, "container_service_down")
        self.assertIn("STOPPED", hint)

    def test_running_with_missing_store_is_not_broken(self):
        code, _ = sandbox_doctor.verdict(self._report(cm=RUNNING))
        self.assertEqual(code, "ok")

    def test_render_has_new_sections(self):
        report = self._report()
        report.update({"version": "1.3.80", "verdict": "container_service_broken",
                       "hint": "h", "events": {"exit": 0, "lines": 1, "file_not_found": True,
                                              "tail": "7023"}})
        text = sandbox_doctor.render(report)
        self.assertIn("[容器存储]", text)
        self.assertIn("[服务事件]", text)
        self.assertIn("file_not_found=True", text)


class ProvisionBrokenTests(unittest.TestCase):
    def test_container_broken_detects_start_pending(self):
        def runner(cmd, timeout=None):
            return {"returncode": 0, "stdout": START_PENDING, "stderr": ""}

        self.assertTrue(provision._container_broken(runner=runner, store=_store()))

    def test_running_service_is_never_broken(self):
        def runner(cmd, timeout=None):
            return {"returncode": 0, "stdout": RUNNING, "stderr": ""}

        self.assertFalse(provision._container_broken(runner=runner, store=_store()))

    def test_unknown_state_is_not_broken(self):
        def runner(cmd, timeout=None):
            return {"returncode": -1, "stdout": "", "stderr": "denied"}

        self.assertFalse(provision._container_broken(runner=runner, store=_store()))

    def test_detect_reports_broken_state(self):
        with mock.patch.object(provision, "_read_edition", return_value="Professional"), \
                mock.patch.object(provision, "_probe_feature", return_value={"state": "Enabled"}), \
                mock.patch.object(provision, "_wsb_exe_exists", return_value=True), \
                mock.patch.object(provision, "_container_broken", return_value=True):
            status = provision.detect(runner=lambda cmd, timeout: {}, registry_reader=lambda: {})
        self.assertEqual(status["state"], "broken")
        self.assertEqual(status["reason"], "container_service_broken")
        self.assertEqual(status["next_step"], "manual")
        self.assertIn("本机模式", status["hint"])

    def test_probe_verdict_maps_broken_to_not_ok(self):
        service = SandboxProvisionService(runner=lambda cmd, timeout: {})
        with mock.patch.object(provision, "status",
                               return_value={"state": "broken", "message": "沙盒起不来",
                                             "hint": "用本机模式", "next_step": "manual"}):
            verdict = service._probe_verdict("")
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "container_service_broken")
        self.assertEqual(verdict["next_step"], "manual")
        self.assertIn("本机模式", verdict["message"])


if __name__ == "__main__":
    unittest.main()
