"""同进程的其他顶层窗不能代替绑定聊天主窗；只使用假窗口。"""
import unittest
from unittest.mock import patch

from src.wechat_guia_send import _GuardFailed
from tests.test_wechat_guia_send import FakeGUI, channel_for, fake_user32


class BoundRootWindowTests(unittest.TestCase):
    def channel(self, foreground=4242, point=4242, root=4242):
        gui = FakeGUI(hwnd=4242, pid=42164)
        api = fake_user32(foreground)
        api.WindowFromPoint.return_value = point
        api.GetAncestor.side_effect = None
        api.GetAncestor.return_value = root
        channel = channel_for(gui, user32=api)
        return gui, api, channel

    def test_same_pid_other_top_level_window_is_not_foreground_chat(self):
        gui, api, channel = self.channel(foreground=777, root=777)
        self.assertFalse(channel._wechat_is_foreground(gui))

    def test_same_pid_child_of_bound_main_is_accepted(self):
        gui, api, channel = self.channel(foreground=777)
        self.assertTrue(channel._wechat_is_foreground(gui))
        api.GetAncestor.assert_called()

    def test_unknown_root_cannot_be_used_as_foreground_chat(self):
        gui, api, channel = self.channel(foreground=777, root=0)
        self.assertFalse(channel._wechat_is_foreground(gui))

    def test_foreground_guard_rejects_same_pid_modal_without_clipboard_or_keys(self):
        gui, api, channel = self.channel(foreground=777, root=777)
        with self.assertRaises(_GuardFailed) as caught:
            channel._require_foreground(gui, channel._clock() + 2)
        self.assertEqual(caught.exception.code, 'foreground_lost')
        api.SendInput.assert_not_called()
        self.assertEqual(channel._clipboard.writes, [])

    def test_click_point_on_same_pid_other_top_level_window_is_blocked(self):
        gui, api, channel = self.channel(point=777, root=777)
        geometry = channel._geometry(gui)
        with self.assertRaises(_GuardFailed) as caught:
            channel._point(gui, 400, 700, geometry)
        self.assertEqual(caught.exception.code, 'blocked_by_window')
        api.SendInput.assert_not_called()
        self.assertNotIn('focus', gui.calls)

    def test_render_child_click_point_of_bound_main_is_allowed(self):
        gui, api, channel = self.channel(point=777)
        channel._point(gui, 400, 700, channel._geometry(gui))
        api.GetAncestor.assert_called()

    def test_root_query_failure_fails_closed(self):
        gui, api, channel = self.channel(foreground=777)
        api.GetAncestor.side_effect = OSError('private-window-query-details')
        self.assertFalse(channel._wechat_is_foreground(gui))
        with self.assertRaises(_GuardFailed) as caught:
            channel._require_foreground(gui, channel._clock() + 2)
        self.assertNotIn('private', str(caught.exception))

    def test_high_bit_window_handles_are_passed_without_truncation(self):
        gui, api, channel = self.channel()
        gui.main_hwnd = 0x100000123
        child = 0x100000456
        api.GetAncestor.return_value = gui.main_hwnd
        with patch('src.wechat_guia_send._foreground_pid', return_value=gui.pid):
            self.assertTrue(channel._bound_window_root(gui, child))
        handle, flags = api.GetAncestor.call_args.args
        self.assertEqual(handle.value, child)
        self.assertEqual(flags, 2)

    def test_reused_pid_and_handle_with_new_process_generation_loses_binding(self):
        gui, api, channel = self.channel()
        channel._account_path = 'bound-account'
        channel._expected_window = dict(hwnd=4242, pid=42164, process_created=1.0)
        channel._window_access = object()
        current = dict(channel._expected_window, process_created=2.0, minimized=False)
        with patch('src.wechat_window.select_window', return_value=(current, {'matched_count': 1})):
            with self.assertRaises(_GuardFailed):
                channel._assert_binding(gui)
        api.SendInput.assert_not_called()
        self.assertEqual(channel._clipboard.writes, [])


if __name__ == '__main__':
    unittest.main()
