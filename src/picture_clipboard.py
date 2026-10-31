"""图片发送进程的短时剪贴板所有权；备份只在内存，不写磁盘。"""
import ctypes
import struct
from pathlib import Path


def file_drop(paths):
    return struct.pack('<IiiII', 20, 0, 0, 0, 1) + ('\0'.join(paths) + '\0\0').encode('utf-16le')


class PictureClipboard:
    def __init__(self, path):
        from .sticker_library import MAX_BYTES, inspect_image
        target = Path(path)
        if not target.is_file() or target.stat().st_size > MAX_BYTES:
            raise ValueError('图片不可用或超过大小上限')
        inspect_image(target.read_bytes())
        import win32clipboard
        self.clip = win32clipboard
        self.path = str(path)
        self.saved = []
        self.sequence = None

    def _restore(self):
        self.clip.EmptyClipboard()
        for fmt, data in self.saved:
            self.clip.SetClipboardData(fmt, file_drop(data) if fmt == 15 else data)

    def __enter__(self):
        self.clip.OpenClipboard()
        changed = False
        try:
            formats = []
            fmt = self.clip.EnumClipboardFormats(0)
            while fmt:
                formats.append(fmt)
                fmt = self.clip.EnumClipboardFormats(fmt)
            for fmt in formats:
                # Windows 可由 DIB 合成位图句柄，不把不可复制的句柄当成字节存回。
                if fmt == 2 and (8 in formats or 17 in formats):
                    continue
                data = self.clip.GetClipboardData(fmt)
                if not isinstance(data, (str, bytes)) and not (fmt == 15 and isinstance(data, tuple)):
                    raise ValueError('剪贴板包含无法安全恢复的格式，本次不发送图片')
                self.saved.append((fmt, data))
            self.clip.EmptyClipboard()
            changed = True
            self.clip.SetClipboardData(15, file_drop([self.path]))
            self.sequence = ctypes.windll.user32.GetClipboardSequenceNumber()
        except Exception:
            if changed:
                self._restore()
            raise
        finally:
            self.clip.CloseClipboard()
        return self

    def __exit__(self, *args):
        try:
            self.clip.OpenClipboard()
            try:
                # 用户在发送期间复制了新内容时，保留用户的新剪贴板。
                if ctypes.windll.user32.GetClipboardSequenceNumber() == self.sequence:
                    self._restore()
            finally:
                self.clip.CloseClipboard()
        except Exception:
            pass
