"""1.3.103 插件清单契约：api_version 整数化 + 严格档 13 字段。

背景（用户 2026-11-08 的判定标准）：
  判断标准不是「能放进 mods/ 就算」，而是「有清单，能单独装、单独卸」。
  清单如果只填三个字段就能过，那它就不是清单，是装饰。

运行：./.venv-wechat/Scripts/python.exe -m unittest tests.test_plugin_contract_1103
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src import plugin_packs
from src.plugin_packs import (
    CAPABILITY_VOCABULARY,
    STRICT_API_VERSION,
    STRICT_REQUIRED_FIELDS,
    normalize_api_version,
    validate_manifest,
)
from src.runtime_identity import APP_VERSION

HOST = APP_VERSION


def _strict(**overrides):
    """一份 13 字段齐全的严格档清单。"""
    data = {
        "id": "demo_pack",
        "name": "测试包",
        "version": "1.0.0",
        "entry": "plugin.py",
        "author": "测试",
        "description": "严格档测试用",
        "api_version": STRICT_API_VERSION,
        "requires": "1.3.0",
        "data_policy": "只写自己的设置与状态",
        "native": "纯 Python",
        "side": "本机侧运行",
        "upstream": "本地测试",
        "capabilities": ["on_ingest"],
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def _legacy(**overrides):
    """兼容档（api_version 1）的最小清单：只有三个必填字段。"""
    data = {"id": "old_pack", "name": "老包", "version": "1.0.0", "api_version": 1}
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


class ApiVersionNormalizeTests(unittest.TestCase):
    """api_version 归一：社区两种方言都要认，版本号语义不能混进来。"""

    def test_integer_and_string_are_the_same_thing(self):
        self.assertEqual(normalize_api_version(1), 1)
        self.assertEqual(normalize_api_version("1"), 1)
        self.assertEqual(normalize_api_version(" 2 "), 2)

    def test_integral_float_accepted(self):
        self.assertEqual(normalize_api_version(1.0), 1)

    def test_version_like_string_rejected(self):
        # "1.0" 是版本号不是整数版本，放进来等于把两套语义混成一个
        self.assertIsNone(normalize_api_version("1.0"))
        self.assertIsNone(normalize_api_version(1.5))

    def test_non_numeric_rejected(self):
        for value in (None, True, False, "", "abc", [], {}):
            with self.subTest(value=value):
                self.assertIsNone(normalize_api_version(value))


class StrictTierTests(unittest.TestCase):
    """api_version 2 = 严格档：13 个字段一个都不能缺。"""

    def test_full_strict_manifest_passes(self):
        ok, errors, meta = validate_manifest(_strict(), HOST)
        self.assertTrue(ok, errors)
        self.assertEqual(errors, [])
        self.assertTrue(meta.get("strict"))
        self.assertEqual(meta["api_version"], STRICT_API_VERSION)

    def test_every_strict_field_is_required(self):
        """逐字段删掉，每个都必须被判非法并点名。"""
        for field in STRICT_REQUIRED_FIELDS:
            if field in ("id", "name", "version", "entry", "api_version"):
                continue  # 这几个在兼容档就必填，文案不同，另有测试覆盖
            data = _strict()
            data.pop(field)
            with self.subTest(field=field):
                ok, errors, _meta = validate_manifest(data, HOST)
                self.assertFalse(ok, field + " 缺失时必须判非法")
                self.assertTrue(any(field in item for item in errors),
                                (field, errors))

    def test_strict_empty_capabilities_rejected(self):
        ok, errors, _meta = validate_manifest(_strict(capabilities=[]), HOST)
        self.assertFalse(ok)
        self.assertTrue(any("capabilities" in item for item in errors), errors)

    def test_strict_unknown_capability_rejected(self):
        ok, errors, _meta = validate_manifest(_strict(capabilities=["on_ingest", "hack_db"]), HOST)
        self.assertFalse(ok)
        self.assertTrue(any("hack_db" in item for item in errors), errors)

    def test_non_hook_capabilities_are_legal(self):
        """一个钩子都不实现的包也合法：纯设置门 / 纯工具库。"""
        for kind in ("settings", "helper"):
            with self.subTest(kind=kind):
                ok, errors, _meta = validate_manifest(_strict(capabilities=[kind]), HOST)
                self.assertTrue(ok, errors)

    def test_capability_vocabulary_covers_hooks_and_kinds(self):
        self.assertIn("on_ingest", CAPABILITY_VOCABULARY)
        self.assertIn("generate", CAPABILITY_VOCABULARY)
        self.assertIn("settings", CAPABILITY_VOCABULARY)
        self.assertIn("helper", CAPABILITY_VOCABULARY)


class CompatTierTests(unittest.TestCase):
    """api_version 1 = 兼容档：存量包不能在升级当天集体失效。"""

    def test_legacy_manifest_without_optional_fields_still_ok(self):
        ok, errors, meta = validate_manifest(_legacy(), HOST)
        self.assertTrue(ok, errors)
        self.assertFalse(meta.get("strict"))
        self.assertTrue(meta["warnings"], "兼容档缺字段应给出提示而不是报错")

    def test_legacy_manifest_keeps_warning_text(self):
        ok, _errors, meta = validate_manifest(_legacy(), HOST)
        self.assertTrue(ok)
        self.assertTrue(any("author" in item for item in meta["warnings"]), meta["warnings"])

    def test_legacy_still_requires_id_name_version(self):
        for field in ("id", "name", "version"):
            data = _legacy()
            data.pop(field)
            with self.subTest(field=field):
                ok, errors, _meta = validate_manifest(data, HOST)
                self.assertFalse(ok)
                self.assertTrue(any("缺少必需字段" in item for item in errors), errors)

    def test_missing_api_version_defaults_to_compat(self):
        data = _legacy()
        data.pop("api_version")
        ok, errors, meta = validate_manifest(data, HOST)
        self.assertTrue(ok, errors)
        self.assertEqual(meta["api_version"], 1)
        self.assertFalse(meta.get("strict"))


class UnsupportedVersionTests(unittest.TestCase):
    def test_future_version_is_incompatible(self):
        ok, errors, meta = validate_manifest(_strict(api_version=3), HOST)
        self.assertFalse(ok)
        self.assertEqual(meta["status"], "incompatible")
        self.assertTrue(any("不兼容" in item for item in errors), errors)

    def test_supported_versions_are_integers(self):
        self.assertTrue(all(isinstance(item, int) for item in plugin_packs.SUPPORTED_API_VERSIONS))
        self.assertIn(STRICT_API_VERSION, plugin_packs.SUPPORTED_API_VERSIONS)


class SeedPackTests(unittest.TestCase):
    """仓库自带的 10 个种子包必须自身满足严格档 —— 否则标准是给别人定的。"""

    def _seed_root(self) -> Path:
        return Path(__file__).resolve().parents[1] / "plugin-packs"

    def test_seed_packs_are_strict_tier(self):
        root = self._seed_root()
        available = set(plugin_packs.OPTIONAL_PLUGIN_PACKS)
        for pack_id in plugin_packs.OPTIONAL_PLUGIN_PACKS:
            pack_dir = root / pack_id
            with self.subTest(pack=pack_id):
                data = json.loads((pack_dir / "plugin.json").read_text("utf-8"))
                self.assertEqual(data["api_version"], STRICT_API_VERSION,
                                 pack_id + " 应已升级到严格档")
                ok, errors, meta = validate_manifest(data, HOST, pack_dir=pack_dir,
                                                     available_ids=available)
                self.assertTrue(ok, (pack_id, errors))
                self.assertTrue(meta.get("strict"), pack_id)

    def test_seed_packs_declare_capabilities(self):
        root = self._seed_root()
        for pack_id in plugin_packs.OPTIONAL_PLUGIN_PACKS:
            data = json.loads((root / pack_id / "plugin.json").read_text("utf-8"))
            with self.subTest(pack=pack_id):
                self.assertTrue(data.get("capabilities"), pack_id + " 必须声明能力")
                for item in data["capabilities"]:
                    self.assertIn(item, CAPABILITY_VOCABULARY, (pack_id, item))

    def test_generator_and_repo_manifests_agree(self):
        """生成器写出来的清单必须与仓库里的一致，否则下次重新生成会漂移。"""
        import sys
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from tools.make_plugin_packs import manifest_of, find_plugin_class
        for pack_id in plugin_packs.OPTIONAL_PLUGIN_PACKS:
            _module, cls = find_plugin_class(pack_id)
            generated = manifest_of(pack_id, cls)
            stored = json.loads((root / "plugin-packs" / pack_id / "plugin.json").read_text("utf-8"))
            with self.subTest(pack=pack_id):
                self.assertEqual(generated, stored)


class InstallRejectsStrictViolationTests(unittest.TestCase):
    """坏清单必须真的装不进去，而不是只在界面上标红。"""

    def test_install_rejects_strict_manifest_missing_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "bad_pack"
            src_dir.mkdir()
            data = _strict(id="bad_pack", name="坏包")
            data.pop("upstream")
            (src_dir / "plugin.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
            (src_dir / "plugin.py").write_text("PLUGIN = None\n", encoding="utf-8")
            dest = root / "dest"
            dest.mkdir()
            result = plugin_packs.install_pack(src_dir, dest, host_version=HOST)
            self.assertFalse(result.get("ok"), result)
            self.assertFalse((dest / "bad_pack").exists(), "坏包不能落盘")


if __name__ == "__main__":
    unittest.main()