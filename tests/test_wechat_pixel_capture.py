# -*- coding: utf-8 -*-
"""遮挡无关抓帧（src/wechat_capture.py）与 PixelGUI 抓帧接缝的离线验收。

全部用假位图 / 假 Win32：不建窗口、不截屏、不碰真实微信，也不依赖桌面是否可用。
"""
import ctypes
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from src import wechat_pixel_gui
from src.wechat_capture import MAX_SIDE, grab_render, render_crop

# 主窗 GetWindowRect 左上角、渲染区左上角与尺寸，全部是屏幕坐标下的实测形态。
ANCHOR = (100, 100)
ORIGIN = (110, 120)
SIZE = (80, 60)


def _bitmap(width, height):
    """每像素颜色由坐标决定，裁剪错位一格都能被逐点断言抓到。"""
    image = Image.new('RGB', (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (x % 256, y % 256, (x * 3 + y * 7) % 256)
    return image


class RenderCropTest(unittest.TestCase):
    def test_screen_box_is_cut_out_of_whole_window_bitmap(self):
        bitmap = _bitmap(200, 150)
        box = (ORIGIN[0] + 5, ORIGIN[1] + 7, ORIGIN[0] + 25, ORIGIN[1] + 27)
        frame = render_crop(bitmap, ANCHOR, ORIGIN, SIZE, box)
        self.assertEqual(frame.size, (20, 20))
        # 位图内的偏移 = 渲染区相对整窗偏移(10,20) + box 相对渲染区偏移(5,7)
        self.assertEqual(frame.getpixel((0, 0)), bitmap.getpixel((15, 27)))

    def test_none_box_returns_render_area_only(self):
        bitmap = _bitmap(200, 150)
        frame = render_crop(bitmap, ANCHOR, ORIGIN, SIZE, None)
        self.assertEqual(frame.size, SIZE)
        self.assertEqual(frame.getpixel((0, 0)), bitmap.getpixel((10, 20)))
        self.assertEqual(frame.getpixel((SIZE[0] - 1, SIZE[1] - 1)),
                         bitmap.getpixel((10 + SIZE[0] - 1, 20 + SIZE[1] - 1)))

    def test_box_outside_render_area_falls_back(self):
        bitmap = _bitmap(200, 150)
        self.assertIsNone(render_crop(bitmap, ANCHOR, ORIGIN, SIZE, (0, 0, 50, 50)))
        # 右边界多出 1px：部分越界也不许从位图里凑，必须整块交回屏幕抓帧。
        self.assertIsNone(render_crop(bitmap, ANCHOR, ORIGIN, SIZE,
                                      (ORIGIN[0] + 1, ORIGIN[1],
                                       ORIGIN[0] + SIZE[0] + 1, ORIGIN[1] + 10)))

    def test_render_area_outside_bitmap_is_refused(self):
        bitmap = _bitmap(200, 150)
        self.assertIsNone(render_crop(bitmap, ANCHOR, (ORIGIN[0] + 500, ORIGIN[1]), SIZE, None))
        self.assertIsNone(render_crop(bitmap, ANCHOR, ORIGIN, (4096, 60), None))

    def test_degenerate_geometry_is_refused(self):
        bitmap = _bitmap(20, 20)
        self.assertIsNone(render_crop(bitmap, ANCHOR, ORIGIN, (0, 0), None))
        self.assertIsNone(render_crop(bitmap, ANCHOR, ORIGIN, (MAX_SIDE + 1, 10), None))
        self.assertIsNone(render_crop(bitmap, ANCHOR, ORIGIN, SIZE,
                                      (ORIGIN[0] + 5, ORIGIN[1] + 5, ORIGIN[0] + 5, ORIGIN[1] + 9)))


class GrabRenderTest(unittest.TestCase):
    def test_printwindow_failure_returns_none(self):
        self.assertIsNone(grab_render(4242, ORIGIN, SIZE, None, bitmap_provider=lambda _h: None))

    def test_printwindow_exception_returns_none(self):
        def boom(hwnd):
            raise OSError('PrintWindow 不可用')
        self.assertIsNone(grab_render(4242, ORIGIN, SIZE, None, bitmap_provider=boom))

    def test_unexpected_provider_shape_returns_none(self):
        self.assertIsNone(grab_render(4242, ORIGIN, SIZE, None, bitmap_provider=lambda _h: 42))

    def test_crop_outlives_closed_bitmap(self):
        frame = grab_render(4242, ORIGIN, SIZE, None,
                            bitmap_provider=lambda _h: (_bitmap(200, 150), ANCHOR))
        self.assertEqual(frame.size, SIZE)
        self.assertIsNotNone(frame.getpixel((0, 0)))


class FakeBase:
    """只够 PixelGUI 用的 guia 基类替身：绝不连真窗口。"""

    def __init__(self, *, hwnd, calibrate=False):
        self.main_hwnd = hwnd
        self.pid = 42164
        self.origin_x, self.origin_y = ORIGIN
        self.render_w, self.render_h = 800, 600
        self.right_pane_left = 300
        self.send_button_region = (700, 560, 800, 600)

    def _update_render_rect(self):
        return True

    def _update_layout(self):
        return True

    @property
    def render_rect(self):
        return (self.origin_x, self.origin_y,
                self.origin_x + self.render_w, self.origin_y + self.render_h)


class FakeUser32:
    def GetWindowThreadProcessId(self, hwnd, pointer):
        pointer._obj.value = 42164
        return 1

    def IsWindow(self, hwnd):
        return 1

    def IsIconic(self, hwnd):
        return 0


FAKE_CTYPES = types.SimpleNamespace(windll=types.SimpleNamespace(user32=FakeUser32()),
                                    c_ulong=ctypes.c_ulong, c_void_p=ctypes.c_void_p,
                                    byref=ctypes.byref)


class PixelGuiCaptureSeamTest(unittest.TestCase):
    """_grab_screen 必须优先窗口自绘，屏幕抓帧只当兜底。"""

    def setUp(self):
        self.gui = wechat_pixel_gui.readonly_gui_class(FakeBase)(hwnd=4242, provider=lambda: None)

    def test_render_capture_is_preferred(self):
        frame = Image.new('RGB', (800, 600))
        with patch.object(wechat_pixel_gui, 'grab_render', return_value=frame) as render, \
                patch.object(wechat_pixel_gui, 'grab_screen',
                             Mock(side_effect=AssertionError('自绘可用时不得回退屏幕抓帧'))) as fallback:
            self.assertIs(self.gui._grab_screen((110, 120, 190, 180)), frame)
        render.assert_called_once_with(4242, (110, 120), (800, 600), (110, 120, 190, 180))
        fallback.assert_not_called()

    def test_screen_grab_is_only_a_fallback(self):
        frame = Image.new('RGB', (800, 600))
        with patch.object(wechat_pixel_gui, 'grab_render', return_value=None), \
                patch.object(wechat_pixel_gui, 'grab_screen', return_value=frame) as fallback:
            self.assertIs(self.gui._grab_screen((110, 120, 190, 180)), frame)
        fallback.assert_called_once_with((110, 120, 190, 180))

    def test_pixel_frame_asks_for_the_render_rect_in_screen_coordinates(self):
        boxes = []

        def capture(hwnd, origin, size, box=None, bitmap_provider=None):
            boxes.append((hwnd, origin, size, box))
            return Image.new('RGB', (800, 600))

        with patch.object(wechat_pixel_gui, 'ctypes', FAKE_CTYPES), \
                patch.object(wechat_pixel_gui, 'grab_render', side_effect=capture):
            frame, geometry = self.gui._pixel_frame()
        self.assertEqual(frame.size, (800, 600))
        self.assertEqual(geometry, (110, 120, 800, 600))
        self.assertEqual(boxes, [(4242, (110, 120), (800, 600), (110, 120, 910, 720))])

    def test_pixel_frame_keeps_geometry_consistency_check(self):
        def shift(hwnd, origin, size, box=None, bitmap_provider=None):
            self.gui.render_w = 801      # 抓帧期间窗口变了宽
            return Image.new('RGB', (800, 600))

        with patch.object(wechat_pixel_gui, 'ctypes', FAKE_CTYPES), \
                patch.object(wechat_pixel_gui, 'grab_render', side_effect=shift):
            with self.assertRaises(RuntimeError) as caught:
                self.gui._pixel_frame()
        self.assertIn('截图期间窗口位置或尺寸改变', str(caught.exception))

    def test_composer_failure_carries_the_reason_code(self):
        self.gui._pixel_frame = lambda: (Image.new('RGB', (800, 600)), (110, 120, 800, 600))
        with patch.object(wechat_pixel_gui, 'inspect_composer',
                          return_value={'confirmed': False, 'reason': 'pane_divider_unconfirmed'}):
            with self.assertRaises(RuntimeError) as caught:
                self.gui._inspect_composer(self.gui._pixel_frame()[0], (110, 120, 800, 600))
        self.assertIn('pane_divider_unconfirmed', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
