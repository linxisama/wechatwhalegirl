# -*- coding: utf-8 -*-
"""1.3.87：被隐藏（IsWindowVisible=False）但不最小化的微信窗口也必须能自动恢复。

真机现场：UIA 找不到窗口后主窗在 130ms 内变成 IsWindowVisible=False 且 IsIconic=False，
1.3.59 的恢复只看 IsIconic，于是后续每次截图都是对着看不见的窗口，标题永远读空。
"""
import unittest
from unittest.mock import patch

from src.wechat_guia_send import RESTORE_SHOW_FLAG, GuiaChannel, _restore_minimized


class FakeUser32:
    """只实现恢复函数用到的两个查询；IsWindowVisible 可给序列，末项复用。"""

    def __init__(self, iconic=(False,), visible=(True,)):
        self.iconic = list(iconic)
        self.visible = list(visible)
        self.shown = []

    @staticmethod
    def _next(seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def IsIconic(self, hwnd):
        return self._next(self.iconic)

    def IsWindowVisible(self, hwnd):
        return self._next(self.visible)

    def ShowWindow(self, hwnd, flag):
        self.shown.append((int(hwnd), flag))
        return 1


class Clock:
    def __init__(self):
        self.now = 0.0
        self.naps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.naps.append(seconds)
        self.now += seconds


class HiddenRestoreTests(unittest.TestCase):
    def test_hidden_window_without_iconic_is_restored(self):
        u = FakeUser32(iconic=(False,), visible=(False, False, True))
        clock = Clock()
        self.assertTrue(_restore_minimized(7, sleep=clock.sleep, clock=clock, user32=u))
        self.assertEqual(u.shown, [(7, RESTORE_SHOW_FLAG)])
        self.assertEqual(RESTORE_SHOW_FLAG, 4)  # SW_SHOWNOACTIVATE，只显示不抢前台
        self.assertTrue(clock.naps)

    def test_hidden_window_that_never_shows_reports_failure(self):
        u = FakeUser32(iconic=(False,), visible=(False,))
        clock = Clock()
        self.assertFalse(_restore_minimized(7, sleep=clock.sleep, clock=clock,
                                            seconds=1.0, user32=u))
        self.assertEqual(u.shown, [(7, RESTORE_SHOW_FLAG)])
        self.assertGreaterEqual(clock.now, 1.0)

    def test_already_visible_window_gets_no_show_window_call(self):
        u = FakeUser32(iconic=(False,), visible=(True,))
        clock = Clock()
        self.assertTrue(_restore_minimized(7, sleep=clock.sleep, clock=clock, user32=u))
        self.assertEqual(u.shown, [])
        self.assertEqual(clock.naps, [])

    def test_old_stub_without_is_window_visible_is_treated_as_visible(self):
        class OldStub:
            def __init__(self):
                self.shown = []

            def IsIconic(self, hwnd):
                return False

            def ShowWindow(self, hwnd, flag):
                self.shown.append((hwnd, flag))

        u = OldStub()
        self.assertTrue(_restore_minimized(7, sleep=lambda _s: None,
                                           clock=lambda: 0.0, user32=u))
        self.assertEqual(u.shown, [])


class FakeGui:
    def __init__(self, hwnd=1234, pid=5678):
        self.main_hwnd, self.pid = hwnd, pid

    def is_alive(self):
        return True


class HiddenChannelTests(unittest.TestCase):
    def make(self):
        gui = FakeGui()
        return GuiaChannel(gui_factory=lambda: gui,
                           expected_window={'hwnd': gui.main_hwnd, 'pid': gui.pid})

    def test_restore_window_if_needed_fires_for_hidden_window(self):
        channel = self.make()
        with patch('src.wechat_guia_send._iconic', return_value=False), \
                patch('src.wechat_guia_send._visible', return_value=False), \
                patch('src.wechat_guia_send._restore_minimized', return_value=True) as restore, \
                patch('src.wechat_guia_send.log_send_phase') as log:
            self.assertTrue(channel._restore_window_if_needed(FakeGui()))
        self.assertEqual(restore.call_args[0][0], 1234)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][0], 'guia_restore_window')
        self.assertIn('隐藏', log.call_args[0][3])
        self.assertIn('已自动恢复', log.call_args[0][3])

    def test_available_reports_minimized_for_hidden_window(self):
        channel = GuiaChannel(gui_factory=lambda: FakeGui())
        with patch('src.wechat_guia_send._iconic', return_value=False), \
                patch('src.wechat_guia_send._visible', return_value=False), \
                patch('src.wechat_guia_send._restore_minimized', return_value=False), \
                patch('src.wechat_guia_send.log_send_phase'):
            result = channel.available()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'minimized')
        self.assertIn('隐藏', result.reason)


if __name__ == '__main__':
    unittest.main()
