"""只读取当前 Windows 会话和输入桌面，不切换桌面、不更改读屏状态。"""
import os


DESKTOP_WAIT_MESSAGE = '桌面暂不可交互：请解锁 Windows 或重新连接远程桌面；等待恢复后处理，不能在锁屏界面发送'


def interactive_desktop_status():
    if os.name != 'nt':
        return True, ''
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    wts = ctypes.WinDLL('wtsapi32', use_last_error=True)
    query = wts.WTSQuerySessionInformationW
    query.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    wts.WTSFreeMemory.restype = None
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.CloseDesktop.restype = wintypes.BOOL
    information = user.GetUserObjectInformationW
    information.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    information.restype = wintypes.BOOL
    memory, size = ctypes.c_void_p(), wintypes.DWORD()
    try:
        if not query(None, 0xffffffff, 8, ctypes.byref(memory), ctypes.byref(size)):
            return False, DESKTOP_WAIT_MESSAGE
        try:
            if size.value < ctypes.sizeof(ctypes.c_int) or ctypes.cast(
                    memory, ctypes.POINTER(ctypes.c_int)).contents.value != 0:
                return False, DESKTOP_WAIT_MESSAGE
        finally:
            if memory.value:
                wts.WTSFreeMemory(memory)
        input_desktop = user.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
        if not input_desktop:
            return False, DESKTOP_WAIT_MESSAGE
        try:
            current = user.GetThreadDesktop(kernel.GetCurrentThreadId())
            receiving = wintypes.BOOL()
            needed = wintypes.DWORD()
            # OpenInputDesktop 在断开的会话仍可能返回句柄，不能只检查句柄非空。
            if not current or not information(current, 6, ctypes.byref(receiving),
                                              ctypes.sizeof(receiving), ctypes.byref(needed)) or not receiving.value:
                return False, DESKTOP_WAIT_MESSAGE
            names = []
            for handle in (current, input_desktop):
                value = ctypes.create_unicode_buffer(512)
                if not information(handle, 2, value, ctypes.sizeof(value), ctypes.byref(needed)):
                    return False, DESKTOP_WAIT_MESSAGE
                names.append(value.value)
            if names[0] != names[1]:
                return False, DESKTOP_WAIT_MESSAGE
        finally:
            user.CloseDesktop(input_desktop)
    except OSError:
        return False, DESKTOP_WAIT_MESSAGE
    return True, ''
