"""真实展开控件形状的负例与正文保护；只生成内存图像，不操作微信。"""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from src.wechat_layout import composer_control_pixels, draft_state, inspect_composer
from src.wechat_pixel_gui import readonly_gui_class


# 只读实测25像素控件的行占用，与实际中文/正文无关。
CONTROL_ROWS = ((0, 3), (0, 5), (1, 6), (4, 7), (5, 7), (5, 8), (6, 8), (6, 8))
SMALL_CONTROL_ROWS = ((0, 3), (0, 5), (3, 6), (4, 6), (5, 7), (5, 7), (5, 7))


def draw_control(frame, composer, shift=(0, 0), *, rows=CONTROL_ROWS, top_offset=5):
    right, top = composer['layout']['box'][2], composer['layout']['box'][1]
    points = {(right - 15 + x + shift[0], top + top_offset + y + shift[1])
              for y, (start, end) in enumerate(rows) for x in range(start, end)}
    for point in points:
        frame.putpixel(point, (189, 189, 189))
    return points


def fixture(top=410):
    image = Image.new('RGB', (700, 520), 'white')
    draw = ImageDraw.Draw(image)
    draw.line((244, 30, 244, 519), fill=(220, 220, 220))
    draw.line((245, 75, 699, 75), fill=(220, 220, 220))
    draw.rectangle((255, top, 689, 505), outline=(220, 220, 220))
    for x in (268, 304, 340, 376, 412):
        draw.rectangle((x, 480, x + 15, 495), outline=(80, 80, 80), width=2)
    return image


class Base:
    def __init__(self, *, hwnd, calibrate):
        self.main_hwnd = hwnd
        self._update_layout()

    def _update_layout(self):
        self.right_pane_left = 1
        self.send_button_region = (1, 1, 2, 2)


def make_gui(frame):
    gui = readonly_gui_class(Base)(hwnd=44, provider=Mock())
    gui._pixel_frame = lambda: (frame.copy(), (0, 0, 700, 520))
    return gui


class ComposerControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with fixture() as frame:
            cls.info = inspect_composer(frame)
        if not cls.info.get('confirmed'):
            raise AssertionError(cls.info)

    def test_exact_complete_glyph_is_excluded_only_with_confirmed_composer(self):
        with fixture() as frame:
            points = draw_control(frame, self.info)
            box = self.info['draft_box']
            self.assertEqual(len(points), 25)
            self.assertEqual(composer_control_pixels(frame, box, self.info), points)
            self.assertEqual(draft_state(frame, box), 'text')
            self.assertEqual(draft_state(frame, box, composer=self.info), 'empty')

    def test_missing_pixel_shift_and_same_bounding_box_wrong_shape_are_not_exempt(self):
        for variation in ('missing', 'shift', 'wrong_shape'):
            with self.subTest(variation=variation), fixture() as frame:
                points = draw_control(frame, self.info, (1, 0) if variation == 'shift' else (0, 0))
                if variation != 'shift':
                    removed = sorted(points)[5]
                    frame.putpixel(removed, (255, 255, 255))
                    if variation == 'wrong_shape':
                        left = min(x for x, y in points)
                        top = min(y for x, y in points)
                        frame.putpixel((left + 3, top + 4), (189, 189, 189))
                self.assertFalse(composer_control_pixels(frame, self.info['draft_box'], self.info))
                self.assertEqual(draft_state(frame, self.info['draft_box'], composer=self.info), 'text')

    def test_one_extra_pixel_inside_glyph_box_or_guard_is_not_hidden(self):
        for offset in ((3, 4), (-1, 3), (8, 3)):
            with self.subTest(offset=offset), fixture() as frame:
                points = draw_control(frame, self.info)
                left, top = min(x for x, y in points), min(y for x, y in points)
                frame.putpixel((left + offset[0], top + offset[1]), (189, 189, 189))
                self.assertFalse(composer_control_pixels(frame, self.info['draft_box'], self.info))
                self.assertEqual(draft_state(frame, self.info['draft_box'], composer=self.info), 'text')

    def test_control_never_removes_single_mark_at_any_editable_edge(self):
        x0, y0, x1, y1 = self.info['draft_box']
        for point in ((x0, y0), (x0, y1 - 1), (x1 - 1, y1 - 1),
                      (x1 - 1, y0 + 25), (x0 + 30, y0), (x0 + 30, y1 - 1)):
            with self.subTest(point=point), fixture() as frame:
                draw_control(frame, self.info)
                frame.putpixel(point, (189, 189, 189))
                self.assertEqual(draft_state(frame, self.info['draft_box'], composer=self.info), 'text')

    def test_unconfirmed_or_inconsistent_composer_never_provides_mask(self):
        for field in ('confirmed', 'toolbar', 'layout', 'draft_box'):
            with self.subTest(field=field), fixture() as frame:
                draw_control(frame, self.info)
                info = deepcopy(self.info)
                info[field] = False if field == 'confirmed' else None
                self.assertFalse(composer_control_pixels(frame, self.info['draft_box'], info))
                self.assertEqual(draft_state(frame, self.info['draft_box'], composer=info), 'text')

    def test_colored_lookalike_is_not_the_observed_gray_control(self):
        with fixture() as frame:
            for point in draw_control(frame, self.info):
                frame.putpixel(point, (80, 160, 80))
            self.assertFalse(composer_control_pixels(frame, self.info['draft_box'], self.info))
            self.assertEqual(draft_state(frame, self.info['draft_box'], composer=self.info), 'text')

    def test_second_observed_19_pixel_template_requires_its_exact_offset_and_full_shape(self):
        with fixture() as frame:
            points = draw_control(frame, self.info, rows=SMALL_CONTROL_ROWS, top_offset=2)
            box = self.info['draft_box']
            self.assertEqual(len(points), 19)
            self.assertEqual(composer_control_pixels(frame, box, self.info), points)
            self.assertEqual(sum(box[0] <= x < box[2] and box[1] <= y < box[3]
                                 for x, y in points), 6)
            self.assertEqual(draft_state(frame, box), 'text')
            self.assertEqual(draft_state(frame, box, composer=self.info), 'empty')

    def test_small_template_extra_pixel_missing_pixel_shift_or_other_shape_is_rejected(self):
        for variation in ('inside_extra', 'guard_extra', 'missing', 'shift', 'wrong_shape', 'old_offset'):
            with self.subTest(variation=variation), fixture() as frame:
                points = draw_control(frame, self.info, (1, 0) if variation == 'shift' else (0, 0),
                                      rows=SMALL_CONTROL_ROWS,
                                      top_offset=5 if variation == 'old_offset' else 2)
                left, top = min(x for x, y in points), min(y for x, y in points)
                if variation in ('inside_extra', 'guard_extra'):
                    frame.putpixel((left + (2 if variation == 'inside_extra' else -1), top + 4),
                                   (189, 189, 189))
                elif variation in ('missing', 'wrong_shape'):
                    frame.putpixel((left, top), (255, 255, 255))
                    if variation == 'wrong_shape':
                        frame.putpixel((left + 2, top + 4), (189, 189, 189))
                self.assertFalse(composer_control_pixels(frame, self.info['draft_box'], self.info))
                self.assertEqual(draft_state(frame, self.info['draft_box'], composer=self.info), 'text')

    def test_small_template_still_checks_every_other_draft_pixel(self):
        box = self.info['draft_box']
        for point in ((box[0], box[1]), (box[2] - 1, box[3] - 1), (box[0] + 30, box[1] + 10)):
            with self.subTest(point=point), fixture() as frame:
                draw_control(frame, self.info, rows=SMALL_CONTROL_ROWS, top_offset=2)
                frame.putpixel(point, (189, 189, 189))
                self.assertEqual(draft_state(frame, box, composer=self.info), 'text')

    def test_small_template_with_caret_is_only_a_candidate_until_actual_blank_frame(self):
        with fixture() as frame:
            draw_control(frame, self.info, rows=SMALL_CONTROL_ROWS, top_offset=2)
            box = self.info['draft_box']
            ImageDraw.Draw(frame).rectangle((box[0] + 8, box[1] + 8,
                                            box[0] + 9, box[1] + 25), fill='black')
            self.assertEqual(draft_state(frame, box, composer=self.info), 'caret_like')


class PixelComposerCacheTests(unittest.TestCase):
    def test_identical_outside_pixels_reuse_only_geometry_while_new_draft_is_still_read(self):
        with fixture() as frame:
            gui = make_gui(frame)
            with patch('src.wechat_pixel_gui.inspect_composer', wraps=inspect_composer) as inspect:
                box = gui.get_input_box()
                self.assertFalse(gui._input_box_has_text(box))
                frame.putpixel((box[0] + 30, box[1] + 20), (189, 189, 189))
                self.assertTrue(gui._input_box_has_text(box))
                self.assertEqual(inspect.call_count, 1)

    def test_every_outside_pixel_and_window_geometry_change_invalidates_cache(self):
        with fixture() as frame:
            gui = make_gui(frame)
            with patch('src.wechat_pixel_gui.inspect_composer', wraps=inspect_composer) as inspect:
                gui.get_input_box()
                frame.putpixel((500, 150), (230, 230, 230))
                gui.get_input_box()
                self.assertEqual(inspect.call_count, 2)
                gui._pixel_frame = lambda: (frame.copy(), (1, 0, 700, 520))
                gui.get_input_box()
                self.assertEqual(inspect.call_count, 3)

    def test_changed_composer_with_same_window_geometry_rejects_old_roi(self):
        with fixture() as frame, fixture(top=380) as changed:
            gui = make_gui(frame)
            box = gui.get_input_box()
            gui._pixel_frame = lambda: (changed.copy(), (0, 0, 700, 520))
            with self.assertRaisesRegex(RuntimeError, '输入框布局改变'):
                gui._input_box_has_text(box)

    def test_control_and_caret_need_actual_blink_before_empty(self):
        with fixture() as frame:
            gui = make_gui(frame)
            box = gui.get_input_box()
            draw_control(frame, gui._pixel_composer)
            self.assertFalse(gui._input_box_has_text(box))
            with frame.copy() as caret:
                ImageDraw.Draw(caret).rectangle((box[0] + 8, box[1] + 8,
                                                box[0] + 10, box[1] + 30), fill='black')
                gui._pixel_frame = Mock(side_effect=[(caret.copy(), (0, 0, 700, 520)),
                                                     (frame.copy(), (0, 0, 700, 520))])
                with patch('src.wechat_pixel_gui.time.sleep'):
                    self.assertFalse(gui._input_box_has_text(box))
                self.assertEqual(gui._pixel_frame.call_count, 2)
                gui._pixel_frame = lambda: (caret.copy(), (0, 0, 700, 520))
                with patch('src.wechat_pixel_gui.time.monotonic', side_effect=[0, 2]):
                    self.assertTrue(gui._input_box_has_text(box))


if __name__ == '__main__':
    unittest.main()
