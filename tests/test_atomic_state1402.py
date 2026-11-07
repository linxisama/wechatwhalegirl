# -*- coding: utf-8 -*-
"""1.3.102：插件清单与插件状态文件必须原子写，写失败不能留下半截 JSON。

这两处原本都用 ``Path.write_text``：它先截断目标文件再写，写到一半失败（磁盘满、
文件被杀毒/索引器占用）就把文件留成半截 JSON。而读取侧为了「坏了也不崩」会静默
回落到空清单 / 默认状态 —— 用户装的插件从界面消失、插件状态被重置，却没有任何报错。
"""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from src import persistence
from src.plugins import PluginBase
from src.plugins.manager import PluginManager


class _StateHost:
    """只借 PluginBase.save_state 用：给它一个 state_file() 和一个 state_dir。"""

    def __init__(self, folder):
        self.folder = pathlib.Path(folder)

    def state_file(self, name):
        self.folder.mkdir(parents=True, exist_ok=True)
        return self.folder / name


class AtomicManifestTests(unittest.TestCase):
    """custom_plugins.json：写失败不能把已有清单抹掉。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_failure_keeps_previous_manifest(self):
        """回归护栏：改前这里不抛异常，文件已被截断成半截 JSON。"""
        manager = PluginManager(self.dir)
        manager.register_custom("a_plugin")
        path = manager._custom_path
        before = path.read_text("utf-8")
        with mock.patch.object(persistence.os, "fsync",
                               side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                manager.register_custom("b_plugin")
        # 旧清单一个字节都不能少，而且仍然能被解析
        self.assertEqual(path.read_text("utf-8"), before)
        self.assertEqual(json.loads(before)["plugins"], ["a_plugin"])
        self.assertEqual(PluginManager(self.dir).custom_plugin_ids(), ["a_plugin"])

    def test_saved_manifest_is_valid_json_without_leftover_temp(self):
        manager = PluginManager(self.dir)
        manager.register_custom("a_plugin")
        payload = json.loads(manager._custom_path.read_text("utf-8"))
        self.assertEqual(payload, {"plugins": ["a_plugin"]})
        leftovers = [p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_second_save_leaves_a_backup(self):
        """atomic_text(backup=True)：覆盖前留一份上一版，坏了还能捞回来。"""
        manager = PluginManager(self.dir)
        manager.register_custom("a_plugin")
        manager.register_custom("b_plugin")
        backup = manager._custom_path.with_suffix(".json.bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(json.loads(backup.read_text("utf-8"))["plugins"], ["a_plugin"])
        self.assertEqual(PluginManager(self.dir).custom_plugin_ids(),
                         ["a_plugin", "b_plugin"])


class AtomicStateTests(unittest.TestCase):
    """插件状态文件：同样的截断风险，读取侧还会静默回落成默认值。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.host = _StateHost(pathlib.Path(self._tmp.name) / "state")

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_state_failure_keeps_previous_state(self):
        PluginBase.save_state(self.host, "s.json", {"count": 1})
        before = self.host.state_file("s.json").read_text("utf-8")
        with mock.patch.object(persistence.os, "fsync",
                               side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                PluginBase.save_state(self.host, "s.json", {"count": 2})
        self.assertEqual(self.host.state_file("s.json").read_text("utf-8"), before)
        self.assertEqual(json.loads(before), {"count": 1})

    def test_save_state_roundtrip_through_load_state(self):
        """写完能原样读回：防「把 dict 当文本写进去」这类二次编码错误。"""
        PluginBase.save_state(self.host, "s.json", {"a": [1, 2], "b": "中文"})
        loaded = PluginBase.load_state(self.host, "s.json", {})
        self.assertEqual(loaded, {"a": [1, 2], "b": "中文"})
        leftovers = [p.name for p in self.host.folder.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_atomic_text_restores_nothing_when_target_absent(self):
        """目标文件本来就不存在时，写失败不能凭空留下一个半截文件。"""
        target = self.host.state_file("new.json")
        target.unlink(missing_ok=True)
        with mock.patch.object(persistence.os, "fsync",
                               side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                PluginBase.save_state(self.host, "new.json", {"x": 1})
        self.assertFalse(target.exists())
        self.assertEqual([p.name for p in self.host.folder.iterdir()], [])


if __name__ == "__main__":
    unittest.main()
