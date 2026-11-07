# -*- coding: utf-8 -*-
"""1.3.99：表情库淘汰逻辑的三处缺陷（A 超容量、B 新图被自己挤掉、C 先删文件后写索引）。

这些用例只碰 src.sticker_library，不引入 GUI/HTTP 依赖，所以自己造图，
不去 import tests/test_sticker_library.py（那份会连带拉起整条运行时）。
"""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.sticker_library import StickerLibrary


def png_bytes(seed: int, size=(120, 120)) -> bytes:
    """造一张可区分的小 PNG：同 seed 同字节（内容寻址靠字节去重）。"""
    width, height = size
    raw = bytes((seed * 37 + i) % 256 for i in range(width * height * 3))
    buffer = io.BytesIO()
    Image.frombytes('RGB', (width, height), raw).save(buffer, format='PNG')
    return buffer.getvalue()


def huge_png() -> bytes:
    """≈8.68MB 的噪声 PNG：比 max_mb=6 的库容量上限还大。"""
    buffer = io.BytesIO()
    Image.frombytes('RGB', (1700, 1700), os.urandom(1700 * 1700 * 3)).save(buffer, format='PNG')
    return buffer.getvalue()


class EvictLimitTests(unittest.TestCase):
    """缺陷 A：单图超过整个库的容量上限时不能把收藏夹清空。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_oversize_image_rejected_before_touching_existing_library(self):
        store = StickerLibrary(self.root, max_mb=6)
        for seed in (1, 2, 3):
            store.put(png_bytes(seed), f'小图{seed}')
        with self.assertRaises(ValueError) as caught:
            store.put(huge_png(), '巨图')
        # 文案要能指导下一步，不是干巴巴一句「失败」
        self.assertIn('容量上限', str(caught.exception))
        self.assertEqual(len(store.entries()), 3)
        self.assertEqual(len(list((self.root / 'images').iterdir())), 3)
        self.assertEqual(len(StickerLibrary(self.root, max_mb=6).entries()), 3)

    def test_oversize_image_on_empty_library_leaves_no_index(self):
        store = StickerLibrary(self.root, max_mb=6)
        with self.assertRaises(ValueError):
            store.put(huge_png(), '巨图')
        self.assertEqual(store.entries(), [])
        self.assertEqual(list((self.root / 'images').iterdir()), [])
        # 拒绝发生在落盘之前：连空索引都不该写出来
        self.assertFalse((self.root / 'index.json').exists())


class EvictKeepNewTests(unittest.TestCase):
    """缺陷 B：库里每张都 used 过之后，新图不能一入库就被自己挤掉。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StickerLibrary(self.root, max_items=10)

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_image_survives_after_every_old_one_was_used(self):
        ids = [self.store.put(png_bytes(seed), f'旧图{seed}')[0]['id'] for seed in range(10)]
        self.store.used(ids)
        entry, fresh = self.store.put(png_bytes(99), '新图')
        self.assertTrue(fresh)
        self.assertIn(entry['id'], [item['id'] for item in self.store.entries()])
        self.assertEqual(len(self.store.entries()), 10)
        self.assertIsNotNone(self.store.path_for(entry['id']))
        remaining = len(list((self.root / 'images').iterdir()))
        self.assertEqual(remaining, 10)


class EvictSaveOrderTests(unittest.TestCase):
    """缺陷 C：索引写失败时不能留下「索引里有、磁盘上没有」的条目。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StickerLibrary(self.root, max_items=10)

    def tearDown(self):
        self._tmp.cleanup()

    def test_failed_index_save_keeps_every_old_image(self):
        ids = [self.store.put(png_bytes(seed), f'旧图{seed}')[0]['id'] for seed in range(10)]
        with patch.object(StickerLibrary, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.put(png_bytes(42), '新图')
        self.assertEqual(len(self.store.entries()), 10)
        for key in ids:
            self.assertIsNotNone(self.store.path_for(key), key)
        self.assertEqual(len(list((self.root / 'images').iterdir())), 10)
        # 磁盘上的索引也必须是旧的 10 条：不能出现「索引里有、文件已经删了」
        self.assertEqual(len(json.loads((self.root / 'index.json').read_text('utf-8'))), 10)
        reloaded = StickerLibrary(self.root, max_items=10)
        self.assertEqual(len(reloaded.entries()), 10)
        for key in ids:
            self.assertTrue(reloaded.path_for(key).is_file(), key)


class EvictOrderTests(unittest.TestCase):
    """正常淘汰：数量封顶、按 LRU 挑、索引里每一条都还读得到。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StickerLibrary(self.root, max_items=10)

    def tearDown(self):
        self._tmp.cleanup()

    def test_twelve_puts_leave_ten_readable_entries(self):
        ids = [self.store.put(png_bytes(seed), f'图{seed}')[0]['id'] for seed in range(12)]
        entries = self.store.entries()
        self.assertEqual(len(entries), 10)
        for item in entries:
            path = self.store.path_for(item['id'])
            self.assertIsNotNone(path)
            self.assertTrue(path.is_file())
        self.assertIsNone(self.store.path_for(ids[0]))
        self.assertIsNone(self.store.path_for(ids[1]))
        self.assertEqual(len(list((self.root / 'images').iterdir())), 10)

    def test_recently_used_entry_is_not_the_one_evicted(self):
        ids = [self.store.put(png_bytes(seed), f'图{seed}')[0]['id'] for seed in range(10)]
        self.store.used([ids[-1]])
        self.store.put(png_bytes(50), '再加一张')
        alive = [item['id'] for item in self.store.entries()]
        self.assertIn(ids[-1], alive)
        self.assertNotIn(ids[0], alive)


if __name__ == '__main__':
    unittest.main()
