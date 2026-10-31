"""1.3.42 发送后确认：光标不再被当成残留草稿。

真机证据（1.3.41，客户机 backend.log）：PrintWindow 位图里光标那一层几乎一直在，
空输入框的 draft_state 恒为 'caret_like'，而 _input_box_has_text 把 caret_like
当成「有文字」，于是绿色气泡已经发出去的消息被报成「未确认清空、结果不确定」，
父进程再报超过发送时限。这里全部用生成的图像和假驱动，不操作真实微信。
"""
import unittest
from unittest.mock import Mock, patch

from PIL import ImageDraw

from src.wechat_guia_send import CONFIRM_RESERVE, CONFIRM_WINDOW, DEFAULT_TIMEOUT
from src.wechat_pixel_gui import readonly_gui_class
from tests.test_pixel_sender1315 import DangerousBase, fixture_frame
from tests.test_wechat_guia_send import FakeGUI, channel_for

GEOMETRY = (0, 0, 896, 648)
DRAFT_BOX = (326, 528, 881, 599)   # fixture_frame 的正文区域（inspect_composer 内缩 7 像素）


def marked_frame(draw_marks):
    frame = fixture_frame()
    draw_marks(ImageDraw.Draw(frame))
    return frame


def caret_frame(width=1):
    """真机空输入框的那一帧：只剩一根 1-3 像素宽、20 多像素高的光标。"""
    return marked_frame(
        lambda d: d.rectangle((334, 536, 333 + width, 558), fill='black'))


def dot_frame():
    """单独一个标点粘上去的样子：很小，但按键前后墨迹一模一样。"""
    return marked_frame(lambda d: d.rectangle((334, 536, 335, 538), fill='black'))


def text_frame(rows=1, cols=12):
    def draw(d):
        for row in range(rows):
            for col in range(cols):
                x, y = 336 + col * 16, 536 + row * 18
                d.rectangle((x, y, x + 11, y + 13), fill='black')
    return marked_frame(draw)


def toolbar_raised_frame():
    """窗口位置与尺寸都不变，但工具栏上移把输入框底边挤高了：布局真的变了。"""
    def draw(d):
        for x in (331, 367, 403, 439, 487):
            d.rectangle((x, 602, x + 15, 617), fill='white')
            d.rectangle((x, 590, x + 15, 605), outline=(80, 80, 80), width=2)
    return marked_frame(draw)


class PixelInkTests(unittest.TestCase):
    def gui_for(self, frames):
        """按顺序出帧；只剩最后一帧时反复返回它（确认窗会轮询多帧）。"""
        gui = readonly_gui_class(DangerousBase)(hwnd=44, provider=Mock())
        queue = [frame.copy() for frame in frames]

        def grab():
            frame = queue.pop(0) if len(queue) > 1 else queue[0]
            return frame.copy(), GEOMETRY

        gui._pixel_frame = grab
        return gui

    def test_caret_only_frame_is_cleared(self):
        # 真机空输入框：光标还在位图上，draft_state 只能给 'caret_like'。
        gui = self.gui_for([caret_frame(1)])
        box = gui.get_input_box()
        self.assertEqual(box, DRAFT_BOX)
        ink = gui.composer_ink(box)
        self.assertEqual(ink['state'], 'caret_like')
        self.assertLessEqual(ink['columns'], 2)
        self.assertGreater(ink['pixels'], 0)
        # 发出去以后只剩同一根光标：墨迹比按键前少得多，判为已清空。
        before = {'pixels': 6048, 'columns': 12, 'state': 'text'}
        self.assertTrue(gui.composer_cleared(box, before))

    def test_unchanged_small_mark_is_not_cleared(self):
        # 单独一个标点/光标形状：columns<=2 会把它误判成清空，靠「确实比按键前少」拦住。
        gui = self.gui_for([caret_frame(3)])
        box = gui.get_input_box()
        before = gui.composer_ink(box)
        self.assertEqual(before['state'], 'caret_like')
        self.assertFalse(gui.composer_cleared(box, before, window=0.0))
        # 另一帧：单独的句点（draft_state 给 'text'），前后墨迹相同同样不算清空。
        dotted = self.gui_for([dot_frame()])
        dot_box = dotted.get_input_box()
        dot_before = dotted.composer_ink(dot_box)
        self.assertEqual(dot_before['state'], 'text')
        self.assertFalse(dotted.composer_cleared(dot_box, dot_before, window=0.0))

    def test_empty_frame_is_cleared(self):
        gui = self.gui_for([fixture_frame()])
        box = gui.get_input_box()
        self.assertEqual(gui.composer_ink(box)['state'], 'empty')
        before = {'pixels': 6048, 'columns': 12, 'state': 'text'}
        self.assertTrue(gui.composer_cleared(box, before, window=0.0))

    def test_polling_returns_true_when_a_later_frame_clears(self):
        # 第一帧正文还在（墨迹和按键前相同），后面几帧才变少：窗口内要等到。
        gui = self.gui_for([text_frame(), text_frame(), text_frame(), caret_frame(2)])
        box = gui.get_input_box()
        before = gui.composer_ink(box)
        self.assertEqual((before['state'], before['pixels']), ('text', 2016))
        ticks = iter([0.0, 0.5, 1.0, 1.5, 2.0])
        with patch('src.wechat_pixel_gui.time.monotonic',
                   side_effect=lambda: next(ticks)), \
                patch('src.wechat_pixel_gui.time.sleep'):
            self.assertTrue(gui.composer_cleared(box, before))

    def test_layout_or_geometry_change_still_raises(self):
        base = fixture_frame()
        gui = readonly_gui_class(DangerousBase)(hwnd=44, provider=Mock())
        gui._pixel_frame = Mock(side_effect=[(base.copy(), GEOMETRY),
                                             (toolbar_raised_frame().copy(), GEOMETRY)])
        box = gui.get_input_box()
        with self.assertRaises(RuntimeError) as layout:
            gui.composer_ink(box)
        self.assertIn('布局改变', str(layout.exception))
        with self.assertRaises(RuntimeError):
            gui.composer_cleared(box, {'pixels': 6048})
        # 同一尺寸、位置变了：仍按原语义抛给调用方，不吞掉。
        moved = readonly_gui_class(DangerousBase)(hwnd=44, provider=Mock())
        moved._pixel_frame = Mock(side_effect=[(base.copy(), GEOMETRY),
                                               (base.copy(), (0, 0, 896, 640))])
        moved_box = moved.get_input_box()
        with self.assertRaises(RuntimeError) as geometry:
            moved.composer_ink(moved_box)
        self.assertIn('窗口位置改变', str(geometry.exception))


class SendConfirmTests(unittest.TestCase):
    def test_confirmation_budget_constants(self):
        self.assertEqual(CONFIRM_WINDOW, 6.0)
        self.assertEqual(CONFIRM_RESERVE, 2.5)
        self.assertEqual(DEFAULT_TIMEOUT, 18.0)   # 外部 18/24/25 秒预算不变

    def test_successful_send_presses_once_inside_six_second_window(self):
        class Counting(FakeGUI):
            clears = 0

            def composer_cleared(self, box, before, window=3.0):
                self.clears += 1
                self.confirm_before = before
                self.confirm_window = window
                return super().composer_cleared(box, before, window)

        gui = Counting()
        now = [0.0]
        channel = channel_for(gui, clock=lambda: now[0])   # 冻结时钟：截止时间是 18 秒
        reads = []
        original = channel._read

        def record(deadline, fn):
            reads.append(deadline)
            return original(deadline, fn)

        channel._read = record
        result = channel.send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(gui.clears, 1)
        self.assertEqual(len(gui.send_clicks), 1)
        self.assertEqual(gui.calls.count('click_send'), 1)
        self.assertEqual(result.diagnostic.get('submission_state'), 'confirmed')
        # 确认阶段用的是按键前那一帧的墨迹量。
        self.assertEqual(gui.confirm_before,
                         {'pixels': 400, 'columns': 12, 'state': 'text'})
        self.assertEqual(gui.confirm_window, 3.0)
        # 确认窗口是 6 秒，而不是把剩下的 18 秒全给确认；其余读取仍用全局截止时间。
        self.assertEqual(reads[-1], CONFIRM_WINDOW)
        self.assertIn(DEFAULT_TIMEOUT, reads)

    def test_title_change_after_press_keeps_confirmed_verdict(self):
        # 按键之后不再 OCR 标题：真机上它一抖就把已经发出去的翻成"结果不确定"。
        class TitleFlips(FakeGUI):
            def composer_cleared(self, box, before, window=3.0):
                self.title = '另一空白会话'
                return super().composer_cleared(box, before, window)

        gui = TitleFlips()
        result = channel_for(gui).send('测试群', '正文')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.diagnostic.get('submission_state'), 'confirmed')
        self.assertEqual(len(gui.send_clicks), 1)

    def test_unconfirmed_clear_is_uncertain_and_never_pressed_again(self):
        class StillDraft(FakeGUI):
            def composer_cleared(self, box, before, window=3.0):
                return False

        gui = StillDraft()
        result = channel_for(gui).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertIn('未确认清空', result.reason)
        self.assertTrue(result.diagnostic.get('send_uncertain'))
        self.assertEqual(result.diagnostic.get('submission_state'), 'uncertain')
        self.assertEqual(len(gui.send_clicks), 1)   # 不重发、不第二次点击

    def test_no_press_when_confirmation_reserve_is_spent(self):
        now = [0.0]
        gui = FakeGUI()
        channel = channel_for(gui, clock=lambda: now[0])
        # 抓一帧要几百毫秒：真机上剩余 2 秒就按下去，确认窗必被全局截止时间吃掉。
        channel._send_gate = lambda: now.__setitem__(0, DEFAULT_TIMEOUT - 2.0)
        result = channel.send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'send_failed')
        self.assertIn('剩余时间不足', result.reason)
        self.assertEqual(gui.send_clicks, [])
        self.assertEqual(gui.calls.count('click_send'), 0)
        self.assertEqual(result.diagnostic.get('submission_state'), 'not_submitted')
        self.assertFalse(result.diagnostic.get('send_key'))

    def test_unmeasurable_ink_before_press_fails_closed(self):
        class NoInk(FakeGUI):
            def composer_ink(self, box):
                raise RuntimeError('抓不到帧')

        gui = NoInk()
        result = channel_for(gui).send('测试群', '正文')
        self.assertFalse(result.ok)
        self.assertIn('没能测量输入框墨迹', result.reason)
        self.assertEqual(gui.send_clicks, [])
        self.assertEqual(result.diagnostic.get('submission_state'), 'not_submitted')


if __name__ == '__main__':
    unittest.main()
