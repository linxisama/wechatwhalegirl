"""首轮标题裁剪来自全部可见字形，不能只保留想要匹配的词。"""
import unittest
from PIL import Image, ImageDraw

from src.wechat_pixel_gui import _title_ink_region


class TitleInkCropTests(unittest.TestCase):
    def test_crop_bounds_all_observed_ink_with_margin(self):
        with Image.new('RGB',(500,100),'white') as frame:
            ImageDraw.Draw(frame).rectangle((125,30,210,45),fill=(60,60,60))
            self.assertEqual(_title_ink_region(frame,(100,10,450,70)),(115,22,221,54))

    def test_other_prefix_suffix_and_colored_marks_are_never_discarded(self):
        with Image.new('RGB',(500,100),'white') as frame:
            draw=ImageDraw.Draw(frame)
            draw.rectangle((125,30,210,45),fill=(60,60,60))
            frame.putpixel((440,30),(255,0,0))
            frame.putpixel((101,30),(0,0,0))
            box=_title_ink_region(frame,(100,10,450,70))
            self.assertEqual(box[0],100)
            self.assertEqual(box[2],450)

    def test_blank_crop_preserves_original_region(self):
        with Image.new('RGB',(500,100),'white') as frame:
            self.assertEqual(_title_ink_region(frame,(100,10,450,70)),(100,10,450,70))
