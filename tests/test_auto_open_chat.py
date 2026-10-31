"""自动打开目标会话（用户明确要求恢复"最初的程序"行为）。

规则：当前窗口不是目标会话时，允许用搜索框搜索并打开它，**打开后必须校验会话名**，
名字对不上就绝不输入正文——这条底线不能因为"要自动化"而放宽。
"""

import unittest
from types import SimpleNamespace

from src.wechat_send_worker import send_text
from src.wechat_sender import IsolatedWeChatSender
from pathlib import Path


class _FakeInput:
    def __init__(self, uia):
        self.uia = uia
        self.value = ''

    def SetFocus(self):
        pass

    @property
    def HasKeyboardFocus(self):
        return True

    def GetValuePattern(self):
        return SimpleNamespace(IsReadOnly=False, SetValue=self._set_value)

    def _set_value(self, value):
        self.value = value

    def SendKeys(self, keys, **kwargs):
        if keys == '{Enter}':
            # 模拟"按下发送键后输入框清空、消息真的发出去"
            self.uia.sent.append(self.value)
            self.value = ''
        else:
            self.value = ''


class _FakeUIA:
    def __init__(self, current='别人的会话', opened=None, can_open=True, raises=None):
        self.current = current
        self.opened = opened
        self.can_open = can_open
        self.raises = raises
        self.sent = []
        self.input = _FakeInput(self)

    def ensure_window(self, *args, **kwargs):
        return True

    def current_chat(self):
        return self.current

    def open_chat(self, who):
        if self.raises:
            raise self.raises
        if not self.can_open:
            return False
        self.current = self.opened if self.opened is not None else who
        return True

    def _chat_input(self):
        return self.input

    def _read_edit_value(self, control):
        return control.value

    def _compare_norm(self, value):
        return str(value or '').strip()


class SendTextAutoOpenTests(unittest.TestCase):
    def test_switch_off_refuses_and_types_nothing(self):
        uia = _FakeUIA(current='别人的会话')
        ok, message = send_text(uia, '目标群', '你好', open_chat=False)
        self.assertFalse(ok)
        self.assertIn('当前不是目标会话', message)
        self.assertEqual(uia.sent, [])
        self.assertEqual(uia.input.value, '')

    def test_switch_on_opens_the_target_then_sends(self):
        uia = _FakeUIA(current='别人的会话')
        ok, message = send_text(uia, '目标群', '你好', open_chat=True)
        self.assertTrue(ok, message)
        self.assertEqual(uia.sent, ['你好'])
        self.assertEqual(uia.current, '目标群')

    def test_opened_but_wrong_chat_still_refuses(self):
        # 搜索命中了别的会话（同名/排行第一不是目标）：绝不打字
        uia = _FakeUIA(current='别人的会话', opened='西莲寺紫')
        ok, message = send_text(uia, '目标群', '你好', open_chat=True)
        self.assertFalse(ok)
        self.assertIn('不是目标', message)
        self.assertEqual(uia.sent, [])
        self.assertEqual(uia.input.value, '')

    def test_open_failure_gives_actionable_message(self):
        uia = _FakeUIA(current='别人的会话', can_open=False)
        ok, message = send_text(uia, '目标群', '你好', open_chat=True)
        self.assertFalse(ok)
        self.assertIn('没能自动打开会话', message)
        self.assertIn('目标群', message)
        self.assertEqual(uia.sent, [])

    def test_open_exception_is_reported_not_raised(self):
        uia = _FakeUIA(current='别人的会话', raises=RuntimeError('搜索框不见了'))
        ok, message = send_text(uia, '目标群', '你好', open_chat=True)
        self.assertFalse(ok)
        self.assertIn('RuntimeError', message)
        self.assertEqual(uia.sent, [])

    def test_already_on_target_skips_opening(self):
        uia = _FakeUIA(current='目标群')
        calls = []
        uia.open_chat = lambda who: calls.append(who) or True
        ok, message = send_text(uia, '目标群', '你好', open_chat=True)
        self.assertTrue(ok, message)
        self.assertEqual(calls, [])
        self.assertEqual(uia.sent, ['你好'])


class JobCarriesTheFlagTests(unittest.TestCase):
    def test_flag_goes_into_every_job(self):
        sender = IsolatedWeChatSender(Path('.'), 'acc', compat=lambda: True,
                                      open_chat=lambda: True)
        job = sender._prepare_job({'action': 'text'})
        self.assertTrue(job['auto_open_chat'])
        self.assertTrue(job['send_compat'])

    def test_default_is_off_for_safety_and_broken_supplier_fails_closed(self):
        self.assertFalse(IsolatedWeChatSender(Path('.'))._prepare_job({})['auto_open_chat'])

        def boom():
            raise RuntimeError('配置读不到')

        sender = IsolatedWeChatSender(Path('.'), '', open_chat=boom)
        self.assertFalse(sender._prepare_job({})['auto_open_chat'])


if __name__ == '__main__':
    unittest.main()
