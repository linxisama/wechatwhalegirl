"""1.3.86：剪贴板被别的程序占用时，发送通道必须自己重试，且恢复动作永远不许把发送判死。

真机证据（backend.log 2026-10-31 20:48:26 / 20:48:49）两次发送都走到 finally 里的
restore，抛 pywintypes.error (5, 'OpenClipboard', '拒绝访问')，整条回复被判「通道内部异常」。
这组用例把三条边界钉死：占用要重试、重试有上限、恢复失败只记一笔。
"""
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pywintypes

from src.wechat_guia_send import _Clipboard, _GuardFailed, _exc_label, _open_clipboard


def _busy():
    return pywintypes.error(5, 'OpenClipboard', '拒绝访问。')


class FakeClipboard:
    """最小替身：前 fails 次 OpenClipboard 报「拒绝访问」，之后放行。"""

    CF_UNICODETEXT = 13

    def __init__(self, fails=0, items=None, text='原文本'):
        self.fails = fails
        self.items = list(items or [])
        self.text_value = text
        self.opens = 0
        self.closes = 0
        self.empties = 0
        self.written = []

    def OpenClipboard(self):
        self.opens += 1
        if self.opens <= self.fails:
            raise _busy()

    def CloseClipboard(self):
        self.closes += 1

    def EmptyClipboard(self):
        self.empties += 1

    def SetClipboardData(self, fmt, value):
        self.written.append((fmt, value))

    def EnumClipboardFormats(self, fmt):
        for index, (code, _) in enumerate(self.items):
            if index and self.items[index - 1][0] == fmt:
                return code
        return self.items[0][0] if self.items and not fmt else 0

    def GetClipboardData(self, fmt):
        for code, value in self.items:
            if code == fmt:
                return value
        raise _busy()

    def SetClipboardText(self, text, fmt=13):
        self.written.append((fmt, text))

    def IsClipboardFormatAvailable(self, fmt):
        return True


class FakeTime:
    """受控时钟：sleep 推进时间，省掉真等预算。"""

    def __init__(self, step=0.05):
        self.now = 0.0
        self.step = step
        self.sleeps = []

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += max(self.step, float(seconds))


class OpenClipboardRetryTest(unittest.TestCase):
    def test_snapshot_retries_until_clipboard_is_free(self):
        clip = FakeClipboard(fails=2, items=[(13, '原文本')])
        clock = FakeTime()
        with patch.dict(sys.modules, {'win32clipboard': clip}), \
                patch('src.wechat_guia_send.time', clock):
            saved = _Clipboard().snapshot()
        self.assertEqual(saved, [(13, '原文本')])
        self.assertEqual(clip.opens, 3)          # 两次被拒 + 第三次成功
        self.assertEqual(len(clock.sleeps), 2)   # 每次被拒才退避一次
        self.assertLessEqual(clock.sleeps[0], clock.sleeps[1])

    def test_snapshot_gives_up_after_budget(self):
        clip = FakeClipboard(fails=10 ** 6, items=[(13, '原文本')])
        clock = FakeTime()
        with patch.dict(sys.modules, {'win32clipboard': clip}), \
                patch('src.wechat_guia_send.time', clock):
            with self.assertRaises(pywintypes.error):
                _Clipboard().snapshot()
        self.assertGreaterEqual(clip.opens, 2)     # 有重试
        self.assertLessEqual(len(clock.sleeps), 20)  # 但绝不无限重试
        self.assertEqual(clip.empties, 0)

    def test_programming_error_is_not_retried(self):
        clip = FakeClipboard()
        clip.OpenClipboard = lambda: (_ for _ in ()).throw(TypeError('替身坏了'))
        clock = FakeTime()
        with patch.dict(sys.modules, {'win32clipboard': clip}), \
                patch('src.wechat_guia_send.time', clock):
            with self.assertRaises(TypeError):
                _Clipboard().snapshot()
        self.assertEqual(clock.sleeps, [])

    def test_copy_reports_busy_clipboard_as_send_failed(self):
        clip = FakeClipboard(fails=10 ** 6)
        clock = FakeTime()
        with patch.dict(sys.modules, {'win32clipboard': clip}), \
                patch('src.wechat_guia_send.time', clock):
            with self.assertRaises(_GuardFailed) as caught:
                _Clipboard().copy('正文')
        self.assertEqual(caught.exception.code, 'send_failed')
        self.assertIn('剪贴板', caught.exception.detail)
        self.assertIn('本次未输入正文', caught.exception.detail)
        self.assertEqual(clip.empties, 0)      # 没打开就不可能清空别人的剪贴板

    def test_copy_succeeds_after_one_retry(self):
        clip = FakeClipboard(fails=1)
        clock = FakeTime()
        with patch.dict(sys.modules, {'win32clipboard': clip}), \
                patch('src.wechat_guia_send.time', clock):
            _Clipboard().copy('正文')
        self.assertEqual(clip.written, [(13, '正文')])
        self.assertEqual(clip.closes, 1)


class RestoreNeverRaisesTest(unittest.TestCase):
    def test_restore_failure_is_recorded_not_raised(self):
        clip = FakeClipboard(fails=10 ** 6)
        clock = FakeTime()
        channel_clip = _Clipboard()
        with patch.dict(sys.modules, {'win32clipboard': clip}), \
                patch('src.wechat_guia_send.time', clock):
            ok = channel_clip.restore([(13, '原文本')])
        self.assertFalse(ok)
        self.assertTrue(channel_clip.restore_error.startswith('error('))
        self.assertIn('OpenClipboard', channel_clip.restore_error)
        self.assertEqual(clip.empties, 0)

    def test_restore_writes_every_format_and_clears_error(self):
        clip = FakeClipboard()
        channel_clip = _Clipboard()
        channel_clip.restore_error = '旧错误'
        with patch.dict(sys.modules, {'win32clipboard': clip}), \
                patch('src.wechat_guia_send.time', FakeTime()):
            ok = channel_clip.restore([(8, b'DIB-BYTES'), (49453, b'PNG')])
        self.assertTrue(ok)
        self.assertEqual(channel_clip.restore_error, '')
        self.assertEqual(clip.written, [(8, b'DIB-BYTES'), (49453, b'PNG')])
        self.assertEqual(clip.empties, 1)
        self.assertEqual(clip.closes, 1)

    def test_restore_empty_snapshot_still_clears_our_text(self):
        clip = FakeClipboard()
        with patch.dict(sys.modules, {'win32clipboard': clip}), \
                patch('src.wechat_guia_send.time', FakeTime()):
            self.assertTrue(_Clipboard().restore([]))
        self.assertEqual(clip.empties, 1)
        self.assertEqual(clip.written, [])


class OpenClipboardHelperTest(unittest.TestCase):
    def test_text_reads_after_retry(self):
        clip = FakeClipboard(fails=1, items=[(13, '原文本')])
        with patch.dict(sys.modules, {'win32clipboard': clip}), \
                patch('src.wechat_guia_send.time', FakeTime()):
            self.assertEqual(_Clipboard().text(), '原文本')
        self.assertEqual(clip.opens, 2)

    def test_helper_defaults_are_bounded(self):
        import src.wechat_guia_send as module
        self.assertGreater(module.CLIPBOARD_OPEN_BUDGET, 0)
        self.assertLessEqual(module.CLIPBOARD_OPEN_BUDGET, 5)
        self.assertLessEqual(module.CLIPBOARD_OPEN_MAX_DELAY, module.CLIPBOARD_OPEN_BUDGET)

    def test_exc_label_keeps_detail_for_pywintypes_error(self):
        label = _exc_label(_busy())
        self.assertTrue(label.startswith('error('))
        self.assertIn('OpenClipboard', label)

    def test_exc_label_truncates_long_text(self):
        label = _exc_label(ValueError('x' * 300))
        self.assertTrue(label.startswith('ValueError('))
        self.assertLessEqual(len(label), 100)


if __name__ == '__main__':
    unittest.main()
