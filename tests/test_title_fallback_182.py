"""无分隔线版式（微信 4.1.15.13）的标题带定位与小字号标题族；合成帧 + 真实回包帧。"""
import os
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

import src.wechat_pixel_gui as pixel_gui
from src.wechat_layout import title_region, title_region_fallback
from src.wechat_pixel_gui import (_SMALL_TITLE_PASSES, _complete_title_pixels,
                                  _title_ink_region, _title_pass_families,
                                  _title_passes, readonly_gui_class)
from tests import test_pixel_title_passes1321 as fixture

FIXTURE = Path(__file__).parent / 'fixtures/wechat-415-titlebar.png'
PANE_LEFT = 308
TITLE = '文件传输助手'


def band_frame(title_height=12, title_top=50, title_left=324, wide=None,
               wide_top=36, divider=True):
    """4.1.15 版式的合成帧：纯白正文、标题栏与正文之间没有横线，只有面板竖线。"""
    image = Image.new('RGB', (1200, 800), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1199, 33), fill=(224, 224, 224))
    if divider:
        draw.rectangle((PANE_LEFT, 33, PANE_LEFT, 799), fill=(220, 220, 220))
    if wide is not None:
        draw.rectangle((PANE_LEFT + 4, wide_top, PANE_LEFT + 4 + wide,
                        wide_top + title_height - 1), fill=(30, 30, 30))
    draw.rectangle((title_left, title_top, title_left + 83,
                    title_top + title_height - 1), fill=(30, 30, 30))
    return image


class BandOCR:
    """只把整块墨迹报成一整行；用来证明族的选择与聚合，不模拟识别质量。"""

    def __init__(self, variants, text=TITLE):
        self.variants, self.text, self.calls = variants, text, []

    def recognize(self, image):
        # 1.3.89 起候选表是 profile 族 + 48 组基础网格，脚本答案用完后一律当作"读不出"，
        # 否则 Mock 侧会以 IndexError 中断而不是模拟这一份没出力。
        index = len(self.calls)
        if index >= len(self.variants):
            self.calls.append(image.size)
            return []
        scale, _threshold, padding, _resample = self.variants[index]
        self.calls.append(image.size)
        width, height = image.size
        return [(self.text, padding, padding,
                 width - 2 * padding, height - 2 * padding)]


class HeaderFallbackTests(unittest.TestCase):
    def test_band_is_the_first_title_row_below_the_pane_divider(self):
        frame = band_frame()
        band = title_region_fallback(frame, PANE_LEFT)
        self.assertIsNotNone(band)
        left, top, right, bottom = band
        self.assertEqual(left, PANE_LEFT + 4)
        self.assertLessEqual(top, 50)
        self.assertGreaterEqual(bottom, 62)
        self.assertEqual(right, PANE_LEFT + int((1200 - PANE_LEFT) * .60))

    def test_strict_title_region_still_needs_the_divider_line(self):
        # 同一帧没有标题横线：strict 必须返回 None，回退才接手。
        self.assertIsNone(title_region(band_frame(), PANE_LEFT))

    def test_no_band_without_a_verified_pane_divider(self):
        self.assertIsNone(title_region_fallback(band_frame(divider=False), PANE_LEFT))

    def test_wide_ink_run_is_not_taken_for_a_title(self):
        # 面板里最上面那段（通知条）比半个面板还宽，必须跳过它取下面那段标题。
        frame = band_frame(wide=320, wide_top=36, title_top=54)
        band = title_region_fallback(frame, PANE_LEFT)
        self.assertIsNotNone(band)
        self.assertGreaterEqual(band[1], 46)
        self.assertLessEqual(band[1], 54)
        self.assertGreaterEqual(band[3], 66)

    def test_bands_merging_into_wide_ink_are_rejected(self):
        # 宽条与标题重叠成一段 22px 高的宽墨迹：既不是标题，也不能猜，只能拒绝。
        self.assertIsNone(title_region_fallback(band_frame(wide=320, wide_top=40,
                                                           title_top=50), PANE_LEFT))

    def test_runs_outside_the_title_height_are_skipped(self):
        self.assertIsNone(title_region_fallback(band_frame(title_height=60), PANE_LEFT))

    def test_bottom_limit_cuts_the_band_before_the_composer(self):
        bounded = title_region_fallback(band_frame(), PANE_LEFT, bottom_limit=60)
        self.assertIsNotNone(bounded)
        self.assertLessEqual(bounded[3], 60)

    def test_real_415_frame_reads_the_title_through_the_small_family(self):
        if not FIXTURE.is_file():
            self.skipTest('缺少 4.1.15 标题条夹具')
        with Image.open(FIXTURE) as opened:
            frame = opened.convert('RGB')
        band = title_region_fallback(frame, PANE_LEFT)
        self.assertIsNotNone(band)
        ink = _title_ink_region(frame, band)
        passes, height = _title_passes(frame, ink)
        self.assertEqual(height, 12)
        self.assertIs(passes, _SMALL_TITLE_PASSES)
        self.assertEqual([name for name, _ in _title_pass_families(passes)],
                         ['small', 'small_fallback'])

    @unittest.skipUnless(os.name == 'nt', 'Windows OCR only')
    def test_real_415_frame_three_small_variants_agree(self):
        from src.isolated_wechat.ocr_relay import LocalOCR
        if not LocalOCR.available():
            self.skipTest('本机没有可用的 Windows OCR 语言包')
        if not FIXTURE.is_file():
            self.skipTest('缺少 4.1.15 标题条夹具')
        with Image.open(FIXTURE) as opened:
            frame = opened.convert('RGB')
        band = title_region_fallback(frame, PANE_LEFT)
        ink = _title_ink_region(frame, band)
        passes, _height = _title_passes(frame, ink)
        readers = []
        for scale, threshold, padding, resample in pixel_gui._pass_variants(passes):
            gui = readonly_gui_class(fixture.QuietBase)(hwnd=44, provider=LocalOCR())
            rows = gui._ocr_crop(frame, ink, scale=scale, threshold=threshold,
                                 padding=padding, resample=resample)
            rows = [(str(text).strip(), x, y, w, h) for text, x, y, w, h in rows
                    if str(text).strip() and h >= 8 and x >= PANE_LEFT + 6
                    and y + h <= band[3] and x + w <= band[2]]
            self.assertTrue(rows)
            self.assertTrue(_complete_title_pixels(frame, band, rows))
            readers.append(''.join(row[0] for row in rows))
        self.assertEqual(set(readers), {TITLE})


class HeaderSourceTests(unittest.TestCase):
    def info(self, frame):
        return {'pane_left': PANE_LEFT, 'draft_box': (325, 582, 1104, 672),
                'box': (325, 582, 1104, 672), 'send_region': (951, 672, 1111, 703),
                'frame': frame.size}

    def make_gui(self, frame, provider):
        gui = readonly_gui_class(fixture.QuietBase)(hwnd=44, provider=provider)
        gui._pixel_frame = lambda: (frame.copy(), (0, 0, frame.width, frame.height))
        gui._inspect_composer = lambda _frame, _geometry: self.info(frame)
        return gui

    def test_divider_layout_keeps_the_strict_source(self):
        frame = fixture.fixture_frame()
        provider = BandOCR(((4, 150, 0, 'nearest'),) * 3, text='鲸鱼娘二号')
        gui = self.make_gui(frame, provider)
        gui.current_chat_title()
        self.assertEqual(gui._pixel_title_region_source, 'divider')

    def test_no_divider_layout_falls_back_to_header_ink_and_reads_the_title(self):
        frame = band_frame()
        variants = [variant for _name, family in
                    _title_pass_families(_SMALL_TITLE_PASSES) for variant in family]
        provider = BandOCR(variants)
        gui = self.make_gui(frame, provider)
        self.assertEqual(gui.current_chat_title(), TITLE)
        self.assertEqual(gui._pixel_title_region_source, 'header_ink')
        evidence = gui.title_diagnostic()
        self.assertEqual(evidence['state'], 'verified')
        self.assertEqual(evidence['profile'], 'small')
        self.assertEqual(evidence['family'], 'small')
        # 1.3.90 去早停：小字号族三票到手后候选表仍要跑满，脚本答案用完后一律读不出。
        ink = _title_ink_region(frame, gui._pixel_title_region)
        self.assertEqual(len(provider.calls),
                         len(pixel_gui._title_candidates(_title_passes(frame, ink)[0])))

    def test_fallback_still_refuses_when_no_header_evidence_exists(self):
        frame = Image.new('RGB', (1200, 800), (250, 250, 250))
        gui = self.make_gui(frame, BandOCR(()))
        with self.assertRaises(RuntimeError) as caught:
            gui.readonly_ready()
        self.assertIn('未确认聊天标题栏区域', str(caught.exception))
