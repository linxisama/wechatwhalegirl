"""1.3.106 数据命名空间 + data_policy 结构化。

两条判定标准的落地：
  「有清单，能单独装、单独卸」—— 插件的数据必须能跟着插件一起清干净，
  所以它必须只在自己的命名空间里写东西；
  「把代价写在明面上」—— data_policy 过去是一段自由文本，只能给人看，
  代码读不出「这个插件碰不碰聊天内容」。

运行：./.venv-wechat/Scripts/python.exe -m unittest tests.test_plugin_data_1106
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.plugin_packs import DATA_POLICY_TAGS, OPTIONAL_PLUGIN_PACKS, validate_manifest
from src.plugins import PluginBase, PluginMeta
from src.plugins.manager import PluginManager
from src.runtime_identity import APP_VERSION

HOST = APP_VERSION


def _strict(**overrides):
    data = {
        "id": "demo_pack",
        "name": "测试包",
        "version": "1.0.0",
        "entry": "plugin.py",
        "author": "测试",
        "description": "数据策略测试用",
        "api_version": 2,
        "requires": "1.3.0",
        "data_policy": ["own_settings", "own_state"],
        "native": "纯 Python",
        "side": "本机侧运行",
        "upstream": "本地测试",
        "capabilities": ["on_ingest"],
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


class Probe(PluginBase):
    META = PluginMeta(id="probe", name="p", description="")


class DataDirTests(unittest.TestCase):
    """data_dir 必须把插件锁在自己的 state_dir/<id>/ 里。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.manager = PluginManager(self.root)
        self.plugin = Probe(self.manager)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_dir_is_under_plugin_id(self):
        expected = self.root / "plugin_state" / "probe"
        self.assertEqual(self.plugin.data_dir(), expected)

    def test_subdirectory_is_created(self):
        target = self.plugin.data_dir("downloads")
        self.assertTrue(target.is_dir())
        self.assertEqual(target, self.root / "plugin_state" / "probe" / "downloads")

    def test_nested_subdirectories(self):
        target = self.plugin.data_dir("a", "b")
        self.assertTrue(target.is_dir())
        self.assertEqual(target.parent.parent.name, "probe")

    def test_parent_traversal_rejected(self):
        for bad in ("..", "."):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.plugin.data_dir(bad)

    def test_separator_rejected(self):
        for bad in ("a/b", "a\\b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.plugin.data_dir(bad)

    def test_drive_letter_rejected(self):
        with self.assertRaises(ValueError):
            self.plugin.data_dir("C:evil")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            self.plugin.data_dir("")


class StateFileTests(unittest.TestCase):
    """state_file 与 data_dir 用同一套校验 —— 否则命名空间就是摆设。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self._tmp.name))
        self.plugin = Probe(self.manager)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plain_name_works(self):
        self.assertEqual(self.plugin.state_file("state.json").name, "state.json")

    def test_parent_still_usable_for_sibling_dirs(self):
        """image_library 用 state_file('cache').parent —— 这条不能被破坏。"""
        parent = self.plugin.state_file("cache").parent
        self.assertEqual(parent.name, "probe")

    def test_traversal_rejected(self):
        for bad in ("..", ".", "", "../other/x.json", "a/b.json", "C:x.json"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.plugin.state_file(bad)

    def test_load_state_survives_missing_file(self):
        self.assertEqual(self.plugin.load_state("nope.json", {"a": 1}), {"a": 1})

    def test_save_then_load_roundtrip(self):
        self.plugin.save_state("rt.json", {"v": 42})
        self.assertEqual(self.plugin.load_state("rt.json", {}), {"v": 42})


class DataPolicyTagTests(unittest.TestCase):
    """data_policy 的结构化标签。"""

    def test_five_tags(self):
        self.assertEqual(DATA_POLICY_TAGS,
                         ("none", "own_settings", "own_state",
                          "chat_content", "external_network"))

    def test_tag_list_accepted(self):
        ok, errors, meta = validate_manifest(
            _strict(data_policy=["own_settings", "chat_content"]), HOST)
        self.assertTrue(ok, errors)
        self.assertEqual(meta["data_policy_tags"], ["own_settings", "chat_content"])

    def test_tags_are_lowercased_and_deduped(self):
        ok, errors, meta = validate_manifest(
            _strict(data_policy=["OWN_SETTINGS", "own_settings", "Own_State"]), HOST)
        self.assertTrue(ok, errors)
        self.assertEqual(meta["data_policy_tags"], ["own_settings", "own_state"])

    def test_unknown_tag_rejected(self):
        ok, errors, _meta = validate_manifest(
            _strict(data_policy=["own_settings", "hack"]), HOST)
        self.assertFalse(ok)
        self.assertTrue(any("hack" in item for item in errors), errors)

    def test_free_text_rejected_in_strict_tier(self):
        ok, errors, meta = validate_manifest(
            _strict(data_policy="只在本机读写"), HOST)
        self.assertFalse(ok)
        self.assertEqual(meta["data_policy_tags"], [])
        self.assertTrue(any("data_policy" in item for item in errors), errors)

    def test_missing_data_policy_rejected_in_strict_tier(self):
        data = _strict()
        data.pop("data_policy")
        ok, errors, _meta = validate_manifest(data, HOST)
        self.assertFalse(ok)
        self.assertTrue(any("data_policy" in item for item in errors), errors)

    def test_free_text_still_accepted_in_compat_tier(self):
        """存量包全是中文自由文本，升级当天不能集体失效。"""
        ok, errors, meta = validate_manifest({
            "id": "old_pack", "name": "老包", "version": "1.0.0",
            "api_version": 1, "data_policy": "只在本机读写",
        }, HOST)
        self.assertTrue(ok, errors)
        self.assertEqual(meta["data_policy_tags"], [])


class SeedPolicyTests(unittest.TestCase):
    """10 个种子包必须声明标签 —— 否则标准是给别人定的。"""

    def _root(self) -> Path:
        return Path(__file__).resolve().parents[1] / "plugin-packs"

    def test_seed_packs_use_tags(self):
        root = self._root()
        for pack_id in OPTIONAL_PLUGIN_PACKS:
            data = json.loads((root / pack_id / "plugin.json").read_text("utf-8"))
            with self.subTest(pack=pack_id):
                self.assertIsInstance(data["data_policy"], list,
                                      pack_id + " 的 data_policy 应是标签列表")
                self.assertTrue(data["data_policy"], pack_id)
                for tag in data["data_policy"]:
                    self.assertIn(tag, DATA_POLICY_TAGS, (pack_id, tag))

    def test_network_packs_declare_external_network(self):
        """联网的包必须自己承认会把数据发到本机以外。"""
        root = self._root()
        for pack_id in ("web_context", "web_lookup"):
            data = json.loads((root / pack_id / "plugin.json").read_text("utf-8"))
            with self.subTest(pack=pack_id):
                self.assertIn("external_network", data["data_policy"])

    def test_generator_agrees_with_repo(self):
        import sys
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from tools.make_plugin_packs import find_plugin_class, manifest_of
        for pack_id in OPTIONAL_PLUGIN_PACKS:
            _module, cls = find_plugin_class(pack_id)
            generated = manifest_of(pack_id, cls)
            stored = json.loads((root / "plugin-packs" / pack_id / "plugin.json").read_text("utf-8"))
            with self.subTest(pack=pack_id):
                self.assertEqual(generated, stored)


if __name__ == "__main__":
    unittest.main()