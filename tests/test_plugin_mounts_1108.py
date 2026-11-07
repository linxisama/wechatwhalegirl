"""1.3.108 三个挂载点：把 8 个钩子归成「在链路的哪一步」。

用户的诊断是「7 种钩子平铺」。问题不在钩子数量，而在于没人说得清
「这个插件到底在链路的哪一步起作用」。

但钩子不能直接改名合并 —— 返回契约互不兼容：
  on_ingest 返回 str  = 立即回复并跳过模型
  on_block  返回 bool = 丢弃这条消息
  on_observed 返回 None = 只登记
揉成一个 observe() 就要靠 isinstance 猜返回值，那是把契约问题变成类型问题。
所以这一层做的是归类，不是合并。

运行：./.venv-wechat/Scripts/python.exe -m unittest tests.test_plugin_mounts_1108
"""
from __future__ import annotations

import unittest

from src import plugin_mounts
from src.plugin_mounts import (
    HOOK_MOUNT,
    MOUNT_POINTS,
    NON_HOOK_KINDS,
    describe,
    mount_of,
    mount_points_of,
    unknown_hooks,
)
from src.plugin_packs import CAPABILITY_HOOKS, CAPABILITY_KINDS, CAPABILITY_VOCABULARY
from src.plugins import PluginBase


class VocabularyTests(unittest.TestCase):
    def test_three_mount_points_in_pipeline_order(self):
        self.assertEqual([name for name, _label in MOUNT_POINTS],
                         ["observe", "compose", "settle"])

    def test_every_mount_point_has_chinese_label(self):
        labels = dict(MOUNT_POINTS)
        for name in ("observe", "compose", "settle"):
            with self.subTest(name=name):
                self.assertTrue(labels.get(name), name + " 必须有中文说明")

    def test_mount_of_known_hook(self):
        self.assertEqual(mount_of("on_ingest"), "observe")
        self.assertEqual(mount_of("on_pre_ai"), "compose")
        self.assertEqual(mount_of("tick"), "settle")

    def test_mount_of_unknown_returns_empty(self):
        self.assertEqual(mount_of("bogus"), "")
        self.assertEqual(mount_of(""), "")


class CoverageTests(unittest.TestCase):
    """HOOK_MOUNT 必须覆盖 PluginBase 的全部钩子 —— 漏一个就说不清那一步。"""

    def test_all_plugin_base_hooks_are_mapped(self):
        real = {name for name in dir(PluginBase)
                if name.startswith("on_") or name == "tick"}
        missing = real - set(HOOK_MOUNT)
        self.assertEqual(missing, set(), "这些钩子没有归属挂载点：" + "、".join(sorted(missing)))

    def test_every_hook_maps_to_a_declared_mount_point(self):
        declared = {name for name, _label in MOUNT_POINTS}
        for hook, mount in HOOK_MOUNT.items():
            with self.subTest(hook=hook):
                self.assertIn(mount, declared)

    def test_vocabulary_is_derived_from_hook_mount(self):
        """plugin_packs 的能力词表必须与这张表一致，不能各写一份。"""
        self.assertEqual(set(CAPABILITY_HOOKS), set(HOOK_MOUNT))
        self.assertEqual(tuple(CAPABILITY_KINDS), NON_HOOK_KINDS)
        self.assertEqual(set(CAPABILITY_VOCABULARY),
                         set(HOOK_MOUNT) | set(NON_HOOK_KINDS))


class GroupingTests(unittest.TestCase):
    def test_single_hook(self):
        self.assertEqual(mount_points_of(["on_ingest"]), ("observe",))

    def test_result_follows_pipeline_order_not_input_order(self):
        self.assertEqual(
            mount_points_of(["on_sent", "on_ingest", "on_pre_ai"]),
            ("observe", "compose", "settle"),
            "输出顺序必须是链路顺序，控制台不该自己排")

    def test_duplicates_collapsed(self):
        self.assertEqual(mount_points_of(["on_ingest", "on_block", "on_observed"]),
                         ("observe",))

    def test_non_hook_kinds_produce_no_mount(self):
        self.assertEqual(mount_points_of(["settings"]), ())
        self.assertEqual(mount_points_of(["helper"]), ())

    def test_empty_input(self):
        self.assertEqual(mount_points_of([]), ())
        self.assertEqual(mount_points_of(None), ())


class UnknownTests(unittest.TestCase):
    def test_unknown_names_reported(self):
        self.assertEqual(unknown_hooks(["on_ingest", "bogus", "settings"]), ("bogus",))

    def test_known_names_not_reported(self):
        self.assertEqual(unknown_hooks(list(HOOK_MOUNT) + list(NON_HOOK_KINDS)), ())

    def test_duplicates_collapsed(self):
        self.assertEqual(unknown_hooks(["x", "x", "y"]), ("x", "y"))


class DescribeTests(unittest.TestCase):
    """控制台插件卡片上那一行字。"""

    def test_single_mount(self):
        self.assertEqual(describe(["on_ingest"]), "观察")

    def test_multiple_mounts_joined(self):
        self.assertEqual(describe(["on_ingest", "on_sent"]), "观察 / 收尾")

    def test_settings_only(self):
        self.assertEqual(describe(["settings"]), "设置门")

    def test_helper_only(self):
        self.assertEqual(describe(["helper"]), "工具库")

    def test_empty(self):
        self.assertEqual(describe([]), "未声明")


class SeedPackMountTests(unittest.TestCase):
    """10 个种子包每个都该说得清自己在哪一步。"""

    def test_seed_packs_discover_with_mounts(self):
        import json
        from pathlib import Path
        from src.plugin_packs import OPTIONAL_PLUGIN_PACKS, discover
        from src.runtime_identity import APP_VERSION
        root = Path(__file__).resolve().parents[1] / "plugin-packs"
        entries = {entry["id"]: entry
                   for entry in discover([root], APP_VERSION,
                                         available_ids=set(OPTIONAL_PLUGIN_PACKS))}
        for pack_id in OPTIONAL_PLUGIN_PACKS:
            with self.subTest(pack=pack_id):
                entry = entries[pack_id]
                self.assertEqual(entry["status"], "ok", entry["errors"])
                self.assertIn("mounts", entry)
                self.assertTrue(entry["mount_label"], pack_id + " 应有一句挂载点说明")

    def test_settings_only_pack_has_no_mounts(self):
        from pathlib import Path
        from src.plugin_packs import discover
        from src.runtime_identity import APP_VERSION
        root = Path(__file__).resolve().parents[1] / "plugin-packs"
        entries = {entry["id"]: entry for entry in discover([root], APP_VERSION)}
        self.assertEqual(entries["sticker_admin"]["mounts"], [])
        self.assertEqual(entries["sticker_admin"]["mount_label"], "设置门")
        self.assertEqual(entries["sticker_memory"]["mount_label"], "工具库")


if __name__ == "__main__":
    unittest.main()