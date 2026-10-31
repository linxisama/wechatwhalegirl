# -*- coding: utf-8 -*-
"""遮挡无关的窗口自绘抓帧：PrintWindow 抓整窗位图，再按渲染区裁剪。

为什么需要它：像素管线（inspect_layout / 输入框 / 标题 OCR）要的是微信自己的像素。
ImageGrab 抓的是屏幕像素——微信主窗被别的窗口完全遮挡时，抓到的是遮挡者，布局
检查因此报「没有唯一像素证据」；无前台窗口/无交互桌面时它更会直接抛
OSError: screen grab failed（2026-10-28 本机实测）。PrintWindow(PW_RENDERFULLCONTENT)
由窗口自绘，两种情形下都仍能拿到真实内容（同一次实测中 PrintWindow 返回 1）。

坐标换算：整窗位图的原点是主窗 GetWindowRect 的左上角，而 guia 的所有抓帧 box
都是**屏幕坐标**。渲染区（origin_x/origin_y + render_w/render_h）相对整窗的偏移
= 渲染区屏幕原点 - 主窗矩形左上角。这里只做这层减法，不改任何识别规则。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

PW_RENDERFULLCONTENT = 2
# 尺寸上限：异常/超大窗口直接放弃自绘，交回屏幕抓帧，绝不为未知尺寸分配位图。
MAX_SIDE = 8192
HGDI_ERROR = ctypes.c_void_p(-1).value

_USER32 = None
_GDI32 = None


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG), ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', _BITMAPINFOHEADER), ('bmiColors', wintypes.DWORD * 1)]


def _win32():
    """Win32 入口只建一次；显式声明原型，否则 64 位句柄会被截成 32 位。"""
    global _USER32, _GDI32
    if _USER32 is None:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                    wintypes.UINT, ctypes.c_void_p,
                                    ctypes.POINTER(_BITMAPINFO), wintypes.UINT]
        gdi32.GetDIBits.restype = ctypes.c_int
        _USER32, _GDI32 = user32, gdi32
    return _USER32, _GDI32


def _dib_image(dc, bitmap, width, height):
    """把已画好的位图取成 PIL 图；自下而上转成自上而下，颜色通道 BGRX→RGB。"""
    from PIL import Image
    info = _BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height          # 负数 = 自上而下，省一次翻转
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0           # BI_RGB
    buffer = ctypes.create_string_buffer(width * height * 4)
    _, gdi32 = _win32()
    if gdi32.GetDIBits(dc, bitmap, 0, height, buffer, ctypes.byref(info), 0) != height:
        return None
    return Image.frombuffer('RGB', (width, height), buffer, 'raw', 'BGRX', 0, 1).copy()


def _invalid_object(handle):
    return not handle or handle in (-1, HGDI_ERROR)


def window_bitmap(hwnd):
    """PrintWindow 抓整窗位图，返回 (图像, 窗口屏幕左上角)；任何失败都返回 None。"""
    try:
        user32, gdi32 = _win32()
        handle = wintypes.HWND(int(hwnd))
        rect = wintypes.RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(rect)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE):
            return None
        screen = user32.GetDC(None)
        if not screen:
            return None
        dc = bitmap = previous = None
        image = None
        try:
            dc = gdi32.CreateCompatibleDC(screen)
            bitmap = gdi32.CreateCompatibleBitmap(screen, width, height) if dc else None
            if not bitmap:
                return None
            previous = gdi32.SelectObject(dc, bitmap)
            if _invalid_object(previous):
                previous = None  # 错误哨兵不是能选回去的 GDI 对象。
                return None
            if not user32.PrintWindow(handle, dc, PW_RENDERFULLCONTENT):
                return None
            # GetDIBits 明确要求位图不再选入任何 DC；部分驱动会直接拒绝旧顺序，
            # 导致遮挡时无谓退回屏幕像素。取色前必须先成功选回原对象。
            if _invalid_object(gdi32.SelectObject(dc, previous)):
                return None
            previous = None
            image = _dib_image(dc, bitmap, width, height)
        finally:
            if dc and previous:
                try:
                    gdi32.SelectObject(dc, previous)
                except Exception:
                    pass
            # 先销毁私有 DC：即使异常路径无法选回原对象，也不能先删除仍选中的位图。
            # 任一清理失败仍继续其余资源的释放，避免屏幕 DC 被异常路径遗漏。
            for function, args in ((gdi32.DeleteDC, (dc,)),
                                   (gdi32.DeleteObject, (bitmap,)),
                                   (user32.ReleaseDC, (None, screen))):
                if args[-1]:
                    try:
                        function(*args)
                    except Exception:
                        pass
        return None if image is None else (image, (rect.left, rect.top))
    except Exception:
        # 抓帧是尽力而为的一层：任何异常都必须退化成「没有自绘内容」，
        # 由调用方回退屏幕抓帧，绝不能把窗口/驱动异常漏给上层。
        return None


def render_crop(bitmap, anchor, origin, render_size, box=None):
    """从整窗位图裁渲染区；box 是屏幕坐标。不在本层职责内或越界 → None。"""
    try:
        left, top = int(anchor[0]), int(anchor[1])
        origin_x, origin_y = int(origin[0]), int(origin[1])
        width, height = int(render_size[0]), int(render_size[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    left_offset, top_offset = origin_x - left, origin_y - top
    if (not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE) or left_offset < 0
            or top_offset < 0 or left_offset + width > bitmap.width
            or top_offset + height > bitmap.height):
        return None
    area = (left_offset, top_offset, left_offset + width, top_offset + height)
    if box is not None:
        try:
            x0, y0, x1, y1 = (int(value) for value in box)
        except (TypeError, ValueError):
            return None
        # 只有**完全**落在渲染区内的子矩形才归自绘层；部分越界交回屏幕抓帧，
        # 免得把窗口边框/桌面像素混进识别输入。
        if x0 < origin_x or y0 < origin_y or x1 > origin_x + width or y1 > origin_y + height:
            return None
        if x1 <= x0 or y1 <= y0:
            return None
        area = (left_offset + x0 - origin_x, top_offset + y0 - origin_y,
                left_offset + x1 - origin_x, top_offset + y1 - origin_y)
    return bitmap.crop(area)


def grab_render(hwnd, origin, render_size, box=None, bitmap_provider=None):
    """遮挡无关的渲染区抓帧；不可用返回 None，由调用方回退屏幕抓帧。"""
    provider = bitmap_provider or window_bitmap
    try:
        captured = provider(hwnd)
    except Exception:
        return None
    if not captured:
        return None
    try:
        bitmap, anchor = captured
    except (TypeError, ValueError):
        return None
    with bitmap:
        return render_crop(bitmap, anchor, origin, render_size, box)


def grab_screen(box=None):
    """屏幕像素抓帧（回退路径）：guia 默认只抓主屏，双屏会错位，故 all_screens=True。"""
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=box, all_screens=True)
