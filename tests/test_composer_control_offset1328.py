"""展开角控件新偏移的逐点保护；只用合成像素，不访问微信或桌面。"""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from src.wechat_layout import composer_control_pixels, draft_state, inspect_composer
from tests.test_pixel_composer_control1316 import fixture, make_gui


# 只读取证为同一25点形状，物理偏移是框右减25、框上加3，不能搜索近似落点。
ROWS = ((0, 3), (0, 5), (1, 6), (4, 7), (5, 7), (5, 8), (6, 8), (6, 8))


def points_for(composer, *, right_offset=25, top_offset=3, shift=(0, 0)):
    _, top, right, _ = composer['layout']['box']
    return frozenset((right - right_offset + x + shift[0], top + top_offset + y + shift[1])
                     for y, (start, end) in enumerate(ROWS) for x in range(start, end))


def put_control(image, composer, **kwargs):
    points = points_for(composer, **kwargs)
    for index, point in enumerate(sorted(points)):
        gray = (176, 181, 189, 197, 201)[index % 5]
        image.putpixel(point, (gray, gray, gray))
    return points


class ComposerControlOffset1328Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with fixture() as frame:
            cls.composer = inspect_composer(frame)
        if not cls.composer.get('confirmed'):
            raise AssertionError(cls.composer)

    def test_exact_new_offset_requires_all_25_points_and_masks_only_actual_points(self):
        with fixture() as frame:
            points = put_control(frame, self.composer)
            box = self.composer['draft_box']
            self.assertEqual(len(points), 25)
            self.assertEqual(sum(box[0] <= x < box[2] and box[1] <= y < box[3]
                                 for x, y in points), 12)
            self.assertEqual(composer_control_pixels(frame, box, self.composer), points)
            self.assertEqual(draft_state(frame, box), 'text')
            self.assertEqual(draft_state(frame, box, composer=self.composer), 'empty')

    def test_real_capture_geometry_uses_same_full_template_without_rescaling(self):
        box = [404, 661, 1104, 749]
        composer = {'confirmed': True, 'draft_box': box,
                    'layout': {'pixel_confirmed': True, 'candidate_count': 1,
                               'box': [397, 655, 1111, 787]},
                    'toolbar': {'confirmed': True, 'draft_text_rect': box}}
        with Image.new('RGB', (1118, 809), 'white') as frame:
            points = put_control(frame, composer)
            self.assertEqual(min(points), (1086, 658))
            self.assertEqual(composer_control_pixels(frame, box, composer), points)
            self.assertEqual(sum(box[0] <= x < box[2] and box[1] <= y < box[3]
                                 for x, y in points), 12)
            self.assertEqual(draft_state(frame, box, composer=composer), 'empty')

    def test_one_missing_pixel_even_outside_draft_disables_whole_template(self):
        box = self.composer['draft_box']
        expected = points_for(self.composer)
        for point in (min(expected), max(expected)):
            with self.subTest(point=point), fixture() as frame:
                put_control(frame, self.composer)
                frame.putpixel(point, (255, 255, 255))
                self.assertFalse(composer_control_pixels(frame, box, self.composer))
                self.assertEqual(draft_state(frame, box, composer=self.composer), 'text')

    def test_extra_ink_inside_glyph_box_or_guard_is_never_hidden(self):
        points = points_for(self.composer)
        left, top = min(x for x, _ in points), min(y for _, y in points)
        for offset in ((3, 4), (-1, 3), (8, 3), (4, -1), (6, 8)):
            with self.subTest(offset=offset), fixture() as frame:
                put_control(frame, self.composer)
                point = (left + offset[0], top + offset[1])
                self.assertNotIn(point, points)
                frame.putpixel(point, (189, 189, 189))
                self.assertFalse(composer_control_pixels(frame, self.composer['draft_box'], self.composer))
                self.assertEqual(draft_state(frame, self.composer['draft_box'], composer=self.composer), 'text')

    def test_one_pixel_shift_in_each_direction_is_rejected(self):
        for shift in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            with self.subTest(shift=shift), fixture() as frame:
                put_control(frame, self.composer, shift=shift)
                self.assertFalse(composer_control_pixels(frame, self.composer['draft_box'], self.composer))
                self.assertEqual(draft_state(frame, self.composer['draft_box'], composer=self.composer), 'text')

    def test_same_count_and_same_bounding_box_different_shape_is_rejected(self):
        with fixture() as frame:
            points = put_control(frame, self.composer)
            left, top = min(x for x, _ in points), min(y for _, y in points)
            removed, added = (left + 1, top + 2), (left + 3, top + 4)
            self.assertIn(removed, points)
            self.assertNotIn(added, points)
            wrong = (points - {removed}) | {added}
            bounds = lambda values: (min(x for x, _ in values), min(y for _, y in values),
                                     max(x for x, _ in values), max(y for _, y in values))
            self.assertEqual(len(wrong), len(points))
            self.assertEqual(bounds(wrong), bounds(points))
            frame.putpixel(removed, (255, 255, 255))
            frame.putpixel(added, (189, 189, 189))
            self.assertFalse(composer_control_pixels(frame, self.composer['draft_box'], self.composer))
            self.assertEqual(draft_state(frame, self.composer['draft_box'], composer=self.composer), 'text')

    def test_colored_lookalike_and_single_colored_control_point_are_rejected(self):
        for only_one in (False, True):
            with self.subTest(only_one=only_one), fixture() as frame:
                points = put_control(frame, self.composer)
                for point in ([max(points)] if only_one else points):
                    frame.putpixel(point, (80, 160, 80))
                self.assertFalse(composer_control_pixels(frame, self.composer['draft_box'], self.composer))
                self.assertEqual(draft_state(frame, self.composer['draft_box'], composer=self.composer), 'text')

    def test_only_twelve_roi_points_cannot_stand_in_for_complete_control(self):
        with fixture() as frame:
            box = self.composer['draft_box']
            points = points_for(self.composer)
            inside = {(x, y) for x, y in points if box[0] <= x < box[2] and box[1] <= y < box[3]}
            self.assertEqual(len(inside), 12)
            for point in inside:
                frame.putpixel(point, (189, 189, 189))
            self.assertFalse(composer_control_pixels(frame, box, self.composer))
            self.assertEqual(draft_state(frame, box, composer=self.composer), 'text')

    def test_adjacent_punctuation_outside_control_guard_remains_draft_text(self):
        points = points_for(self.composer)
        left, top = min(x for x, _ in points), min(y for _, y in points)
        for point in ((left - 3, top + 5), (left + 11, top + 5), (left + 5, top + 11)):
            with self.subTest(point=point), fixture() as frame:
                put_control(frame, self.composer)
                frame.putpixel(point, (189, 189, 189))
                self.assertEqual(composer_control_pixels(frame, self.composer['draft_box'], self.composer), points)
                self.assertEqual(draft_state(frame, self.composer['draft_box'], composer=self.composer), 'text')

    def test_every_editable_edge_and_corner_still_rejects_one_mark(self):
        x0, y0, x1, y1 = self.composer['draft_box']
        for point in ((x0, y0), (x1 - 1, y0), (x0, y1 - 1), (x1 - 1, y1 - 1),
                      (x0 + 30, y0), (x0 + 30, y1 - 1), (x0, y0 + 25), (x1 - 1, y0 + 25)):
            with self.subTest(point=point), fixture() as frame:
                put_control(frame, self.composer)
                frame.putpixel(point, (189, 189, 189))
                self.assertEqual(draft_state(frame, self.composer['draft_box'], composer=self.composer), 'text')

    def test_composer_evidence_missing_or_inconsistent_never_masks(self):
        original = self.composer
        mutations = (
            lambda info: info.update(confirmed=False),
            lambda info: info['layout'].update(pixel_confirmed=False),
            lambda info: info['layout'].update(candidate_count=2),
            lambda info: info['toolbar'].update(confirmed=False),
            lambda info: info['toolbar'].update(draft_text_rect=[0, 0, 1, 1]),
            lambda info: info.update(draft_box=[0, 0, 1, 1]),
            lambda info: info['layout'].update(box=[255, 409, 689, 505]),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index), fixture() as frame:
                put_control(frame, original)
                inconsistent = deepcopy(original)
                mutate(inconsistent)
                self.assertFalse(composer_control_pixels(frame, original['draft_box'], inconsistent))
                self.assertEqual(draft_state(frame, original['draft_box'], composer=inconsistent), 'text')

    def test_two_complete_templates_are_ambiguous_and_neither_is_masked(self):
        with fixture() as frame:
            new_points = put_control(frame, self.composer)
            old_points = put_control(frame, self.composer, right_offset=15, top_offset=5)
            self.assertFalse(new_points & old_points)
            self.assertFalse(composer_control_pixels(frame, self.composer['draft_box'], self.composer))
            self.assertEqual(draft_state(frame, self.composer['draft_box'], composer=self.composer), 'text')

    def test_new_control_plus_caret_requires_actual_blink_and_static_line_remains_nonempty(self):
        with fixture() as blank:
            gui = make_gui(blank)
            box = gui.get_input_box()
            put_control(blank, gui._pixel_composer)
            with blank.copy() as caret:
                ImageDraw.Draw(caret).rectangle((box[0] + 8, box[1] + 8,
                                                box[0] + 10, box[1] + 30), fill='black')
                self.assertEqual(draft_state(caret, box, composer=gui._pixel_composer), 'caret_like')
                gui._pixel_frame = Mock(side_effect=[(caret.copy(), (0, 0, 700, 520)),
                                                     (blank.copy(), (0, 0, 700, 520))])
                with patch('src.wechat_pixel_gui.time.sleep'):
                    self.assertFalse(gui._input_box_has_text(box))
                self.assertEqual(gui._pixel_frame.call_count, 2)
                gui._pixel_frame = lambda: (caret.copy(), (0, 0, 700, 520))
                with patch('src.wechat_pixel_gui.time.monotonic', side_effect=[0, 2]):
                    self.assertTrue(gui._input_box_has_text(box))


if __name__ == '__main__':
    unittest.main()
