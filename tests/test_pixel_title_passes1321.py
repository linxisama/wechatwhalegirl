"""标题栏多遍 OCR 的完整性判定；全部用合成帧，不碰真实微信。"""
import unittest
from unittest.mock import Mock

from PIL import Image, ImageDraw

from src.wechat_pixel_gui import readonly_gui_class, _TITLE_PASSES, _title_ink_region
from src.wechat_layout import inspect_composer, title_region

BAND = (311, 0, 659, 93)  # 夹具实测标题区，只在本模块用来核对坐标。


def fixture_frame():
    image = Image.new('RGB', (896, 648), 'white')
    draw = ImageDraw.Draw(image)
    draw.line((307, 30, 307, 647), fill=(220, 220, 220))
    draw.line((308, 95, 895, 95), fill=(220, 220, 220))
    draw.rectangle((319, 524, 883, 628), outline=(220, 220, 220))
    for x in (331, 367, 403, 439, 487):
        draw.rectangle((x, 602, x + 15, 617), outline=(80, 80, 80), width=2)
    return image


def rows_for(ink, scale, *boxes):
    """把帧坐标的识别框换算成 OCR 报告的放大坐标（recognize 只看放大图）。"""
    return [(text, (left - ink[0]) * scale, (top - ink[1]) * scale,
             (right - left) * scale, (bottom - top) * scale)
            for text, left, top, right, bottom in boxes]


def draw_glyphs(frame, left, top, count, pitch=14, size=12, height=26):
    # 1.3.87 起 14-17px 走边界档，旧表用例改用 26px 墨迹（仍是 25px 以上的旧表区间）。
    draw = ImageDraw.Draw(frame)
    for index in range(count):
        x = left + index * pitch
        draw.rectangle((x, top, x + size - 1, top + height - 1), fill='black')


class QuietBase:
    """只提供 gui 需要的纯字段：构造过程不读校准文件、不校准、不热唤醒。"""

    def __init__(self, *, hwnd, calibrate=False):
        self.main_hwnd, self.pid = hwnd, 55
        self.origin_x = self.origin_y = 0
        self.render_w, self.render_h = 896, 648
        self.render_rect = (0, 0, 896, 648)
        self.right_pane_left = 197
        self.send_button_region = (700, 580, 895, 647)

    def _load_layout(self):
        raise AssertionError('标题用例不应读取旧校准文件')

    def calibrate_layout(self, *args, **kwargs):
        raise AssertionError('标题用例不应自动校准')

    def _get_uia(self, *args, **kwargs):
        raise AssertionError('标题用例不应热唤醒 UIA')


class TitlePassTests(unittest.TestCase):
    def make_gui(self, frame, provider):
        gui = readonly_gui_class(QuietBase)(hwnd=44, provider=provider)
        gui._pixel_frame = lambda: (frame.copy(), (0, 0, 896, 648))
        return gui

    def test_fixture_band_matches_this_module(self):
        with fixture_frame() as frame:
            self.assertEqual(title_region(frame, inspect_composer(frame)['pane_left']), BAND)

    def test_dropped_last_glyph_returns_empty(self):
        provider = Mock()
        with fixture_frame() as frame:
            draw_glyphs(frame, 330, 20, 5)
            ink = _title_ink_region(frame, BAND)
            # 五个字都有墨迹，但只认出前四个：漏掉的字就落在识别框右边的空白里。
            provider.recognize.side_effect = [rows_for(ink, scale, ('鲸鱼娘二', 330, 20, 384, 34))
                                             for scale, _ in _TITLE_PASSES]
            self.assertEqual(self.make_gui(frame, provider).current_chat_title(), '')
        provider.recognize.assert_called_once()

    def test_icons_right_of_the_band_do_not_reject_a_complete_title(self):
        provider = Mock()
        with fixture_frame() as frame:
            draw_glyphs(frame, 330, 20, 5)
            # 标题区右边界以外的窗口按钮/工具栏图标与标题同一行，不属于标题文字。
            draw = ImageDraw.Draw(frame)
            for x in (700, 720, 740, 760):
                draw.rectangle((x, 20, x + 9, 33), fill='black')
            ink = _title_ink_region(frame, BAND)
            provider.recognize.side_effect = [rows_for(ink, scale, ('鲸鱼娘二号', 330, 20, 398, 34))
                                             for scale, _ in _TITLE_PASSES]
            self.assertEqual(self.make_gui(frame, provider).current_chat_title(), '鲸鱼娘二号')

    def test_disagreeing_passes_return_empty(self):
        provider = Mock()
        with fixture_frame() as frame:
            draw_glyphs(frame, 330, 20, 5)
            ink = _title_ink_region(frame, BAND)
            texts = ['鲸鱼娘二号', '鲸鱼娘三号', '鲸鱼娘二号']
            provider.recognize.side_effect = [
                rows_for(ink, scale, (text, 330, 20, 398, 34))
                for (scale, _), text in zip(_TITLE_PASSES, texts)]
            self.assertEqual(self.make_gui(frame, provider).current_chat_title(), '')

    def test_same_length_wrong_glyph_that_passes_completeness_is_rejected(self):
        # 真机实测过的最短复现：三个变体都读满整行、完整性都是 True，但 4 倍把「助」读成「曲」。
        # 错字不产生框外墨迹，完整性检查抓不到，只能靠三份结果逐字一致拦下。
        provider = Mock()
        with fixture_frame() as frame:
            draw_glyphs(frame, 330, 20, 6)
            ink = _title_ink_region(frame, BAND)
            texts = ['文件传输助手', '文件传输曲手', '文件传输助手']
            provider.recognize.side_effect = [
                rows_for(ink, scale, (text, 330, 20, 412, 34))
                for (scale, _), text in zip(_TITLE_PASSES, texts)]
            self.assertEqual(self.make_gui(frame, provider).current_chat_title(), '')
        self.assertEqual(provider.recognize.call_count, len(_TITLE_PASSES))

    def test_full_coverage_and_agreeing_passes_return_the_joined_name(self):
        provider = Mock()
        with fixture_frame() as frame:
            draw_glyphs(frame, 330, 20, 2)
            draw_glyphs(frame, 362, 20, 2)
            ink = _title_ink_region(frame, BAND)
            provider.recognize.side_effect = [
                rows_for(ink, scale, ('鲸鱼', 330, 20, 356, 34), ('二号', 362, 20, 388, 34))
                for scale, _ in _TITLE_PASSES]
            self.assertEqual(self.make_gui(frame, provider).current_chat_title(), '鲸鱼 二号')

    def test_uncovered_ink_left_of_the_box_returns_empty(self):
        provider = Mock()
        with fixture_frame() as frame:
            draw_glyphs(frame, 320, 20, 1)
            draw_glyphs(frame, 345, 20, 2)
            ink = _title_ink_region(frame, BAND)
            provider.recognize.side_effect = [rows_for(ink, scale, ('二号', 345, 20, 371, 34))
                                             for scale, _ in _TITLE_PASSES]
            self.assertEqual(self.make_gui(frame, provider).current_chat_title(), '')
        provider.recognize.assert_called_once()

    def test_box_touching_the_band_edge_cannot_confirm_the_line_ends(self):
        provider = Mock()
        with fixture_frame() as frame:
            draw_glyphs(frame, 640, 20, 1)
            ink = _title_ink_region(frame, BAND)
            provider.recognize.side_effect = [rows_for(ink, scale, ('一号', 630, 20, 658, 34))
                                             for scale, _ in _TITLE_PASSES]
            self.assertEqual(self.make_gui(frame, provider).current_chat_title(), '')
        provider.recognize.assert_called_once()


if __name__ == '__main__':
    unittest.main()
