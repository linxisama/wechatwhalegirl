"""同色扫描提速的历史结果对照；只处理合成像素，不接触桌面或微信。"""
import itertools
import random
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from src import wechat_layout as layout
from tests.test_pixel_sender1315 import fixture_frame


def historical_light(first, second):
    """独立描述旧规则的三个条件，不调用新函数或其辅助函数。"""
    if sum(first) < 420 or sum(second) < 420:
        return False
    for color in (first, second):
        if max(color) - min(color) > 45:
            return False
    differences = [abs(first[i] - second[i]) for i in range(min(len(first), len(second)))]
    largest = max(differences)
    return largest >= 3 and largest <= 125


def historical_edge(before, center, after):
    for first, second in ((before, after), (before, center), (center, after)):
        if historical_light(first, second):
            return True
    return False


def evidence(image):
    composer = layout.inspect_composer(image)
    title = layout.title_region(image, composer['pane_left']) if composer.get('confirmed') else None
    return composer, title


class PixelEdgeFastpath1331Tests(unittest.TestCase):
    def test_all_uniform_gray_levels_remain_non_edges_in_rgb_and_rgba(self):
        for shade in range(256):
            for color in ((shade,) * 3, (shade,) * 4):
                with self.subTest(color=color):
                    self.assertFalse(historical_edge(color, color, color))
                    self.assertFalse(layout._edge_at(color, color, color))

    def test_threshold_boundaries_keep_historical_pair_results(self):
        pairs = (
            ((139, 140, 140), (143, 143, 143), False),  # 亮度419。
            ((140, 140, 140), (143, 143, 143), True),   # 亮度420、色差3。
            ((180, 180, 180), (182, 182, 182), False),  # 色差2。
            ((140, 140, 140), (255, 255, 255), True),
            ((130, 145, 145), (255, 255, 255), True),   # 最大色差125。
            ((129, 146, 146), (255, 255, 255), False),  # 最大色差126。
            ((160, 160, 205), (180, 180, 180), True),   # 通道跨度45。
            ((160, 160, 206), (180, 180, 180), False),  # 通道跨度46。
        )
        for first, second, expected in pairs:
            with self.subTest(first=first, second=second):
                self.assertEqual(historical_light(first, second), expected)
                for triple in ((first, first, second), (first, second, first),
                               (second, first, first)):
                    self.assertEqual(layout._edge_at(*triple), historical_edge(*triple))

    def test_boundary_colors_and_random_triples_match_historical_rule(self):
        rng = random.Random(1331)
        levels = (0, 2, 3, 45, 46, 125, 126, 139, 140, 141, 209, 210, 254, 255)
        colors = list(itertools.product(levels, repeat=3))
        colors.extend((i, i, i) for i in range(256))
        for _ in range(50000):
            triple = tuple(rng.choice(colors) for _ in range(3))
            self.assertEqual(layout._edge_at(*triple), historical_edge(*triple), triple)

    def assert_historical_evidence(self, image):
        current = evidence(image)
        with patch.object(layout, '_edge_at', historical_edge):
            historical = evidence(image)
        self.assertEqual(current, historical)
        return current

    def test_valid_composer_and_title_keep_complete_evidence(self):
        with fixture_frame() as image:
            composer, title = self.assert_historical_evidence(image)
            self.assertTrue(composer['confirmed'])
            self.assertIsNotNone(title)

    def test_single_pixel_dividers_at_each_parity_keep_complete_evidence(self):
        with fixture_frame() as original:
            for bounds in ((0, 0, 894, 647), (1, 1, 895, 648)):
                with self.subTest(bounds=bounds), original.crop(bounds) as image:
                    composer, _ = self.assert_historical_evidence(image)
                    self.assertTrue(composer['confirmed'])

    def test_ambiguous_connections_remain_rejected_with_same_evidence(self):
        with fixture_frame() as image:
            draw = ImageDraw.Draw(image)
            draw.line((285, 30, 285, 647), fill=(220, 220, 220))
            draw.line((286, 80, 895, 80), fill=(220, 220, 220))
            composer, title = self.assert_historical_evidence(image)
            self.assertFalse(composer['confirmed'])
            self.assertEqual(composer['reason'], 'pane_divider_unconfirmed')
            self.assertIsNone(title)

    def test_nonuniform_colored_noise_keeps_complete_negative_evidence(self):
        rng = random.Random(1331)
        with Image.frombytes('RGB', (320, 240), rng.randbytes(320 * 240 * 3)) as image:
            composer, _ = self.assert_historical_evidence(image)
            self.assertFalse(composer['confirmed'])

    def test_white_background_has_same_full_rejection(self):
        with Image.new('RGB', (320, 240), 'white') as image:
            composer, _ = self.assert_historical_evidence(image)
            self.assertFalse(composer['confirmed'])


if __name__ == '__main__':
    unittest.main()
