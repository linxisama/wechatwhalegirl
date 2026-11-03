"""Pixel-equivalent acceleration must preserve small marks and all frame evidence."""
import itertools
import random
import unittest

from PIL import Image, ImageDraw

from src.wechat_pixel_masks import mark_mask
from src import wechat_pixel_gui as pixel
from src import wechat_layout as layout
from tests.test_pixel_composer_control1316 import fixture, draw_control


class PixelMarkMaskTests(unittest.TestCase):
    def assert_reference_masks(self, colors):
        with Image.new('RGB', (len(colors), 1)) as image:
            image.putdata(colors)
            for maximum, minimum in ((180, 180), (205, 210)):
                with self.subTest(thresholds=(maximum, minimum)), mark_mask(
                        image, maximum=maximum, minimum=minimum) as mask:
                    expected = bytes(255 if max(c) < maximum or
                                     (max(c)-min(c) > 45 and min(c) < minimum) else 0
                                     for c in colors)
                    self.assertEqual(mask.mode, 'L')
                    self.assertEqual(mask.size, image.size)
                    self.assertEqual(mask.tobytes(), expected)

    def test_threshold_boundaries_for_all_channel_orders(self):
        levels = (0, 1, 45, 46, 134, 135, 136, 179, 180, 181, 204, 205,
                  206, 209, 210, 211, 254, 255)
        self.assert_reference_masks(list(itertools.product(levels, repeat=3)))

    def test_random_colors_keep_the_historical_predicate(self):
        rng = random.Random(1333)
        self.assert_reference_masks([tuple(rng.randrange(256) for _ in range(3))
                                     for _ in range(8192)])

    def test_title_ink_keeps_marks_at_every_region_edge(self):
        region = (20, 15, 180, 85)
        for point in ((20, 15), (179, 15), (20, 84), (179, 84)):
            with self.subTest(point=point), Image.new('RGB', (200, 100), 'white') as frame:
                frame.putpixel(point, (134, 180, 180))  # Exactly 46 chroma, low channel<180.
                x, y = point
                self.assertEqual(pixel._title_ink_region(frame, region),
                                 (max(20, x-10), max(15, y-8),
                                  min(180, x+11), min(85, y+9)))

    def test_empty_title_and_rgb_conversion_preserve_region(self):
        region = (2, 3, 30, 40)
        for mode in ('L', 'RGB', 'RGBA'):
            with self.subTest(mode=mode), Image.new(mode, (40, 50), 'white') as frame:
                self.assertEqual(pixel._title_ink_region(frame, region), region)
                passes, height = pixel._title_passes(frame, region)
                self.assertEqual(height, 0)
                self.assertEqual(passes, tuple((s, t, 0) for s, t in pixel._TITLE_PASSES))

    def test_measured_glyph_height_keeps_profile_boundaries(self):
        # 1.3.87 起 14-17px 是 boundary 档，medium 下沿改为 18px；25px 以上仍是旧表。
        for height in (14, 15, 16, 17, 18, 24, 25):
            with self.subTest(height=height), Image.new('RGB', (60, 60), 'white') as frame:
                ImageDraw.Draw(frame).rectangle((10, 10, 20, 9+height), fill='black')
                passes, actual = pixel._title_passes(frame, (0, 0, 60, 60))
                self.assertEqual(actual, height)
                if 14 <= height <= 17:
                    self.assertIs(passes, pixel._BOUNDARY_TITLE_PASSES)
                elif 18 <= height <= 24:
                    self.assertIs(passes, pixel._MEDIUM_TITLE_PASSES)
                else:
                    self.assertEqual(passes, tuple((s, t, 0) for s, t in pixel._TITLE_PASSES))

    def test_one_uncovered_colored_title_pixel_rejects_completeness(self):
        with Image.new('RGB', (200, 100), 'white') as frame:
            rows = [('two words', 40, 25, 80, 20)]
            region = (20, 10, 180, 70)
            self.assertTrue(pixel._complete_title_pixels(frame, region, rows))
            for point in ((21, 25), (178, 25)):
                frame.putpixel(point, (134, 180, 180))
                self.assertFalse(pixel._complete_title_pixels(frame, region, rows))
                frame.putpixel(point, (255, 255, 255))

    def test_draft_keeps_single_pixel_at_every_editable_boundary(self):
        with fixture() as original:
            composer = layout.inspect_composer(original)
            self.assertTrue(composer['confirmed'])
            x0, y0, x1, y1 = box = composer['draft_box']
            for point in ((x0, y0), (x1-1, y0), (x0, y1-1), (x1-1, y1-1)):
                with self.subTest(point=point), original.copy() as frame:
                    draw_control(frame, composer)
                    frame.putpixel(point, (204, 255, 255))
                    self.assertEqual(layout.draft_state(frame, box, composer=composer), 'text')

    def test_exact_control_exemption_cannot_hide_another_pixel(self):
        with fixture() as frame:
            composer = layout.inspect_composer(frame)
            draw_control(frame, composer)
            box = composer['draft_box']
            self.assertEqual(layout.draft_state(frame, box, composer=composer), 'empty')
            frame.putpixel((box[0]+35, box[1]+12), (204, 204, 204))
            self.assertEqual(layout.draft_state(frame, box, composer=composer), 'text')

    def test_caret_density_counts_disconnected_marks_in_full_image(self):
        with Image.new('RGB', (80, 50), 'white') as image:
            draw = ImageDraw.Draw(image)
            draw.line((10, 5, 10, 24), fill='black')
            self.assertEqual(layout.draft_state(image, (0, 0, 80, 50)), 'caret_like')
            image.putpixel((70, 40), (0, 0, 0))
            self.assertEqual(layout.draft_state(image, (0, 0, 80, 50)), 'text')

    def test_mask_result_is_owned_and_fresh_for_each_frame(self):
        with Image.new('RGB', (30, 20), 'white') as image:
            with mark_mask(image) as before:
                image.putpixel((29, 19), (0, 0, 0))
                with mark_mask(image) as after:
                    self.assertIsNone(before.getbbox())
                    self.assertEqual(after.getbbox(), (29, 19, 30, 20))


if __name__ == '__main__':
    unittest.main()
