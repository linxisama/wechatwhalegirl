# -*- coding: utf-8 -*-
"""1.3.43 预按键时间预算回归：模拟时钟下预按键花掉 9.0 秒仍必须按下发送键。

真机证据：客人端日志里通道起点到按下发送键要 13~17.5 秒，而 DEFAULT_TIMEOUT=18.0、
CONFIRM_RESERVE=2.5，导致闸门在按键前直接拒绝（「发送前剩余时间不足，本次未按发送键」）。
这些用例把"预按键耗时"变成模拟时钟上的确定输入，锁定两条不变式：
  1) 预按键 <= 9.0 秒时，发送键必须被按下并走完墨迹确认；
  2) 预按键把预算吃到只剩 < CONFIRM_RESERVE 时，必须拒绝按键（fail-closed 不退化）。
"""
import unittest

from src.wechat_guia_send import CONFIRM_RESERVE, DEFAULT_TIMEOUT

try:  # 正常以工程根为 sys.path[0] 运行
    from tests.test_wechat_guia_send import FakeGUI, channel_for
except ImportError:  # discover -s tests 以 tests 为起点时是扁平模块名
    from test_wechat_guia_send import FakeGUI, channel_for


class FakeClock:
    """只随 sleep 前进的模拟时钟：把预按键耗时变成可控输入，而不是靠真机等待。"""

    def __init__(self, start=0.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))


class SpendingGUI(FakeGUI):
    """每次像素/OCR 读取都"花掉"一段预算，总额固定，模拟真机每次抓帧 + 识别的往返成本。"""

    def __init__(self, clock, prepress_total, chunk=1.5, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock
        self.remaining_spend = float(prepress_total)
        self.chunk = float(chunk)

    def _spend(self):
        if self.remaining_spend <= 0:
            return
        step = min(self.chunk, self.remaining_spend)
        self.clock.now += step
        self.remaining_spend -= step

    def ocr_zoomed(self, box, scale=3):
        self._spend()
        return super().ocr_zoomed(box, scale)

    def get_input_box(self):
        self._spend()
        return super().get_input_box()

    def refresh_composer(self):
        self._spend()
        return super().refresh_composer()

    def ocr(self, region):
        self._spend()
        return super().ocr(region)

    def composer_cleared(self, box, before, window=3.0):
        self.calls.append('ink_confirm')
        return super().composer_cleared(box, before, window)


class PrePressBudgetTest(unittest.TestCase):
    def _run(self, prepress_total):
        clock = FakeClock()
        gui = SpendingGUI(clock, prepress_total)
        channel = channel_for(gui, clock=clock, sleep=clock.sleep)
        result = channel.send(gui.title, '你好，预按键预算用例')
        return gui, clock, result

    def test_nine_seconds_prepress_still_presses_and_confirms(self):
        """9.0 秒预按键 + 2.5 秒预留仍在 18 秒窗口内：必须按键并完成墨迹确认。"""
        self.assertEqual(DEFAULT_TIMEOUT, 18.0)
        self.assertEqual(CONFIRM_RESERVE, 2.5)
        gui, clock, result = self._run(9.0)
        self.assertGreaterEqual(clock.now, 9.0)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(gui.send_clicks), 1)
        self.assertIn('click_send', gui.calls)
        self.assertIn('ink_confirm', gui.calls)

    def test_sixteen_seconds_prepress_is_refused_before_the_send_key(self):
        """只剩 2.0 秒 < CONFIRM_RESERVE：一个发送键都不许按（fail-closed 不退化）。"""
        gui, clock, result = self._run(16.0)
        self.assertGreaterEqual(clock.now, 16.0)
        self.assertFalse(result.ok)
        self.assertEqual(gui.send_clicks, [])
        self.assertNotIn('click_send', gui.calls)
        self.assertIn('未按发送键', result.reason)


if __name__ == '__main__':
    unittest.main()
