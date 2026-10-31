"""1.3.48-B1：插件清单契约（plugin_manifest）与安装器加固的回归测试。

三块内容：
- 群里两套 plugin.json 方言（规格方言 / 内核方言）都必须能读，并归一成同一结构；
- entry 逃逸（绝对路径、..、反斜杠、盘符）与不支持的 api_version 一律拒绝（fail-closed）；
- 安装器：带清单的 zip 按清单 id 落盘，试加载失败时原文件字节一个都不变。
"""

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from src import plugin_installer as pi
from src.plugin_manifest import (
    DEFAULT_API_VERSION,
    DEFAULT_VERSION,
    entry_file,
    load_manifest,
    manifest_from_source,
    validate_manifest,
)


def make_zip(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def plugin_source(plugin_id: str, extra: str = "") -> str:
    template = (
        "from src.plugins import PluginBase, PluginMeta\n"
        "\n"
        "\n"
        "class Probe(PluginBase):\n"
        "    META = PluginMeta(id={pid!r}, name={pid!r}, description='\u6d4b\u8bd5\u7528')\n"
    )
    return template.format(pid=plugin_id) + extra


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ManifestLoadTests(unittest.TestCase):
    def test_spec_dialect_loads(self):
        """规格方言（{name,version,entry}，没有 id）能读，id 留空由文件名兜底。"""
        manifest = load_manifest(
            {"name": "\u5c0f\u5de5\u5177", "version": "1.2.3", "entry": "main.py"})
        self.assertEqual(manifest["name"], "\u5c0f\u5de5\u5177")
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual(manifest["entry"], "main.py")
        self.assertEqual(manifest["id"], "")
        self.assertEqual(manifest["api_version"], DEFAULT_API_VERSION)
        self.assertEqual(validate_manifest(manifest), [])

    def test_kernel_dialect_loads(self):
        """内核方言（author 是对象、带 permissions、entry 带函数名）能读。"""
        manifest = load_manifest({
            "id": "weather_card",
            "name": "\u5929\u6c14\u5361\u7247",
            "version": "2.0.1",
            "api_version": "1",
            "author": {"name": "\u9cb8\u9c7c\u5a18", "contact": "a@b.c"},
            "description": "\u67e5\u5929\u6c14",
            "entry": "main.py:setup",
            "permissions": ["network", "files"],
        })
        self.assertEqual(manifest["id"], "weather_card")
        self.assertEqual(manifest["entry"], "main.py:setup")
        self.assertEqual(entry_file(manifest["entry"]), "main.py")
        self.assertEqual(manifest["permissions"], ["network", "files"])
        self.assertEqual(manifest["author"], {"name": "\u9cb8\u9c7c\u5a18", "contact": "a@b.c"})
        self.assertEqual(validate_manifest(manifest), [])

    def test_author_string_and_dict_forms(self):
        """author 写成字符串或 {name,contact} 都要归一成同一形状。"""
        as_text = load_manifest({"name": "a", "author": "\u5c0f\u660e"})
        as_dict = load_manifest({"name": "a", "author": {"name": "\u5c0f\u660e"}})
        self.assertEqual(as_text["author"], {"name": "\u5c0f\u660e", "contact": ""})
        self.assertEqual(as_dict["author"], as_text["author"])

    def test_defaults_and_int_coercion(self):
        """缺省值明确；version/api_version 写成数字要转成字符串，不能报错。"""
        empty = load_manifest({})
        self.assertEqual(empty["version"], DEFAULT_VERSION)
        self.assertEqual(empty["api_version"], DEFAULT_API_VERSION)
        numeric = load_manifest({"name": "n", "version": 1, "api_version": 1})
        self.assertEqual(numeric["version"], "1")
        self.assertEqual(numeric["api_version"], "1")
        self.assertEqual(validate_manifest(numeric), [])

    def test_invalid_json_raises_value_error(self):
        """坏 JSON / 顶层不是对象 → ValueError，而不是静默当成空清单。"""
        with self.assertRaises(ValueError):
            load_manifest(b"{not json")
        with self.assertRaises(ValueError):
            load_manifest(b"[]")


class ValidateTests(unittest.TestCase):
    def test_clean_manifest_passes(self):
        self.assertEqual(
            validate_manifest({"id": "ok_id", "entry": "main.py", "api_version": "1"}), [])

    def test_entry_absolute_rejected(self):
        """entry 写绝对路径 → 拒绝（不让插件把文件装到插件目录外）。"""
        errors = validate_manifest({"id": "ok_id", "entry": "/etc/evil.py"})
        self.assertTrue(errors)
        self.assertIn("entry", errors[0])

    def test_entry_parent_rejected(self):
        """entry 含 .. → 拒绝。"""
        self.assertTrue(validate_manifest({"id": "ok_id", "entry": "../evil.py"}))

    def test_entry_backslash_rejected(self):
        """entry 含反斜杠 → 拒绝（Windows 上 \\ 同样能逃逸，必须提前挡住）。"""
        self.assertTrue(validate_manifest({"id": "ok_id", "entry": "sub\\evil.py"}))

    def test_entry_drive_rejected(self):
        """entry 带盘符 → 拒绝。"""
        self.assertTrue(validate_manifest({"id": "ok_id", "entry": "C:evil.py"}))

    def test_entry_too_deep_rejected(self):
        """entry 层数超过 MAX_ENTRY_DEPTH → 拒绝。"""
        self.assertTrue(validate_manifest({"id": "ok_id", "entry": "a/b/c/d.py"}))

    def test_entry_with_function_passes(self):
        """<file>.py:<func> 的写法是合法的，不能被误杀。"""
        self.assertEqual(validate_manifest({"id": "ok_id", "entry": "main.py:setup"}), [])
        self.assertEqual(validate_manifest({"id": "ok_id", "entry": "sub/main.py"}), [])

    def test_api_version_unsupported_rejected(self):
        """宿主不支持的 api_version → 拒绝，并说明宿主支持哪些版本。"""
        errors = validate_manifest({"id": "ok_id", "api_version": "2"})
        self.assertTrue(errors)
        self.assertIn("API", errors[0])

    def test_id_invalid_rejected(self):
        """id 写了就必须匹配 ^[a-z][a-z0-9_]{1,47}$。"""
        self.assertTrue(validate_manifest({"id": "Bad-Id"}))
        self.assertTrue(validate_manifest({"id": "x"}))

    def test_missing_id_and_entry_tolerated(self):
        """缺 id / 缺 entry 不算错：老插件包没有清单也是合法的。"""
        self.assertEqual(validate_manifest({}), [])
        self.assertEqual(validate_manifest({"name": "\u53ea\u6709\u540d\u5b57"}), [])


class SourceMetaTests(unittest.TestCase):
    def test_manifest_from_source_reads_meta(self):
        """裸 .py 里 META 的 id/name/version 能抽出来，供安装记录使用。"""
        source = (
            "from src.plugins import PluginBase, PluginMeta\n"
            "\n"
            "class P(PluginBase):\n"
            "    META = PluginMeta(id='bare_plugin', name='\u88f8\u63d2\u4ef6',\n"
            "                      description='x', version='3.1.4')\n"
        )
        manifest = manifest_from_source(source)
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["id"], "bare_plugin")
        self.assertEqual(manifest["version"], "3.1.4")

    def test_manifest_from_source_returns_none_for_plain_code(self):
        """抽不出 META 就返回 None；调用方按老办法处理，绝不因此拒绝安装。"""
        self.assertIsNone(manifest_from_source("print('hello')\n"))
        self.assertIsNone(manifest_from_source("def broken(:\n"))


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.plugins_dir = self.root / "plugins"
        self.plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _zip(self, files: dict, name: str = "pkg.zip") -> Path:
        path = self.root / name
        path.write_bytes(make_zip(files))
        return path

    def test_zip_with_manifest_installs_under_manifest_id(self):
        """带 plugin.json 的 zip 安装成功，落盘文件名来自清单 id，版本也记下。"""
        path = self._zip({
            "plugin.json": json.dumps({
                "id": "weather_card", "name": "\u5929\u6c14\u5361\u7247",
                "version": "2.0.1", "entry": "main.py"}),
            "main.py": plugin_source("weather_card"),
        })
        result = pi.install_from_file(path, self.plugins_dir, confirmed=True)
        self.assertEqual(result["plugin_id"], "weather_card")
        self.assertEqual(result["version"], "2.0.1")
        self.assertFalse(result["overwritten"])
        self.assertEqual(sorted(p.name for p in self.plugins_dir.iterdir()),
                         ["weather_card.py"])

    def test_spec_dialect_zip_uses_meta_id(self):
        """规格方言没写 id：用源码 META.id 当文件名，保证 manager 能加载。"""
        path = self._zip({
            "plugin.json": json.dumps({
                "name": "\u5c0f\u5de5\u5177", "version": "1.2.3", "entry": "main.py"}),
            "main.py": plugin_source("fancy_tool"),
        })
        result = pi.install_from_file(path, self.plugins_dir, confirmed=True)
        self.assertEqual(result["plugin_id"], "fancy_tool")
        self.assertTrue((self.plugins_dir / "fancy_tool.py").is_file())

    def test_zip_with_bad_entry_is_rejected(self):
        """清单 entry 逃逸 → 直接拒绝安装，一个文件都不落盘。"""
        path = self._zip({
            "plugin.json": json.dumps({"id": "evil_one", "entry": "../evil.py"}),
            "main.py": plugin_source("evil_one"),
        })
        with self.assertRaises(pi.InstallError) as caught:
            pi.install_from_file(path, self.plugins_dir, confirmed=True)
        self.assertIn("清单", str(caught.exception))
        self.assertEqual(list(self.plugins_dir.iterdir()), [])

    def test_zip_with_unsupported_api_version_rejected(self):
        """api_version 不受支持 → 拒绝，并说明宿主支持哪个版本。"""
        path = self._zip({
            "plugin.json": json.dumps(
                {"id": "future_api", "api_version": "9", "entry": "main.py"}),
            "main.py": plugin_source("future_api"),
        })
        with self.assertRaises(pi.InstallError) as caught:
            pi.install_from_file(path, self.plugins_dir, confirmed=True)
        self.assertIn("API", str(caught.exception))
        self.assertEqual(list(self.plugins_dir.iterdir()), [])

    def test_manifest_id_mismatch_rejected_and_nothing_written(self):
        """清单 id 与源码 META.id 不一致 → 装不了（装进去 manager 也会拒），且不留 tmp。"""
        path = self._zip({
            "plugin.json": json.dumps({"id": "declared_id", "entry": "main.py"}),
            "main.py": plugin_source("other_id"),
        })
        with self.assertRaises(pi.InstallError) as caught:
            pi.install_from_file(path, self.plugins_dir, confirmed=True)
        self.assertIn("\u4e0d\u4e00\u81f4", str(caught.exception))
        self.assertEqual(list(self.plugins_dir.iterdir()), [])

    def test_trial_load_failure_keeps_original_bytes(self):
        """试加载抛异常时安装失败，原文件字节必须一个都不变（hash 对比）。"""
        good = self._zip({"main.py": plugin_source("keep_me")}, name="good.zip")
        pi.install_from_file(good, self.plugins_dir, confirmed=True)
        installed = self.plugins_dir / "keep_me.py"
        before = digest(installed)

        bad = self._zip({
            "main.py": plugin_source("keep_me", extra="raise RuntimeError('boom')\n")},
            name="bad.zip")
        with self.assertRaises(pi.InstallError) as caught:
            pi.install_from_file(bad, self.plugins_dir, confirmed=True,
                                 overwrite=True)
        self.assertIn("boom", str(caught.exception))
        self.assertEqual(digest(installed), before)
        self.assertEqual(sorted(p.name for p in self.plugins_dir.iterdir()),
                         ["keep_me.py"])

    def test_overwritten_reflects_reality(self):
        """overwritten 必须是真实判断：首次 False，覆盖时 True。"""
        path = self._zip({"main.py": plugin_source("twice_test")}, name="twice.zip")
        first = pi.install_from_file(path, self.plugins_dir, confirmed=True)
        self.assertFalse(first["overwritten"])
        with self.assertRaises(pi.InstallError):
            pi.install_from_file(path, self.plugins_dir, confirmed=True)
        second = pi.install_from_file(path, self.plugins_dir, confirmed=True,
                                     overwrite=True)
        self.assertTrue(second["overwritten"])


if __name__ == "__main__":
    unittest.main()
