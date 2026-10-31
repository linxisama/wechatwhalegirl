"""在创建时限定实际用户访问，兼容管理员程序与同用户沙盒共享进程。"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _check_path(path):
    path = Path(path).absolute()
    # 已有链接和 junction 不能借私有目录创建进入另一个资料位置。
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, 'st_file_attributes', 0) & 0x400 or item.is_symlink():
            raise OSError('私有目录路径包含链接，未创建目录')
    return path


def _directory_sddl(user_sid):
    if not isinstance(user_sid, str) or not re.fullmatch(r'S-1-(?:\d+-)*\d+', user_sid):
        raise ValueError('无法核验私有目录所属用户')
    # OW 跟随目录所有者；提权进程的默认所有者可能是 Administrators。
    # 明确记录 TokenUser，才能让该用户的过滤令牌访问同一份共享目录。
    return 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;' + user_sid + ')'


def _verify_windows_directory(path, user_sid, kernel, advapi):
    """无 ACL 的文件系统可能忽略创建描述符；落盘核验前不准写入配对密钥。"""
    import ctypes
    from ctypes import wintypes

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ('count', 'used', 'free')]

    class AceHeader(ctypes.Structure):
        _fields_ = [('kind', ctypes.c_ubyte), ('flags', ctypes.c_ubyte), ('size', wintypes.WORD)]

    advapi.GetNamedSecurityInfoW.argtypes = (wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p))
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD))
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetAclInformation.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int)
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    advapi.GetAce.restype = wintypes.BOOL
    acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        result = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None,
                                             ctypes.byref(acl), None, ctypes.byref(descriptor))
        if result:
            raise ctypes.WinError(result)
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not acl.value or control.value & 0x1004 != 0x1004:  # DACL_PRESENT | DACL_PROTECTED
            raise OSError('私有目录访问权限未生效，未写入配对资料')
        info = AclSizeInformation()
        if not advapi.GetAclInformation(acl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.count != 3:
            raise OSError('私有目录访问权限不一致，未写入配对资料')
        sids = set()
        for index in range(info.count):
            ace, sid_text = ctypes.c_void_p(), ctypes.c_void_p()
            if not advapi.GetAce(acl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            if header.kind != 0 or header.flags != 3 or mask != 0x1F01FF or header.size < 20:
                raise OSError('私有目录访问权限不一致，未写入配对资料')
            try:
                if not advapi.ConvertSidToStringSidW(ace.value + 8, ctypes.byref(sid_text)):
                    raise ctypes.WinError(ctypes.get_last_error())
                sids.add(ctypes.wstring_at(sid_text))
            finally:
                if sid_text.value:
                    kernel.LocalFree(sid_text)
        if sids != {'S-1-5-18', 'S-1-5-32-544', user_sid}:
            raise OSError('私有目录实际用户权限不一致，未写入配对资料')
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)


def _create_windows_directory(path):
    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p),
                    ('inherit_handle', wintypes.BOOL)]

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [('sid', ctypes.c_void_p), ('attributes', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.argtypes = ()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CreateDirectoryW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes))
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.IsValidSid.argtypes = (ctypes.c_void_p,)
    advapi.IsValidSid.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD))
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.IsValidSecurityDescriptor.argtypes = (ctypes.c_void_p,)
    advapi.IsValidSecurityDescriptor.restype = wintypes.BOOL

    token, sid_text, descriptor = wintypes.HANDLE(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        needed = wintypes.DWORD()
        result = advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
        if result or ctypes.get_last_error() != 122 or not 0 < needed.value <= 65536:
            raise OSError('无法核验私有目录所属用户，未创建目录')
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(SidAndAttributes)).contents.sid
        if not advapi.IsValidSid(sid):
            raise OSError('无法核验私有目录所属用户，未创建目录')
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        user_sid = ctypes.wstring_at(sid_text)
        sddl = _directory_sddl(user_sid)
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.IsValidSecurityDescriptor(descriptor):
            raise OSError('私有目录安全描述符无效，未创建目录')
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor.value, False)
        # 安全描述符直接参与创建；已有目录必须失败，不能借此改旧 ACL。
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        _verify_windows_directory(path, user_sid, kernel, advapi)
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if sid_text.value:
            kernel.LocalFree(sid_text)
        if token.value:
            kernel.CloseHandle(token)


def create_private_directory(path):
    """只创建一个新目录；不创建父目录、不复用旧目录、不修改已有 ACL。"""
    path = _check_path(path)
    if os.name == 'nt':
        _create_windows_directory(path)
    else:
        path.mkdir(mode=0o700)
    return path
