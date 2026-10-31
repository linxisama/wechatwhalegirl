"""连发合并快速模式窗口 100ms → 800ms，以及已有安装的一次性迁移（1.3.52）。

全部用例只在 tempfile 临时目录里建 PluginManager，绝不触碰真实数据根
C:/Users/16522/AppData/Local/WhaleGirlWeChat。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.plugins.manager import PluginManager


class MergeWindowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'data'
        self.settings_path = self.root / 'plugin_settings.json'
        self.marker_path = self.root / 'plugin_settings.migrations.json'

    def tearDown(self):
        self._tmp.cleanup()

    # ── 工具 ────────────────────────────────────────────────────────────────
    def _write_settings(self, payload):
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    def _write_marker(self, payload):
        self.root.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps(payload), encoding='utf-8')

    def _manager(self):
        return PluginManager(self.root)

    def _merge_ms(self, manager):
        return manager.get_settings('reply_preferences')['mergeMs']

    def _disk_merge_ms(self):
        return json.loads(self.settings_path.read_text('utf-8'))['reply_preferences']['mergeMs']

    # ── a. 全新数据目录 ─────────────────────────────────────────────────────
    def test_fresh_data_dir_uses_800_default(self):
        manager = self._manager()
        self.assertEqual(self._merge_ms(manager), 800)
        # 没有旧值可迁，就不该凭空写出迁移标记
        self.assertFalse(self.marker_path.exists())
        # 设置文件顶层的键只能是插件 id，不能混入迁移标记
        if self.settings_path.exists():
            payload = json.loads(self.settings_path.read_text('utf-8'))
            self.assertNotIn('fast_merge_800', payload)

    # ── b. 盘上是旧工厂默认 100 ─────────────────────────────────────────────
    def test_existing_100_is_migrated_and_persisted(self):
        self._write_settings({'reply_preferences': {'enabled': True, 'fastMode': True, 'mergeMs': 100}})
        manager = self._manager()
        self.assertEqual(self._merge_ms(manager), 800)
        self.assertEqual(self._disk_merge_ms(), 800, '迁移结果必须落盘，否则下次启动又变回 100')
        marker = json.loads(self.marker_path.read_text('utf-8'))
        self.assertEqual(marker, {'fast_merge_800': True})

    # ── c. 用户自己调过的 300 ───────────────────────────────────────────────
    def test_custom_value_is_kept_and_not_marked(self):
        self._write_settings({'reply_preferences': {'enabled': True, 'fastMode': True, 'mergeMs': 300}})
        manager = self._manager()
        self.assertEqual(self._merge_ms(manager), 300)
        self.assertEqual(self._disk_merge_ms(), 300)
        self.assertFalse(self.marker_path.exists(), '没迁移就不该记录标记')

    # ── d. 标记已存在 ───────────────────────────────────────────────────────
    def test_marker_blocks_second_migration(self):
        self._write_settings({'reply_preferences': {'mergeMs': 100}})
        self._write_marker({'fast_merge_800': True})
        manager = self._manager()
        self.assertEqual(self._merge_ms(manager), 100, '标记已记录就说明迁移做过了，用户后来改回 100 也不能再动')
        self.assertEqual(self._disk_merge_ms(), 100)

    # ── e. 保存设置抛异常 ───────────────────────────────────────────────────
    def test_save_failure_only_warns(self):
        self._write_settings({'reply_preferences': {'mergeMs': 100}})
        with mock.patch.object(PluginManager, '_save_settings', side_effect=OSError('磁盘只读')):
            with self.assertLogs('src.plugins.manager', level='WARNING') as logs:
                manager = self._manager()  # 构造不抛异常才算过关
        self.assertTrue(any('迁移' in line for line in logs.output), logs.output)
        # 内存值保留 800，不回滚成半迁移
        self.assertEqual(self._merge_ms(manager), 800)
        self.assertFalse(self.marker_path.exists(), '设置没落盘就不该写标记，下次启动还要重试')

    # ── f. plugin_settings.json 损坏 ────────────────────────────────────────
    def test_corrupt_settings_file_still_starts_with_800(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text('{ 这不是 JSON', encoding='utf-8')
        manager = self._manager()
        self.assertEqual(self._merge_ms(manager), 800)

    # ── g. 迁移不碰其它插件 ─────────────────────────────────────────────────
    def test_migration_touches_only_reply_preferences(self):
        self._write_settings({
            'reply_preferences': {'mergeMs': 100},
            'burst_merge': {'enabled': False, 'capMs': 1200},
        })
        manager = self._manager()
        self.assertEqual(self._merge_ms(manager), 800)
        burst = manager.get_settings('burst_merge')
        self.assertEqual(burst['enabled'], False)
        self.assertEqual(burst['capMs'], 1200, '迁移只能动 reply_preferences.mergeMs')


if __name__ == '__main__':
    unittest.main()
