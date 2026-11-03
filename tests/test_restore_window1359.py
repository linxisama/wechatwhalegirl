# -*- coding: utf-8 -*-
"""1.3.59 窗口最小化自动恢复（用户 m11214「发了」却没有任何回复的根因）

真机现场：用户发来消息、DSH 也生成了回复，可发送阶段第一步就 guia_unavailable
「账号与窗口绑定已失效」——微信窗口缩在托盘里。这里钉住三条边界：只对绑定窗口动手、
只显示不抢前台、恢复不了照旧 fail-closed。
"""
import unittest
from unittest.mock import patch

from src.wechat_guia_send import RESTORE_SHOW_FLAG, GuiaChannel, _restore_minimized


class FakeUser32:
    def __init__(self, iconic_sequence):
        self.iconic = list(iconic_sequence)
        self.shown = []

    def IsIconic(self, hwnd):
        if len(self.iconic) > 1:
            return self.iconic.pop(0)
        return self.iconic[0]

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


class RestoreMinimizedTests(unittest.TestCase):
    def test_visible_window_is_left_alone(self):
        u = FakeUser32([False])
        clock = Clock()
        self.assertTrue(_restore_minimized(7, sleep=clock.sleep, clock=clock, user32=u))
        self.assertEqual(u.shown, [])

    def test_iconic_window_is_shown_without_activation(self):
        u = FakeUser32([True, True, False])
        clock = Clock()
        self.assertTrue(_restore_minimized(7, sleep=clock.sleep, clock=clock, user32=u))
        self.assertEqual(u.shown, [(7, RESTORE_SHOW_FLAG)])
        self.assertEqual(RESTORE_SHOW_FLAG, 4)  # SW_SHOWNOACTIVATE，绝不抢前台
        self.assertTrue(clock.naps)

    def test_window_that_stays_minimized_reports_failure(self):
        u = FakeUser32([True])
        clock = Clock()
        self.assertFalse(_restore_minimized(7, sleep=clock.sleep, clock=clock, seconds=1.0, user32=u))
        self.assertEqual(u.shown, [(7, RESTORE_SHOW_FLAG)])
        self.assertGreaterEqual(clock.now, 1.0)

    def test_show_window_error_is_not_fatal(self):
        class Boom(FakeUser32):
            def ShowWindow(self, hwnd, flag):
                raise OSError('denied')

        u = Boom([True])
        self.assertFalse(_restore_minimized(7, sleep=lambda s: None, clock=lambda: 0.0, user32=u))

    def test_zero_hwnd_is_a_plain_failure(self):
        self.assertFalse(_restore_minimized(0))


class FakeGui:
    def __init__(self, hwnd=1234, pid=5678):
        self.main_hwnd = hwnd
        self.pid = pid


class ChannelRestoreTests(unittest.TestCase):
    def make(self, hwnd=1234, pid=5678):
        return GuiaChannel(gui_factory=lambda: FakeGui(hwnd, pid),
                           expected_window={'hwnd': hwnd, 'pid': pid})

    def test_minimized_window_is_restored_and_logged(self):
        channel = self.make()
        with patch('src.wechat_guia_send._iconic', return_value=True), \
                patch('src.wechat_guia_send._visible', return_value=True), \
                patch('src.wechat_guia_send._restore_minimized', return_value=True) as restore, \
                patch('src.wechat_guia_send.log_send_phase') as log:
            self.assertTrue(channel._restore_window_if_needed(FakeGui()))
        self.assertEqual(restore.call_args[0][0], 1234)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][0], 'guia_restore_window')
        self.assertIn('已自动恢复', log.call_args[0][3])

    def test_hidden_window_is_restored_and_logged(self):
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

    def test_visible_window_touches_nothing(self):
        channel = self.make()
        with patch('src.wechat_guia_send._iconic', return_value=False), \
                patch('src.wechat_guia_send._visible', return_value=True), \
                patch('src.wechat_guia_send._restore_minimized') as restore, \
                patch('src.wechat_guia_send.log_send_phase') as log:
            self.assertTrue(channel._restore_window_if_needed(FakeGui()))
        restore.assert_not_called()
        log.assert_not_called()

    def test_restore_failure_is_reported_not_swallowed(self):
        channel = self.make()
        with patch('src.wechat_guia_send._iconic', return_value=True), \
                patch('src.wechat_guia_send._visible', return_value=True), \
                patch('src.wechat_guia_send._restore_minimized', return_value=False), \
                patch('src.wechat_guia_send.log_send_phase') as log:
            self.assertFalse(channel._restore_window_if_needed(FakeGui()))
        self.assertIn('自动恢复失败', log.call_args[0][3])

    def test_build_restores_before_binding_check(self):
        channel = self.make()
        order = []
        channel._restore_window_if_needed = lambda gui: order.append('restore')
        channel._assert_binding = lambda gui: order.append('binding')
        channel._build()
        self.assertEqual(order, ['restore', 'binding'])


if __name__ == '__main__':
    unittest.main()
