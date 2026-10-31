"""提交前的窗口变化与输入框伸缩；所有窗口、鼠标和微信都是假对象。"""
import unittest

from src.wechat_guia_send import _send_input_click
from tests.test_wechat_guia_send import FakeGUI, channel_for, fake_user32


class SubmissionBoundaryTests(unittest.TestCase):
    def test_other_chat_empty_box_never_confirms_the_submitted_target(self):
        class OtherChatEmpty(FakeGUI):
            def _input_box_has_text(self, box=None):
                if self.sent and self.title != '测试群':
                    return False
                return super()._input_box_has_text(box)

        gui = OtherChatEmpty(send_ok=False)
        channel = channel_for(gui)
        channel._phase = lambda stage: setattr(gui, 'title', '另一空白会话') if stage == 'confirmation' else None
        result = channel.send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostic.get('submission_state'), 'uncertain')
        self.assertEqual(gui.calls.count('input'), 1)
        self.assertEqual(len(gui.send_clicks), 1)

    def test_state_changes_while_confirming_keep_the_pre_press_verdict(self):
        # 1.3.42：按键之后只剩「墨迹是否变少」这一条证据。会话名/前台/绑定的变化在按下
        # 发送键之前已经各核验过两次，按键之后再复核只会把已经发出去的消息翻成"结果
        # 不确定"（真机上标题 OCR 一抖就翻转结论，这才是假失败的来源），所以这三类变化
        # 不再翻转结论。唯一还能拦住的是时钟：_read 读完仍复核截止时间，超时照旧按
        # "结果不确定、不重发"处理。
        for change, expected_ok in (('chat', True), ('foreground', True),
                                    ('binding', True), ('deadline', False)):
            with self.subTest(change=change):
                now = [0.0]
                api = fake_user32()

                class ChangesWhileConfirming(FakeGUI):
                    def composer_cleared(self, box, before, window=3.0):
                        if change == 'chat':
                            self.title = '另一空白会话'
                        elif change == 'foreground':
                            api.GetForegroundWindow.return_value = 999
                        elif change == 'binding':
                            self.pid = 999
                        else:
                            now[0] = 30.0
                        return super().composer_cleared(box, before, window)

                gui = ChangesWhileConfirming()
                result = channel_for(gui, user32=api, clock=lambda: now[0]).send('测试群', '正文')
                self.assertEqual(result.ok, expected_ok, result.reason)
                self.assertEqual(result.diagnostic.get('submission_state'),
                                 'confirmed' if expected_ok else 'uncertain')
                self.assertEqual(gui.calls.count('input'), 1)
                self.assertEqual(len(gui.send_clicks), 1)  # 按键只发生一次，绝不重发

    def test_layout_expands_immediately_when_paste_arrives(self):
        class ExpandsOnPaste(FakeGUI):
            def get_input_box(self):
                return (300, 620, 1200, 850) if self.pasted else super().get_input_box()

            def _input_box_has_text(self, box=None):
                if self.pasted and box != self.get_input_box():
                    raise RuntimeError('粘贴后旧区域不再有效')
                return super()._input_box_has_text(box)

        gui = ExpandsOnPaste()
        result = channel_for(gui).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(gui._last_input_box, (300, 620, 1200, 850))
        self.assertEqual(gui.calls.count('input'), 1)
        self.assertEqual(len(gui.send_clicks), 1)

    def test_input_phase_changed_chat_or_new_draft_is_not_pasted_over(self):
        for change in ('chat', 'draft'):
            with self.subTest(change=change):
                gui = FakeGUI()
                channel = channel_for(gui)

                def phase(stage):
                    if stage == 'input':
                        if change == 'chat':
                            gui.title = '另一会话'
                        else:
                            gui.pasted = True  # 模拟用户写入，未投递任何Ctrl+V
                            gui.draft_body = '必须保留的草稿'

                channel._phase = phase
                result = channel.send('测试群', '正文')
                self.assertFalse(result.ok)
                self.assertNotIn('input', gui.calls)
                self.assertEqual(gui.send_clicks, [])
                self.assertEqual(channel._clipboard.writes, [])
                self.assertEqual(result.diagnostic.get('submission_state'), 'not_submitted')

    def test_clipboard_write_that_changes_chat_or_draft_does_not_paste(self):
        for change in ('chat', 'draft'):
            with self.subTest(change=change):
                gui = FakeGUI()
                channel = channel_for(gui)
                clipboard = channel._clipboard
                copy = clipboard.copy

                def racing_copy(text):
                    copy(text)
                    if change == 'chat':
                        gui.title = '另一会话'
                    else:
                        gui.pasted = True
                        gui.draft_body = '必须保留的草稿'

                clipboard.copy = racing_copy
                result = channel.send('测试群', '正文')
                self.assertFalse(result.ok)
                self.assertNotIn('input', gui.calls)
                self.assertEqual(gui.send_clicks, [])
                self.assertEqual(clipboard.value, '原剪贴板')
                self.assertEqual(result.diagnostic.get('submission_state'), 'not_submitted')

    def test_input_focus_uses_the_guarded_sendinput_click_without_style_changes(self):
        class NoStyleChanges(FakeGUI):
            def wx_click(self, x, y):
                raise AssertionError('旧点击会修改渲染子窗样式，禁止调用')

        gui = NoStyleChanges()
        result = channel_for(gui).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(gui.calls.count('focus'), 1)
        self.assertEqual(gui.calls.count('click_send'), 1)

    def test_focus_phase_changes_cannot_click_or_paste(self):
        for change in ('foreground', 'point', 'geometry', 'deadline', 'chat'):
            with self.subTest(change=change):
                now = [0.0]
                gui = FakeGUI()
                api = fake_user32()
                channel = channel_for(gui, user32=api, clock=lambda: now[0])

                def phase(stage):
                    if stage != 'focus':
                        return
                    if change == 'foreground':
                        api.GetForegroundWindow.return_value = 999
                    elif change == 'point':
                        api.WindowFromPoint.return_value = 999
                    elif change == 'geometry':
                        gui.origin_x += 10
                    elif change == 'deadline':
                        now[0] = 30.0
                    else:
                        gui.title = '另一会话'

                channel._phase = phase
                result = channel.send('测试群', '正文')
                self.assertFalse(result.ok)
                self.assertEqual(gui.clicks, [])
                self.assertNotIn('input', gui.calls)
                api.SendInput.assert_not_called()
                self.assertEqual(result.diagnostic.get('submission_state'), 'not_submitted')

    def test_changed_draft_box_from_refresh_is_used_for_confirmation(self):
        class Resized(FakeGUI):
            def refresh_composer(self):
                self.refreshed = True
                return (300, 660, 1200, 850)

            def get_input_box(self):
                return ((300, 660, 1200, 850) if getattr(self, 'refreshed', False)
                        else super().get_input_box())

            def _input_box_has_text(self, box=None):
                if getattr(self, 'refreshed', False) and box != self.get_input_box():
                    raise RuntimeError('正文区域未经像素核验')
                return super()._input_box_has_text(box)

        gui = Resized()
        result = channel_for(gui).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(gui.send_clicks), 1)

    def test_draft_box_that_shrinks_after_submission_is_reacquired(self):
        class Collapsed(FakeGUI):
            def get_input_box(self):
                return (300, 700, 1200, 850) if self.sent else super().get_input_box()

            def _input_box_has_text(self, box=None):
                if self.sent and box != self.get_input_box():
                    raise RuntimeError('正文区域未经像素核验')
                return super()._input_box_has_text(box)

        gui = Collapsed()
        result = channel_for(gui).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(gui.send_clicks), 1)

    def test_send_phase_changes_cannot_reach_mouse_down(self):
        for change in ('foreground', 'point', 'geometry', 'deadline', 'chat', 'body'):
            with self.subTest(change=change):
                now = [0.0]
                gui = FakeGUI()
                api = fake_user32()
                channel = channel_for(gui, user32=api, clock=lambda: now[0])

                def phase(stage):
                    if stage != 'send_key':
                        return
                    if change == 'foreground':
                        api.GetForegroundWindow.return_value = 999
                    elif change == 'point':
                        api.WindowFromPoint.return_value = 999
                    elif change == 'geometry':
                        gui.origin_x += 10
                    elif change == 'deadline':
                        now[0] = 30.0
                    elif change == 'chat':
                        gui.title = '另一会话'
                    else:
                        gui.draft_body = '用户后来写入的草稿'

                channel._phase = phase
                result = channel.send('测试群', '正文')
                self.assertFalse(result.ok)
                self.assertEqual(gui.send_clicks, [])
                self.assertEqual(len([call for call in api.SendInput.call_args_list
                                      if call.args[0] == 1]), 2)  # 只有先前的聚焦按下/抬起
                self.assertEqual(len(gui.focus_clicks), 1)
                self.assertEqual(gui.calls.count('input'), 1)
                self.assertTrue(result.diagnostic.get('send_uncertain'))

    def test_foreground_lost_during_hover_prevents_mouse_down(self):
        gui = FakeGUI()
        api = fake_user32()

        def sleep(seconds):
            if api.SetCursorPos.called and gui.pasted:
                api.GetForegroundWindow.return_value = 999

        result = channel_for(gui, user32=api, sleep=sleep).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(gui.send_clicks, [])
        self.assertEqual(sum(call.args[0] == 1 for call in api.SendInput.call_args_list), 2)
        self.assertEqual(len(gui.focus_clicks), 1)

    def test_cursor_move_failure_prevents_mouse_down(self):
        api = fake_user32()
        api.SetCursorPos.return_value = 0
        with self.assertRaises(RuntimeError):
            _send_input_click(api, 1180, 925, lambda seconds: None)
        api.SendInput.assert_not_called()

    def test_invalid_refreshed_box_cannot_reach_mouse_down(self):
        for box in (None, (), (0, 0, 100, 100), (300, 660, 1201, 850),
                    (300, 660, 1200, '850')):
            with self.subTest(box=box):
                gui = FakeGUI()
                gui.refresh_composer = lambda: box
                result = channel_for(gui).send('测试群', '正文')
                self.assertFalse(result.ok)
                self.assertEqual(gui.send_clicks, [])
                self.assertEqual(result.diagnostic.get('submission_state'), 'not_submitted')

    def test_unconfirmed_postsend_box_is_uncertain_without_a_second_click(self):
        class InvalidAfterSubmit(FakeGUI):
            def get_input_box(self):
                return None if self.sent else super().get_input_box()

        gui = InvalidAfterSubmit()
        result = channel_for(gui).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(len(gui.send_clicks), 1)
        self.assertTrue(result.diagnostic.get('send_uncertain'))

    def test_virtual_desktop_change_during_hover_prevents_mouse_down(self):
        api = fake_user32()

        def change_display(seconds):
            api.GetSystemMetrics.side_effect = lambda index: {
                76: 0, 77: 0, 78: 2560, 79: 1440}[index]

        with self.assertRaises(RuntimeError):
            _send_input_click(api, 1180, 925, change_display)
        api.SendInput.assert_not_called()

    def test_virtual_desktop_query_failure_prevents_mouse_down(self):
        api = fake_user32()
        api.GetSystemMetrics.side_effect = OSError('display query unavailable')
        with self.assertRaises(RuntimeError):
            _send_input_click(api, 1180, 925, lambda seconds: None)
        api.SetCursorPos.assert_not_called()
        api.SendInput.assert_not_called()

    def test_virtual_desktop_coordinates_include_negative_screen(self):
        api = fake_user32()
        api.GetSystemMetrics.side_effect = lambda index: {
            76: -1920, 77: -200, 78: 3840, 79: 1280}[index]
        api.SendInput.side_effect = None
        api.SendInput.return_value = 1
        _send_input_click(api, -1200, 400, lambda seconds: None)
        self.assertEqual(api.SendInput.call_count, 2)
        for call in api.SendInput.call_args_list:
            mouse = call.args[1]._obj.payload.mouse
            self.assertEqual(mouse.dx, (720 * 65535) // 3839)
            self.assertEqual(mouse.dy, (600 * 65535) // 1279)
            self.assertEqual(mouse.flags & 0xC001, 0xC001)

    def test_invalid_virtual_desktop_or_outside_target_does_not_move_or_press(self):
        for bounds, point in [((0, 0, 0, 1080), (10, 10)),
                              ((0, 0, 1920, 0), (10, 10)),
                              ((0, 0, 1920, 1080), (-1, 10)),
                              ((0, 0, 1920, 1080), (1920, 10)),
                              ((0, 0, 1920, 1080), (10, 1080))]:
            with self.subTest(bounds=bounds, point=point):
                api = fake_user32()
                api.GetSystemMetrics.side_effect = lambda index: dict(zip((76, 77, 78, 79), bounds))[index]
                with self.assertRaises(RuntimeError):
                    _send_input_click(api, *point, lambda seconds: None)
                api.SetCursorPos.assert_not_called()
                api.SendInput.assert_not_called()


if __name__ == '__main__':
    unittest.main()
