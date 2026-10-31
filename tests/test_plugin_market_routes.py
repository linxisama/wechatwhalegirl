"""插件市场 HTTP 路由的端到端测试（真实 FastAPI 路由 + mock 掉的网络层）。

为什么 mock 网络层：真实安装要下载第三方代码，而联网在测试里既慢又不可复现。
这里只把 **httpx 的传输层**换掉（返回我们准备好的插件源码），其余全部走真实代码：
真实路由 → 真实安装器（含安全检查）→ 真实文件落盘 → 真实 PluginManager 加载。

顺带记录一个已验证的安全事实：用自签证书的 HTTPS 服务做端到端联调时，
安装器会拒绝并报 `CERTIFICATE_VERIFY_FAILED` —— 说明证书校验没有被绕过。
**刻意不提供"跳过证书校验"开关**，那是安全降级。
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock
import zipfile

import httpx

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from fastapi.testclient import TestClient

    HAS_CLIENT = True
except Exception:
    HAS_CLIENT = False

PLUGIN_CODE = '''"""测试用插件。"""
from src.plugins import FieldSchema, PluginBase, PluginMeta


class MarketProbe(PluginBase):
    META = PluginMeta(
        id="market_probe",
        name="市场探针",
        description="安装流程验证用",
        category="utility",
        enabled_by_default=False,
        config_schema={"enabled": FieldSchema(type="bool", label="启用")},
        default_settings={"enabled": False},
    )
'''

DANGEROUS_CODE = '''import os
from src.plugins import PluginBase, PluginMeta


class Evil(PluginBase):
    META = PluginMeta(id="evil_probe", name="坏插件", description="")
    def post_load(self):
        os.system("calc")
'''


def zip_bytes(files: dict) -> bytes:
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class MarketRouteTests(unittest.TestCase):
    def setUp(self):
        import yaml

        from src.wechat_app import WeChatRuntime, create_app, ROOT

        # 临时目录放在项目根的 .tmp 下（start_gui.bat / 自检.bat 也会建它）；
        # 直接跑 unittest 时它可能还不存在，这里自己兜一下，不依赖外部脚本。
        (ROOT / ".tmp").mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = pathlib.Path(self._tmp.name)
        config = self.root / "test.yaml"
        config.write_text(yaml.safe_dump({
            "ai": {"api_url": "https://example.test/v1", "api_key": "fake-key",
                   "model": "test-model"},
            "wechat": {"backend": "mock"},
            "wechat_only": {"backend": "mock"},
        }), encoding="utf-8")

        self.bot = WeChatRuntime(config_path=config, db_path=self.root / "test.sqlite")
        self.app = create_app(self.bot)
        self.client = TestClient(self.app)
        self.client.__enter__()

        self.manager = self.bot.plugins
        # 清掉上一个用例可能留下的自定义插件
        for plugin_id in list(self.manager.custom_plugin_ids()):
            path = self.manager.custom_plugin_path(plugin_id)
            if path.is_file():
                path.unlink()
            self.manager.unregister_custom(plugin_id)
        self.manager.reload()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._tmp.cleanup()

    def _mock_fetch(self, payload: bytes, status: int = 200):
        """把 httpx 的传输层换成固定响应（安装器内部用的是 httpx.Client）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=payload)

        transport = httpx.MockTransport(handler)
        original = httpx.Client

        def patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        return unittest.mock.patch.object(httpx, "Client", patched)

    def _install(self, payload, url="https://example.com/market_probe.py",
                 plugin_id="", confirmed=True, overwrite=False):
        with self._mock_fetch(payload):
            return self.client.post("/api/plugins/install", json={
                "url": url, "plugin_id": plugin_id,
                "confirmed": confirmed, "overwrite": overwrite})

    # ── 预检 ─────────────────────────────────────────────────────────────────
    def test_inspect_returns_metadata_without_writing(self):
        with self._mock_fetch(PLUGIN_CODE.encode()):
            response = self.client.post("/api/plugins/inspect",
                                        json={"url": "https://example.com/market_probe.py"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["plugin_id"], "market_probe")
        self.assertEqual(body["problems"], [])
        self.assertIn("PluginBase", body["preview"])
        # 关键：预检**不落盘**
        self.assertFalse(self.manager.custom_plugin_path("market_probe").is_file())

    def test_inspect_reports_dangerous_source(self):
        with self._mock_fetch(DANGEROUS_CODE.encode()):
            response = self.client.post("/api/plugins/inspect",
                                        json={"url": "https://example.com/evil.py"})
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["problems"], "应报出危险用法")
        self.assertTrue(any("系统命令" in p for p in body["problems"]))

    def test_inspect_rejects_http(self):
        response = self.client.post("/api/plugins/inspect",
                                    json={"url": "http://example.com/a.py"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("https", response.json()["detail"])

    def test_inspect_handles_download_failure(self):
        with self._mock_fetch(b"", status=500):
            response = self.client.post("/api/plugins/inspect",
                                        json={"url": "https://example.com/a.py"})
        self.assertEqual(response.status_code, 400)

    # ── 安装 ─────────────────────────────────────────────────────────────────
    def test_install_requires_confirmation(self):
        response = self._install(PLUGIN_CODE.encode(), confirmed=False)
        self.assertEqual(response.status_code, 400)
        self.assertIn("确认", response.json()["detail"])
        self.assertFalse(self.manager.custom_plugin_path("market_probe").is_file())

    def test_install_writes_file_and_loads_plugin(self):
        response = self._install(PLUGIN_CODE.encode())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["loaded"], "装完应该立刻被加载")

        path = self.manager.custom_plugin_path("market_probe")
        self.assertTrue(path.is_file(), "文件应落在自定义插件目录")
        self.assertIn("market_probe", self.manager._plugins)

    def test_installed_plugin_appears_in_list(self):
        self._install(PLUGIN_CODE.encode())
        plugins = self.client.get("/api/plugins").json()
        found = [p for p in plugins if p["id"] == "market_probe"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "市场探针")
        self.assertIn("enabled", found[0]["schema"])

    def test_installed_plugin_survives_reload(self):
        self._install(PLUGIN_CODE.encode())
        self.manager.reload()
        self.assertIn("market_probe", self.manager._plugins)

    def test_dangerous_source_is_refused(self):
        response = self._install(DANGEROUS_CODE.encode(), url="https://example.com/evil.py")
        self.assertEqual(response.status_code, 400)
        self.assertIn("安全检查", response.json()["detail"])
        self.assertFalse(self.manager.custom_plugin_path("evil_probe").is_file())

    def test_install_from_zip(self):
        data = zip_bytes({"README.md": "hi", "market_probe.py": PLUGIN_CODE})
        response = self._install(data, url="https://example.com/pack.zip")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.manager.custom_plugin_path("market_probe").is_file())

    def test_duplicate_install_needs_overwrite(self):
        self.assertEqual(self._install(PLUGIN_CODE.encode()).status_code, 200)
        again = self._install(PLUGIN_CODE.encode())
        self.assertEqual(again.status_code, 400)
        self.assertIn("已存在", again.json()["detail"])

    def test_install_with_explicit_id(self):
        # 显式标识必须与插件代码里的 META.id 一致，否则加载不到
        response = self._install(PLUGIN_CODE.encode(), plugin_id="market_probe")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["loaded"])
        self.assertTrue(self.manager.custom_plugin_path("market_probe").is_file())

    def test_install_mismatched_id_reports_load_failure(self):
        """文件名标识与代码里的 META.id 不一致时，必须**报失败**而不是假装成功。

        这种不一致会让文件躺在磁盘上但插件从不生效 —— 静默成功最难排查，
        所以接口要如实报 500 并说明原因。
        """
        response = self._install(PLUGIN_CODE.encode(), plugin_id="different_name")
        self.assertEqual(response.status_code, 500)
        self.assertIn("加载失败", response.json()["detail"])
        # 文件保留，方便用户修正后重装
        self.assertTrue(self.manager.custom_plugin_path("different_name").is_file())

    # ── 自定义清单 / 删除 ────────────────────────────────────────────────────
    def test_custom_list_shows_installed(self):
        self._install(PLUGIN_CODE.encode())
        body = self.client.get("/api/plugins/custom").json()
        ids = [item["id"] for item in body["plugins"]]
        self.assertIn("market_probe", ids)
        entry = next(i for i in body["plugins"] if i["id"] == "market_probe")
        self.assertTrue(entry["exists"])
        self.assertTrue(entry["loaded"])
        self.assertIn("custom_plugins", body["dir"])

    def test_delete_removes_file_and_plugin(self):
        self._install(PLUGIN_CODE.encode())
        response = self.client.delete("/api/plugins/market_probe")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.manager.custom_plugin_path("market_probe").is_file())
        self.assertNotIn("market_probe", self.manager._plugins)
        self.assertNotIn("market_probe", self.manager.custom_plugin_ids())

    def test_delete_core_builtin_is_refused(self):
        response = self.client.delete("/api/plugins/conversation_memory")
        self.assertEqual(response.status_code, 403)
        self.assertIn("不允许删除", response.json()["detail"])

    def test_delete_unknown_is_refused(self):
        response = self.client.delete("/api/plugins/nope_not_here")
        self.assertEqual(response.status_code, 403)

    def test_builtin_plugins_unaffected_after_install_and_delete(self):
        before = len(self.manager._plugins)
        self._install(PLUGIN_CODE.encode())
        self.assertEqual(len(self.manager._plugins), before + 1)
        self.client.delete("/api/plugins/market_probe")
        self.assertEqual(len(self.manager._plugins), before)


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class LocalInstallRouteTests(unittest.TestCase):
    """本地文件安装的路由测试（与远程共用同一套安全检查）。"""

    def setUp(self):
        import yaml

        from src.wechat_app import WeChatRuntime, create_app, ROOT

        (ROOT / ".tmp").mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = pathlib.Path(self._tmp.name)
        config = self.root / "test.yaml"
        config.write_text(yaml.safe_dump({
            "ai": {"api_url": "https://example.test/v1", "api_key": "fake-key",
                   "model": "test-model"},
            "wechat": {"backend": "mock"},
            "wechat_only": {"backend": "mock"},
        }), encoding="utf-8")
        self.bot = WeChatRuntime(config_path=config, db_path=self.root / "test.sqlite")
        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()
        self.manager = self.bot.plugins

        for plugin_id in list(self.manager.custom_plugin_ids()):
            path = self.manager.custom_plugin_path(plugin_id)
            if path.is_file():
                path.unlink()
            self.manager.unregister_custom(plugin_id)
        self.manager.reload()
        self.plugin_file = self.root / "market_probe.py"
        self.plugin_file.write_text(PLUGIN_CODE, encoding="utf-8")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_inspect_local_returns_metadata(self):
        response = self.client.post("/api/plugins/inspect-local",
                                    json={"path": str(self.plugin_file)})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["plugin_id"], "market_probe")
        self.assertFalse(self.manager.custom_plugin_path("market_probe").is_file())

    def test_inspect_local_rejects_non_py(self):
        other = self.root / "notes.txt"
        other.write_text("hello", encoding="utf-8")
        response = self.client.post("/api/plugins/inspect-local", json={"path": str(other)})
        self.assertEqual(response.status_code, 400)
        self.assertIn(".py", response.json()["detail"])

    def test_inspect_local_reports_dangerous(self):
        evil = self.root / "evil.py"
        evil.write_text(DANGEROUS_CODE, encoding="utf-8")
        response = self.client.post("/api/plugins/inspect-local", json={"path": str(evil)})
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["problems"])

    def test_install_local_requires_confirmation(self):
        response = self.client.post("/api/plugins/install-local",
                                    json={"path": str(self.plugin_file), "confirmed": False})
        self.assertEqual(response.status_code, 400)
        self.assertIn("确认", response.json()["detail"])
        self.assertFalse(self.manager.custom_plugin_path("market_probe").is_file())

    def test_install_local_writes_and_loads(self):
        response = self.client.post("/api/plugins/install-local",
                                    json={"path": str(self.plugin_file), "confirmed": True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["loaded"])
        self.assertTrue(self.manager.custom_plugin_path("market_probe").is_file())
        self.assertIn("market_probe", self.manager._plugins)

    def test_install_local_appears_in_plugin_list(self):
        self.client.post("/api/plugins/install-local",
                         json={"path": str(self.plugin_file), "confirmed": True})
        plugins = self.client.get("/api/plugins").json()
        self.assertIn("market_probe", [p["id"] for p in plugins])

    def test_install_local_then_delete(self):
        self.client.post("/api/plugins/install-local",
                         json={"path": str(self.plugin_file), "confirmed": True})
        response = self.client.delete("/api/plugins/market_probe")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("market_probe", self.manager._plugins)

    def test_install_local_missing_file(self):
        response = self.client.post("/api/plugins/install-local",
                                    json={"path": str(self.root / "nope.py"),
                                          "confirmed": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("找不到文件", response.json()["detail"])

    def _fake_tk(self, result):
        """造一个假的 tkinter，让文件选择框返回固定值。"""
        fake = type(sys)("tkinter")
        filedialog = type(sys)("tkinter.filedialog")
        filedialog.askopenfilename = lambda **kwargs: result
        fake.filedialog = filedialog
        fake.Tk = lambda: type("Root", (), {
            "withdraw": lambda self: None,
            "attributes": lambda self, *a: None,
            "destroy": lambda self: None})()
        return {"tkinter": fake, "tkinter.filedialog": filedialog}

    def test_browse_returns_selected_path(self):
        with unittest.mock.patch.dict(sys.modules, self._fake_tk(r"C:\picked\p.py")):
            response = self.client.post("/api/plugins/browse")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["path"], r"C:\picked\p.py")

    def test_browse_cancel_returns_empty_path(self):
        with unittest.mock.patch.dict(sys.modules, self._fake_tk("")):
            response = self.client.post("/api/plugins/browse")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["path"], "")


if __name__ == "__main__":
    unittest.main()
