"""OCR 词框合成边界；全部使用内存替身，不读取桌面或执行输入。"""
import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.isolated_wechat.ocr_relay import line_from_words, recognize


def word(text, x=1, y=1, width=10, height=10):
    return SimpleNamespace(text=text, bounding_rect=SimpleNamespace(
        x=x, y=y, width=width, height=height))


class OCRWordBoundaryTests(unittest.TestCase):
    def test_real_enlarged_filehelper_words_allow_only_small_outward_box_overlap(self):
        boxes = [(45, 95, 58, 54), (100, 93, 54, 54), (154, 89, 50, 55),
                 (204, 85, 54, 54), (259, 81, 49, 50), (310, 76, 52, 52)]
        words = [word(character, x, y, width, height)
                 for character, (x, y, width, height) in zip('文件传输助手', boxes)]
        self.assertEqual(line_from_words(words, 1302, 165), ['文件传输助手', 45, 76, 317, 73])

    def test_overlap_does_not_accept_covering_equal_left_edges_or_large_intersections(self):
        first = word('文', 45, 75, 58, 54)
        for second in (word('件', 45, 75, 54, 54), word('件', 45, 75, 58, 54),
                       word('件', 46, 75, 54, 54), word('件', 98, 75, 54, 54),
                       word('件', 95, 75, 54, 54)):
            with self.subTest(rect=second.bounding_rect):
                self.assertIsNone(line_from_words([first, second], 200, 160))
        # 即使绝对只重叠一像素，窄字框的相对交叠过大仍不确定。
        self.assertIsNone(line_from_words([word('文', 5, width=10, height=20),
                                          word('件', 14, width=10, height=20)], 100, 100))

    def test_word_spaces_are_not_silently_trimmed_into_a_different_title(self):
        for text in (' 文', '文 ', '\t文', '文\u3000'):
            with self.subTest(text=text):
                self.assertIsNone(line_from_words([word(text)], 100, 100))
        self.assertEqual(line_from_words([word('文  件', width=40)], 100, 100)[0], '文  件')

    def test_words_from_nonoverlapping_vertical_bands_cannot_be_joined(self):
        for y in (11, 50):
            with self.subTest(y=y):
                self.assertIsNone(line_from_words([word('文'), word('件', x=12, y=y)], 100, 100))
        self.assertEqual(line_from_words([word('文'), word('件', x=12, y=3)], 100, 100)[0], '文件')

    def test_empty_excessive_and_oversized_word_data_reject_whole_line(self):
        for words in ([], [word('文')] * 513, [word('文' * 513)]):
            with self.subTest(count=len(words)):
                self.assertIsNone(line_from_words(words, 100, 100))
        self.assertIsNone(line_from_words([word('文' * 256, width=1),
                                          word('件' * 256, x=8, width=1)], 100, 100))

    def test_recognize_uses_word_geometry_and_keeps_two_output_lines(self):
        class Line:
            def __init__(self, words):
                self.words = words

            @property
            def text(self):
                raise AssertionError('引擎自动插空格的 line.text 不能参与匹配')

        result = SimpleNamespace(lines=[
            Line([word('文'), word('件', x=12)]),
            Line([word('发', y=20), word('送', x=12, y=20)]),
            Line([word('坏'), word('行', x=12, y=40)]),
        ])
        operation = SimpleNamespace(status=1, get_results=lambda: result)
        bitmap = SimpleNamespace(close=lambda: None)
        engine = SimpleNamespace(recognize_async=lambda image: operation)
        writer = SimpleNamespace(write_bytes=lambda pixels: None,
                                 detach_buffer=lambda: object(), close=lambda: None)
        replacements = {
            'winsdk.windows.graphics.imaging': SimpleNamespace(
                SoftwareBitmap=SimpleNamespace(create_copy_from_buffer=lambda *args: bitmap),
                BitmapPixelFormat=SimpleNamespace(BGRA8=1),
                BitmapAlphaMode=SimpleNamespace(IGNORE=1)),
            'winsdk.windows.media.ocr': SimpleNamespace(OcrEngine=SimpleNamespace(
                available_recognizer_languages=[SimpleNamespace(language_tag='zh-CN')],
                try_create_from_language=lambda language: engine,
                max_image_dimension=4096)),
            'winsdk.windows.storage.streams': SimpleNamespace(DataWriter=lambda: writer),
        }
        with patch.dict(sys.modules, replacements):
            rows = asyncio.run(recognize(bytes(100 * 100 * 4), 100, 100))
        self.assertEqual(rows, [['文件', 1, 1, 21, 10], ['发送', 1, 20, 21, 10]])
