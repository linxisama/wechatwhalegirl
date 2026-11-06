"""1.3.90 标题读取健壮性：去早停、远端角标截断、墨迹裁剪图；全用合成帧和假 OCR。"""
import unittest

from PIL import ImageDraw

from src.wechat_pixel_gui import (_title_candidates, _title_ink_region, _title_passes,
                                  _title_words, readonly_gui_class)
from tests import test_pixel_title_passes1321 as fixture

BAND = (311, 0, 659, 93)  # 与 test_pixel_title_passes1321 同一夹具实测标题区。
TITLE = '鲸鱼娘二号'
TITLE_BOX = (330, 20, 96, 18)  # 五个 15px 宽的字块，帧坐标；完整性检查要求的整行范围。
BADGE_BOX = (560, 20, 18, 18)  # 右上角未读角标：与标题同一行、间距远超词间距。


def medium_frame(badge=False):
    """墨迹高 18px 的合成帧；badge=True 时再画一块远处的角标墨迹。"""
    image = fixture.fixture_frame()
    draw = ImageDraw.Draw(image)
    for index in range(5):
        x = 330 + index * 20
        draw.rectangle((x, 20, x + 15, 37), fill='black')
    if badge:
        left, top, width, height = BADGE_BOX
        draw.rectangle((left, top, left + width - 1, top + height - 1), fill='black')
    return image


class ScriptedOCR:
    """按候选序号作答：给了答案就按该候选的倍率换算词框，没给就是 0 行。

    词框先按候选自己的倍率与白边放大，再交给 _ocr_crop 缩回帧坐标，走的正是真实
    recognize 看到的那张图；这样"脚本答案对但坐标对不上"不会假通过。
    """

    def __init__(self, ink, variants, plan):
        self.ink, self.variants, self.plan = ink, variants, dict(plan)
        self.calls = []

    def recognize(self, image):
        index = len(self.calls)
        self.calls.append(index)
        action = self.plan.get(index)
        if not action:
            return []
        scale, _threshold, padding, _resample = self.variants[index]
        return [(text,
                 padding + round((left - self.ink[0]) * scale),
                 padding + round((top - self.ink[1]) * scale),
                 round(width * scale),
                 round(height * scale))
                for text, (left, top, width, height) in action]


class TitleVoteTests(unittest.TestCase):
    def make_gui(self, frame, provider):
        gui = readonly_gui_class(fixture.QuietBase)(hwnd=44, provider=provider)
        gui._pixel_frame = lambda: (frame.copy(), (0, 0, frame.width, frame.height))
        return gui

    def plan(self, frame):
        ink = _title_ink_region(frame, BAND)
        return ink, _title_candidates(_title_passes(frame, ink)[0])

    def test_every_candidate_is_tried_before_the_votes_are_counted(self):
        with medium_frame() as frame:
            ink, variants = self.plan(frame)
            # 前三份就逐字一致：1.3.89 的早停会在第三份之后收工。
            provider = ScriptedOCR(ink, variants, {index: [(TITLE, TITLE_BOX)]
                                                   for index in range(3)})
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), TITLE)
            evidence = gui.title_diagnostic()
        self.assertEqual(evidence['state'], 'verified')
        self.assertEqual(evidence['family'], 'medium')
        # 去早停：票数只在候选表跑完后才算，先到的读数不能锁死结果。
        self.assertEqual(len(provider.calls), len(variants))
        self.assertEqual(len(evidence['passes']), len(variants))

    def test_a_late_majority_overturns_the_reading_that_arrived_first(self):
        # 真机「极客先锋社招新群（77）」的形态：错读先攒到 5 票（旧早停就此锁死），
        # 正确读法随后才攒到 9 票。去早停之后，冠军必须是票数最多的那个。
        with medium_frame() as frame:
            ink, variants = self.plan(frame)
            plan = {index: [('鲸鱼娘三号', TITLE_BOX)] for index in range(5)}
            plan.update({index: [(TITLE, TITLE_BOX)] for index in range(5, 14)})
            provider = ScriptedOCR(ink, variants, plan)
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), TITLE)
            evidence = gui.title_diagnostic()
        self.assertEqual(evidence['state'], 'verified')
        self.assertEqual(len(provider.calls), len(variants))

    def test_a_far_badge_is_not_appended_to_the_title(self):
        with medium_frame(badge=True) as frame:
            ink, variants = self.plan(frame)
            plan = {index: [(TITLE, TITLE_BOX), ('0', BADGE_BOX)]
                    for index in range(len(variants))}
            provider = ScriptedOCR(ink, variants, plan)
            gui = self.make_gui(frame, provider)
            title = gui.current_chat_title()
            evidence = gui.title_diagnostic()
        self.assertEqual(title, TITLE)
        self.assertFalse(title.endswith(' '))
        self.assertNotIn('0', title)
        self.assertEqual(evidence['state'], 'verified')


class TitleWordsTests(unittest.TestCase):
    """_title_words 的间距规则：中等间距插空格，远端元素直接截断。"""

    def test_a_medium_gap_still_joins_with_one_space(self):
        rows = [('鲸鱼', 330, 20, 26, 18), ('二号', 362, 20, 26, 18)]
        self.assertEqual(_title_words(rows), '鲸鱼 二号')

    def test_a_gap_beyond_two_glyph_heights_ends_the_line(self):
        rows = [('鲸鱼娘二号', 330, 20, 96, 18), ('0', 560, 20, 18, 18)]
        self.assertEqual(_title_words(rows), '鲸鱼娘二号')

    def test_the_truncated_tail_is_dropped_entirely(self):
        # 角标之后还有别的框：它们同样不属于标题，一个字符都不能留下。
        rows = [('鲸鱼娘二号', 330, 20, 96, 18), ('0', 560, 20, 18, 18),
                ('9', 600, 20, 18, 18)]
        self.assertEqual(_title_words(rows), '鲸鱼娘二号')

    def test_overlapping_boxes_still_reject_the_whole_line(self):
        rows = [('鲸鱼娘二号', 330, 20, 96, 18), ('二号', 340, 20, 26, 18)]
        self.assertEqual(_title_words(rows), '')


class TitleInkImageTests(unittest.TestCase):
    def make_gui(self, frame, provider):
        gui = readonly_gui_class(fixture.QuietBase)(hwnd=44, provider=provider)
        gui._pixel_frame = lambda: (frame.copy(), (0, 0, frame.width, frame.height))
        return gui

    def plan(self, frame):
        ink = _title_ink_region(frame, BAND)
        return ink, _title_candidates(_title_passes(frame, ink)[0])

    def test_never_read_returns_none(self):
        with medium_frame() as frame:
            provider = ScriptedOCR(*self.plan(frame), {})
            self.assertIsNone(self.make_gui(frame, provider).title_ink_image())

    def test_the_crop_matches_the_measured_ink_region(self):
        with medium_frame() as frame:
            ink, variants = self.plan(frame)
            provider = ScriptedOCR(ink, variants, {index: [(TITLE, TITLE_BOX)]
                                                   for index in range(len(variants))})
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), TITLE)
            image = gui.title_ink_image()
        self.assertIsNotNone(image)
        self.assertEqual(image.mode, 'L')
        self.assertEqual(image.size, (ink[2] - ink[0], ink[3] - ink[1]))
        # frame 是上下文管理器，退出后仍要能取像素：裁剪图必须已经脱离源帧。
        self.assertIsNotNone(image.getbbox())

    def test_a_read_that_never_reaches_the_ink_leaves_no_stale_crop(self):
        with medium_frame() as frame:
            ink, variants = self.plan(frame)
            provider = ScriptedOCR(ink, variants, {index: [(TITLE, TITLE_BOX)]
                                                   for index in range(len(variants))})
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), TITLE)
            self.assertIsNotNone(gui.title_ink_image())
            # 第二次连标题区都定不出来（版式变动）：不能把上一次的墨迹留给调用方。
            gui._title_region_cached = lambda _frame, _info: None
            self.assertEqual(gui.current_chat_title(), '')
            self.assertEqual(gui.title_diagnostic()['state'], 'header_missing')
            self.assertIsNone(gui.title_ink_image())


if __name__ == '__main__':
    unittest.main()
