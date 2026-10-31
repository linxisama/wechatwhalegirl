"""自绘抓帧必须遵守 GDI 位图读取与释放顺序；全部使用假驱动。"""
import ctypes
import unittest
from unittest.mock import patch

from src import wechat_capture as capture


class FakeGDI:
    def __init__(self):
        self.events = []
        self.selected = {200: 50}
        self.selection_failures = {}
        self.select_count = 0
        self.bitmap_result = 300
        self.read_result = None

    def CreateCompatibleDC(self, screen):
        self.events.append(('create_dc', screen))
        return 200

    def CreateCompatibleBitmap(self, screen, width, height):
        self.events.append(('create_bitmap', width, height))
        return self.bitmap_result

    def SelectObject(self, dc, obj):
        self.select_count += 1
        self.events.append(('select', obj))
        if self.select_count in self.selection_failures:
            failure = self.selection_failures[self.select_count]
            if isinstance(failure, Exception):
                raise failure
            return failure
        old = self.selected[dc]
        self.selected[dc] = obj
        return old

    def GetDIBits(self, dc, bitmap, start, height, buffer, info, usage):
        self.events.append(('read', bitmap))
        # Win32 要求读取的位图此时不得仍被选入任何设备上下文。
        if bitmap in self.selected.values():
            return 0
        return height if self.read_result is None else self.read_result

    def DeleteDC(self, dc):
        self.events.append(('delete_dc', dc))
        self.selected.pop(dc, None)
        return 1

    def DeleteObject(self, bitmap):
        self.events.append(('delete_bitmap', bitmap))
        if bitmap in self.selected.values():
            raise AssertionError('仍选入设备上下文的位图无法释放')
        return 1


class FakeUser:
    def __init__(self, gdi):
        self.gdi = gdi
        self.print_result = 1
        self.print_error = None

    def GetWindowRect(self, hwnd, pointer):
        rect = pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = -500, 100, -480, 110
        return 1

    def GetDC(self, hwnd):
        return 100

    def PrintWindow(self, hwnd, dc, flags):
        self.gdi.events.append(('print', dc))
        if self.print_error is not None:
            raise self.print_error
        return self.print_result

    def ReleaseDC(self, hwnd, screen):
        self.gdi.events.append(('release_screen', screen))
        return 1


class CaptureGDIContractTests(unittest.TestCase):
    def setUp(self):
        self.gdi = FakeGDI()
        self.user = FakeUser(self.gdi)

    def capture(self):
        with patch.object(capture, '_win32', return_value=(self.user, self.gdi)):
            return capture.window_bitmap(42)

    def kinds(self):
        return [event[0] for event in self.gdi.events]

    def assert_released(self):
        self.assertEqual(self.kinds().count('delete_dc'), 1)
        self.assertEqual(self.kinds().count('delete_bitmap'), 1)
        self.assertEqual(self.kinds().count('release_screen'), 1)

    def test_bitmap_is_unselected_before_getdibits(self):
        result = self.capture()
        self.assertIsNotNone(result)
        image, anchor = result
        self.assertEqual(anchor, (-500, 100))
        self.assertEqual(image.size, (20, 10))
        image.close()
        self.assertLess(self.gdi.events.index(('select', 50)), self.kinds().index('read'))
        self.assert_released()

    def test_printwindow_failure_still_releases_all_resources(self):
        self.user.print_result = 0
        self.assertIsNone(self.capture())
        self.assertNotIn('read', self.kinds())
        self.assert_released()

    def test_printwindow_exception_still_releases_all_resources(self):
        self.user.print_error = OSError('fake drawing failure')
        self.assertIsNone(self.capture())
        self.assert_released()

    def test_null_selectobject_does_not_draw_or_restore_invalid_handle(self):
        self.gdi.selection_failures[1] = None
        self.assertIsNone(self.capture())
        self.assertNotIn('print', self.kinds())
        self.assertEqual(self.gdi.select_count, 1)
        self.assert_released()

    def test_hgdi_error_is_not_a_restorable_object(self):
        for invalid in (-1, ctypes.c_void_p(-1).value):
            with self.subTest(invalid=invalid):
                self.setUp()
                self.gdi.selection_failures[1] = invalid
                self.assertIsNone(self.capture())
                self.assertNotIn('print', self.kinds())
                self.assertNotIn('read', self.kinds())
                self.assertEqual(self.gdi.select_count, 1)
                self.assert_released()

    def test_failed_detach_never_calls_getdibits(self):
        for invalid in (None, -1, ctypes.c_void_p(-1).value):
            with self.subTest(invalid=invalid):
                self.setUp()
                self.gdi.selection_failures[2] = invalid
                self.assertIsNone(self.capture())
                self.assertNotIn('read', self.kinds())
                self.assert_released()

    def test_repeated_detach_failure_destroys_dc_before_bitmap(self):
        self.gdi.selection_failures.update({2: None, 3: None})
        self.assertIsNone(self.capture())
        self.assertNotIn('read', self.kinds())
        self.assert_released()
        self.assertLess(self.kinds().index('delete_dc'), self.kinds().index('delete_bitmap'))

    def test_cleanup_restore_exception_does_not_skip_resource_release(self):
        self.user.print_result = 0
        self.gdi.selection_failures[2] = OSError('fake cleanup failure')
        self.assertIsNone(self.capture())
        self.assert_released()

    def test_detach_exception_still_releases_resources(self):
        self.gdi.selection_failures[2] = OSError('fake select failure')
        self.assertIsNone(self.capture())
        self.assertNotIn('read', self.kinds())
        self.assert_released()

    def test_bitmap_creation_failure_releases_dc_and_screen_only(self):
        self.gdi.bitmap_result = None
        self.assertIsNone(self.capture())
        self.assertEqual(self.kinds().count('delete_dc'), 1)
        self.assertEqual(self.kinds().count('release_screen'), 1)
        self.assertNotIn('delete_bitmap', self.kinds())

    def test_partial_dib_read_is_refused_and_resources_released(self):
        self.gdi.read_result = 9
        self.assertIsNone(self.capture())
        self.assert_released()


if __name__ == '__main__':
    unittest.main()
