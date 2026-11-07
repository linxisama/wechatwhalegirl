"""1.3.107 卸载清干净：删掉插件，行为与数据一起消失。

用户的判定标准第 2 条：
  「有清单，能单独装、单独卸。删掉 jar，行为消失，不残留。」

过去 remove_pack 只删目录：设置留在 plugin_settings.json、状态留在
plugin_state/<id>/，重装之后「老设置又回来了」—— 那不是卸载，那是禁用。

运行：./.venv-wechat/Scripts/python.exe -m unittest tests.test_plugin_purge_1107
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.plugin_packs import _purge_plugin_data, remove_pack
from src.plugins.manager import PluginManager


def make_pack(root: Path, pack_id: str) -> Path:
    pack_dir = root / pack_id
    pack_dir.mkdir(parents=True)
    manifest = {
        "id": pack_id,
        "name": pack_id,
        "version": "1.0.0",
        "entry": "plugin.py",
        "author": "测试",
        "description": "卸载测试用",
        "api_version": 2,
        "requires": "1.3.0",
        "data_policy": ["own_settings", "own_state"],
        "native": "纯 Python",
        "side": "本机侧运行",
        "upstream": "本地测试",
        "capabilities": ["on_ingest"],
    }
    (pack_dir / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                          encoding="utf-8")
    (pack_dir / "plugin.py").write_text("PLUGIN = None\n", encoding="utf-8")
    return pack_dir


class RemoveWithoutPurgeTests(unittest.TestCase):
    """默认行为必须完全不变 —— 清数据不可逆，不能替调用方决定。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.packs = self.root / "packs"
        self.packs.mkdir()
        self.state = self.root / "plugin_state"
        self.state.mkdir()
        self.settings = self.root / "plugin_settings.json"
        self.settings.write_text(json.dumps({"demo": {"enabled": False}}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_directory_removed(self):
        make_pack(self.packs, "demo")
        result = remove_pack(self.packs, "demo")
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.packs / "demo").exists())

    def test_state_and_settings_survive(self):
        make_pack(self.packs, "demo")
        (self.state / "demo").mkdir()
        (self.state / "demo" / "state.json").write_text("{}", encoding="utf-8")
        remove_pack(self.packs, "demo")
        self.assertTrue((self.state / "demo").is_dir(), "默认不删状态")
        stored = json.loads(self.settings.read_text("utf-8"))
        self.assertIn("demo", stored, "默认不删设置")

    def test_purged_field_is_empty_by_default(self):
        make_pack(self.packs, "demo")
        result = remove_pack(self.packs, "demo")
        self.assertEqual(result.get("purged"), [])


class PurgeTests(unittest.TestCase):
    """purge=True：连数据一起清。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.packs = self.root / "packs"
        self.packs.mkdir()
        self.state = self.root / "plugin_state"
        self.state.mkdir()
        self.settings = self.root / "plugin_settings.json"
        self.settings.write_text(
            json.dumps({"demo": {"enabled": False}, "other": {"enabled": True}}),
            encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_purge_removes_state_and_settings(self):
        make_pack(self.packs, "demo")
        (self.state / "demo").mkdir()
        (self.state / "demo" / "state.json").write_text("{}", encoding="utf-8")
        result = remove_pack(self.packs, "demo", purge=True,
                             state_dir=self.state, settings_path=self.settings)
        self.assertTrue(result["ok"], result)
        self.assertEqual(sorted(result["purged"]), ["settings", "state"])
        self.assertFalse((self.state / "demo").exists())
        stored = json.loads(self.settings.read_text("utf-8"))
        self.assertNotIn("demo", stored)

    def test_other_plugins_untouched(self):
        make_pack(self.packs, "demo")
        (self.state / "demo").mkdir()
        (self.state / "other").mkdir()
        remove_pack(self.packs, "demo", purge=True,
                    state_dir=self.state, settings_path=self.settings)
        self.assertTrue((self.state / "other").is_dir(), "别的插件的状态不能被波及")
        stored = json.loads(self.settings.read_text("utf-8"))
        self.assertIn("other", stored)

    def test_reinstall_gets_defaults_not_old_settings(self):
        """这条是整批的核心：重装之后拿到默认值，不是上次的老设置。"""
        make_pack(self.packs, "demo")
        (self.state / "demo").mkdir()
        (self.state / "demo" / "state.json").write_text('{"old": true}', encoding="utf-8")
        remove_pack(self.packs, "demo", purge=True,
                    state_dir=self.state, settings_path=self.settings)
        make_pack(self.packs, "demo")
        self.assertFalse((self.state / "demo").exists(), "状态目录必须不存在")
        stored = json.loads(self.settings.read_text("utf-8"))
        self.assertNotIn("demo", stored, "设置必须不存在")

    def test_missing_state_dir_is_not_an_error(self):
        make_pack(self.packs, "demo")
        result = remove_pack(self.packs, "demo", purge=True,
                             state_dir=self.state, settings_path=self.settings)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["purged"], ["settings"])

    def test_missing_settings_file_is_not_an_error(self):
        make_pack(self.packs, "demo")
        (self.state / "demo").mkdir()
        self.settings.unlink()
        result = remove_pack(self.packs, "demo", purge=True,
                             state_dir=self.state, settings_path=self.settings)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["purged"], ["state"])

    def test_corrupt_settings_file_does_not_block_removal(self):
        make_pack(self.packs, "demo")
        (self.state / "demo").mkdir()
        self.settings.write_text("{not json", encoding="utf-8")
        result = remove_pack(self.packs, "demo", purge=True,
                             state_dir=self.state, settings_path=self.settings)
        self.assertTrue(result["ok"], result)
        self.assertIn("state", result["purged"])
        self.assertEqual(self.settings.read_text("utf-8"), "{not json",
                         "坏文件不该被我们改写成别的东西")

    def test_no_paths_given_skips_purge(self):
        make_pack(self.packs, "demo")
        result = remove_pack(self.packs, "demo", purge=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["purged"], [])


class PathSafetyTests(unittest.TestCase):
    """purge 不能成为新的越界入口。"""

    def test_traversal_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packs = root / "packs"
            packs.mkdir()
            for bad in ("..", "../evil", "a/b", ""):
                with self.subTest(bad=bad):
                    result = remove_pack(packs, bad, purge=True,
                                         state_dir=root / "state",
                                         settings_path=root / "settings.json")
                    self.assertFalse(result["ok"], bad)

    def test_purge_helper_ignores_symlinked_state(self):
        """state_dir/<id> 若是符号链接，不能顺着链接删到别处。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("x", encoding="utf-8")
            link = state / "demo"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("本机不支持创建目录符号链接")
            purged = _purge_plugin_data("demo", state, None)
            self.assertNotIn("state", purged)
            self.assertTrue((outside / "keep.txt").is_file(), "不能顺着链接删到别处")


class ManagerIntegrationTests(unittest.TestCase):
    """manager 知道自己的布局，它封装的那层必须能直接用。"""

    def test_purge_plugin_data_clears_manager_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(Path(tmp))
            plugin_id = "demo_pack"
            folder = manager.state_dir / plugin_id
            folder.mkdir(parents=True)
            (folder / "state.json").write_text("{}", encoding="utf-8")
            manager._settings[plugin_id] = {"enabled": False}
            manager._save_settings()

            purged = manager.purge_plugin_data(plugin_id)

            self.assertEqual(sorted(purged), ["settings", "state"])
            self.assertFalse(folder.exists())
            self.assertNotIn(plugin_id, manager._settings)


if __name__ == "__main__":
    unittest.main()