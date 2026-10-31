"""OCR 既有可变槽的独占原位发布，避免 Sandbox 替换文件后的缺名缓存。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
import time

from .protocol import (MAX_DOCUMENT_BYTES, ProtocolError, UUID32, _bytes,
                       checked_path, read_document, signed, write_document)

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _kernel():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.SetFilePointerEx.argtypes = (wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD)
    kernel.SetFilePointerEx.restype = wintypes.BOOL
    kernel.WriteFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    kernel.WriteFile.restype = wintypes.BOOL
    for name in ('SetEndOfFile', 'FlushFileBuffers', 'CloseHandle'):
        getattr(kernel, name).argtypes = (wintypes.HANDLE,)
        getattr(kernel, name).restype = wintypes.BOOL
    return kernel


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('OCR 原发布预算已耗尽')
    return remaining


def _write_existing(path, raw, *, deadline):
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ProtocolError('OCR 发布截止时间无效')
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_DOCUMENT_BYTES:
        raise ProtocolError('OCR 发布文档超过限制')
    kernel = _kernel()
    while True:
        _remaining(deadline)
        # share=0 与客体的共享读取互斥，读者不会看见部分新正文；
        # OPEN_EXISTING 不会把已丢失的结果槽误建为另一次发布。
        handle = kernel.CreateFileW(str(path), 0x40000000, 0, None, 3, 0x80, None)
        if handle not in (None, 0, -1, _INVALID_HANDLE_VALUE):
            break
        error = ctypes.get_last_error()
        if error != 32:
            raise ctypes.WinError(error)
        # 只等待本次独占句柄获取；写入之后任何异常都不再写第二次。
        time.sleep(min(.005, _remaining(deadline)))
    try:
        _remaining(deadline)
        if not kernel.SetFilePointerEx(handle, 0, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        _remaining(deadline)
        written = wintypes.DWORD()
        data = ctypes.create_string_buffer(raw)
        if not kernel.WriteFile(handle, data, len(raw), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(raw):
            raise OSError('OCR 槽写入不完整，结果未知')
        if not kernel.SetEndOfFile(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.FlushFileBuffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        _remaining(deadline)
    finally:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _slot(path, pairing):
    path = checked_path(path, pairing)
    folder = checked_path(pairing.root / 'ocr', pairing)
    if path == folder / 'relay.json':
        return path, 'relay', None
    if path.parent == folder and path.name.endswith('.result.json'):
        identifier = path.name.removesuffix('.result.json')
        if UUID32.fullmatch(identifier):
            return path, 'result', identifier
    return path, None, None


def publish_ocr_document(path, pairing, payload, exclusive=False):
    path, kind, identifier = _slot(path, pairing)
    if kind is None or exclusive:
        return write_document(path, pairing, payload, exclusive=exclusive)
    if kind == 'result' and (not isinstance(payload, dict)
                             or payload.get('request_id') != identifier):
        raise ProtocolError('OCR 回执与结果槽身份不符')
    # 心跳首次建立以后保留既有名字；结果槽必须由本次客体预建，缺失就失败。
    if not path.exists():
        if kind == 'result':
            raise FileNotFoundError('本次 OCR 结果槽已不存在')
        return write_document(path, pairing, payload, exclusive=True)
    raw = _bytes(signed(pairing, payload))
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ProtocolError('OCR 发布文档超过限制')
    if kind == 'relay':
        deadline = time.monotonic() + .05
    else:
        # 只重读原有签名请求的元数据以保留原 deadline，不重新识别或投递请求。
        request = path.with_name(identifier + '.request.json')
        job = read_document(request, pairing, 8192)
        stamp = job.get('deadline')
        if (job.get('request_id') != identifier or job.get('epoch') != payload.get('epoch')
                or type(stamp) not in (int, float) or not math.isfinite(stamp)):
            raise ProtocolError('无法核验原 OCR 发布身份')
        remaining = stamp - time.time()
        if not 0 < remaining <= 7:
            raise TimeoutError('原 OCR 请求已过期')
        deadline = time.monotonic() + min(.1, remaining)
    if os.name != 'nt':
        # 非 Windows 的离线往返保持原原子文档发布；生产沙盒仅运行于 Windows。
        return write_document(path, pairing, payload)
    _write_existing(path, raw, deadline=deadline)
    return path
