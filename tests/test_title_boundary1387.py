# -*- coding: utf-8 -*-
"""1.3.87：标题墨迹 14-17px 新增 boundary 档，18px 及以上保持原 medium 行为。"""
import unittest

from PIL import ImageDraw

from src.wechat_pixel_gui import (
    _BOUNDARY_TITLE_PASSES,
    _MEDIUM_TITLE_PASSES,
    _TITLE_PASSES,
    _pass_variants,
    _title_ink_region,
    _title_pass_families,
    _title_passes,
)

from test_pixel_title_passes1321 import BAND, fixture_frame


def frame_with_ink_height(height):
    frame = fixture_frame()
    draw = ImageDraw.Draw(frame)
    draw.rectangle((330, 20, 345, 20 + height - 1), fill='black')
    return frame


class TitleBoundaryTests(unittest.TestCase):
    def test_16px_ink_selects_boundary_passes(self):
        frame = frame_with_ink_height(16)
        try:
            passes, height = _title_passes(frame, _title_ink_region(frame, BAND))
        finally:
            frame.close()
        self.assertEqual(height, 16)
        self.assertIs(passes, _BOUNDARY_TITLE_PASSES)
        self.assertEqual(len(passes), 3)

    def test_boundary_families_start_with_three_variants_and_legacy_fallback(self):
        families = _title_pass_families(_BOUNDARY_TITLE_PASSES)
        self.assertEqual([name for name, _variants in families], ['boundary', 'legacy'])
        self.assertEqual(len(families[0][1]), 3)
        self.assertEqual(families[0][1], _pass_variants(_BOUNDARY_TITLE_PASSES))
        self.assertEqual(len(families[1][1]), 3)
        self.assertEqual(families[1][1],
                         _pass_variants(tuple((s, t, 0) for s, t in _TITLE_PASSES)))

    def test_height_bands_keep_18px_and_above_on_medium_or_legacy(self):
        # 14-17 边界档；18-24 仍是 medium；25 及以上仍是旧表——18px 帧行为一个字不改。
        for height in (14, 15, 16, 17):
            with self.subTest(height=height):
                frame = frame_with_ink_height(height)
                try:
                    passes, measured = _title_passes(frame, _title_ink_region(frame, BAND))
                finally:
                    frame.close()
                self.assertEqual(measured, height)
                self.assertIs(passes, _BOUNDARY_TITLE_PASSES)
        for height in (18, 24):
            with self.subTest(height=height):
                frame = frame_with_ink_height(height)
                try:
                    passes, measured = _title_passes(frame, _title_ink_region(frame, BAND))
                finally:
                    frame.close()
                self.assertEqual(measured, height)
                self.assertIs(passes, _MEDIUM_TITLE_PASSES)
        for height in (25, 26):
            with self.subTest(height=height):
                frame = frame_with_ink_height(height)
                try:
                    passes, measured = _title_passes(frame, _title_ink_region(frame, BAND))
                finally:
                    frame.close()
                self.assertEqual(measured, height)
                self.assertEqual(passes, tuple((s, t, 0) for s, t in _TITLE_PASSES))


if __name__ == '__main__':
    unittest.main()
