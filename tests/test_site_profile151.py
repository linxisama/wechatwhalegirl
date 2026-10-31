"""1.3.48 自检升级：Windows 名称按 build、多屏不再误警、微信版本兼容门、OCR 安装路径、端口可覆盖。

这些断言全部离线：注册表、OCR 语言包、屏幕指标都被替换成可控假值，
只有端口用例会起子进程（它测的就是环境变量在导入期生效）。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from src import site_profile
from src.runtime_identity import DEFAULT_PORT

ROOT = Path(site_profile.__file__).resolve().parent.parent


class _FakePath:
    """只实现 _probe_windows_sandbox 用到的那两个方法。"""

    def __init__(self, text, exists=True):
        self._text = str(text)
        self._exists = bool(exists)

    def __truediv__(self, part):
        return _FakePath(self._text + "\\" + str(part), self._exists)

    def is_file(self):
        return self._exists

    def __str__(self):
        return self._text


def _edition(edition_id="Professional", build="26200", display="25H2"):
    return {"EditionID": edition_id, "ProductName": "Windows 10 Pro",
            "DisplayVersion": display, "CurrentBuildNumber": build}


class WindowsNameTests(unittest.TestCase):
    def test_build_decides_family_not_registry_product_name(self):
        """本机注册表 ProductName 写的是 'Windows 10 Pro'，build 26200 必须说成 Windows 11。"""
        self.assertEqual(site_profile._windows_name(26200, "Windows 10 Pro", "Professional"), "Windows 11")
        self.assertEqual(site_profile._windows_name("22000", "Windows 10 Pro", "Professional"), "Windows 11")
        self.assertEqual(site_profile._windows_name(19045, "Windows 10 Pro", "Professional"), "Windows 10")
        self.assertEqual(site_profile._windows_name(10240, "Windows 10 Home", "Core"), "Windows 10")
        # build 读不到时才退回注册表名，不猜版本
        self.assertEqual(site_profile._windows_name(0, "Windows 10 Pro", "Professional"), "Windows 10 Pro")
        self.assertEqual(site_profile._windows_name("", "", ""), "Windows（版本未知）")

    def test_sandbox_detail_shows_windows_11_and_keeps_dual_criteria(self):
        with patch.object(site_profile, "_registry_edition", return_value=_edition()), \
                patch.object(site_profile, "Path", lambda value: _FakePath(value)):
            item = site_profile._probe_windows_sandbox()
            self.assertEqual(item["status"], "ok")
            self.assertTrue(item["detail"].startswith("Windows 11（build 26200，25H2，Professional）"), item["detail"])
            self.assertNotIn("Windows 10", item["detail"])
            self.assertTrue(item["data"]["sandbox_available"])
            # 家庭版有 exe 也不算可用：EditionID 判据不能被 build 覆盖
            with patch.object(site_profile, "_registry_edition", return_value=_edition("Core")):
                home = site_profile._probe_windows_sandbox()
            self.assertEqual(home["status"], "warn")
            self.assertIn("不是专业版/企业版", home["detail"])
            # 没 exe 也不行：exe 判据同样保留
            with patch.object(site_profile, "Path", lambda value: _FakePath(value, False)):
                missing = site_profile._probe_windows_sandbox()
            self.assertEqual(missing["status"], "warn")
            self.assertIn("没有 WindowsSandbox.exe", missing["detail"])


class _FakeUser32:
    def __init__(self, monitors):
        self._monitors = int(monitors)

    def GetSystemMetrics(self, index):
        return {0: 2560, 1: 1440, 80: self._monitors}.get(int(index), 0)

    def SystemParametersInfoW(self, action, a, rect, flags):
        target = rect._obj
        target.left, target.top, target.right, target.bottom = 0, 0, 2560, 1400
        return 1


class ScreenWordingTests(unittest.TestCase):
    def test_multi_monitor_is_ok_because_clicks_follow_the_virtual_desktop(self):
        with patch.object(site_profile.ctypes, "WinDLL", lambda *a, **k: _FakeUser32(2)):
            item = site_profile._probe_screen()
        self.assertEqual(item["status"], "ok", item["detail"])
        self.assertIn("MOUSEEVENTF_VIRTUALDESK", item["detail"])
        self.assertIn("不会落到别的屏", item["detail"])
        self.assertNotIn("可能落到别的屏", item["detail"])
        self.assertEqual(item["data"]["monitors"], 2)
        self.assertEqual(item["data"]["width"], 2560)
        self.assertEqual(item["data"]["height"], 1440)


class WechatVersionGateTests(unittest.TestCase):
    def test_version_parts_tolerates_dirty_input(self):
        self.assertEqual(site_profile._version_parts("4.1.15.56"), (4, 1, 15, 56))
        self.assertEqual(site_profile._version_parts("4.1.15"), (4, 1, 15))
        self.assertEqual(site_profile._version_parts("4,1,15"), (4, 1, 15))
        self.assertEqual(site_profile._version_parts(" 4.1 "), (4, 1))
        for bad in ("", None, "abc", "NA", "-"):
            self.assertEqual(site_profile._version_parts(bad), (), bad)

    def test_gate_maps_major_versions(self):
        self.assertEqual(site_profile._wechat_version_gate("4.1.15.56")[0], "ok")
        self.assertEqual(site_profile._wechat_version_gate("4.1.15")[0], "ok")
        for unsupported in ("4.0.6.3", "3.9.12.1", "5.0.0"):
            status, note = site_profile._wechat_version_gate(unsupported)
            self.assertEqual(status, "warn", unsupported)
            self.assertIn("未验证", note, unsupported)
        self.assertIn("测试一次", site_profile._wechat_version_gate("4.0.6.3")[1])
        for missing in ("", "abc", None):
            status, note = site_profile._wechat_version_gate(missing)
            self.assertEqual(status, "warn")
            self.assertIn("解析不出来", note)

    def _probe(self, version):
        processes = [{"pid": 4242, "name": "Weixin.exe", "exe": str(ROOT / "Weixin.exe")}]
        window = {"width": 800, "height": 600, "left": 0, "top": 0, "title": "微信"}
        with patch.object(site_profile, "_wechat_processes", return_value=processes), \
                patch.object(site_profile, "_find_window", return_value=window), \
                patch.object(site_profile, "_file_version", return_value=version):
            return site_profile._probe_wechat()

    def test_probe_wechat_ok_on_41(self):
        item = self._probe("4.1.15.56")
        self.assertEqual(item["status"], "ok", item["detail"])
        self.assertTrue(item["data"]["version_ok"])

    def test_probe_wechat_warns_on_unverified_version(self):
        item = self._probe("4.0.6.3")
        self.assertEqual(item["status"], "warn")
        self.assertIn("未验证", item["detail"])
        self.assertFalse(item["data"]["version_ok"])
        self.assertEqual(item["data"]["version"], "4.0.6.3")


class OcrStepTests(unittest.TestCase):
    def test_missing_chinese_package_gives_actionable_steps(self):
        with patch.object(site_profile, "_ocr_languages", return_value=["en-US", "ja-JP"]):
            item = site_profile._probe_ocr()
        self.assertEqual(item["status"], "warn")
        self.assertIn("光学字符识别", item["detail"])
        self.assertIn("设置 → 时间和语言 → 语言和区域 → 中文 → 语言选项", item["detail"])
        self.assertEqual(item["data"]["languages"], ["en-US", "ja-JP"])
        self.assertEqual(item["data"]["chinese"], [])


class PortOverrideTests(unittest.TestCase):
    def _port(self, value):
        env = {k: v for k, v in os.environ.items() if k != "WECHAT_CONSOLE_PORT"}
        if value is not None:
            env["WECHAT_CONSOLE_PORT"] = value
        result = subprocess.run([sys.executable, "-c", "from src.runtime_identity import PORT;print(PORT)"],
                                cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(result.stdout.strip())

    def test_env_override_and_fallbacks(self):
        self.assertEqual(self._port(None), 8091)
        self.assertEqual(self._port("9123"), 9123)
        for broken in ("abc", "0", "70000", "-1", ""):
            self.assertEqual(self._port(broken), 8091, broken)

    def test_generated_connector_port_matches_backend(self):
        # 生成出去的接入脚本自带一份 console_port；它和后台 _console_port 必须是同一条容错规则，
        # 否则环境变量写错时脚本会崩而控制台只是回落端口。
        from src.agent_mcp_connector import CONNECTOR
        from src.runtime_identity import _console_port

        class _FakeOS:
            """只提供 os.environ，免得为了跑一次断言去改本进程的环境变量。"""

            def __init__(self, value, present=True):
                self.environ = {"WECHAT_CONSOLE_PORT": value} if present else {}

        space = {}
        exec(compile(CONNECTOR, "<connector>", "exec"), space)
        generated = space["console_port"]

        def generated_port(value, present=True):
            space["os"] = _FakeOS(value, present)
            return generated()

        for broken in ("abc", "0", "70000", "", " "):
            self.assertEqual(generated_port(broken), DEFAULT_PORT, broken)
        self.assertEqual(generated_port(None, present=False), DEFAULT_PORT)
        for good in ("9123", " 9123 ", "65535", "1"):
            self.assertEqual(generated_port(good), int(good), good)
        for raw in ("abc", "0", "70000", "", "9123", " 9123 ", "-1", "65536"):
            self.assertEqual(generated_port(raw), _console_port(raw), raw)


    def test_defaults_are_not_hardcoded_urls(self):
        for name in ("runtime_identity.py", "desktop_startup.py", "agent_mcp_connector.py"):
            source = (ROOT / "src" / name).read_text(encoding="utf-8")
            self.assertNotIn("http://127.0.0.1:8091", source, name)
        startup = (ROOT / "src" / "desktop_startup.py").read_text(encoding="utf-8")
        self.assertIn("default=PORT", startup)
        identity = (ROOT / "src" / "runtime_identity.py").read_text(encoding="utf-8")
        self.assertIn("WECHAT_CONSOLE_PORT", identity)
        self.assertEqual(DEFAULT_PORT, 8091)

    def test_runtime_identity_keeps_lf_without_bom(self):
        data = (ROOT / "src" / "runtime_identity.py").read_bytes()
        self.assertNotIn(b"\r\n", data)
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertGreater(data.count(b"\n"), 0)


if __name__ == "__main__":
    unittest.main()
