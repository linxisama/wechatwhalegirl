"""中等字号标题的多变体聚合、分级回退与重采样；全部用合成帧和假 OCR，不碰真实微信。"""
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import src.wechat_pixel_gui as pixel_gui
from src.wechat_pixel_gui import (_MEDIUM_TITLE_FALLBACK_PASSES, _MEDIUM_TITLE_PASSES,
                                  _TITLE_PASSES, TITLE_READ_BUDGET, _pass_variants,
                                  _title_ink_region, _title_pass_families, _title_passes,
                                  readonly_gui_class)
from tests import test_pixel_title_passes1321 as fixture

BAND = (311, 0, 659, 93)  # 与 test_pixel_title_passes1321 同一夹具实测标题区。
TITLE = '鲸鱼娘二号'


def medium_frame(frame=None):
    """墨迹高 18px 的合成帧：只有落在 16-24px 区间才走中等字号表。"""
    owned = frame is None
    image = fixture.fixture_frame() if owned else frame
    draw = ImageDraw.Draw(image)
    for index in range(5):
        x = 330 + index * 20
        draw.rectangle((x, 20, x + 15, 37), fill='black')  # 高 18、字距 4，整行 330-425
    return image


def legacy_frame():
    """墨迹高 26px 的合成帧：25px 以上仍旧走 (4,150)(6,150)(8,150) 旧表。"""
    image = fixture.fixture_frame()
    fixture.draw_glyphs(image, 330, 20, 5)
    return image


class ScriptedOCR:
    """按变体顺序返回固定词框：只模拟「读到什么」，不参与挑选结果。

    变体按族展开后的顺序排（主族三份在前、回退族三份在后），没给到的变体当作 0 行，
    这样每族都会真的跑到，断言里也就能看出是哪一族胜出。
    """

    def __init__(self, ink, variants, texts, box, width):
        self.ink, self.variants, self.texts = ink, variants, list(texts)
        self.box, self.width = box, width
        self.calls = []

    def recognize(self, image):
        index = len(self.calls)
        self.calls.append((image.size, image.mode))
        text = self.texts[index] if index < len(self.texts) else None
        if not text:
            return []
        scale, _threshold, padding, _resample = self.variants[index]
        left, top, height = self.box
        return [(text,
                 padding + round((left - self.ink[0]) * scale),
                 padding + round((top - self.ink[1]) * scale),
                 round(self.width * scale),
                 round(height * scale))]


class MediumTitleTests(unittest.TestCase):
    def make_gui(self, frame, provider):
        gui = readonly_gui_class(fixture.QuietBase)(hwnd=44, provider=provider)
        gui._pixel_frame = lambda: (frame.copy(), (0, 0, frame.width, frame.height))
        return gui

    def plan(self, frame):
        """本次读取真的会依次尝试的变体表（按族展开），不是写死的 3 或 6。"""
        ink = _title_ink_region(frame, BAND)
        return ink, [variant for _family, family in _title_pass_families(_title_passes(frame, ink)[0])
                     for variant in family]

    def medium_ocr(self, frame, texts, width=96, box=(330, 20, 18)):
        ink, variants = self.plan(frame)
        return ScriptedOCR(ink, variants, texts, box, width), ink

    def families_seen(self, evidence):
        return [row['family'] for row in evidence['passes']]

    def test_three_measured_variants_reading_the_same_line_verify_the_title(self):
        with medium_frame() as frame:
            provider, _ink = self.medium_ocr(frame, [TITLE, TITLE, TITLE])
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), TITLE)
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'verified')
            self.assertEqual(evidence['profile'], 'medium')
            self.assertEqual(evidence['glyph_height'], 18)
            self.assertEqual(len(evidence['passes']), 3)
            self.assertTrue(all(row['complete'] for row in evidence['passes']))
            # 主族三份一致即采信，不必再花回退族的预算。
            self.assertEqual(evidence['family'], 'medium')
            self.assertEqual(evidence['families_tried'], 1)
        self.assertEqual(len(provider.calls), 3)

    def test_one_empty_variant_leaves_only_two_reads_and_fails(self):
        with medium_frame() as frame:
            provider, _ink = self.medium_ocr(frame, [TITLE, None, TITLE])
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '')
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'incomplete')
            self.assertEqual([row['rows'] for row in evidence['passes'][:3]], [1, 0, 1])
            # 空变体不再提前判死，主族的三个变体都会被跑到；主族只有两份读数、作废后
            # 剩下两族各自跑满，所以证据里三族各三行。三族都作废才报 incomplete。
            self.assertEqual(self.families_seen(evidence),
                             ['medium'] * 3 + ['medium_fallback'] * 3
                             + ['medium_binarized'] * 3)
        self.assertEqual(len(provider.calls), 9)

    def test_two_complete_reads_disagreeing_fail_as_disagreement(self):
        with medium_frame() as frame:
            provider, _ink = self.medium_ocr(frame, [TITLE, '鲸鱼娘三号', TITLE])
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '')
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'disagreement')
            self.assertTrue(all(row['complete'] for row in evidence['passes']
                                if row['family'] == 'medium'))
            # 诊断里只留计数，不留任何读到的会话名。
            self.assertNotIn('鲸鱼', str(evidence))

    def test_all_variants_empty_report_ocr_empty(self):
        with medium_frame() as frame:
            provider, _ink = self.medium_ocr(frame, [None, None, None])
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '')
            self.assertEqual(gui.title_diagnostic()['state'], 'ocr_empty')

    def test_incomplete_read_voids_its_family_before_the_remaining_variants(self):
        with medium_frame() as frame:
            # 只覆盖前四个字：右边界外还有墨迹，这一份读数必须当场作废；本族凑不出三份
            # 旁证，剩下的变体不再跑，直接换下一族。两族各自的第一份都不完整，所以每族
            # 只跑了一次——这正是"族内三份齐全才可能通过"的最小代价。
            provider, _ink = self.medium_ocr(frame, [TITLE, TITLE, TITLE], width=76)
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '')
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'incomplete')
            self.assertEqual(self.families_seen(evidence),
                             ['medium', 'medium_fallback',
                              'medium_binarized', 'medium_binarized', 'medium_binarized'])
            self.assertFalse(any(row['complete'] for row in evidence['passes']))
        self.assertEqual(len(provider.calls), 5)

    def test_legacy_profile_keeps_the_old_table_and_nearest_resampling(self):
        with legacy_frame() as frame:
            ink = _title_ink_region(frame, BAND)
            passes, height = _title_passes(frame, ink)
            self.assertEqual(height, 26)
            self.assertEqual(passes, ((4, 150, 0), (6, 150, 0), (8, 150, 0)))
            self.assertEqual([variant[3] for variant in _pass_variants(passes)],
                             ['nearest', 'nearest', 'nearest'])
            # 旧式表只有一族：回退族只服务于中等字号档，30px 以上的行为一个字都不动。
            self.assertEqual([name for name, _family in _title_pass_families(passes)],
                             ['legacy'])
            provider, _ink = self.medium_ocr(frame, [TITLE, TITLE, TITLE], width=68,
                                             box=(330, 20, 14))
            gui = self.make_gui(frame, provider)
            recorded = []
            real = Image.Image.resize
            def spy(image, size, resample=None, **kwargs):
                recorded.append((size, resample))
                return real(image, size, resample, **kwargs)
            with patch.object(Image.Image, 'resize', spy):
                self.assertEqual(gui.current_chat_title(), TITLE)
            self.assertEqual(gui.title_diagnostic()['profile'], 'legacy')
        width, height_ = ink[2] - ink[0], ink[3] - ink[1]
        self.assertEqual([size for size, _ in recorded],
                         [(round(width * scale), round(height_ * scale)) for scale in (4, 6, 8)])
        self.assertEqual({resample for _, resample in recorded}, {Image.Resampling.NEAREST})

    def test_medium_table_mixes_pixel_faithful_and_smooth_measurements(self):
        # 主族只留平滑放大：本机 1266x749 帧上带硬阈值 150 的两个变体都读成错字，
        # 而平滑不二值化在 1.5-2.5 倍全对，所以主族三份都是 smooth 且不带阈值。
        self.assertEqual(_MEDIUM_TITLE_PASSES,
                         ((1.75, None, 0, 'smooth'), (2, None, 0, 'smooth'),
                          (2.5, None, 0, 'smooth')))
        self.assertEqual({variant[3] for variant in _MEDIUM_TITLE_PASSES}, {'smooth'})
        self.assertEqual({variant[1] for variant in _MEDIUM_TITLE_PASSES}, {None})
        self.assertEqual({variant[2] for variant in _MEDIUM_TITLE_PASSES}, {0})
        # 族内三份倍率互不相同，同一种错读不会被复制三份。
        self.assertEqual(len({variant[0] for variant in _MEDIUM_TITLE_PASSES}), 3)
        # 旧式表没有重采样字段时补 'nearest'，历史行为不变。
        self.assertEqual(_pass_variants(tuple((s, t, 0) for s, t in _TITLE_PASSES)),
                         tuple((s, t, 0, 'nearest') for s, t in _TITLE_PASSES))

    def test_resample_parameter_reaches_pil_resize(self):
        provider = Mock()
        provider.recognize.return_value = []
        recorded = []
        real = Image.Image.resize
        def spy(image, size, resample=None, **kwargs):
            recorded.append(resample)
            return real(image, size, resample, **kwargs)
        with medium_frame() as frame:
            gui = self.make_gui(frame, provider)
            ink = _title_ink_region(frame, BAND)
            with patch.object(Image.Image, 'resize', spy):
                gui._ocr_crop(frame, ink, scale=2, threshold=150, padding=0, resample='nearest')
                gui._ocr_crop(frame, ink, scale=2, threshold=None, padding=0, resample='smooth')
        self.assertEqual(recorded, [Image.Resampling.NEAREST, Image.Resampling.LANCZOS])
        self.assertNotEqual(recorded[0], recorded[1])

    def test_medium_variants_keep_colour_only_where_binarization_is_off(self):
        with medium_frame() as frame:
            provider, _ink = self.medium_ocr(frame, [TITLE, TITLE, TITLE])
            self.make_gui(frame, provider).current_chat_title()
        # 主族三份都不二值化，全部保持真彩色；只有回退族的第一项才会变成灰度 'L'。
        self.assertEqual([mode for _size, mode in provider.calls], ['RGB', 'RGB', 'RGB'])

    def test_fallback_family_is_the_measured_table_and_the_second_trust_level(self):
        # 回退族保留 1118x809 沙盒帧上 136+48 组网格里唯一读得全的原始三变体（含一份
        # 硬阈值 150 的 NEAREST）：主族换成本机帧的平滑三份之后，这一族仍覆盖沙盒帧。
        self.assertEqual(_MEDIUM_TITLE_FALLBACK_PASSES,
                         ((2, 150, 0, 'nearest'), (2, None, 0, 'smooth'),
                          (3, None, 0, 'smooth')))
        # 每族内部三份倍率互不相同；族与族之间允许复用同一份实测变体。
        self.assertEqual([name for name, _family in
                          _title_pass_families(_MEDIUM_TITLE_PASSES)],
                         ['medium', 'medium_fallback', 'medium_binarized'])
        self.assertEqual(_title_pass_families(_MEDIUM_TITLE_PASSES)[0][1],
                         _MEDIUM_TITLE_PASSES)
        self.assertEqual(_title_pass_families(_MEDIUM_TITLE_PASSES)[1][1],
                         _MEDIUM_TITLE_FALLBACK_PASSES)

    def test_empty_main_family_falls_back_to_three_agreeing_reads(self):
        with medium_frame() as frame:
            provider, _ink = self.medium_ocr(frame, [None, None, None,
                                                     TITLE, TITLE, TITLE])
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), TITLE)
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'verified')
            self.assertEqual(evidence['family'], 'medium_fallback')
            self.assertEqual(evidence['families_tried'], 2)
            self.assertEqual(self.families_seen(evidence),
                             ['medium'] * 3 + ['medium_fallback'] * 3)
            self.assertTrue(all(row['complete'] for row in evidence['passes']
                                if row['family'] == 'medium_fallback'))

    def test_one_unwitnessed_wrong_read_cannot_veto_three_agreeing_fallback_reads(self):
        with medium_frame() as frame:
            # 主族里只有一份错字读数（另两份读不出），它没有旁证，不能否决回退族的三份一致。
            provider, _ink = self.medium_ocr(frame, ['鲸鱼娘三号', None, None,
                                                     TITLE, TITLE, TITLE])
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), TITLE)
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'verified')
            self.assertEqual(evidence['family'], 'medium_fallback')
            self.assertNotIn('鲸鱼娘三号', str(evidence))

    def test_all_families_failing_returns_no_title_and_keeps_reason_code(self):
        with medium_frame() as frame:
            # 主族一份也读不出、回退族只有两份一致、二值化族一行都没有：三族全作废，
            # 不许拼接、不许放行，也不许退回任何一族的局部读数。
            provider, _ink = self.medium_ocr(frame, [None, None, None,
                                                     TITLE, None, TITLE])
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '')
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'incomplete')
            self.assertEqual(evidence['family'], '')
            self.assertEqual(evidence['families_tried'], 3)

    def test_read_budget_between_variants_stops_and_reports_ocr_budget(self):
        with medium_frame() as frame:
            provider, _ink = self.medium_ocr(frame, [TITLE] * 6)
            gui = self.make_gui(frame, provider)
            clock = {'now': 0.0}
            scripted = provider.recognize
            def tick(image):
                # 每跑完一个变体就把时钟推过预算：下一个变体之前必须被拦下。
                clock['now'] += TITLE_READ_BUDGET + 1
                return scripted(image)
            with patch.object(pixel_gui.time, 'monotonic', lambda: clock['now']), \
                 patch.object(provider, 'recognize', tick):
                self.assertEqual(gui.current_chat_title(), '')
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'ocr_budget')
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(evidence['family'], '')

    def test_read_budget_before_the_first_variant_costs_no_ocr(self):
        with medium_frame() as frame:
            provider, _ink = self.medium_ocr(frame, [TITLE] * 6)
            gui = self.make_gui(frame, provider)
            # 第一次取时钟是算截止时间（0），此后每次取时钟都已越过预算。
            clock = iter((0.0, TITLE_READ_BUDGET + 1))
            with patch.object(pixel_gui.time, 'monotonic',
                              lambda: next(clock, TITLE_READ_BUDGET + 1)):
                self.assertEqual(gui.current_chat_title(), '')
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'ocr_budget')
            self.assertEqual(evidence['passes'], [])
            self.assertEqual(len(provider.calls), 0)

    def test_budget_constant_is_environment_overridable_but_never_unbounded(self):
        self.assertEqual(TITLE_READ_BUDGET, 8.0)
        self.assertEqual(pixel_gui._title_read_budget(), TITLE_READ_BUDGET)
        with patch.dict('os.environ', {'WHALE_TITLE_READ_BUDGET': '21'}):
            self.assertEqual(pixel_gui._title_read_budget(), 21.0)
        for value in ('not-a-number', '0', '-3'):
            with patch.dict('os.environ', {'WHALE_TITLE_READ_BUDGET': value}):
                self.assertEqual(pixel_gui._title_read_budget(), TITLE_READ_BUDGET)


if __name__ == '__main__':
    unittest.main()
