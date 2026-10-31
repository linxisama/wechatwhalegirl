import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.reply_text import strip_model_markers, tidy_reply


class StripModelMarkersTests(unittest.TestCase):
    def test_removes_paired_acp_tag_at_start(self):
        raw = '<acp tokens="35" type="text">m00006</acp>主人这是想说什么呀…'
        self.assertEqual(strip_model_markers(raw), '主人这是想说什么呀…')

    def test_removes_multiple_and_multiline_tags(self):
        raw = '第一句\n<acp tokens="7" type="text">\nm00007\n</acp>第二句<acp>m1</acp>'
        self.assertEqual(strip_model_markers(raw), '第一句\n第二句')

    def test_removes_dangling_tag(self):
        self.assertEqual(strip_model_markers('<acp tokens="9">嗯？'), '嗯？')
        self.assertEqual(strip_model_markers('嗯？</acp>'), '嗯？')

    def test_keeps_other_angle_bracket_text(self):
        for raw in ('a < b 的时候再说', '我<3你', '别删 <div> 标签', '<3'):
            self.assertEqual(strip_model_markers(raw), raw)

    def test_empty_and_none(self):
        self.assertEqual(strip_model_markers(''), '')
        self.assertEqual(strip_model_markers(None), '')

    def test_tidy_reply_still_works_after_strip(self):
        raw = '<acp tokens="1" type="text">m1</acp>你好 呀，今天  天气不错  '
        self.assertEqual(tidy_reply(strip_model_markers(raw)), '你好呀，今天天气不错')


if __name__ == '__main__':
    unittest.main()
