# -*- coding: utf-8 -*-
"""1.3.87：打开会话的每一个按键之前都要重新核前台，中途丢了绝不再按。"""
import unittest
from unittest.mock import patch

from src import wechat_guia_send as mod
from src.wechat_guia_send import _GuardFailed

from test_open_chat1361 import make_channel


def run_open(channel, gui, keys, fail_at):
    calls = []

    def guard(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == fail_at:
            raise _GuardFailed('foreground_lost', '')

    channel._require_foreground = guard
    with patch.object(mod, '_paste_keys',
                      side_effect=lambda _u, k=0x56: keys.append(('paste', k))), \
            patch.object(mod, '_tap_key',
                         side_effect=lambda _u, k: keys.append(('tap', k))):
        return channel._open_target_chat(gui, '西莲寺紫', 200.0)


class OpenChatForegroundTests(unittest.TestCase):
    def test_foreground_lost_between_ctrl_f_and_ctrl_v_stops_before_paste(self):
        channel, gui = make_channel(['西莲寺紫'])
        keys = []
        result = run_open(channel, gui, keys, fail_at=2)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'foreground_lost')
        self.assertEqual(keys, [('paste', 0x46)])
        self.assertNotIn(('paste', 0x56), keys)
        self.assertEqual(channel._clipboard.restored, 1)

    def test_foreground_lost_before_enter_never_presses_enter(self):
        channel, gui = make_channel(['西莲寺紫'])
        keys = []
        result = run_open(channel, gui, keys, fail_at=3)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'foreground_lost')
        self.assertEqual(keys, [('paste', 0x46), ('paste', 0x56)])
        self.assertNotIn(('tap', 0x0D), keys)


if __name__ == '__main__':
    unittest.main()
