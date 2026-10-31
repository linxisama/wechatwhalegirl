# -*- coding: utf-8 -*-
"""「坐标+OCR」发送通道验收测试（微信 4.1.x 没有 mmui:: 控件时的唯一例外通道）。

全部用假 guia 实例 + mock：不真机发送、不点击真实微信窗口。
"""
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from src.wechat_guia_send import (DEFAULT_TIMEOUT, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_LEFTDOWN,
                                  MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MOVE, MOUSEEVENTF_VIRTUALDESK,
                                  TITLE_LINE_TOLERANCE, GuiaSendResult,
                                  GuiaChannel, REASON_TEXT, probe, send_text_via_guia)


class FakeGUI:
    """假 guia 实例：只记录调用顺序，绝不碰真窗口。"""

    def __init__(self, title='测试群', alive=True, hwnd=4242, input_ok=True, send_ok=True,
                 rows=None, front_ok=True, pid=42164):
        self.main_hwnd = hwnd
        self.pid = pid
        self.render_w, self.render_h, self.right_pane_left = 1200, 900, 300
        self.origin_x, self.origin_y = 100, 50
        self.send_button_region = (1000, 850, 1200, 900)
        self.pasted = False
        self.draft_body = ''
        self.sent = False
        self.title = title
        # rows 给定时按 (text, x, y, w, h) 原样返回，用来复现真机"标题被切成多段"的情形
        self.rows = rows
        # send_rows 给定时按 (label, x, y, w, h) 原样返回，用来复现发送键结构闸门的各种结论。
        self.send_rows = None
        self.clicks = []            # 所有点击落点（取焦点 + 发送键）
        self.wx_clicks = []         # 走 gui.wx_click（mouse_event）的点击
        self.send_clicks = []       # 走 SendInput 绝对坐标的发送键点击
        self.focus_clicks = []      # 走 SendInput 的输入框聚焦点击，与发送计数分开
        self.mouse_flags = []       # SendInput 鼠标事件的 flags 序列
        self.ocr_regions = []
        self.refresh_region = None
        self.front_ok = front_ok
        self.alive = alive
        self.input_ok = input_ok
        self.send_ok = send_ok
        self.calls = []
        self._uia = None
        self._uia_tried = False

    def _get_uia(self, refresh=False):
        raise AssertionError('热激活被禁止：通道必须全程走 OCR')

    def is_alive(self):
        return self.alive

    def bring_to_front(self, keep_topmost=False):
        self.calls.append('front')
        if self.front_ok:
            self.user32.GetForegroundWindow.return_value = self.main_hwnd
        return self.front_ok

    def restore_zorder(self):
        self.calls.append('restore_zorder')

    def ocr_zoomed(self, box, scale=3):
        self.calls.append('ocr')
        if self.rows is not None:
            return list(self.rows)
        # x 必须落在右侧面板内（>= right_pane_left=300），否则会被标题行过滤器当成侧栏
        return [(self.title, 320, 10, 80, 20)] if self.title else []

    def get_input_box(self):
        self.calls.append('box')
        return (300, 680, 1200, 850)

    def _update_render_rect(self):
        pass

    def refresh_composer(self):
        # 粘贴后必须重做一次像素核验：真实驱动的 send_region 会在这里重新测量。
        self.calls.append('recheck')
        if self.refresh_region is not None:
            self.send_button_region = tuple(self.refresh_region)
        return self.get_input_box()

    def ocr(self, region):
        self.calls.append('send_ocr')
        self.ocr_regions.append(tuple(region))
        if self.send_rows is not None:
            return list(self.send_rows)
        return [('发送', 1050, 865, 60, 20)]

    def wx_click(self, x, y):
        self.clicks.append((x, y))
        self.wx_clicks.append((x, y))
        if y >= self.origin_y + 850:
            self.calls.append('click_send')
            self.sent = True
        else:
            self.calls.append('focus')

    def mouse_click(self, x, y):
        """SendInput 点击分别模拟输入框聚焦和发送，不把聚焦算成发送。"""
        self.clicks.append((x, y))
        if y >= self.origin_y + 850:
            self.send_clicks.append((x, y))
            self.calls.append('click_send')
            self.sent = True
        else:
            self.focus_clicks.append((x, y))
            self.calls.append('focus')

    def _input_box_has_text(self, box=None):
        if self.sent:
            return not self.send_ok
        return self.pasted and self.input_ok

    def composer_ink(self, box):
        """按键前的墨迹量：假驱动只区分"粘贴过正文"和"空框"两种事实。"""
        if not self.pasted:
            return {'pixels': 0, 'columns': 0, 'state': 'empty'}
        return {'pixels': 400, 'columns': 12, 'state': 'text'}

    def composer_cleared(self, box, before, window=3.0):
        """委派给 _input_box_has_text 的反面，既有用例靠 override 它制造窗口状态变化。"""
        return not self._input_box_has_text(box)

    def focus_input(self, *args, **kwargs):
        raise AssertionError('禁止调用会绕过落点检查的 focus_input')

    def input_text(self, *args, **kwargs):
        raise AssertionError('禁止调用可能向别的窗口粘贴的 input_text')

    def click_send(self, *args, **kwargs):
        raise AssertionError('禁止调用内部会重试发送的 click_send')


class FakeClipboard:
    def __init__(self, gui=None):
        self.value = '原剪贴板'
        self.gui = gui
        self.writes = []
        self.snapshots = 0

    def snapshot(self):
        self.snapshots += 1
        return self.value

    def copy(self, text):
        self.writes.append(text)
        self.value = text
        if self.gui:
            self.gui.calls.append('clipboard_write')

    def restore(self, snapshot):
        self.writes.append(snapshot)
        self.value = snapshot
        if self.gui:
            self.gui.calls.append('clipboard_restore')

    def text(self):
        return self.value


def fake_user32(fg=0):
    """所有窗口/键盘 API 都是假的；进程归属与 Ctrl+V 可独立观察。"""
    user32 = Mock()
    user32.GetForegroundWindow.return_value = fg
    user32.IsIconic.return_value = 0
    user32.WindowFromPoint.return_value = 4242
    user32.GetAncestor.side_effect = lambda hwnd, flags: 4242 if hwnd.value in (4242, 777) else hwnd.value
    user32.GetClassNameW.side_effect = lambda hwnd, buf, size: setattr(buf, 'value', 'FakeWindow')
    def owner(hwnd, pointer):
        pointer._obj.value = 42164 if hwnd.value in (4242, 777) else 9492
        return 1
    user32.GetWindowThreadProcessId.side_effect = owner
    user32.GetSystemMetrics.side_effect = lambda index: {
        0: 1920, 1: 1080, 76: 0, 77: 0, 78: 1920, 79: 1080}[index]
    user32.SetCursorPos.return_value = 1
    user32.SendInput.return_value = 4
    return user32


def channel_for(gui, timeout=DEFAULT_TIMEOUT, clock=None, activate=None,
                user32=None, clipboard=None, sleep=None):
    user32 = user32 if user32 is not None else fake_user32()
    gui.user32 = user32
    clipboard = clipboard if clipboard is not None else FakeClipboard(gui)
    def paste(n, events, size):
        if n == 1:                      # SendInput 鼠标点击：发送键的唯一投递方式
            gui.mouse_flags.append(events._obj.payload.mouse.flags)
            if events._obj.payload.mouse.flags & MOUSEEVENTF_LEFTUP:
                gui.mouse_click(*user32.SetCursorPos.call_args.args)
            return 1
        key = events[1].payload.keyboard.vk
        if key == 0x56:
            gui.calls.append('input')
            gui.pasted = True
            gui.draft_body = clipboard.value
        elif key == 0x43:
            clipboard.value = gui.draft_body
            gui.calls.append('copy_readback')
        elif key == 0x41:
            gui.calls.append('select_readback')
        return 4
    user32.SendInput.side_effect = paste
    def activation(g):
        ok = activate(g) if activate else False
        if ok:
            user32.GetForegroundWindow.return_value = gui.main_hwnd
        return ok
    return GuiaChannel(gui_factory=lambda: gui, timeout=timeout, clock=clock,
                       sleep=sleep if sleep is not None else lambda _s: None,
                       activate=activation, user32=user32, clipboard=clipboard, send_gate=lambda: None)


class GuiaAvailabilityTest(unittest.TestCase):
    def test_factory_failure_becomes_chinese_reason(self):
        def broken():
            raise RuntimeError('未找到微信主窗口，请确认微信已登录并运行')
        result = GuiaChannel(gui_factory=broken).available()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'guia_unavailable')
        self.assertIn('「坐标+OCR」发送通道不可用', result.reason)
        self.assertIn('未找到微信主窗口', result.reason)
        self.assertEqual(result.diagnostic['waiting_code'], 'guia_unavailable')

    def test_closed_wechat_is_unavailable(self):
        result = channel_for(FakeGUI(alive=False)).available()
        self.assertEqual(result.reason_code, 'guia_unavailable')
        self.assertIn('微信窗口已关闭', result.reason)

    def test_minimized_reports_code_and_never_clicks(self):
        gui = FakeGUI(hwnd=1234)
        with patch('src.wechat_guia_send._iconic', return_value=True):
            result = channel_for(gui).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'minimized')
        self.assertIn('最小化', result.reason)
        self.assertEqual(gui.calls, [])

    def test_probe_never_raises(self):
        def broken():
            raise OSError('driver missing')
        result = probe(gui_factory=broken)
        self.assertFalse(result.ok)
        self.assertIn('「坐标+OCR」发送通道不可用', result.reason)

    def test_hot_activate_is_disabled_on_the_real_driver_surface(self):
        gui = FakeGUI()
        channel_for(gui)._build()
        self.assertIsNone(gui._get_uia())
        self.assertIsNone(gui._uia)
        self.assertTrue(gui._uia_tried)


class GuiaSendGuardTest(unittest.TestCase):
    def test_other_chat_is_never_typed(self):
        gui = FakeGUI(title='别人的群')
        result = channel_for(gui).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'target_not_open')
        self.assertIn('别人的群', result.reason)
        # 1.3.61 起：标题是别的会话时会先用 Ctrl+F 搜索打开目标会话，粘的是会话名。
        # 这里守的仍是红线本身——正文一个字都没进输入框、发送键一次都没按。
        self.assertNotIn('正文', gui.draft_body)
        self.assertNotIn('click_send', gui.calls)

    def test_unreadable_title_fails_closed(self):
        gui = FakeGUI(title='')
        result = channel_for(gui).send('测试群', '正文')
        self.assertEqual(result.reason_code, 'title_unverified')
        self.assertNotIn('input', gui.calls)

    def test_truncated_title_is_rejected(self):
        gui = FakeGUI(title='件传输助')
        result = channel_for(gui).send('文件传输助手', '正文')
        self.assertFalse(result.ok, result.reason)
        # 1.3.61 起：标题是别的会话时会先用 Ctrl+F 搜索打开目标会话，粘的是会话名。
        # 这里守的仍是红线本身——正文一个字都没进输入框、发送键一次都没按。
        self.assertNotIn('正文', gui.draft_body)
        self.assertNotIn('click_send', gui.calls)

    def test_chat_switch_after_typing_blocks_enter(self):
        class Switching(FakeGUI):
            def _input_box_has_text(self, box=None):
                if self.pasted:
                    self.title = '别人的群'
                return super()._input_box_has_text(box)
        gui = Switching()
        result = channel_for(gui).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'target_not_open')
        self.assertIn('草稿', result.reason)
        self.assertNotIn('click_send', gui.calls)

    def test_empty_body_or_bad_target_touches_nothing(self):
        gui = FakeGUI()
        channel = channel_for(gui)
        self.assertEqual(channel.send('', '正文').reason_code, 'invalid_target')
        self.assertEqual(channel.send('测试\n群', '正文').reason_code, 'invalid_target')
        self.assertEqual(channel.send('测试群', '').reason_code, 'invalid_target')
        self.assertEqual(gui.calls, [])

    def test_timeout_before_enter_does_not_send(self):
        gui = FakeGUI()
        clock = iter([0.0, 100.0])
        result = channel_for(gui, timeout=18.0, clock=lambda: next(clock, 100.0)).send('测试群', '正文')
        self.assertEqual(result.reason_code, 'send_failed')
        self.assertIn('超过发送时限', result.reason)
        self.assertNotIn('click_send', gui.calls)
        self.assertIn('restore_zorder', gui.calls)

    def test_success_is_confirmed_by_empty_input_box(self):
        gui = FakeGUI()
        result = channel_for(gui).send('测试群', '正文')
        self.assertTrue(result.ok)
        self.assertIn('已通过「坐标+OCR」通道发送到「测试群」', result.reason)
        self.assertEqual(gui.calls.count('click_send'), 1)
        self.assertEqual(gui.calls.count('front'), 1)

    def test_unconfirmed_send_is_never_retried(self):
        gui = FakeGUI(send_ok=False)
        result = channel_for(gui).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'send_failed')
        self.assertEqual(gui.calls.count('click_send'), 1)
        self.assertNotIn('input', gui.calls[gui.calls.index('click_send'):])
        self.assertTrue(result.diagnostic['send_uncertain'])
        self.assertIn('本次不重发', result.reason)

    def test_paste_failure_does_not_press_enter(self):
        gui = FakeGUI(input_ok=False)
        result = channel_for(gui).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertIn('正文没有粘进输入框', result.reason)
        self.assertNotIn('click_send', gui.calls)

    def test_driver_exception_is_reported_as_uncertain(self):
        class Exploding(FakeGUI):
            def mouse_click(self, x, y):
                # 发送键由通道自己用 SendInput 投递，异常就出在这一击上。
                if y >= self.origin_y + 850:
                    raise RuntimeError('uia broke')
                return super().mouse_click(x, y)
        result = channel_for(Exploding()).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertIn('结果不确定，本次不重发', result.reason)
        self.assertTrue(result.diagnostic['send_uncertain'])

    def test_unavailable_driver_returns_reason_text_directly(self):
        def broken():
            raise RuntimeError('未找到微信主窗口，请确认微信已登录并运行')
        result = send_text_via_guia('测试群', '正文', gui_factory=broken)
        self.assertEqual(result.reason_code, 'guia_unavailable')
        self.assertIn(REASON_TEXT['guia_unavailable'], result.reason)


class SendButtonGateTest(unittest.TestCase):
    """发送键闸门（1.3.22）：点绿色块自身中心，且必须用粘贴后重新核验的区域。"""

    def test_click_point_is_the_green_block_center(self):
        gui = FakeGUI()
        gui.send_rows = [('发送', 1050, 865, 60, 20)]
        result = channel_for(gui).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        # origin (100,50) + 块自身中心 (1050+30, 865+10)；区域中心是 (1200, 925)。
        self.assertEqual(gui.send_clicks, [(1180, 925)])
        self.assertNotIn((1200, 925), gui.clicks)
        self.assertEqual(gui.wx_clicks, [])
        self.assertEqual(len(gui.focus_clicks), 1)

    def test_button_top_clipped_by_the_region_edge_still_clicks_the_block_center(self):
        gui = FakeGUI()
        # 真机实测：绿色按钮压着工具栏分隔线，顶边比 send_region 上边高 3 行。
        gui.send_rows = [('发送', 1050, 847, 60, 24)]
        result = channel_for(gui).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(gui.send_clicks, [(1180, 909)])

    def test_block_beyond_the_scan_margin_is_never_clicked(self):
        gui = FakeGUI()
        # 比 send_region 上边高 7 行，超出允许上探的 6 行：被裁掉的块不能点。
        gui.send_rows = [('发送', 1050, 843, 60, 24)]
        result = channel_for(gui).send('测试群', '正文')
        self.assertFalse(result.ok, result.reason)
        self.assertIn('未唯一定位到发送按钮', result.reason)
        self.assertNotIn('click_send', gui.calls)
        self.assertEqual(gui.send_clicks, [])

    def test_zero_or_two_candidates_are_never_clicked(self):
        for rows in ([], [('发送', 1050, 865, 60, 20), ('发送', 1060, 850, 60, 20)]):
            gui = FakeGUI()
            gui.send_rows = rows
            result = channel_for(gui).send('测试群', '正文')
            self.assertFalse(result.ok, result.reason)
            self.assertIn('未唯一定位到发送按钮', result.reason)
            self.assertNotIn('click_send', gui.calls)
            self.assertEqual(gui.send_clicks, [])
            self.assertEqual(len(gui.clicks), 1)  # 只剩取焦点的那次输入框点击

    def test_refreshed_region_is_used_after_paste(self):
        gui = FakeGUI()
        # 正文变多 → 输入框变高、工具栏下移，粘贴后重新核验到的区域整体下移 10 行。
        gui.refresh_region = (1000, 860, 1200, 910)
        gui.send_rows = [('发送', 1050, 875, 60, 20)]
        result = channel_for(gui).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(gui.ocr_regions, [(1000, 860, 1200, 910)])
        self.assertEqual(gui.send_clicks, [(1180, 935)])

    def test_send_key_is_one_sendinput_click_at_the_block_center(self):
        """真机实测：wx_click 的 mouse_event 点击微信根本不处理（草稿仍留全选在框里），
        所以发送键必须用 SendInput 绝对坐标点击，且按下/抬起各一次、绝不重试。"""
        gui = FakeGUI()
        user32 = fake_user32()
        result = channel_for(gui, user32=user32).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(gui.mouse_flags,
                         [MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_LEFTDOWN,
                          MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_LEFTUP,
                          MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_LEFTDOWN,
                          MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_LEFTUP])
        self.assertEqual(user32.SetCursorPos.call_args_list, [call(850, 815), call(1180, 925)])
        self.assertNotIn((1180, 925), gui.wx_clicks)
        self.assertEqual(gui.calls.count('click_send'), 1)
        # 键盘批次（Ctrl+V/Ctrl+A/Ctrl+C）之后才是发送键的两次鼠标事件
        self.assertEqual([c.args[0] for c in user32.SendInput.call_args_list][-2:], [1, 1])

    def test_rejected_click_is_reported_uncertain_and_never_retried(self):
        """系统拒收鼠标事件（SendInput 返回 0）时只能报不确定，绝不补一次点击。"""
        gui = FakeGUI()
        user32 = fake_user32()

        channel = channel_for(gui, user32=user32)
        deliver = user32.SendInput.side_effect

        def partial(n, events, size):
            if n == 1 and gui.pasted:
                flags = events._obj.payload.mouse.flags
                gui.mouse_flags.append(flags)
                if flags & MOUSEEVENTF_LEFTDOWN:
                    return 0        # 系统拒收这次按下：通道只能报不确定
            return deliver(n, events, size)

        user32.SendInput.side_effect = partial
        result = channel.send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertIn('结果不确定', result.reason)
        self.assertEqual(gui.send_clicks, [])
        self.assertEqual([c.args[0] for c in user32.SendInput.call_args_list][-1:], [1])
        self.assertEqual(gui.mouse_flags,
                         [MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_LEFTDOWN,
                          MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_LEFTUP,
                          MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_LEFTDOWN])
        self.assertEqual(user32.mouse_event.call_args_list, [call(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)])

    def test_composer_is_reverified_after_paste_before_locating_the_button(self):
        gui = FakeGUI()
        result = channel_for(gui).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        calls = gui.calls
        self.assertEqual(calls.count('recheck'), 1)
        self.assertLess(calls.index('clipboard_write'), calls.index('recheck'))
        self.assertLess(calls.index('recheck'), calls.index('send_ocr'))


class GuiaWorkerFallbackTest(unittest.TestCase):
    """send_worker：UIA 可用不换道、控件缺失才降级、按过回车绝不换道。"""

    def job(self, **extra):
        job = {'action': 'text', 'who': '测试群', 'text': '正文', 'guia_send': True,
               'send_compat': False, 'auto_open_chat': False}
        job.update(extra)
        return job

    def run_execute(self, job, send_text, guia=None):
        from src.wechat_send_worker import execute
        phases = []
        with patch('src.interactive_desktop.interactive_desktop_status', return_value=(True, '')), \
             patch('wechatauto.uia_driver.WeChatUIA', return_value=Mock()), \
             patch('src.wechat_window.install_window_discovery'), \
             patch('src.wechat_send_worker._foreground_window', return_value=11), \
             patch('src.wechat_send_worker._restore_foreground'), \
             patch('src.wechat_send_worker.send_text', side_effect=send_text), \
             patch('src.wechat_send_worker.send_text_via_guia',
                   side_effect=guia or AssertionError('不得降级')):
            return execute(job, phases.append), phases

    def test_uia_success_keeps_original_channel(self):
        result, phases = self.run_execute(self.job(), lambda *a, **k: (True, 'foreground'))
        self.assertTrue(result[0])
        self.assertIn('desktop', phases)

    def test_missing_input_control_degrades_to_guia(self):
        fallback = GuiaSendResult(True, '', '已通过「坐标+OCR」通道发送到「测试群」',
                                  {'guia_channel': True, 'ready': True, 'waiting_code': ''})
        result, _ = self.run_execute(self.job(),
                                     lambda *a, **k: (False, '找到了会话，但没有可用的聊天输入框'),
                                     guia=Mock(return_value=fallback))
        self.assertTrue(result[0], result[1])
        self.assertIn('已切换坐标+OCR', result[1])

    def test_enter_already_pressed_never_switches_channel(self):
        def uia(driver, who, text, phase, open_chat=False):
            phase('send_key')
            return False, '已按发送键但输入框未确认清空，结果待核实'
        result, phases = self.run_execute(self.job(), uia)
        self.assertFalse(result[0])
        self.assertIn('send_key', phases)

    def test_guia_disabled_job_never_touches_the_channel(self):
        result, _ = self.run_execute(self.job(guia_send=None),
                                     lambda *a, **k: (False, '找到了会话，但没有可用的聊天输入框'))
        self.assertFalse(result[0])
        self.assertIn('没有可用的聊天输入框', result[1])

    def test_uncertain_guia_failure_requests_reconnect(self):
        fallback = GuiaSendResult(False, 'send_failed', '已尝试发送但输入框未确认清空，结果不确定，本次不重发',
                                  {'ready': False, 'waiting_code': 'send_failed', 'send_uncertain': True})
        job = self.job()
        result, _ = self.run_execute(job, lambda *a, **k: (False, '没有输入控件'),
                                     guia=Mock(return_value=fallback))
        self.assertFalse(result[0])
        self.assertTrue(job['_send_uncertain'])

    def test_window_check_reports_guia_ready(self):
        from src.wechat_send_worker import execute
        job = {'action': 'window-check', 'guia_send': True}
        diagnostic = {'ready': False, 'waiting_code': 'input_unavailable',
                      'reason': '微信 4.x 默认不生成控件树（只给窗口外壳）'}
        with patch('src.wechat_window.inspect_window', return_value=dict(diagnostic)), \
             patch('src.wechat_send_worker.guia_probe',
                   return_value=GuiaSendResult(True, '', '「坐标+OCR」发送通道可用',
                                               {'guia_channel': True, 'ready': True})) as probe_:
            ok, reason = execute(job)
        self.assertTrue(ok)
        probe_.assert_called_once()
        self.assertTrue(job['_diagnostic']['guia_channel'])
        self.assertEqual(job['_diagnostic']['waiting_code'], '')
        self.assertIn('坐标+OCR', reason)

    def test_window_check_keeps_old_reason_when_guia_unavailable(self):
        from src.wechat_send_worker import execute
        job = {
            'action': 'window-check', 'guia_send': True}
        diagnostic = {'ready': False, 'waiting_code': 'input_unavailable', 'reason': '缺少聊天输入控件'}
        failed = GuiaSendResult(False, 'guia_unavailable', '「坐标+OCR」发送通道不可用：没有 OCR',
                                {'waiting_code': 'guia_unavailable'})
        with patch('src.wechat_window.inspect_window', return_value=dict(diagnostic)), \
             patch('src.wechat_send_worker.guia_probe', return_value=failed):
            ok, reason = execute(job)
        self.assertFalse(ok)
        self.assertIn('缺少聊天输入控件', reason)
        self.assertIn('「坐标+OCR」发送通道不可用', reason)

    def test_window_check_surfaces_guia_failure_code_without_logging_send_failure(self):
        from src.wechat_send_worker import execute
        for code in ('chat_not_open', 'target_not_open', 'blocked_by_window'):
            with self.subTest(code=code):
                job = {'action':'window-check', 'guia_send':True,
                       'account_path':'guest-account', 'who':'目标会话'}
                native = {'ready':False, 'waiting_code':'accessibility_unavailable',
                          'reason':'仅暴露窗口外壳', 'hwnd':123, 'pid':456}
                failed = GuiaSendResult(False, code, '当前通道检查未通过',
                                        {'ready':False, 'waiting_code':code})
                with patch('src.wechat_window.inspect_window', return_value=native), \
                     patch('src.wechat_send_worker.guia_probe', return_value=failed) as probe_, \
                     patch('src.wechat_send_worker.log_send_failure') as failure:
                    ok, _ = execute(job)
                self.assertFalse(ok)
                self.assertFalse(job['_diagnostic']['ready'])
                self.assertEqual(job['_diagnostic']['waiting_code'],code)
                self.assertEqual(job['_diagnostic']['native_waiting_code'],'accessibility_unavailable')
                self.assertEqual(job['_diagnostic']['hwnd'],123)
                self.assertEqual(job['_diagnostic']['pid'],456)
                probe_.assert_called_once_with(account_path='guest-account',who='目标会话')
                failure.assert_not_called()

    def test_window_check_without_guia_flag_does_not_probe(self):
        from src.wechat_send_worker import execute
        diagnostic = {'ready': False, 'waiting_code': 'input_unavailable', 'reason': '缺少聊天输入控件'}
        with patch('src.wechat_window.inspect_window', return_value=dict(diagnostic)), \
             patch('src.wechat_send_worker.guia_probe',
                   side_effect=AssertionError('老任务不得探测 OCR 通道')):
            self.assertEqual(execute({'action': 'window-check'}), (False, '缺少聊天输入控件'))

    def test_sender_enables_guia_for_its_own_jobs(self):
        from src.wechat_sender import IsolatedWeChatSender
        self.assertTrue(IsolatedWeChatSender(Path('.'))._prepare_job({'action': 'text'})['guia_send'])


class GuiaTitleLineTest(unittest.TestCase):
    """标题行拼装：真机（微信 4.1.15.13 / DPI125% / right_pane_left=278）的 OCR 实况。"""

    def channel(self, gui):
        return channel_for(gui)

    def test_split_title_fragments_are_joined_in_x_order(self):
        # 真机实测：'O' 是标题旁的图标（x=331 y=59），'西莲寺紫' 紧跟着在 x=395 y=60；
        # 下面两条是正文/其它元素，必须被基线容差挡在外面。
        rows = [
            ('O', 331, 59, 21, 21),
            ('西莲寺紫', 395, 60, 17, 16),
            ('0', 1191, 106, 37, 34),
            ('头顶打个大占看，这是被讠隹扣的？', 874, 112, 17, 17),
        ]
        self.assertEqual(self.channel(FakeGUI(rows=rows)).current_chat(), 'O西莲寺紫')

    def test_fragments_are_ordered_by_x_not_by_y(self):
        # y 顺序与 x 顺序故意相反：拼接必须按 x 升序，否则标题会变成乱序字符串
        rows = [('莲寺', 420, 61, 17, 16), ('西', 350, 58, 17, 16)]
        self.assertEqual(self.channel(FakeGUI(rows=rows)).current_chat(), '西莲寺')

    def test_sidebar_rows_never_join_the_title(self):
        # x=218 是左侧会话列表（right_pane_left=278 之外），同一条 y 上也不能参与
        rows = [('社招新群', 218, 60, 17, 17), ('测试群', 330, 60, 17, 17)]
        self.assertEqual(self.channel(FakeGUI(rows=rows)).current_chat(), '测试群')

    def test_no_candidate_rows_returns_empty(self):
        self.assertEqual(self.channel(FakeGUI(rows=[])).current_chat(), '')
        only_sidebar = [('社招新群', 218, 60, 17, 17), ('   ', 400, 61, 10, 10)]
        self.assertEqual(self.channel(FakeGUI(rows=only_sidebar)).current_chat(), '')

    def test_blank_fragments_are_dropped_before_joining(self):
        rows = [('  ', 320, 59, 8, 8), ('测试群', 331, 60, 60, 18)]
        self.assertEqual(self.channel(FakeGUI(rows=rows)).current_chat(), '测试群')

    def test_extra_title_prefix_is_not_assumed_to_be_an_icon(self):
        # OCR 无法证明 O 是图标；不能把另一个完整名称当成当前收件人。
        rows = [('O', 331, 59, 21, 21), ('西莲寺紫', 395, 60, 17, 16)]
        gui = FakeGUI(rows=rows)
        result = self.channel(gui).send('西莲寺紫', '正文')
        self.assertFalse(result.ok, result.reason)
        # 1.3.61 起：标题是别的会话时会先用 Ctrl+F 搜索打开目标会话，粘的是会话名。
        # 这里守的仍是红线本身——正文一个字都没进输入框、发送键一次都没按。
        self.assertNotIn('正文', gui.draft_body)
        self.assertNotIn('click_send', gui.calls)

    def test_tolerance_is_a_module_constant(self):
        self.assertEqual(TITLE_LINE_TOLERANCE, 12)


class GuiaForegroundTest(unittest.TestCase):
    """抢前台：ALT 轻敲优先，失败才回退 gui.bring_to_front，两条都失败才报不可用。"""

    def test_alt_tap_success_skips_bring_to_front(self):
        gui = FakeGUI(hwnd=4242)
        tapped = []
        result = channel_for(gui, activate=lambda g: tapped.append(g) or True).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(tapped, [gui])
        self.assertNotIn('front', gui.calls)

    def test_alt_tap_failure_falls_back_to_bring_to_front(self):
        gui = FakeGUI(hwnd=4242)
        result = channel_for(gui, activate=lambda g: False).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(gui.calls.count('front'), 1)

    def test_alt_tap_exception_falls_back_to_bring_to_front(self):
        def boom(gui):
            raise RuntimeError('ctypes 不可用')
        gui = FakeGUI(hwnd=4242)
        result = channel_for(gui, activate=boom).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(gui.calls.count('front'), 1)

    def test_both_paths_fail_reports_unavailable(self):
        gui = FakeGUI(hwnd=4242, front_ok=False)
        user32 = fake_user32(999)
        with patch('ctypes.windll', Mock(user32=user32)), \
                patch('src.wechat_guia_send.foreground_owner', return_value=('', 0)):
            result = channel_for(gui, activate=lambda g: False, user32=user32).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'guia_unavailable')
        self.assertIn('无法把微信窗口置前', result.reason)
        self.assertIn('restore_zorder', gui.calls)
        self.assertNotIn('input', gui.calls)
        self.assertNotIn('click_send', gui.calls)

    def test_already_foreground_skips_tap_and_bring_to_front(self):
        # 微信本来就在前台：ALT 轻敲与 bring_to_front 一次都不该发生
        gui = FakeGUI(hwnd=4242)
        tapped = []
        user32 = fake_user32(4242)
        with patch('ctypes.windll', Mock(user32=user32)):
            result = channel_for(gui, activate=lambda g: tapped.append(g) or True, user32=user32).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(tapped, [])
        self.assertNotIn('front', gui.calls)

    def test_already_foreground_by_render_child_of_bound_main(self):
        # 渲染子窗句柄不同，但根窗口仍是绑定主窗，同样不敲 ALT。
        gui = FakeGUI(hwnd=4242, pid=42164)
        tapped = []
        user32 = fake_user32(777)
        with patch('ctypes.windll', Mock(user32=user32)), \
                patch('src.wechat_guia_send._foreground_pid', return_value=42164):
            result = channel_for(gui, activate=lambda g: tapped.append(g) or True, user32=user32).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(tapped, [])
        self.assertNotIn('front', gui.calls)

    def test_foreground_failure_names_the_holder(self):
        # 抢不到前台时必须说清是谁挡着，并把 foreground=<class>:<pid> 写进失败日志
        gui = FakeGUI(hwnd=4242, front_ok=False)
        user32 = fake_user32(999)
        logged = []
        with patch('ctypes.windll', Mock(user32=user32)), \
                patch('src.wechat_guia_send._foreground_pid', return_value=9492), \
                patch('src.wechat_guia_send.foreground_owner',
                      return_value=('metasequoiaime_windows', 9492)), \
                patch('src.wechat_guia_send.log_send_failure',
                      side_effect=lambda *a, **kw: logged.append(kw)):
            result = channel_for(gui, activate=lambda g: False, user32=user32).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'guia_unavailable')
        self.assertIn('当前前台是 metasequoiaime_windows', result.reason)
        self.assertEqual(result.diagnostic['foreground'], 'metasequoiaime_windows:9492')
        self.assertEqual(logged[-1]['foreground'], 'metasequoiaime_windows:9492')
        self.assertNotIn('input', gui.calls)
        self.assertNotIn('click_send', gui.calls)

    def test_foreground_owner_reads_class_and_pid(self):
        from src.wechat_guia_send import foreground_owner

        def fake_class(hwnd, buf, size):
            buf.value = 'metasequoiaime_windows'

        user32 = Mock()
        user32.GetForegroundWindow.return_value = 123
        user32.GetClassNameW.side_effect = fake_class
        with patch('ctypes.windll', Mock(user32=user32)), \
                patch('src.wechat_guia_send._foreground_pid', return_value=9492):
            self.assertEqual(foreground_owner(), ('metasequoiaime_windows', 9492))

    def test_foreground_owner_swallows_errors(self):
        from src.wechat_guia_send import foreground_owner
        user32 = Mock()
        user32.GetForegroundWindow.side_effect = OSError('没有桌面会话')
        with patch('ctypes.windll', Mock(user32=user32)):
            self.assertEqual(foreground_owner(), ('', 0))

    def test_retaps_when_bring_to_front_also_fails(self):
        # 前台锁偶尔要第二次轻敲才松开：老路径失败后必须再敲一次，而不是直接报不可用
        gui = FakeGUI(hwnd=4242, front_ok=False)
        answers = [False, True]
        result = channel_for(gui, activate=lambda g: answers.pop(0)).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(gui.calls.count('front'), 1)

    def test_no_hwnd_skips_ctypes_tap(self):
        from src.wechat_guia_send import _alt_tap_activate
        self.assertFalse(_alt_tap_activate(0))

    def test_tap_polls_until_foreground_switches(self):
        # 真机实测：SetForegroundWindow 返回 1，但 GetForegroundWindow 要过一会儿才变；
        # 所以复查必须轮询，查一次就判失败会把已成功的抢前台当成失败。
        from src.wechat_guia_send import _alt_tap_activate
        values = iter([11, 11, 4242])
        user32 = Mock()
        user32.GetForegroundWindow.side_effect = lambda: next(values)
        waits = []
        with patch('ctypes.windll', Mock(user32=user32)):
            self.assertTrue(_alt_tap_activate(4242, sleep=waits.append))
        self.assertEqual(user32.keybd_event.call_args_list,
                         [call(0x12, 0, 0, 0), call(0x12, 0, 2, 0)])
        user32.SetForegroundWindow.assert_called_once_with(4242)
        self.assertGreaterEqual(len(waits), 2)

    def test_foreground_owner_pid_counts_as_success(self):
        # 真机实测：前台可能是微信的另一个顶层窗口，句柄不等于 main_hwnd，但微信在最前
        from src.wechat_guia_send import _alt_tap_activate
        user32 = Mock()
        user32.GetForegroundWindow.return_value = 999
        with patch('ctypes.windll', Mock(user32=user32)), \
                patch('src.wechat_guia_send._foreground_pid', return_value=42164):
            self.assertTrue(_alt_tap_activate(4242, sleep=lambda _s: None, owner_pid=42164))

    def test_foreground_pid_mismatch_is_not_success(self):
        from src.wechat_guia_send import _alt_tap_activate
        user32 = Mock()
        user32.GetForegroundWindow.return_value = 999
        with patch('ctypes.windll', Mock(user32=user32)), \
                patch('src.wechat_guia_send._foreground_pid', return_value=9492):
            self.assertFalse(_alt_tap_activate(4242, sleep=lambda _s: None,
                                               owner_pid=42164, tries=2))

    def test_tap_gives_up_after_bounded_polls(self):
        from src.wechat_guia_send import _alt_tap_activate
        user32 = Mock()
        user32.GetForegroundWindow.return_value = 11
        waits = []
        with patch('ctypes.windll', Mock(user32=user32)):
            self.assertFalse(_alt_tap_activate(4242, sleep=waits.append, tries=3))
        self.assertEqual(len(waits), 1 + 3)   # 先等 150ms，再复查 3 次


class SendChannelHintTest(unittest.TestCase):
    def test_new_reason_codes_have_actionable_hints(self):
        from src.wechat_app import send_channel_hint
        self.assertIn('程序不会替你切会话', send_channel_hint({'waiting_code': 'target_not_open',
                                                            'reason': '当前会话不是目标会话'}))
        self.assertIn('winsdk', send_channel_hint({'waiting_code': 'guia_unavailable',
                                                  'reason': REASON_TEXT['guia_unavailable']}))


class ClipboardSnapshotTest(unittest.TestCase):
    """不读写系统剪贴板，验证原始内容在覆盖前已经完整物化。"""

    def test_snapshot_materializes_unicode_and_custom_bytes_before_copy(self):
        from src.wechat_guia_send import _Clipboard
        clip = Mock()
        clip.EnumClipboardFormats.side_effect = [13, 50000, 0]
        clip.GetClipboardData.side_effect = ['原文本', b'original-rich-format']
        with patch.dict('sys.modules', {'win32clipboard': clip}):
            saved = _Clipboard().snapshot()
        self.assertEqual(saved, [(13, '原文本'), (50000, b'original-rich-format')])
        clip.EmptyClipboard.assert_not_called()
        clip.SetClipboardData.assert_not_called()
        clip.CloseClipboard.assert_called_once()

    def test_unrestorable_handle_is_skipped_before_emptying_clipboard(self):
        # 1.3.58：句柄类格式（CF_BITMAP=2）跨进程存不住，只记名跳过；快照阶段绝不碰剪贴板。
        from src.wechat_guia_send import _Clipboard
        clip = Mock()
        clip.EnumClipboardFormats.return_value = 2
        clip.GetClipboardData.return_value = 123456
        with patch.dict('sys.modules', {'win32clipboard': clip}):
            channel_clip = _Clipboard()
            saved = channel_clip.snapshot()
        self.assertEqual(saved, [])
        self.assertEqual(channel_clip.skipped, (2,))
        self.assertTrue(channel_clip.partial)
        clip.GetClipboardData.assert_not_called()   # 句柄格式根本不去取
        clip.EmptyClipboard.assert_not_called()
        clip.SetClipboardData.assert_not_called()
        clip.CloseClipboard.assert_called_once()

    def test_restore_writes_every_original_format(self):
        from src.wechat_guia_send import _Clipboard
        clip = Mock()
        with patch.dict('sys.modules', {'win32clipboard': clip}):
            _Clipboard().restore([(13, '原文本'), (50000, b'original-rich-format')])
        clip.EmptyClipboard.assert_called_once()
        self.assertEqual(clip.SetClipboardData.call_args_list,
                         [call(13, '原文本'), call(50000, b'original-rich-format')])
        clip.CloseClipboard.assert_called_once()

    def test_snapshot_read_error_closes_without_mutating_clipboard(self):
        from src.wechat_guia_send import _Clipboard
        clip = Mock()
        clip.EnumClipboardFormats.return_value = 13
        clip.GetClipboardData.side_effect = OSError('clipboard busy')
        with patch.dict('sys.modules', {'win32clipboard': clip}):
            with self.assertRaises(OSError):
                _Clipboard().snapshot()
        clip.EmptyClipboard.assert_not_called()
        clip.CloseClipboard.assert_called_once()

    def test_empty_clipboard_is_restored_empty(self):
        from src.wechat_guia_send import _Clipboard
        clip = Mock()
        clip.EnumClipboardFormats.return_value = 0
        with patch.dict('sys.modules', {'win32clipboard': clip}):
            clipboard = _Clipboard()
            snapshot = clipboard.snapshot()
            clipboard.restore(snapshot)
        self.assertEqual(snapshot, [])
        clip.EmptyClipboard.assert_called_once()
        clip.SetClipboardData.assert_not_called()


class GuiaInputIsolationTest(unittest.TestCase):
    """真正的安全断言落在剪贴板和 SendInput 边界，不能只检查驱动返回了失败。"""

    def test_click_does_not_gain_foreground_never_writes_or_pastes(self):
        gui = FakeGUI()
        user32 = fake_user32(999)
        clipboard = FakeClipboard(gui)
        channel = channel_for(gui, user32=user32, clipboard=clipboard)
        with patch.object(channel, '_foreground', return_value=True):
            result = channel.send('测试群', '绝不能到别的程序')
        self.assertEqual(result.reason_code, 'foreground_lost')
        self.assertIn('本次未输入', result.reason)
        self.assertEqual(gui.calls.count('focus'), 0)
        self.assertEqual(clipboard.writes, [])
        self.assertEqual(clipboard.snapshots, 0)
        user32.SendInput.assert_not_called()
        self.assertNotIn('click_send', gui.calls)
        self.assertEqual(result.diagnostic['foreground_pid'], 9492)

    def test_covered_input_never_clicks_or_writes(self):
        gui = FakeGUI()
        user32 = fake_user32()
        user32.WindowFromPoint.return_value = 999
        clipboard = FakeClipboard(gui)
        result = channel_for(gui, user32=user32, clipboard=clipboard).send('测试群', '正文')
        self.assertEqual(result.reason_code, 'blocked_by_window')
        self.assertNotIn('focus', gui.calls)
        self.assertNotIn('click_send', gui.calls)
        self.assertEqual(clipboard.writes, [])
        user32.SendInput.assert_not_called()
        self.assertEqual(result.diagnostic['point_pid'], 9492)
        self.assertEqual(result.diagnostic['foreground_pid'], 42164)

    def test_unknown_point_owner_also_fails_closed(self):
        gui = FakeGUI()
        user32 = fake_user32()
        user32.WindowFromPoint.return_value = 0
        clipboard = FakeClipboard(gui)
        result = channel_for(gui, user32=user32, clipboard=clipboard).send('测试群', '正文')
        self.assertEqual(result.reason_code, 'blocked_by_window')
        self.assertEqual(clipboard.writes, [])
        self.assertNotIn('focus', gui.calls)
        user32.SendInput.assert_not_called()

    def test_success_order_and_clipboard_restored_before_send(self):
        gui = FakeGUI()
        user32 = fake_user32()
        clipboard = FakeClipboard(gui)
        result = channel_for(gui, user32=user32, clipboard=clipboard).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(clipboard.value, '原剪贴板')
        self.assertEqual(clipboard.writes[:2], ['正文', '原剪贴板'])
        # 不变式①：每一次剪贴板写入都必须被一次恢复收尾（首尾校验）。回读点数量是实现
        # 细节（1.3.42 只有 2 个：_send_gate 等待之后、SendInput 的 before_press 内），
        # 所以只断言「写入/恢复成对」，不把轮数写死。
        self.assertEqual(len(clipboard.writes) % 2, 0, '每次剪贴板写入都必须跟一次恢复')
        self.assertEqual(clipboard.writes[1::2], ['原剪贴板'] * (len(clipboard.writes) // 2))
        self.assertEqual(clipboard.writes[-1], '原剪贴板', '最后一次剪贴板操作必须是恢复')
        self.assertTrue(all(v.startswith('whale-readback-') for v in clipboard.writes[2::2]))
        events = gui.calls
        self.assertLess(events.index('focus'), events.index('clipboard_write'))
        self.assertLess(events.index('clipboard_write'), events.index('input'))
        self.assertLess(events.index('input'), events.index('clipboard_restore'))
        self.assertIn('ocr', events[events.index('input') + 1:events.index('click_send')])
        self.assertLess(events.index('clipboard_restore'), events.index('click_send'))
        self.assertEqual(events.count('click_send'), 1)
        # 不变式②：正文只粘贴一次（vk 0x56 只出现一次）；回读轮数由实际回读点推导
        # （fake 里每次 Ctrl+C 记一次 copy_readback），键序必须是 0x56 + [Ctrl+A, Ctrl+C] * 轮数，
        # 不写死「必须恰好 N 轮」——换个实现加了回读点也不该变红。
        keys = [c.args[1][1].payload.keyboard.vk for c in user32.SendInput.call_args_list
                if c.args[0] == 4]
        rounds = events.count('copy_readback')
        self.assertEqual(keys.count(0x56), 1, '正文只允许粘贴一次')
        self.assertGreaterEqual(rounds, 1, '正文完整性回读至少发生一次')
        self.assertEqual(keys[1:], [0x41, 0x43] * rounds, '每轮回读必须是 Ctrl+A 后 Ctrl+C')
        # 不变式③：所有回读（选中/复制）都必须早于点击发送键——正文完整性核验必须在按下
        # 发送键之前完成，这才是原来那个「回读 3 次」真正想保护的东西。
        readback_at = [i for i, name in enumerate(events)
                       if name in ('select_readback', 'copy_readback')]
        self.assertLess(max(readback_at), events.index('click_send'))
        # 不变式④：任何键盘 SendInput 序列里都不含 Enter(0x0D)：发送只能靠点击发送键。
        self.assertNotIn(0x0D, [e.payload.keyboard.vk for c in user32.SendInput.call_args_list
                                if c.args[0] == 4 for e in c.args[1]])
        count, inputs, size = next(call.args for call in user32.SendInput.call_args_list if call.args[0] == 4)
        self.assertEqual(count, 4)
        self.assertIn(size, (28, 40))
        self.assertEqual([(e.type, e.payload.keyboard.vk, e.payload.keyboard.flags) for e in inputs],
                         [(1, 0x11, 0), (1, 0x56, 0), (1, 0x56, 2), (1, 0x11, 2)])

    def test_locating_input_exhausts_budget_without_click_or_clipboard(self):
        now = [0.0]
        class SlowGUI(FakeGUI):
            def get_input_box(self):
                now[0] = 19.0
                return super().get_input_box()
        gui = SlowGUI()
        user32 = fake_user32()
        clipboard = FakeClipboard(gui)
        result = channel_for(gui, clock=lambda: now[0], user32=user32,
                             clipboard=clipboard).send('测试群', '正文')
        self.assertEqual(result.reason_code, 'send_failed')
        self.assertIn('超过发送时限', result.reason)
        self.assertNotIn('focus', gui.calls)
        self.assertEqual(clipboard.writes, [])
        user32.SendInput.assert_not_called()

    def test_hung_locator_returns_at_deadline_and_late_result_never_types(self):
        import threading
        import time
        release = threading.Event()
        finished = threading.Event()
        class HungGUI(FakeGUI):
            def get_input_box(self):
                release.wait(2)
                finished.set()
                return super().get_input_box()
        gui = HungGUI()
        user32 = fake_user32()
        clipboard = FakeClipboard(gui)
        started = time.monotonic()
        try:
            result = channel_for(gui, timeout=0.05, user32=user32,
                                 clipboard=clipboard).send('测试群', '正文')
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertIn('超过发送时限', result.reason)
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertNotIn('focus', gui.calls)
        self.assertEqual(clipboard.writes, [])
        user32.SendInput.assert_not_called()

    def test_foreground_wait_is_at_most_300ms(self):
        now = [0.0]
        waits = []
        def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds
        gui = FakeGUI()
        user32 = fake_user32(999)
        clipboard = FakeClipboard(gui)
        channel = channel_for(gui, user32=user32, clipboard=clipboard,
                              clock=lambda: now[0], sleep=sleep)
        with patch.object(channel, '_foreground', return_value=True):
            result = channel.send('测试群', '正文')
        self.assertEqual(result.reason_code, 'foreground_lost')
        self.assertLessEqual(sum(waits), 0.300001)
        self.assertEqual(clipboard.writes, [])
        user32.SendInput.assert_not_called()

    def test_foreground_changes_while_snapshotting_clipboard_no_write(self):
        gui = FakeGUI()
        user32 = fake_user32()
        clipboard = FakeClipboard(gui)
        original = clipboard.snapshot
        def snapshot():
            user32.GetForegroundWindow.return_value = 999
            return original()
        clipboard.snapshot = snapshot
        result = channel_for(gui, user32=user32, clipboard=clipboard).send('测试群', '正文')
        self.assertEqual(result.reason_code, 'foreground_lost')
        # 只恢复原值，不写正文，不投递 Ctrl+V。
        self.assertEqual(clipboard.writes, ['原剪贴板'])
        self.assertEqual(user32.SendInput.call_count, 2)
        self.assertTrue(all(call.args[0] == 1 for call in user32.SendInput.call_args_list))
        self.assertEqual(len(gui.focus_clicks), 1)
        self.assertEqual(gui.send_clicks, [])

    def test_foreground_changes_during_clipboard_write_blocks_ctrl_v_and_restores(self):
        gui = FakeGUI()
        user32 = fake_user32()
        clipboard = FakeClipboard(gui)
        original = clipboard.copy
        def copy(body):
            original(body)
            user32.GetForegroundWindow.return_value = 999
        clipboard.copy = copy
        result = channel_for(gui, user32=user32, clipboard=clipboard).send('测试群', '正文')
        self.assertEqual(result.reason_code, 'foreground_lost')
        self.assertEqual(clipboard.value, '原剪贴板')
        self.assertEqual(user32.SendInput.call_count, 2)
        self.assertTrue(all(call.args[0] == 1 for call in user32.SendInput.call_args_list))
        self.assertEqual(len(gui.focus_clicks), 1)
        self.assertNotIn('click_send', gui.calls)

    def test_foreground_changes_after_paste_no_title_ocr_or_send_and_restores(self):
        gui = FakeGUI()
        user32 = fake_user32()
        clipboard = FakeClipboard(gui)
        def sleep(seconds):
            if gui.pasted:
                user32.GetForegroundWindow.return_value = 999
        result = channel_for(gui, user32=user32, clipboard=clipboard,
                             sleep=sleep).send('测试群', '正文')
        self.assertEqual(result.reason_code, 'foreground_lost')
        self.assertEqual(clipboard.value, '原剪贴板')
        self.assertEqual(user32.SendInput.call_count, 3)  # 聚焦按下/抬起，再一次 Ctrl+V
        self.assertEqual(gui.calls.count('ocr'), 4)  # 标题、聚焦后、粘贴前与剪贴板写后
        self.assertNotIn('click_send', gui.calls)

    def test_covered_send_button_is_not_clicked_and_never_retries_paste(self):
        gui = FakeGUI()
        user32 = fake_user32()
        user32.WindowFromPoint.side_effect = lambda point: 999 if point.y >= 900 else 4242
        clipboard = FakeClipboard(gui)
        result = channel_for(gui, user32=user32, clipboard=clipboard).send('测试群', '正文')
        self.assertEqual(result.reason_code, 'blocked_by_window')
        self.assertNotIn('click_send', gui.calls)
        self.assertEqual(clipboard.value, '原剪贴板')
        self.assertEqual(sum(c.args[1][1].payload.keyboard.vk == 0x56
                             for c in user32.SendInput.call_args_list if c.args[0] == 4), 1)
        self.assertFalse(result.diagnostic.get('send_uncertain', False))

    def test_partial_sendinput_never_retries_and_restores_clipboard(self):
        gui = FakeGUI()
        user32 = fake_user32()
        clipboard = FakeClipboard(gui)
        channel = channel_for(gui, user32=user32, clipboard=clipboard)
        deliver = user32.SendInput.side_effect
        user32.SendInput.side_effect = lambda count, events, size: (2 if count == 4 else deliver(count, events, size))
        result = channel.send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(clipboard.value, '原剪贴板')
        self.assertEqual(user32.SendInput.call_count, 3)
        self.assertEqual(sum(call.args[0] == 4 for call in user32.SendInput.call_args_list), 1)
        self.assertEqual(user32.keybd_event.call_args_list,
                         [call(0x56, 0, 2, 0), call(0x11, 0, 2, 0)])
        self.assertNotIn('click_send', gui.calls)

    def test_preexisting_draft_never_writes_or_sends(self):
        """1.3.58：草稿改由发送前自动清空（Ctrl+A + Delete），但仍不许碰剪贴板、不许回车。"""
        gui = FakeGUI()
        gui.pasted = True          # 这个假 GUI 的草稿永远清不干净 → 模拟"清空失败"
        user32 = fake_user32()
        clipboard = FakeClipboard(gui)
        result = channel_for(gui, user32=user32, clipboard=clipboard).send('测试群', '正文')
        self.assertIn('草稿', result.reason)
        self.assertFalse(result.ok)
        self.assertEqual(clipboard.writes, [])
        keys = [call.args[1][1].payload.keyboard.vk
                for call in user32.SendInput.call_args_list if call.args[0] != 1]
        self.assertEqual(keys, [0x41, 0x2E], '只许 Ctrl+A 与 Delete')
        self.assertNotIn(0x0D, keys, '绝不许按回车')
        self.assertEqual(sum(call.args[0] == 1 for call in user32.SendInput.call_args_list), 2)
        self.assertEqual(len(gui.focus_clicks), 1)
        self.assertNotIn('click_send', gui.calls)

    def test_draft_is_auto_cleared_then_the_reply_is_sent(self):
        """1.3.58：输入框有草稿时先 Ctrl+A + Delete 清掉，再照常粘贴并发送。"""
        gui = FakeGUI()
        gui.pasted = True
        user32 = fake_user32()
        clipboard = FakeClipboard(gui)
        channel = channel_for(gui, user32=user32, clipboard=clipboard)
        deliver = user32.SendInput.side_effect
        keys = []

        def side_effect(count, events, size):
            if count != 1:
                keys.append(events[1].payload.keyboard.vk)
                if events[1].payload.keyboard.vk == 0x2E:
                    gui.pasted = False          # 假 GUI：Delete 真的把草稿删掉了
            sent = deliver(count, events, size)
            return 2 if count == 2 and sent != 2 else sent

        user32.SendInput.side_effect = side_effect
        result = channel.send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(keys[:2], [0x41, 0x2E])
        self.assertIn(0x56, keys)
        self.assertNotIn(0x0D, keys, '绝不许按回车')
        self.assertEqual(clipboard.writes[0], '正文')
        self.assertEqual(clipboard.value, '原剪贴板', '发完必须把用户剪贴板还原')
        self.assertIn('click_send', gui.calls)

    def test_window_moves_during_locator_no_stale_coordinate_click(self):
        class MovingGUI(FakeGUI):
            def get_input_box(self):
                self.origin_x += 100
                return super().get_input_box()
        gui = MovingGUI()
        clipboard = FakeClipboard(gui)
        result = channel_for(gui, clipboard=clipboard).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertIn('位置或尺寸发生变化', result.reason)
        self.assertNotIn('focus', gui.calls)
        self.assertEqual(clipboard.writes, [])

    def test_screenshots_use_all_screens_without_retry(self):
        # 1.3.21：_build 不再用实例属性把 _grab_screen 换成 ImageGrab（那会连同
        # PixelGUI 的遮挡无关抓帧一起盖掉）。「双屏正确 + 不重试」这条保证挪到
        # 回退路径 grab_screen 上继续成立，同一个断言不删。
        from src.wechat_capture import grab_screen
        gui = FakeGUI()
        channel_for(gui)._build()
        self.assertNotIn('_grab_screen', vars(gui))
        with patch('PIL.ImageGrab.grab', side_effect=OSError('screen grab failed')) as grab:
            with self.assertRaises(OSError):
                grab_screen((100, 200, 300, 400))
        grab.assert_called_once_with(bbox=(100, 200, 300, 400), all_screens=True)

    def test_unknown_foreground_owner_never_writes(self):
        gui = FakeGUI()
        user32 = fake_user32(0)
        clipboard = FakeClipboard(gui)
        channel = channel_for(gui, user32=user32, clipboard=clipboard)
        with patch.object(channel, '_foreground', return_value=True):
            result = channel.send('测试群', '正文')
        self.assertEqual(result.reason_code, 'foreground_lost')
        self.assertEqual(clipboard.writes, [])
        user32.SendInput.assert_not_called()


class BuildCaptureSeamTest(unittest.TestCase):
    """1.3.21：_build 不得再用实例属性盖掉 PixelGUI 的遮挡无关抓帧。"""

    def test_build_keeps_pixel_gui_grab_screen(self):
        class PixelLikeGUI(FakeGUI):
            def _grab_screen(self, box=None):
                return ('自绘帧', box)
        gui = PixelLikeGUI()
        channel_for(gui)._build()
        self.assertEqual(gui._grab_screen((1, 2, 3, 4)), ('自绘帧', (1, 2, 3, 4)))
        self.assertNotIn('_grab_screen', vars(gui))


class ReadonlyLayoutFailureDetailTest(unittest.TestCase):
    """1.3.21：布局检查失败必须带出异常 message 与 inspect_composer 的原因码。"""

    def test_reason_code_survives_into_available_detail(self):
        class DetailGUI(FakeGUI):
            def ocr_available(self):
                return True

            def readonly_ready(self):
                raise RuntimeError('输入框或工具栏没有唯一像素证据（pane_divider_unconfirmed）')

        with patch('src.wechat_guia_send._iconic', return_value=False):
            result = channel_for(DetailGUI()).available()
        self.assertEqual(result.reason_code, 'guia_unavailable')
        self.assertIn('只读布局检查失败', result.reason)
        self.assertIn('RuntimeError', result.reason)
        self.assertIn('pane_divider_unconfirmed', result.reason)


if __name__ == '__main__':
    unittest.main()
