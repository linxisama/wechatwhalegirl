"""原生依赖离线体检；不导入微信库，不查找窗口或接触微信进程。"""
import importlib
import os
import struct
import sys
from pathlib import Path


def check_native_dependencies():
    result = {'bits': struct.calcsize('P') * 8, 'frozen': bool(getattr(sys, 'frozen', False))}
    if os.name != 'nt':
        return dict(result, skipped='仅 Windows 需要 pywin32')
    if result['frozen']:
        import ctypes
        library = Path(sys._MEIPASS) / 'mfc140u.dll'
        if not library.is_file():
            raise RuntimeError('安装包缺少 mfc140u.dll，请覆盖安装完整新版，不要在系统 Python 中补装依赖')
        # 使用完整路径加载，不能让构建机已安装的系统 MFC 掩盖打包遗漏。
        mfc = ctypes.WinDLL(str(library))
        result['app_local_mfc'] = library.name
    for name in ('win32ui', 'win32api', 'win32gui', 'pythoncom', 'pywintypes'):
        module = importlib.import_module(name)
        result[name] = Path(module.__file__).name
    return dict(result, ok=True)


def dependency_error_hint(exc):
    text = str(exc).lower()
    if not isinstance(exc, (ImportError, OSError)) or not any(
            value in text for value in ('win32', 'dll', 'pywintypes', 'pythoncom', 'mfc140')):
        return ''
    if getattr(sys, 'frozen', False):
        return '安装版原生依赖加载失败：请保留配置覆盖安装完整新版；检查匹配程序位数的微软 VC++ 运行库。系统 Python 的 pip 安装不影响本程序'
    return '源码原生依赖加载失败：请用启动本程序的同一 Python 修复 pywin32，并检查 Python 位数及微软 VC++ 运行库；虚拟环境不要执行 pywin32_postinstall'
