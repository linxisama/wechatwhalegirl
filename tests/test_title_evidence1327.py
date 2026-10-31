"""标题误读的实测图回归与失败来源；不读取窗口、不连接微信。"""
import math
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw
from src.wechat_pixel_gui import (readonly_gui_class, _MEDIUM_TITLE_PASSES, _pass_variants,
                                  _title_pass_families, _title_passes, _title_ink_region)
from src.wechat_guia_send import GuiaChannel, probe, _ok
from src.wechat_app import send_channel_hint
from tests import test_pixel_title_passes1321 as fixture


class TitleEvidenceTests(unittest.TestCase):
    def frame(self):
        frame = fixture.fixture_frame()
        with Image.open(Path(__file__).parent / 'fixtures/filehelper-title-18px.png') as title:
            frame.paste(title, (320, 15))
        return frame

    def gui(self, frame, provider):
        gui = readonly_gui_class(fixture.QuietBase)(hwnd=44, provider=provider)
        gui._pixel_frame = lambda: (frame.copy(), (0, 0, 896, 648))
        return gui

    @unittest.skipUnless(os.name == 'nt', 'Windows OCR title-only regression')
    def test_actual_18px_title_needs_three_correct_complete_fixed_passes(self):
        from src.isolated_wechat.ocr_relay import LocalOCR
        self.assertTrue(LocalOCR.available())
        with self.frame() as frame:
            gui = self.gui(frame, LocalOCR())
            self.assertEqual(gui.current_chat_title(), '文件传输助手')
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'verified')
            self.assertEqual(evidence['profile'], 'medium')
            # 真机上主族只有一份无旁证的错字读数（另两份 0 行），整族作废后由回退族的三份
            # 一致读数胜出：按族名筛出胜出族，不按"最后三行"这种位置猜。
            plan = [name for name, _variants in _title_pass_families(_MEDIUM_TITLE_PASSES)]
            self.assertIn(evidence['family'], plan)
            self.assertLessEqual(evidence['families_tried'], len(plan))
            winner = [row for row in evidence['passes'] if row['family'] == evidence['family']]
            self.assertEqual(len(winner), 3)
            self.assertTrue(all(row['complete'] for row in winner), evidence['passes'])

    def test_profile_is_chosen_only_from_all_measured_ink_height(self):
        with self.frame() as frame:
            ink = _title_ink_region(frame, fixture.BAND)
            passes, height = _title_passes(frame, ink)
            self.assertEqual(height, 18)
            # 18px 墨迹只能落在中等字号固定表上：表是按真机实测定的。主族只放平滑放大，
            # 但三族合起来两种重采样都在，那一帧的像素证据由二值化族提供。
            self.assertEqual(passes, _MEDIUM_TITLE_PASSES)
            self.assertEqual({variant[3] for variant in _pass_variants(passes)},
                             {'smooth'})
            every = [variant for _name, family in _title_pass_families(passes)
                     for variant in family]
            self.assertEqual({variant[3] for variant in every}, {'nearest', 'smooth'})
        with fixture.fixture_frame() as frame:
            fixture.draw_glyphs(frame, 330, 20, 5)
            passes, height = _title_passes(frame, _title_ink_region(frame, fixture.BAND))
            self.assertEqual(height, 14)
            self.assertEqual(passes, ((4, 150, 0), (6, 150, 0), (8, 150, 0)))

    def test_fractional_inverse_uses_rounded_dimensions_and_excludes_white_padding(self):
        provider = Mock()
        provider.recognize.return_value = [('标题', 56, 50, 266, 50)]
        with self.frame() as frame:
            gui = self.gui(frame, provider)
            region = (320, 15, 445, 49)
            rows = gui._ocr_crop(frame, region, scale=2.5, threshold=150, padding=32)
            sx, sy = 312 / 125, 85 / 34
            left, top = math.floor(24 / sx), math.floor(18 / sy)
            right, bottom = math.ceil(290 / sx), math.ceil(68 / sy)
            self.assertEqual(rows, [('标题', 320 + left, 15 + top, right-left, bottom-top)])
            passed_image = provider.recognize.call_args.args[0]
            # mock保留的是已关闭图；图像大小仍可用于核对输出白边。
            self.assertEqual(passed_image.size, (376, 149))
            provider.recognize.return_value = [('越界', 31, 50, 20, 20)]
            self.assertEqual(gui._ocr_crop(frame, region, scale=2.5, threshold=150, padding=32), [])

    def test_same_length_disagreement_still_rejects_and_reports_no_raw_titles(self):
        provider = Mock()
        names = iter(('文件传输助手', '文牛传输助手', '文件传输助手'))
        # 变体按族展开后的顺序排：主族三份互相矛盾即作废，回退族这里一份也读不出。
        variants = [variant for _family, family in
                    _title_pass_families(_MEDIUM_TITLE_PASSES) for variant in family]
        def recognize(image):
            # 合成覆盖真实文字的框；不靠目标名称挑结果，倍率/白边直接取自被测表。
            index = provider.recognize.call_count - 1
            text = next(names, None)
            if index >= len(variants) or text is None:
                return []
            scale, _threshold, pad, _resample = variants[index]
            return [(text, pad+round(10*scale), pad+round(8*scale), round(106*scale), round(19*scale))]
        provider.recognize.side_effect = recognize
        with self.frame() as frame:
            gui = self.gui(frame, provider)
            self.assertEqual(gui.current_chat_title(), '')
            evidence = gui.title_diagnostic()
            self.assertEqual(evidence['state'], 'disagreement')
            # 主族三份完整读数互相矛盾：整族作废，回退族读不出也就不可能拼出标题。
            self.assertEqual([row['family'] for row in evidence['passes']][:3],
                             ['medium'] * 3)
            self.assertNotIn('文件', str(evidence))
            self.assertNotIn('文牛', str(evidence))

    def test_next_read_error_does_not_reuse_previous_success(self):
        channel = GuiaChannel()
        gui = SimpleNamespace(current_chat_title=Mock(side_effect=['私密标题', RuntimeError('secret-value')]))
        self.assertEqual(channel.current_chat(gui), '私密标题')
        self.assertEqual(channel.title_diagnostic()['state'], 'verified')
        self.assertEqual(channel.current_chat(gui), '')
        evidence = channel.title_diagnostic()
        self.assertEqual(evidence['state'], 'ocr_error')
        self.assertNotIn('secret-value', str(evidence))
        self.assertNotIn('私密标题', str(evidence))

    def test_probe_distinguishes_unknown_title_from_verified_other_chat(self):
        channel = GuiaChannel()
        channel.available = Mock(return_value=_ok('离线'))
        channel.current_chat = Mock(return_value='')
        channel._title_evidence = {'state': 'disagreement'}
        with patch('src.wechat_guia_send._channel', return_value=channel):
            result = probe(who='目标')
            self.assertEqual(result.reason_code, 'title_unverified')
            self.assertEqual(result.diagnostic['title_read']['state'], 'disagreement')
            channel.current_chat.return_value = '另一个完整标题'
            channel._title_evidence = {'state': 'verified'}
            self.assertEqual(probe(who='目标').reason_code, 'chat_not_open')

    def test_runtime_hint_does_not_ask_to_reopen_a_chat_after_ocr_failure(self):
        hint = send_channel_hint({'waiting_code':'title_unverified','reason':'三份结果不一致'})
        self.assertIn('保留诊断', hint)
        self.assertNotIn('先在微信里手动点开', hint)

    def test_send_failing_before_any_title_read_cannot_export_old_verified_evidence(self):
        from src.wechat_guia_send import _fail
        channel = GuiaChannel()
        gui = SimpleNamespace(current_chat_title=lambda: '上一轮私密标题')
        self.assertEqual(channel.current_chat(gui), '上一轮私密标题')
        with patch.object(channel, '_attempt', return_value=_fail('guia_unavailable')), \
             patch('src.wechat_guia_send.foreground_owner', return_value=('',0)), \
             patch.object(channel, '_win32', return_value=Mock()):
            result = channel.send('目标', '离线正文')
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostic['title_read']['state'], 'not_read')
        self.assertNotIn('上一轮', str(result.diagnostic))


if __name__ == '__main__':
    unittest.main()
