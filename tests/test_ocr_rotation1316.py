"""WinRT text-angle 词框必须映射回原图，不能裁掉真实标题。"""
import math
import unittest
from types import SimpleNamespace

from src.isolated_wechat.ocr_relay import line_from_words


def word(text,x,y,w,h):
    return SimpleNamespace(text=text,bounding_rect=SimpleNamespace(x=x,y=y,width=w,height=h))


class OCRRotationTests(unittest.TestCase):
    def test_measured_title_bounds_rotate_back_to_the_same_horizontal_band(self):
        boxes=[(33,79,45,39),(76,75,42,39),(118,72,41,39),
               (159,69,42,38),(203,66,39,38),(244,62,42,40)]
        row=line_from_words([word(t,*b) for t,b in zip('文件传输助手',boxes)],1032,129,angle=4.3)
        self.assertEqual(row[0],'文件传输助手')
        self.assertLess(row[2],50)
        self.assertLess(row[4],50)
        self.assertGreater(row[3],250)

    def test_clockwise_rotation_uses_image_center_and_all_four_word_corners(self):
        row=line_from_words([word('A',40,40,20,10)],100,100,angle=10)
        radians=math.radians(10)
        expected=[(50+math.cos(radians)*(x-50)-math.sin(radians)*(y-50),
                   50+math.sin(radians)*(x-50)+math.cos(radians)*(y-50))
                  for x,y in ((40,40),(60,40),(40,50),(60,50))]
        self.assertEqual(row[1:],[math.floor(min(x for x,y in expected)),
            math.floor(min(y for x,y in expected)),
            math.ceil(max(x for x,y in expected))-math.floor(min(x for x,y in expected)),
            math.ceil(max(y for x,y in expected))-math.floor(min(y for x,y in expected))])

    def test_none_and_full_circle_preserve_original_geometry(self):
        data=[word('发送',20,20,30,20)]
        self.assertEqual(line_from_words(data,100,100),line_from_words(data,100,100,angle=360))

    def test_invalid_or_transformed_outside_image_rejects_entire_line(self):
        for angle in (True,float('nan'),float('inf'),'4.3',90):
            with self.subTest(angle=angle):
                self.assertIsNone(line_from_words([word('A',40,40,20,10)],100,100,angle=angle))
        self.assertIsNone(line_from_words([word('A',0,0,20,10)],100,100,angle=10))
