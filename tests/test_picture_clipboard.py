"""剪贴板只用替身；不读取、不改动测试电脑的真实剪贴板。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from src.picture_clipboard import PictureClipboard, file_drop


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'a.png'
        Image.new('RGB', (2, 2), 'blue').save(self.path)
        self.sequence = 1
        self.items = {13: '用户原来的文字'}
        self.clip = Mock()
        def enum(previous):
            formats = list(self.items)
            return formats[0] if not previous else (formats[formats.index(previous)+1] if formats.index(previous)+1 < len(formats) else 0)
        def empty():
            self.items.clear();self.sequence += 1
        def set_data(fmt, data):
            self.items[fmt] = data;self.sequence += 1
        self.clip.EnumClipboardFormats.side_effect = enum
        self.clip.GetClipboardData.side_effect = lambda fmt:self.items[fmt]
        self.clip.EmptyClipboard.side_effect = empty
        self.clip.SetClipboardData.side_effect = set_data
        self.native = SimpleNamespace(windll=SimpleNamespace(user32=SimpleNamespace(
            GetClipboardSequenceNumber=lambda:self.sequence)))

    def context(self):
        return patch.dict('sys.modules', {'win32clipboard': self.clip})

    def test_owner_restores_original_contents_even_when_send_is_cancelled(self):
        with self.context(), patch('src.picture_clipboard.ctypes', self.native):
            with self.assertRaises(RuntimeError):
                with PictureClipboard(self.path):
                    self.assertEqual(list(self.items), [15])
                    raise RuntimeError('发送取消')
        self.assertEqual(self.items, {13:'用户原来的文字'})

    def test_user_new_copy_is_not_overwritten(self):
        with self.context(), patch('src.picture_clipboard.ctypes', self.native):
            with PictureClipboard(self.path):
                self.items = {13:'用户后来复制的内容'};self.sequence += 1
        self.assertEqual(self.items, {13:'用户后来复制的内容'})

    def test_uncopyable_handle_rejects_before_clearing_clipboard(self):
        self.items = {2:12345}
        with self.context(), patch('src.picture_clipboard.ctypes', self.native):
            with self.assertRaises(ValueError):
                with PictureClipboard(self.path):
                    self.fail('不能修改无法备份的剪贴板')
        self.clip.EmptyClipboard.assert_not_called()

    def test_file_drop_uses_unicode_names_and_double_terminator(self):
        data = file_drop(['D:\\图库\\鲸鱼.png'])
        self.assertEqual(int.from_bytes(data[:4], 'little'), 20)
        self.assertEqual(data[20:].decode('utf-16le'), 'D:\\图库\\鲸鱼.png\0\0')
