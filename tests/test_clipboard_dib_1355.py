import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.wechat_guia_send import _Clipboard, _GuardFailed


class FakeClipboard:
    """最小替身：只实现 _Clipboard 用到的那几个函数。"""

    CF_UNICODETEXT = 13
    CF_BITMAP = 2
    CF_DIB = 8
    CF_DIBV5 = 17
    CF_HDROP = 15

    def __init__(self, items, unreadable=()):
        self.items = list(items)
        self.unreadable = set(unreadable)
        self.written = []

    def OpenClipboard(self, *a):
        return None

    def CloseClipboard(self):
        return None

    def EmptyClipboard(self):
        return None

    def EnumClipboardFormats(self, fmt):
        codes = [code for code, _ in self.items]
        for code in codes:
            if code > fmt:
                return code
        return 0

    def GetClipboardData(self, fmt):
        if fmt in self.unreadable:
            raise TypeError('format not readable')
        for code, value in self.items:
            if code == fmt:
                return value
        raise TypeError('missing')

    def SetClipboardData(self, fmt, value):
        self.written.append((fmt, value))


class ClipboardSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get('win32clipboard')
        self.fake = FakeClipboard([(2, 0), (8, b'DIB-BYTES'), (49453, b'PNG')])
        sys.modules['win32clipboard'] = self.fake

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop('win32clipboard', None)
        else:
            sys.modules['win32clipboard'] = self._saved

    def test_bitmap_handle_is_skipped_when_dib_is_present(self):
        snap = _Clipboard().snapshot()
        self.assertEqual([fmt for fmt, _ in snap], [8, 49453])

    def test_bitmap_handle_without_dib_is_skipped_not_fatal(self):
        # 1.3.58：剪贴板不再挡发送。存不住的格式记名跳过，其余照常恢复。
        sys.modules['win32clipboard'] = FakeClipboard([(2, 0), (49453, b'PNG')])
        clip = _Clipboard()
        self.assertEqual(clip.snapshot(), [(49453, b'PNG')])
        self.assertEqual(clip.skipped, (2,))
        self.assertTrue(clip.partial)

    def test_dibv5_also_covers_bitmap(self):
        sys.modules['win32clipboard'] = FakeClipboard([(2, 0), (17, b'DIBV5')])
        self.assertEqual([fmt for fmt, _ in _Clipboard().snapshot()], [17])

    def test_unreadable_format_aborts_without_touching_clipboard(self):
        fake = FakeClipboard([(8, b'DIB'), (49161, b'x')], unreadable=(49161,))
        sys.modules['win32clipboard'] = fake
        with self.assertRaises(TypeError):
            _Clipboard().snapshot()
        self.assertEqual(fake.written, [])

    def test_bitmap_handle_is_never_fetched(self):
        # 真机 2026-10-31 20:48：pywin32 的 GetClipboardData(CF_BITMAP) 会 GlobalLock 别人的
        # GDI 句柄，抛 (6, 'GetClipboardData:GlobalLock', '句柄无效')。快照根本不该去取它。
        class BitmapExplodes(FakeClipboard):
            def GetClipboardData(self, fmt):
                if fmt == 2:
                    raise OSError(6, 'GetClipboardData:GlobalLock', '句柄无效')
                return super().GetClipboardData(fmt)
        sys.modules['win32clipboard'] = BitmapExplodes([(2, 0), (8, b'DIB-BYTES')])
        self.assertEqual(_Clipboard().snapshot(), [(8, b'DIB-BYTES')])

    def test_unreadable_private_format_is_skipped(self):
        # 真机 2026-10-31 20:5x：从 Excel/Word 复制时，私有格式 0xBFxx 会抛
        # (0, 'GetClipboardData', 'No error message is available')。标准格式还在，就不该卡死整条回复。
        import pywintypes
        class PrivateExplodes(FakeClipboard):
            def GetClipboardData(self, fmt):
                if fmt == 49156:
                    raise pywintypes.error(0, 'GetClipboardData', 'No error message is available')
                return super().GetClipboardData(fmt)
        sys.modules['win32clipboard'] = PrivateExplodes([(8, b'DIB'), (13, 'hello'), (49156, b'x')])
        clip = _Clipboard()
        self.assertEqual(clip.snapshot(), [(8, b'DIB'), (13, 'hello')])
        self.assertIn(49156, clip.skipped)

    def test_unreadable_standard_format_is_skipped_not_fatal(self):
        # 1.3.58：标准格式被别的程序持有（真机 22:28）也只是少备份一项，不能拦下整条回复。
        import pywintypes
        class DibExplodes(FakeClipboard):
            def GetClipboardData(self, fmt):
                if fmt == 8:
                    raise pywintypes.error(0, 'GetClipboardData', 'No error message is available')
                return super().GetClipboardData(fmt)
        sys.modules['win32clipboard'] = DibExplodes([(8, b'DIB'), (13, 'hello')])
        clip = _Clipboard()
        self.assertEqual(clip.snapshot(), [(13, 'hello')])
        self.assertEqual(clip.skipped, (8,))

    def test_metafile_handles_are_skipped_not_fetched(self):
        sys.modules['win32clipboard'] = FakeClipboard([(3, 0), (13, 'hello'), (14, 0)])
        clip = _Clipboard()
        self.assertEqual(clip.snapshot(), [(13, 'hello')])
        self.assertEqual(clip.skipped, (3, 14))

    def test_repeated_format_code_does_not_loop_forever(self):
        class Looping(FakeClipboard):
            def EnumClipboardFormats(self, fmt):
                return 2
        sys.modules['win32clipboard'] = Looping([(2, 123456)])
        clip = _Clipboard()
        self.assertEqual(clip.snapshot(), [])      # 循环必须终止；位图句柄只记名跳过
        self.assertEqual(clip.skipped, (2,))

    def test_hdrop_list_is_kept(self):
        sys.modules['win32clipboard'] = FakeClipboard([(15, ['C:\\a.txt'])])
        self.assertEqual(_Clipboard().snapshot(), [(15, ['C:\\a.txt'])])

    def test_restore_writes_every_saved_format(self):
        clip = _Clipboard()
        clip.restore([(8, b'DIB-BYTES'), (49453, b'PNG')])
        self.assertEqual(self.fake.written, [(8, b'DIB-BYTES'), (49453, b'PNG')])


if __name__ == '__main__':
    unittest.main()
