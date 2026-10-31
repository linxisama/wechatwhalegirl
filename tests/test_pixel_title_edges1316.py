"""同高度的搜索栏横线不能抹掉标题锚点，仍需独立竖线起点和唯一交叉。"""
import unittest

from PIL import Image, ImageDraw

from src.wechat_layout import title_region


def bridged_frame(*, header=True, caption=True, divider_top=40):
    image = Image.new('RGB', (896, 648), 'white')
    draw = ImageDraw.Draw(image)
    draw.line((307, divider_top, 307, 647), fill=(220, 220, 220))
    if caption:
        draw.line((70, 40, 895, 40), fill=(220, 220, 220))
    if header:
        # 左侧搜索栏下沿与聊天标题下沿同高，原 _runs 会把两边并成一条。
        draw.line((70, 95, 304, 95), fill=(220, 220, 220))
        draw.line((308, 95, 895, 95), fill=(220, 220, 220))
    return image


class CrossingTitleEdgesTests(unittest.TestCase):
    def test_bridged_search_line_uses_independent_caption_and_vertical_origin(self):
        with bridged_frame() as frame:
            band = title_region(frame, 307)
            self.assertIsNotNone(band)
            self.assertEqual(band[0], 313)
            self.assertGreater(band[1], 40)
            self.assertGreater(band[3], 90)
            self.assertLess(band[3], 98)

    def test_caption_alone_cannot_become_chat_header(self):
        with bridged_frame(header=False) as frame:
            self.assertIsNone(title_region(frame, 307))

    def test_two_crossing_headers_remain_ambiguous(self):
        with bridged_frame() as frame:
            ImageDraw.Draw(frame).line((70, 120, 895, 120), fill=(220, 220, 220))
            self.assertIsNone(title_region(frame, 307))

    def test_missing_caption_or_unmatched_vertical_origin_remains_unproven(self):
        for options in ({'caption': False}, {'divider_top': 0}, {'divider_top': 55}):
            with self.subTest(options=options), bridged_frame(**options) as frame:
                self.assertIsNone(title_region(frame, 307))

    def test_header_without_vertical_continuity_above_or_below_is_rejected(self):
        for y0, y1 in ((65, 88), (102, 125)):
            with self.subTest(gap=(y0, y1)), bridged_frame() as frame:
                ImageDraw.Draw(frame).line((307, y0, 307, y1), fill='white')
                self.assertIsNone(title_region(frame, 307))

    def test_client_offset_keeps_positive_origin_and_crossing_evidence(self):
        with bridged_frame() as original:
            for left, top in ((0, 0), (1, 1), (2, 0)):
                with self.subTest(offset=(left, top)), original.crop((left, top, 895, 647)) as frame:
                    self.assertIsNotNone(title_region(frame, 307 - left))
