"""1.3.63 插件包体系测试：清单校验、发现、安装、删除、manager 集成。

运行：./.venv-wechat/Scripts/python.exe -m unittest discover -s tests -p 'test_plugin_packs163.py'
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import plugin_packs
from src.plugin_packs import (
    ENV_FROM_PACKS,
    OPTIONAL_PLUGIN_PACKS,
    compare_versions,
    discover,
    install_pack,
    parse_version,
    remove_pack,
    validate_manifest,
)
from src.plugin_policy import CORE_PLUGINS
from src.plugins.manager import PluginManager
from src.runtime_identity import APP_VERSION

HOST = APP_VERSION


def _manifest(pack_id: str, **overrides):
    data = {
        "id": pack_id,
        "name": "测试包 " + pack_id,
        "version": "1.0.0",
        "entry": "plugin.py",
        "description": "测试用插件包",
        "author": "测试",
        "api_version": "1",
        "requires": "1.3.0",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def _plugin_source(pack_id: str) -> str:
    return (
        "from src.plugins import PluginBase, PluginMeta, FieldSchema\n"
        "\n"
        "class PackPlugin(PluginBase):\n"
        "    META = PluginMeta(\n"
        "        id=" + repr(pack_id) + ",\n"
        "        name='测试包 " + pack_id + "',\n"
        "        description='测试用插件包',\n"
        "        category='utility',\n"
        "        config_schema={'enabled': FieldSchema(type='bool', label='启用')},\n"
        "        default_settings={'enabled': True},\n"
        "        enabled_by_default=True,\n"
        "        version='1.0.0',\n"
        "        author='测试',\n"
        "    )\n"
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_pack(root: Path, pack_id: str, manifest=None, plugin_src: str | None = None,
              with_entry: bool = True) -> Path:
    pack_dir = root / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    data = _manifest(pack_id) if manifest is None else manifest
    _write(pack_dir / "plugin.json", json.dumps(data, ensure_ascii=False, indent=2))
    if with_entry:
        _write(pack_dir / "plugin.py", plugin_src if plugin_src is not None else _plugin_source(pack_id))
    return pack_dir


def make_zip(zip_path: Path, files: dict) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return zip_path


def _temp_dirs(root: Path):
    return [item.name for item in root.iterdir() if item.is_dir() and item.name.startswith(".install-")]


class TestManifestValidation(unittest.TestCase):
    def test_missing_id(self):
        data = _manifest("demo_pack")
        data.pop("id")
        ok, errors, _meta = validate_manifest(data, HOST)
        self.assertFalse(ok)
        self.assertTrue(any("缺少必需字段 id" in item for item in errors), errors)

    def test_missing_name(self):
        data = _manifest("demo_pack")
        data.pop("name")
        ok, errors, _meta = validate_manifest(data, HOST)
        self.assertFalse(ok)
        self.assertTrue(any("缺少必需字段 name" in item for item in errors), errors)

    def test_missing_version(self):
        data = _manifest("demo_pack")
        data.pop("version")
        ok, errors, _meta = validate_manifest(data, HOST)
        self.assertFalse(ok)
        self.assertTrue(any("缺少必需字段 version" in item for item in errors), errors)

    def test_invalid_version(self):
        ok, errors, _meta = validate_manifest(_manifest("demo_pack", version="abc"), HOST)
        self.assertFalse(ok)
        self.assertTrue(any("version 不合法" in item for item in errors), errors)

    def test_missing_entry_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack_dir = make_pack(Path(tmp), "no_entry", with_entry=False)
            data = _manifest("no_entry")
            ok, errors, _meta = validate_manifest(data, HOST, pack_dir=pack_dir)
        self.assertFalse(ok)
        self.assertTrue(any("入口文件不存在" in item for item in errors), errors)

    def test_missing_dependency(self):
        data = _manifest("demo_pack", dependencies=["ghost_plugin"])
        ok, errors, _meta = validate_manifest(data, HOST, available_ids={"other_plugin"})
        self.assertFalse(ok)
        self.assertTrue(any("依赖的插件未安装" in item for item in errors), errors)

    def test_api_version_incompatible(self):
        ok, errors, meta = validate_manifest(_manifest("demo_pack", api_version="2"), HOST)
        self.assertFalse(ok)
        self.assertEqual(meta["status"], "incompatible")
        self.assertTrue(any("不兼容" in item for item in errors), errors)

    def test_host_version_too_old(self):
        ok, errors, meta = validate_manifest(_manifest("demo_pack", requires="9.9.9"), HOST)
        self.assertFalse(ok)
        self.assertEqual(meta["status"], "incompatible")
        self.assertTrue(any("宿主版本" in item for item in errors), errors)

    def test_valid_manifest(self):
        ok, errors, meta = validate_manifest(_manifest("demo_pack"), HOST)
        self.assertTrue(ok, errors)
        self.assertEqual(errors, [])
        self.assertEqual(meta["id"], "demo_pack")
        self.assertEqual(meta["entry"], "plugin.py")
        self.assertEqual(meta["version"], "1.0.0")
        self.assertEqual(meta["status"], "ok")

    def test_optional_fields_degrade_to_warnings(self):
        data = _manifest("demo_pack")
        for key in ("author", "description", "upstream", "native", "side", "data_policy"):
            data.pop(key, None)
        ok, errors, meta = validate_manifest(data, HOST)
        self.assertTrue(ok, errors)
        self.assertTrue(meta["warnings"], "缺可选字段时应给出可读提示而不是异常")
        self.assertTrue(any("author" in item for item in meta["warnings"]), meta["warnings"])

    def test_parse_and_compare_version(self):
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("1.2"), (1, 2, 0))
        self.assertIsNone(parse_version("abc"))
        self.assertEqual(compare_versions("1.2.0", "1.10.0"), -1)
        self.assertEqual(compare_versions("2.0", "2.0.0"), 0)


class TestDiscover(unittest.TestCase):
    def test_bad_json_only_breaks_one_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = make_pack(root, "good_pack")
            broken = root / "broken_json"
            broken.mkdir()
            _write(broken / "plugin.json", "{ 这不是 JSON")
            entries = discover([root], HOST)
            self.assertTrue(good.is_dir())
        status = {entry["id"] or Path(entry["path"]).name: entry["status"] for entry in entries}
        self.assertEqual(status.get("good_pack"), "ok")
        self.assertEqual(status.get("broken_json"), "invalid")
        bad = [item for item in entries if Path(item["path"]).name == "broken_json"][0]
        self.assertTrue(any("JSON" in item for item in bad["errors"]), bad["errors"])

    def test_bad_version_marked_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_pack(root, "bad_ver", manifest=_manifest("bad_ver", version="v1.x"))
            entries = discover([root], HOST)
        self.assertEqual(entries[0]["status"], "invalid")
        self.assertTrue(any("version" in item for item in entries[0]["errors"]), entries[0]["errors"])

    def test_duplicate_id_first_wins_and_shadowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first = make_pack(base / "a", "dup_pack", manifest=_manifest("dup_pack", name="第一份"))
            make_pack(base / "b", "dup_pack", manifest=_manifest("dup_pack", name="第二份"))
            entries = discover([base / "a", base / "b"], HOST)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["status"], "ok")
        self.assertEqual(entries[0]["name"], "第一份")
        self.assertEqual(entries[1]["status"], "shadowed")
        self.assertEqual(entries[1]["shadowed_by"], str(first))

    def test_missing_dir_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = discover([Path(tmp) / "not_here"], HOST)
        self.assertEqual(entries, [])


class TestInstallPack(unittest.TestCase):
    def test_install_from_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_pack(base / "src", "demo_pack")
            dest = base / "packs"
            result = install_pack(source, dest)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["id"], "demo_pack")
            self.assertTrue((dest / "demo_pack" / "plugin.json").is_file())
            self.assertEqual(_temp_dirs(dest), [])

    def test_install_from_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = make_zip(base / "pack.zip", {
                "demo_pack/plugin.json": json.dumps(_manifest("demo_pack"), ensure_ascii=False),
                "demo_pack/plugin.py": _plugin_source("demo_pack"),
            })
            dest = base / "packs"
            result = install_pack(archive, dest)
            self.assertTrue(result["ok"], result)
            self.assertTrue((dest / "demo_pack" / "plugin.py").is_file())
            self.assertEqual(_temp_dirs(dest), [])

    def test_install_existing_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_pack(base / "src", "demo_pack")
            dest = base / "packs"
            self.assertTrue(install_pack(source, dest)["ok"])
            _write(source / "plugin.json", json.dumps(_manifest("demo_pack", version="2.0.0"), ensure_ascii=False))
            second = install_pack(source, dest)
            self.assertFalse(second["ok"])
            self.assertEqual(second["reason"], "exists")
            installed = json.loads((dest / "demo_pack" / "plugin.json").read_text("utf-8"))
            self.assertEqual(installed["version"], "1.0.0")

    def test_zip_slip_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = make_zip(base / "evil.zip", {
                "../evil.txt": "坏东西",
                "demo_pack/plugin.json": json.dumps(_manifest("demo_pack"), ensure_ascii=False),
            })
            dest = base / "packs"
            result = install_pack(archive, dest)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "unsafe_zip")
            self.assertFalse((base / "evil.txt").exists())
            self.assertFalse((dest / "demo_pack").exists())
            self.assertEqual(_temp_dirs(dest) if dest.exists() else [], [])

    def test_invalid_manifest_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_pack(base / "src", "bad_pack",
                               manifest=_manifest("bad_pack", version="not-a-version"))
            dest = base / "packs"
            result = install_pack(source, dest)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "invalid")
            self.assertFalse((dest / "bad_pack").exists())
            self.assertEqual(_temp_dirs(dest) if dest.exists() else [], [])

    def test_final_check_failure_removes_copied_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_pack(base / "src", "rb_pack")
            dest = base / "packs"
            meta = {"id": "rb_pack", "name": "回滚测试", "version": "1.0.0", "entry": "plugin.py"}
            with mock.patch.object(plugin_packs, "validate_manifest",
                                   side_effect=[(True, [], meta), (False, ["清单损坏"], meta)]):
                result = install_pack(source, dest)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "invalid")
            self.assertFalse((dest / "rb_pack").exists())
            self.assertEqual(_temp_dirs(dest) if dest.exists() else [], [])

    def test_install_twice_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_pack(base / "src", "demo_pack")
            dest = base / "packs"
            self.assertTrue(install_pack(source, dest)["ok"])
            again = install_pack(source, dest)
            self.assertFalse(again["ok"])
            self.assertEqual(again["reason"], "exists")
            self.assertEqual(sorted(item.name for item in dest.iterdir()), ["demo_pack"])


class TestRemovePack(unittest.TestCase):
    def test_remove_normal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dest = base / "packs"
            make_pack(dest, "demo_pack")
            result = remove_pack(dest, "demo_pack")
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["removed"])
            self.assertFalse((dest / "demo_pack").exists())

    def test_remove_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dest = base / "packs"
            dest.mkdir()
            outside = base / "outside"
            outside.mkdir()
            result = remove_pack(dest, "../outside")
            self.assertFalse(result["ok"])
            self.assertIn(result["reason"], ("bad_id", "path_traversal"))
            self.assertTrue(outside.is_dir())

    def test_remove_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "packs"
            dest.mkdir()
            result = remove_pack(dest, "not_there")
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "not_found")


class TestManagerIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.packs = self.base / "plugin_packs"
        self.packs.mkdir()
        self.data = self.base / "data"

    def tearDown(self):
        self._tmp.cleanup()

    def _manager(self, **kwargs):
        return PluginManager(self.data, pack_dirs=[self.packs], **kwargs)

    def test_pack_loads_and_source_is_pack(self):
        with mock.patch.dict(os.environ, {ENV_FROM_PACKS: "0"}):
            make_pack(self.packs, "demo_pack")
            manager = self._manager()
            self.assertIn("demo_pack", manager._plugins)
            self.assertEqual(manager.plugin_source("demo_pack"), "pack")
            rows = {row["id"]: row for row in manager.list_plugins()}
            self.assertEqual(rows["demo_pack"]["source"], "pack")
            status = manager.plugin_status()
            self.assertGreaterEqual(status["counts"]["pack"], 1)
            self.assertTrue(status["message"].startswith("共 "), status["message"])
            pack_status = manager.pack_status()
            self.assertEqual(pack_status["installed"], 1)
            self.assertEqual(pack_status["broken"], 0)

    def test_builtin_id_pack_is_informational_not_broken(self):
        # 1.3.67：包 id 与内置插件同名是正常形态（包=内置插件的可选发行版），
        # 不能算成「插件包有问题」，否则全新安装的插件包页面全是红色错误。
        with mock.patch.dict(os.environ, {ENV_FROM_PACKS: '0'}):
            make_pack(self.packs, 'reply_pace')
            manager = self._manager()
            status = manager.pack_status()
            rows = {row['id']: row for row in status['packs']}
            self.assertEqual(rows['reply_pace']['status'], 'builtin')
            self.assertEqual(rows['reply_pace']['errors'], [])
            self.assertEqual(status['broken'], 0)

    def test_bad_packs_do_not_affect_others(self):
        with mock.patch.dict(os.environ, {ENV_FROM_PACKS: "0"}):
            make_pack(self.packs, "good_pack")
            broken = self.packs / "broken_json"
            broken.mkdir()
            _write(broken / "plugin.json", "{ 坏掉的清单")
            make_pack(self.packs, "no_entry", with_entry=False)
            make_pack(self.packs, "boom_pack", plugin_src="raise RuntimeError('测试用异常')\n")
            manager = self._manager()
            self.assertIn("good_pack", manager._plugins)
            self.assertIn("reply_pace", manager._plugins)
            status = manager.pack_status()
            self.assertEqual(status["installed"], 1)
            self.assertEqual(status["broken"], 3)
            summary = manager.plugin_status()
            self.assertGreaterEqual(summary["pack_broken"], 3)
            self.assertTrue(summary["latest_error"], "前端要能拿到最近错误文案")
            self.assertTrue(summary["message"])

    def test_from_packs_env_hides_uninstalled_optional(self):
        with mock.patch.dict(os.environ, {ENV_FROM_PACKS: "1"}):
            manager = self._manager()
            self.assertTrue(manager.plugins_from_packs())
            for pack_id in OPTIONAL_PLUGIN_PACKS:
                self.assertNotIn(pack_id, manager._plugins)
            status = manager.pack_status()
            missing = set(status["missing"])
            self.assertEqual(missing, set(OPTIONAL_PLUGIN_PACKS))
            self.assertTrue(status["packs"])
            self.assertTrue(all(item["status"] == "not_installed" for item in status["packs"]))
            self.assertGreaterEqual(len(manager.list_plugins()), 20)
            self.assertTrue(manager.plugin_status()["ok"])

    def test_default_mode_keeps_builtin_optional_plugins(self):
        with mock.patch.dict(os.environ, {ENV_FROM_PACKS: "0"}):
            manager = self._manager()
            for pack_id in OPTIONAL_PLUGIN_PACKS:
                self.assertIn(pack_id, manager._plugins)
                self.assertEqual(manager.plugin_source(pack_id), "builtin")
            self.assertEqual(manager.pack_status()["count"], 0)

    def test_missing_or_broken_pack_dirs_keep_core_plugins(self):
        with mock.patch.dict(os.environ, {ENV_FROM_PACKS: "0"}):
            missing_dir = self.base / "no_such_dir"
            manager = PluginManager(self.data, pack_dirs=[missing_dir])
            self.assertTrue(set(CORE_PLUGINS).issubset(set(manager._plugins)))
            self.assertEqual(manager.pack_status()["count"], 0)
            self.assertFalse(missing_dir.exists(), "目录不存在时不应自动创建")

            for index in range(3):
                broken = self.packs / ("broken_" + str(index))
                broken.mkdir()
                _write(broken / "plugin.json", "{ 坏清单 " + str(index))
            data2 = self.base / "data2"
            manager2 = PluginManager(data2, pack_dirs=[self.packs])
            self.assertTrue(set(CORE_PLUGINS).issubset(set(manager2._plugins)))
            self.assertEqual(manager2.pack_status()["broken"], 3)
            self.assertTrue(manager2.list_plugins())

    def test_removed_pack_stays_missing_without_reinstall(self):
        with mock.patch.dict(os.environ, {ENV_FROM_PACKS: "1"}):
            source = make_pack(self.base / "src", "reply_pace")
            self.assertTrue(install_pack(source, self.packs)["ok"])
            manager = self._manager()
            self.assertIn("reply_pace", manager._plugins)
            self.assertEqual(manager.plugin_source("reply_pace"), "pack")
            self.assertTrue(manager.get_settings("reply_pace")["enabled"],
                            "发行包模式下沿用插件自己的默认值")
            removed = remove_pack(self.packs, "reply_pace")
            self.assertTrue(removed["ok"], removed)
            manager.load_packs()
            self.assertNotIn("reply_pace", manager._plugins)
            self.assertIn("reply_pace", manager.pack_status()["missing"])
            self.assertFalse((self.packs / "reply_pace").exists(), "不允许静默重装")

    def test_new_pack_listed_but_not_auto_enabled(self):
        with mock.patch.dict(os.environ, {ENV_FROM_PACKS: "0"}):
            make_pack(self.packs, "demo_pack")
            manager = self._manager()
            self.assertIn("demo_pack", manager._plugins)
            self.assertFalse(manager.get_settings("demo_pack")["enabled"],
                             "新发现的插件包只登记，不自动启用")
            rows = {row["id"]: row for row in manager.list_plugins()}
            self.assertFalse(rows["demo_pack"]["enabled"])
            manager.update_settings("demo_pack", {"enabled": True})
            self.assertTrue(manager.get_settings("demo_pack")["enabled"])
            again = PluginManager(self.data, pack_dirs=[self.packs])
            self.assertTrue(again.get_settings("demo_pack")["enabled"],
                            "用户手动启用后，重启不能被重新关掉")

    def test_pack_settings_roundtrip(self):
        with mock.patch.dict(os.environ, {ENV_FROM_PACKS: "0"}):
            make_pack(self.packs, "demo_pack")
            manager = self._manager()
            manager.update_settings("demo_pack", {"enabled": False})
            self.assertFalse(manager.get_settings("demo_pack")["enabled"])
            manager.reload()
            self.assertFalse(manager.get_settings("demo_pack")["enabled"])
            self.assertIn("demo_pack", manager._plugins)

    def test_seed_packs_are_valid(self):
        seed_root = Path(__file__).resolve().parents[1] / "plugin-packs"
        self.assertTrue(seed_root.is_dir(), "工程根应当有 plugin-packs 目录")
        available = set(OPTIONAL_PLUGIN_PACKS)
        for pack_id in OPTIONAL_PLUGIN_PACKS:
            pack_dir = seed_root / pack_id
            self.assertTrue((pack_dir / "plugin.json").is_file(), pack_id)
            self.assertTrue((pack_dir / "plugin.py").is_file(), pack_id)
            self.assertTrue((pack_dir / "README.md").is_file(), pack_id)
            self.assertTrue((pack_dir / "settings.example.json").is_file(), pack_id)
            data = json.loads((pack_dir / "plugin.json").read_text("utf-8"))
            ok, errors, meta = validate_manifest(data, HOST, pack_dir=pack_dir,
                                                 available_ids=available)
            self.assertTrue(ok, (pack_id, errors))
            self.assertEqual(meta["status"], "ok")


class TestPluginPacksApi(unittest.TestCase):
    def _client(self, manager):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src import plugin_packs_api

        app = FastAPI()
        plugin_packs_api.register(app, None)

        class _Bot:
            pass

        bot = _Bot()
        bot.plugins = manager
        app.state.bot = bot
        return TestClient(app)

    def test_api_install_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            packs = base / "plugin_packs"
            packs.mkdir()
            manager = PluginManager(base / "data", pack_dirs=[packs])
            client = self._client(manager)
            source = make_pack(base / "src", "demo_pack")
            listed = client.get("/api/plugins/packs")
            self.assertEqual(listed.status_code, 200)
            self.assertTrue(listed.json()["ok"])
            installed = client.post("/api/plugins/packs/install", json={"path": str(source)})
            self.assertEqual(installed.status_code, 200)
            self.assertTrue(installed.json()["ok"], installed.json())
            self.assertEqual(installed.json()["id"], "demo_pack")
            rescan = client.post("/api/plugins/packs/rescan")
            self.assertTrue(rescan.json()["ok"])
            self.assertGreaterEqual(rescan.json().get("installed", 0), 1)

    def test_api_missing_params_are_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            manager = PluginManager(base / "data", pack_dirs=[base / "plugin_packs"])
            client = self._client(manager)
            self.assertEqual(client.post("/api/plugins/packs/install", json={}).status_code, 400)
            self.assertEqual(
                client.post("/api/plugins/packs/nope/enabled", json={}).status_code, 400)

    def test_api_toggle_and_delete_errors_are_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            manager = PluginManager(base / "data", pack_dirs=[base / "plugin_packs"])
            client = self._client(manager)
            toggled = client.post("/api/plugins/packs/nope/enabled", json={"enabled": True})
            self.assertEqual(toggled.status_code, 200)
            self.assertEqual(toggled.json()["reason"], "not_found")
            self.assertTrue(toggled.json()["message"])
            deleted = client.delete("/api/plugins/packs/BadId")
            self.assertEqual(deleted.status_code, 200)
            self.assertFalse(deleted.json()["ok"])
            self.assertEqual(deleted.json()["reason"], "bad_id")


if __name__ == "__main__":
    unittest.main()
