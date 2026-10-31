"""可选按钮 OCR 不能占用正式发送时限；完整结构和区域闸门仍必须成立。"""
import unittest
from unittest.mock import Mock

from PIL import Image
from src.wechat_pixel_gui import readonly_gui_class
from src.wechat_ocr_labels import locate_send_button
from tests import test_pixel_sender1315 as fixtures


class PixelButtonBudgetTests(unittest.TestCase):
    def make_gui(self, frame, provider, region):
        gui = readonly_gui_class(fixtures.DangerousBase)(hwnd=44, provider=provider)
        gui._pixel_frame = lambda: (frame.copy(), (0, 0, frame.width, frame.height))
        gui._pixel_geometry = (0, 0, frame.width, frame.height)
        gui._pixel_composer = {'send_region': list(region)}
        return gui

    def test_unavailable_or_slow_optional_ocr_is_not_started_for_a_unique_button(self):
        fixture = fixtures.ShortButtonOCRTests()
        provider = Mock()
        provider.recognize.side_effect = TimeoutError('同步远程 OCR 不得占用预算')
        with fixture.image() as frame:
            self.assertEqual(self.make_gui(frame, provider, fixture.REGION).ocr(fixture.REGION),
                             [('发送', 200, 50, 56, 24)])
        provider.recognize.assert_not_called()

    def test_ambiguous_missing_or_incomplete_structure_still_rejects(self):
        fixture = fixtures.ShortButtonOCRTests()
        frames = [fixture.image(second=True), fixture.image(glyphs=()),
                  fixture.image(glyphs=((215,57,222,66),)), Image.new('RGB',(300,100),'white')]
        for frame in frames:
            with frame:
                provider = Mock()
                self.assertEqual(locate_send_button(frame,fixture.REGION), [])
                self.assertEqual(self.make_gui(frame,provider,fixture.REGION).ocr(fixture.REGION), [])
                provider.recognize.assert_not_called()

    def test_unverified_region_geometry_or_composer_still_rejects(self):
        fixture = fixtures.ShortButtonOCRTests()
        with fixture.image() as frame:
            for invalid in ('region','geometry','composer'):
                gui = self.make_gui(frame,Mock(),fixture.REGION)
                if invalid == 'geometry': gui._pixel_geometry = (1,0,frame.width,frame.height)
                if invalid == 'composer': gui._pixel_composer = None
                region = (101,40,280,85) if invalid == 'region' else fixture.REGION
                with self.assertRaises(RuntimeError): gui.ocr(region)


if __name__ == '__main__': unittest.main()
