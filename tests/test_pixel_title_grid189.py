"""1.3.89 标题表决网格：候选表构成、逐变体投票、早停、异常与预算边界；全用合成帧。"""
import unittest
from unittest.mock import patch

from PIL import ImageDraw

import src.wechat_pixel_gui as pixel_gui
from src.isolated_wechat.protocol import ProtocolError
from src.wechat_pixel_gui import (_BOUNDARY_TITLE_PASSES, _MEDIUM_TITLE_PASSES,
                                  _SMALL_TITLE_PASSES, _TITLE_BASE_GRID, _TITLE_PASSES,
                                  _title_candidates, _title_ink_region, _title_pass_families,
                                  _title_passes, readonly_gui_class)
from tests import test_pixel_title_passes1321 as fixture

BAND = (311, 0, 659, 93)  # 与 test_pixel_title_passes1321 同一夹具实测标题区。
TITLE = '鲸鱼娘二号'
BOX = (330, 20, 96, 18)  # 五个 15px 宽的字块，帧坐标；完整性检查要求的整行范围。


def medium_frame():
    """墨迹高 18px 的合成帧：只有落在 18-24px 才走 medium 档的实测表。"""
    image = fixture.fixture_frame()
    draw = ImageDraw.Draw(image)
    for index in range(5):
        x = 330 + index * 20
        draw.rectangle((x, 20, x + 15, 37), fill='black')
    return image


class GridOCR:
    """按候选序号作答：plan 里给了就按该候选的倍率换算词框，没给就是 0 行。

    词框先按该候选自己的倍率与白边放大，再交给 _ocr_crop 缩回帧坐标，走的正是真实
    recognize 看到的那张图；这样"脚本答案对但坐标对不上"不会假通过。
    """

    def __init__(self, ink, variants, plan):
        self.ink, self.variants, self.plan = ink, variants, dict(plan)
        self.calls = []

    def recognize(self, image):
        index = len(self.calls)
        self.calls.append(index)
        action = self.plan.get(index)
        if action is None:
            return []
        if isinstance(action, Exception):
            raise action
        text, (left, top, width, height) = action
        scale, _threshold, padding, _resample = self.variants[index]
        return [(text,
                 padding + round((left - self.ink[0]) * scale),
                 padding + round((top - self.ink[1]) * scale),
                 round(width * scale),
                 round(height * scale))]


class TitleGridTests(unittest.TestCase):
    def make_gui(self, frame, provider):
        gui = readonly_gui_class(fixture.QuietBase)(hwnd=44, provider=provider)
        gui._pixel_frame = lambda: (frame.copy(), (0, 0, frame.width, frame.height))
        return gui

    def plan(self, frame):
        ink = _title_ink_region(frame, BAND)
        passes = _title_passes(frame, ink)[0]
        return ink, _title_candidates(passes), passes

    def scripted(self, frame, plan):
        ink, variants, _passes = self.plan(frame)
        return GridOCR(ink, variants, plan), variants

    def profile_variants(self, passes):
        """profile 族按族顺序展开并去重：候选表的前半段必须是这一串。"""
        seen = []
        for _family, variants in _title_pass_families(passes):
            for variant in variants:
                if variant not in seen:
                    seen.append(variant)
        return seen

    def test_candidates_put_the_measured_profile_first_and_the_48_grid_after_it(self):
        legacy = tuple((scale, threshold, 0) for scale, threshold in _TITLE_PASSES)
        for passes in (_MEDIUM_TITLE_PASSES, _BOUNDARY_TITLE_PASSES,
                       _SMALL_TITLE_PASSES, legacy):
            candidates = _title_candidates(passes)
            profile = self.profile_variants(passes)
            # 长度是并集而不是和：基础网格与实测 profile 表本来就有重叠（medium 重叠 6 组）。
            grid_only = [variant for variant in _TITLE_BASE_GRID if variant not in profile]
            self.assertEqual(candidates, tuple(profile) + tuple(grid_only))
            self.assertEqual(len(candidates), len(set(candidates)))
            self.assertEqual(len(_TITLE_BASE_GRID), 48)
            for variant in _TITLE_BASE_GRID:
                self.assertIn(variant, candidates)
            # 实测优证据排在最前：真机上多数帧几百毫秒内就能收敛。
            self.assertEqual(candidates[0], profile[0])

    def test_three_agreeing_reads_verify_the_title_only_after_the_whole_grid(self):
        with medium_frame() as frame:
            provider, variants = self.scripted(frame, {index: (TITLE, BOX) for index in range(3)})
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), TITLE)
            evidence = gui.title_diagnostic()
        self.assertEqual(evidence['state'], 'verified')
        self.assertEqual(evidence['family'], 'medium')
        self.assertEqual(evidence['families_tried'], 3)
        self.assertEqual([row['family'] for row in evidence['passes'][:3]], ['medium'] * 3)
        self.assertTrue(all(row['complete'] for row in evidence['passes'][:3]))
        # 1.3.90 去早停：三票到手也要把候选表跑完，否则真机上先到的错读数会锁死结果。
        self.assertEqual(len(provider.calls), len(variants))

    def test_two_votes_against_one_keep_reading_and_end_as_disagreement(self):
        with medium_frame() as frame:
            provider, variants = self.scripted(frame, {0: (TITLE, BOX), 1: (TITLE, BOX),
                                                       2: ('鲸鱼娘三号', BOX)})
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '')
            evidence = gui.title_diagnostic()
        # 2 票领先 1 票不达标（要 3 票且领先 2 票），所以候选表必须跑满。
        self.assertEqual(evidence['state'], 'disagreement')
        self.assertEqual(len(provider.calls), len(variants))

    def test_a_grid_variant_that_reads_the_whole_line_can_win_alone(self):
        with medium_frame() as frame:
            ink, variants, passes = self.plan(frame)
            start = len(self.profile_variants(passes))
            provider = GridOCR(ink, variants, {index: (TITLE, BOX)
                                               for index in range(start, start + 3)})
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), TITLE)
            evidence = gui.title_diagnostic()
        self.assertEqual(evidence['state'], 'verified')
        # profile 三族一份都没读出整行，胜出的是基础网格里的候选，族名记为 grid。
        self.assertEqual(evidence['family'], 'grid')
        self.assertEqual(evidence['families_tried'], 3)
        self.assertEqual([row['family'] for row in evidence['passes']][:start],
                         ['medium'] * 3 + ['medium_fallback'] * 2 + ['medium_binarized'] * 3)
        self.assertEqual({row['family'] for row in evidence['passes'][start:]}, {'grid'})
        self.assertEqual(len(provider.calls), len(variants))

    def test_an_oversized_crop_is_skipped_instead_of_aborting_the_read(self):
        with medium_frame() as frame:
            provider, variants = self.scripted(frame, {0: ProtocolError('OCR 区域超过限制'),
                                                       1: (TITLE, BOX), 2: (TITLE, BOX),
                                                       3: (TITLE, BOX)})
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), TITLE)
            evidence = gui.title_diagnostic()
        self.assertEqual(evidence['state'], 'verified')
        # 顶到 MAX_DIMENSION 的那份只是 0 行，不该中断整次读取。
        self.assertEqual(evidence['passes'][0]['rows'], 0)
        self.assertEqual(len(provider.calls), len(variants))

    def test_other_protocol_errors_still_abort_the_read(self):
        with medium_frame() as frame:
            provider, _variants = self.scripted(frame, {0: ProtocolError('OCR 进程已退出')})
            gui = self.make_gui(frame, provider)
            with self.assertRaises(ProtocolError):
                gui.current_chat_title()
            evidence = gui.title_diagnostic()
        self.assertEqual(evidence['exception_type'], 'ProtocolError')

    def test_exhausted_budget_returns_no_title_and_crops_nothing(self):
        with medium_frame() as frame:
            provider, _variants = self.scripted(frame, {})
            gui = self.make_gui(frame, provider)
            with patch.object(pixel_gui, '_title_read_budget', lambda: 0):
                self.assertEqual(gui.current_chat_title(), '')
            evidence = gui.title_diagnostic()
        self.assertEqual(evidence['state'], 'ocr_budget')
        self.assertEqual(evidence['passes'], [])
        self.assertEqual(provider.calls, [])


if __name__ == '__main__':
    unittest.main()
