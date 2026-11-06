"""只读像素定位与构造保护；全程生成图像，不操作真实微信。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from src.wechat_layout import inspect_composer, draft_state, draft_has_text, title_region
from src.wechat_ocr_labels import explain_send_button, locate_send_button
from src.wechat_pixel_gui import (readonly_gui_class, ocr_provider, _title_words,
                                   _title_candidates, _MEDIUM_TITLE_FALLBACK_PASSES,
                                   _MEDIUM_TITLE_PASSES, _TITLE_PASSES)
from src.wechat_guia_send import probe


def fixture_frame(*, toolbar=True, header=True):
    image = Image.new('RGB', (896, 648), 'white')
    draw = ImageDraw.Draw(image)
    draw.line((307, 30, 307, 647), fill=(220, 220, 220))
    if header:
        draw.line((308, 95, 895, 95), fill=(220, 220, 220))
    draw.rectangle((319, 524, 883, 628), outline=(220, 220, 220))
    if toolbar:
        for x in (331, 367, 403, 439, 487):
            draw.rectangle((x, 602, x + 15, 617), outline=(80, 80, 80), width=2)
    return image


class LayoutEvidenceTests(unittest.TestCase):
    def test_closed_frame_toolbar_and_header_provide_separate_evidence(self):
        with fixture_frame() as frame:
            info = inspect_composer(frame)
            self.assertTrue(info['confirmed'], info)
            self.assertGreaterEqual(info['draft_box'][0], info['pane_left'])
            self.assertLess(info['draft_box'][3], info['send_region'][1] + 1)
            band = title_region(frame, info['pane_left'])
            self.assertIsNotNone(band)
            self.assertLessEqual(band[3], 97)
            self.assertLess(band[2], 815)  # 右上窗口按钮不在候选区域。

    def test_missing_toolbar_or_closed_frame_never_uses_proportional_fallback(self):
        with fixture_frame(toolbar=False) as frame:
            self.assertFalse(inspect_composer(frame)['confirmed'])
        with Image.new('RGB', (896, 648), 'white') as frame:
            self.assertFalse(inspect_composer(frame)['confirmed'])

    def test_one_pixel_client_offset_does_not_hide_a_thin_pane_divider(self):
        with fixture_frame() as original:
            # 实际客户区比主窗少一像素边框时，分隔线的 x 奇偶性会改变。
            for box in ((0, 0, 894, 647), (1, 1, 895, 648), (2, 0, 896, 647)):
                with self.subTest(box=box), original.crop(box) as frame:
                    info = inspect_composer(frame)
                    self.assertTrue(info['confirmed'], info)
                    self.assertGreaterEqual(info['draft_box'][0], info['pane_left'])

    def test_close_sidebar_line_requires_independent_header_connection(self):
        with fixture_frame() as frame:
            # 旁边的滚动条也形成强竖线，但只有右侧面板竖线接上标题横线。
            ImageDraw.Draw(frame).line((295, 30, 295, 647), fill=(220, 220, 220))
            info = inspect_composer(frame)
            self.assertTrue(info['confirmed'], info)
            self.assertGreater(info['pane_left'], 300)
            self.assertTrue(info['layout']['pane_header_edges'])
        with fixture_frame(header=False) as frame:
            ImageDraw.Draw(frame).line((295, 30, 295, 647), fill=(220, 220, 220))
            self.assertFalse(inspect_composer(frame)['confirmed'])

    def test_two_header_connections_remain_ambiguous(self):
        with fixture_frame() as frame:
            draw = ImageDraw.Draw(frame)
            draw.line((285, 30, 285, 647), fill=(220, 220, 220))
            draw.line((286, 80, 895, 80), fill=(220, 220, 220))
            info = inspect_composer(frame)
            self.assertFalse(info['confirmed'], info)
            self.assertEqual(info['reason'], 'pane_divider_unconfirmed')

    def test_missing_header_cannot_treat_a_body_line_as_title(self):
        with fixture_frame(header=False) as frame:
            self.assertIsNone(title_region(frame, 307))

    def test_full_window_caption_edge_is_not_the_chat_header(self):
        with fixture_frame() as frame:
            ImageDraw.Draw(frame).line((0, 40, 895, 40), fill=(220, 220, 220))
            band = title_region(frame, 307)
            self.assertIsNotNone(band)
            self.assertGreater(band[1], 40)
            self.assertGreater(band[3], 90)
        with fixture_frame(header=False) as frame:
            ImageDraw.Draw(frame).line((0, 40, 895, 40), fill=(220, 220, 220))
            self.assertIsNone(title_region(frame, 307))

    def test_multiple_panel_header_edges_do_not_choose_an_arbitrary_crop(self):
        with fixture_frame() as frame:
            ImageDraw.Draw(frame).line((308, 80, 895, 80), fill=(220, 220, 220))
            self.assertIsNone(title_region(frame, 307))

    def test_header_short_extension_is_anchored_by_the_measured_pane_boundary_band(self):
        with fixture_frame() as frame:
            draw = ImageDraw.Draw(frame)
            draw.line((295, 30, 295, 647), fill=(220, 220, 220))
            draw.line((296, 95, 895, 95), fill=(220, 220, 220))
            # 主窗口上沿从左侧导航后开始，仍比整条面板边界带更靠左。
            draw.line((70, 40, 895, 40), fill=(220, 220, 220))
            band = title_region(frame, 307)
            self.assertIsNotNone(band)
            self.assertEqual(band[0], 313)
            self.assertGreater(band[1], 40)
            self.assertGreater(band[3], 90)

    def test_single_punctuation_and_color_emoji_are_not_empty(self):
        box = (10, 10, 180, 65)
        with Image.new('RGB', (200, 100), 'white') as frame:
            self.assertEqual(draft_state(frame, box), 'empty')
            ImageDraw.Draw(frame).rectangle((18, 22, 20, 24), fill='black')
            self.assertTrue(draft_has_text(frame, box))
        with Image.new('RGB', (200, 100), 'white') as frame:
            ImageDraw.Draw(frame).ellipse((18, 22, 32, 36), fill=(255, 220, 0))
            self.assertEqual(draft_state(frame, box), 'text')

    def test_caret_like_vertical_draft_is_not_assumed_empty_from_one_frame(self):
        with Image.new('RGB', (200, 100), 'white') as frame:
            ImageDraw.Draw(frame).line((18, 22, 18, 36), fill='black')
            self.assertEqual(draft_state(frame, (10, 10, 180, 65)), 'caret_like')
            self.assertTrue(draft_has_text(frame, (10, 10, 180, 65)))

    def test_three_physical_pixel_caret_is_only_a_candidate_and_remains_nonempty(self):
        with Image.new('RGB', (200, 100), 'white') as frame:
            ImageDraw.Draw(frame).rectangle((18, 22, 20, 44), fill='black')
            box = (10, 10, 180, 65)
            self.assertEqual(draft_state(frame, box), 'caret_like')
            self.assertTrue(draft_has_text(frame, box))

    def test_single_mark_touching_each_editable_roi_edge_is_never_cropped_away(self):
        box = (10, 10, 180, 65)
        for point in ((10, 25), (179, 25), (25, 10), (25, 64), (179, 10)):
            with self.subTest(point=point), Image.new('RGB', (200, 100), 'white') as frame:
                frame.putpixel(point, (189, 189, 189))
                self.assertEqual(draft_state(frame, box), 'text')

    def test_unproven_top_right_decoration_cannot_be_ignored_as_frame_border(self):
        with Image.new('RGB', (200, 100), 'white') as frame:
            # 即使只有一个中灰像素、位于右上角，仍可能是贴边标点。
            frame.putpixel((177, 12), (189, 189, 189))
            ImageDraw.Draw(frame).rectangle((18, 14, 20, 36), fill='black')
            self.assertEqual(draft_state(frame, (10, 10, 180, 65)), 'text')


class ShortButtonOCRTests(unittest.TestCase):
    """发送键结构闸门：唯一绿块 + 块内两枚白簇；OCR 只是加分项，读不出也不再阻塞。

    1.3.22 之前的断言要求 OCR 必须读出两份『发送』；真机上白字绿底读不出来（标题区同样的
    OCR 读得出会话名），所以门改成结构证据，OCR 降级为不阻塞的加分确认。
    """

    REGION = (100, 40, 280, 85)
    GLYPHS = ((215, 57, 222, 66), (224, 57, 231, 66))

    def image(self, second=False, glyphs=None):
        image = Image.new('RGB', (300, 100), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((200, 50, 255, 73), fill=(0, 200, 100))
        for box in (self.GLYPHS if glyphs is None else glyphs):
            draw.rectangle(box, fill='white')
        if second:
            draw.rectangle((130, 50, 185, 73), fill=(0, 200, 100))
        return image

    @staticmethod
    def recognized(image, label='发送'):
        width = (image.width - 60) // 4
        return [(label, 22 + i * (width + 10) * 2, 22, width * 2 - 4, 20) for i in range(2)]

    def test_unique_green_block_with_two_glyph_clusters_is_the_button(self):
        with self.image() as frame:
            self.assertEqual(locate_send_button(frame, self.REGION, None),
                             [('发送', 200, 50, 56, 24)])
            evidence = explain_send_button(frame, self.REGION, None)
            self.assertTrue(evidence['ok'])
            self.assertEqual(evidence['reason'], 'unique_green_block_with_two_glyph_clusters')
            self.assertEqual(evidence['green_blocks'], 1)
            self.assertEqual(len(evidence['glyphs']), 2)
            # 上探 6 行：真机上按钮顶边压在 send_region 上边之外，不上探就永远只有残块。
            self.assertEqual(evidence['scan_window'], [100, 34, 280, 85])

    def test_ocr_is_only_a_bonus_and_never_blocks_the_structural_evidence(self):
        with self.image() as frame:
            for recognize in (self.recognized,
                              lambda image: self.recognized(image, '发动'),
                              lambda image: self.recognized(image)[:1],
                              lambda image: [],
                              Mock(side_effect=RuntimeError('OCR 引擎不可用'))):
                self.assertEqual(locate_send_button(frame, self.REGION, recognize),
                                 [('发送', 200, 50, 56, 24)])

    def test_multiple_green_buttons_are_rejected_without_calling_ocr(self):
        recognize = Mock(side_effect=AssertionError('形状不唯一时不应调用OCR'))
        with self.image(second=True) as frame:
            self.assertEqual(locate_send_button(frame, self.REGION, recognize), [])
            self.assertEqual(explain_send_button(frame, self.REGION)['reason'],
                             'multiple_green_blocks')
        recognize.assert_not_called()


class SendButtonGateFailClosedTests(unittest.TestCase):
    """任一信号不成立都返回空列表：宁可报「未唯一定位到发送按钮」，也绝不按发送键。"""

    REGION = (100, 40, 280, 85)

    def reject(self, frame, reason, region=None):
        region = self.REGION if region is None else region
        self.assertEqual(locate_send_button(frame, region,
                         Mock(side_effect=AssertionError('形状不成立时不应调用OCR'))), [])
        self.assertEqual(explain_send_button(frame, region)['reason'], reason)

    def button(self, glyphs):
        with Image.new('RGB', (300, 100), 'white') as frame:
            draw = ImageDraw.Draw(frame)
            draw.rectangle((200, 50, 255, 73), fill=(0, 200, 100))
            for box in glyphs:
                draw.rectangle(box, fill='white')
            return self.reject(frame, self.reason)

    def test_disabled_gray_button_has_no_green_block(self):
        # 输入框空白时按钮是灰底灰字（真机实测绿色像素为 0），不可能被当成可点的发送键。
        with Image.new('RGB', (300, 100), 'white') as frame:
            ImageDraw.Draw(frame).rectangle((200, 50, 255, 73), fill=(230, 230, 230))
            self.reject(frame, 'no_green_block')

    def test_single_glyph_cluster_is_rejected(self):
        self.reason = 'glyphs_not_two'
        self.button(((215, 57, 231, 66),))

    def test_three_glyph_clusters_are_rejected(self):
        self.reason = 'glyphs_not_two'
        self.button(((215, 57, 219, 66), (221, 57, 225, 66), (227, 57, 231, 66)))

    def test_glyph_geometry_out_of_proportion_is_rejected(self):
        # 右字缩成一个小点：高度差过大，不是两枚汉字。
        self.reason = 'glyph_geometry_invalid'
        self.button(((215, 57, 222, 66), (224, 66, 227, 69)))

    def test_glyphs_touching_the_glyph_inset_edge_are_rejected(self):
        # 字形被内缩区裁到上下不对称（贴顶）：露出的笔画不足以证明是这两个字。
        self.reason = 'glyph_centering_invalid'
        self.button(((209, 53, 216, 62), (218, 53, 225, 62)))

    def test_block_clipped_by_the_scan_window_is_rejected(self):
        # 绿色块顶边正好压在扫描窗上边（真机上按钮就是压着分隔线的）。
        with Image.new('RGB', (300, 100), 'white') as frame:
            ImageDraw.Draw(frame).rectangle((200, 34, 255, 57), fill=(0, 200, 100))
            self.reject(frame, 'green_block_clipped')

    def test_region_outside_the_frame_is_rejected(self):
        with Image.new('RGB', (300, 100), 'white') as frame:
            self.reject(frame, 'region_out_of_frame', region=(285, 40, 320, 85))


class DangerousBase:
    def __init__(self, *, hwnd, calibrate=False):
        self.main_hwnd, self.pid = hwnd, 55
        self.origin_x = self.origin_y = 0
        self.render_w, self.render_h = 896, 648
        self.render_rect = (0, 0, 896, 648)
        if not self._load_layout():
            self.calibrate_layout()
        self._get_uia()
        self._update_layout()

    def _load_layout(self):
        raise AssertionError('禁止读取旧的比例校准文件')

    def calibrate_layout(self, *args, **kwargs):
        raise AssertionError('禁止自动校准/激活')

    def _get_uia(self, *args, **kwargs):
        raise AssertionError('禁止构造时热唤醒')

    def _update_layout(self):
        self.right_pane_left = 197
        self.send_button_region = (700, 580, 895, 647)


class PixelGUIProtectionTests(unittest.TestCase):
    def make_gui(self, frame, provider=None):
        gui = readonly_gui_class(DangerousBase)(hwnd=44, provider=provider or Mock())
        gui._pixel_frame = lambda: (frame.copy(), (0, 0, 896, 648))
        return gui

    def test_constructor_cannot_reach_dependency_calibration_or_hot_uia(self):
        gui = readonly_gui_class(DangerousBase)(hwnd=44, provider=Mock())
        self.assertIsNone(gui._get_uia())
        self.assertFalse(gui.calibrate_layout())
        self.assertFalse(gui._wake_accessibility())
        self.assertFalse(gui._hot_activate_accessibility())

    def test_geometry_refresh_keeps_measured_input_and_send_area(self):
        with fixture_frame() as frame:
            gui = self.make_gui(frame)
            box = gui.get_input_box()
            send = gui.send_button_region
            gui._update_layout()
            self.assertEqual(gui.send_button_region, send)
            self.assertEqual(box, tuple(gui._pixel_composer['draft_box']))
            self.assertNotEqual(gui.right_pane_left, 197)

    def test_three_pixel_vertical_mark_requires_a_real_blank_frame_before_empty(self):
        with fixture_frame() as frame:
            gui = self.make_gui(frame)
            box = gui.get_input_box()
            with frame.copy() as caret:
                ImageDraw.Draw(caret).rectangle((box[0] + 8, box[1] + 8,
                                                box[0] + 10, box[1] + 30), fill='black')
                gui._pixel_frame = Mock(side_effect=[(caret.copy(), (0, 0, 896, 648)),
                                                     (frame.copy(), (0, 0, 896, 648))])
                with patch('src.wechat_pixel_gui.time.sleep'):
                    self.assertFalse(gui._input_box_has_text(box))
                self.assertEqual(gui._pixel_frame.call_count, 2)
                gui._pixel_frame = lambda: (caret.copy(), (0, 0, 896, 648))
                with patch('src.wechat_pixel_gui.time.monotonic', side_effect=[0, 2]), \
                     patch('src.wechat_pixel_gui.time.sleep'):
                    self.assertTrue(gui._input_box_has_text(box))

    def test_title_uses_only_header_band_for_every_pass_and_preserves_space(self):
        provider = Mock()
        # 每次多倍率/预处理都要独立认出整行，正文里的文字不能参与标题判定。
        # 候选表是 profile 族 + 48 组基础网格：脚本答案用完必须回空，否则 Mock 抛 StopIteration。
        answers = [[('文件传输助手', 40, 40, 336, 56), ('正文诱导', 52, 300, 120, 40)],
                   [('文件传输助手', 60, 60, 504, 84)],
                   [('文件传输助手', 80, 80, 672, 112)]]
        provider.recognize.side_effect = lambda _image: answers.pop(0) if answers else []
        with fixture_frame() as frame:
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '文件传输助手')
        answers = [[('Test A', 40, 40, 336, 56)],
                   [('Test A', 60, 60, 504, 84)],
                   [('Test A', 80, 80, 672, 112)]]
        with fixture_frame() as frame:
            self.assertEqual(self.make_gui(frame, provider).current_chat_title(), 'Test A')

    def test_title_words_keep_separated_words_instead_of_collapsing_names(self):
        self.assertEqual(_title_words([('Test', 10, 40, 32, 14), ('A', 49, 40, 8, 14)]), 'Test A')
        self.assertEqual(_title_words([('文', 10, 40, 14, 14), ('件', 25, 40, 14, 14)]), '文件')
        self.assertEqual(_title_words([('Other', 10, 40, 35, 14), ('Name', 30, 40, 32, 14)]), '')

    def test_title_all_ocr_passes_must_agree_on_complete_name(self):
        provider = Mock()
        # 三份读数互不相同且都不完整：1.3.89 起逐变体投票，凑不出 3 票一致就不采信。
        answers = [[('Alice Smith', 40, 40, 336, 56)],
                   [('Alice', 60, 60, 270, 84)],
                   [('Alice', 80, 80, 360, 112)]]
        provider.recognize.side_effect = lambda _image: (
            answers.pop(0) if answers else [])  # 候选表远长于三份脚本答案
        with fixture_frame() as frame:
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '')
            # 三份互不相同的完整读数各一票，永远凑不到 3 票一致：判不一致，不采信。
            self.assertEqual(gui.title_diagnostic()['state'], 'disagreement')

    def test_title_unrecognized_suffix_inside_the_band_cannot_match_a_shorter_name(self):
        provider = Mock()
        # 只认出左边一块（放大换算回帧坐标是 330..355），右边还有没认出的标题墨迹。
        provider.recognize.return_value = [('Alice', 40, 32, 104, 60)]
        with fixture_frame() as frame:
            draw = ImageDraw.Draw(frame)
            # 1.3.87 起 14-17px 走边界档；本用例锁 18px 及以上的 medium 行为，故画 18px 墨迹。
            draw.rectangle((330, 40, 355, 57), fill='black')
            draw.rectangle((600, 40, 610, 57), fill='black')
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '')
        # 空读数与"漏字"读数都只是不投票，不再整族作废：1.3.89 起候选表 = profile 族
        # 去重 + 48 组基础网格，全部跑完仍凑不出 3 票一致才判空。
        evidence = gui.title_diagnostic()
        attempted = [row['family'] for row in evidence['passes']]
        self.assertEqual(evidence['state'], 'ocr_empty')
        self.assertEqual(attempted[:3], ['medium'] * 3)
        self.assertEqual(set(attempted[3:]), {'medium_fallback', 'medium_binarized', 'grid'})
        self.assertEqual(provider.recognize.call_count, len(attempted))

    def test_invalid_ocr_box_cannot_leave_a_partial_title(self):
        provider = Mock()
        provider.recognize.return_value = [
            ('Alice', 33, 153, 105, 42), ('Smith', 0, -20, 100, 42)]
        with fixture_frame() as frame:
            gui = self.make_gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '')
            # 空读数不再短路，因此断言的是所有候选都跑过：这帧标题区没有墨迹（墨迹高 0），
            # profile 只剩旧式表三份，其余 48 组基础网格补位（一组与旧表重合，去重后 50）。
            evidence = gui.title_diagnostic()
        self.assertEqual(provider.recognize.call_count, len(evidence['passes']))
        self.assertEqual(evidence['state'], 'ocr_empty')

    def test_missing_header_does_not_even_ocr_body(self):
        provider = Mock()
        with fixture_frame(header=False) as frame:
            self.assertEqual(self.make_gui(frame, provider).current_chat_title(), '')
        provider.recognize.assert_not_called()

    def test_invalid_isolated_channel_cannot_fall_back_to_local_ocr(self):
        with patch.dict('os.environ', {'WHALE_ISOLATED_CHANNEL': 'bad-pair'}), \
             patch('src.isolated_wechat.ocr_relay.GuestOCR', side_effect=ValueError('invalid')) as guest, \
             patch('src.isolated_wechat.ocr_relay.LocalOCR') as local:
            with self.assertRaises(ValueError):
                ocr_provider()
        guest.assert_called_once_with('bad-pair')
        local.assert_not_called()

    def test_target_probe_only_reads_and_reports_mismatch(self):
        channel = SimpleNamespace(available=Mock(return_value=SimpleNamespace(ok=True)),
            current_chat=Mock(return_value='另一个会话'), title_matches=lambda title, who: title == who)
        with patch('src.wechat_guia_send._channel', return_value=channel):
            result = probe(account_path='bound', who='目标会话')
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostic['waiting_code'], 'chat_not_open')
        self.assertNotIn('另一个会话', result.reason)


if __name__ == '__main__':
    unittest.main()
