"""回复不再默认拆两条（1.3.57）：工厂默认 1 + 盘上旧默认 2 的一次性迁移。

全部用例只在 tempfile 临时目录里建 PluginManager，绝不触碰真实数据根
C:/Users/16522/AppData/Local/WhaleGirlWeChat。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.plugins.manager import PluginManager


TWO_SENTENCES = '好的呀，我这就去看看。回头再跟你说细节！'


class SingleSegmentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'data'
        self.settings_path = self.root / 'plugin_settings.json'
        self.marker_path = self.root / 'plugin_settings.migrations.json'

    def tearDown(self):
        self._tmp.cleanup()

    # ── 工具 ────────────────────────────────────────────────────────────
    def _write_settings(self, payload):
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    def _write_marker(self, payload):
        self.root.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps(payload), encoding='utf-8')

    def _manager(self):
        return PluginManager(self.root)

    def _segments(self, manager):
        return manager.get_settings('chat_controls')['replySegments']

    def _disk_segments(self):
        return json.loads(self.settings_path.read_text('utf-8'))['chat_controls']['replySegments']

    # ── a. 全新数据目录 ────────────────────────────────────────────────
    def test_fresh_data_dir_defaults_to_one_segment(self):
        manager = self._manager()
        self.assertEqual(self._segments(manager), 1)
        # 没有旧值可迁，就不该凭空写出迁移标记
        self.assertFalse(self.marker_path.exists())

    # ── b. 盘上是旧默认 2 ─────────────────────────────────────────────
    def test_existing_two_is_migrated_and_persisted(self):
        self._write_settings({'chat_controls': {'enabled': True, 'replySegments': 2}})
        manager = self._manager()
        self.assertEqual(self._segments(manager), 1)
        self.assertEqual(self._disk_segments(), 1, '迁移结果必须落盘，否则下次启动又变回 2')
        self.assertEqual(json.loads(self.marker_path.read_text('utf-8')),
                         {'single_segment_1357': True})

    # ── c. 用户自己调过的 3 ───────────────────────────────────────────
    def test_custom_value_is_kept_and_not_marked(self):
        self._write_settings({'chat_controls': {'enabled': True, 'replySegments': 3}})
        manager = self._manager()
        self.assertEqual(self._segments(manager), 3)
        self.assertEqual(self._disk_segments(), 3)
        self.assertFalse(self.marker_path.exists(), '没迁移就不该记录标记')

    # ── d. 标记已存在 ─────────────────────────────────────────────────
    def test_marker_blocks_second_migration(self):
        self._write_settings({'chat_controls': {'replySegments': 2}})
        self._write_marker({'single_segment_1357': True})
        manager = self._manager()
        self.assertEqual(self._segments(manager), 2, '标记已记录就说迁移做过了，用户改回 2 不许再动')
        self.assertEqual(self._disk_segments(), 2)

    # ── e. 保存设置抛异常 ─────────────────────────────────────────────
    def test_save_failure_keeps_disk_and_skips_marker(self):
        self._write_settings({'chat_controls': {'replySegments': 2}})
        with mock.patch.object(PluginManager, '_save_settings', side_effect=OSError('磁盘只读')):
            manager = self._manager()
        self.assertEqual(self._segments(manager), 1, '内存里已经改过，下一次保存会带上')
        self.assertEqual(self._disk_segments(), 2, '没落盘就绝不写标记，下次启动还能重来')
        self.assertFalse(self.marker_path.exists())

    # ── f. 已经是 1 ───────────────────────────────────────────────────
    def test_already_one_writes_no_marker(self):
        self._write_settings({'chat_controls': {'replySegments': 1}})
        manager = self._manager()
        self.assertEqual(self._segments(manager), 1)
        self.assertFalse(self.marker_path.exists())

    # ── g. 行为：只有一条时不再插 PACE 标记 ────────────────────────────
    async def test_single_segment_emits_no_pace_marker(self):
        manager = self._manager()
        plugin = manager._plugins['chat_controls']
        out = await plugin.on_post_reply({'kind': 'private', 'text': 'x', 'id': 'm1'},
                                         TWO_SENTENCES, None)
        self.assertNotIn('\x01PACE:', out)
        self.assertIn('我这就去看看', out)


class SingleSegmentAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_segment_emits_no_pace_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(Path(tmp) / 'data')
            plugin = manager._plugins['chat_controls']
            out = await plugin.on_post_reply({'kind': 'private', 'text': 'x', 'id': 'm1'},
                                             TWO_SENTENCES, None)
            self.assertNotIn('\x01PACE:', out)
            self.assertIn('我这就去看看', out)


if __name__ == '__main__':
    unittest.main()
