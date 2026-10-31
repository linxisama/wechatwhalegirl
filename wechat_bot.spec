# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（微信控制台）。

为什么用 **onedir** 而不是 QQ 版的 --onefile：
本程序要带上微信自动化、本地语音识别（ctranslate2/onnxruntime/av）与 PyQt6，
单文件包启动时每次都要把几百 MB 解压到临时目录，冷启动可能十几秒到几十秒，
而且更容易被杀软误报。onedir 启动快、增量友好；安装包由 Inno Setup 统一用
lzma2 压缩，最终用户拿到的仍然是一个 setup.exe。

入口用 `gui_launcher.py`：它负责启动日志、`--self-test` 与 `--ocr-worker`
（冻结后 ocr_worker 不能再按"脚本路径"启动，见该文件注释）。
"""
import os
import importlib.util
import sys
import pefile
from pathlib import Path
from PyInstaller.depend.bindepend import get_imports
from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct,
    VSVersionInfo,
)

ROOT = os.path.abspath(os.path.dirname(SPEC))          # noqa: F821 - spec 注入
VERSION = os.environ.get("WECHAT_APP_VERSION", "1.0.0")


def _version_resource(version: str) -> VSVersionInfo:
    """构造 exe 的版本资源。

    注意：PyInstaller 的 `version=` 只接受 VSVersionInfo 对象或版本资源文件路径，
    直接传 "1.0.0" 这种字符串会被当成文件名去打开。所以在这里构造对象，
    让"属性 → 详细信息"里能看到真实版本（排查"用户装的是哪一版"时很有用）。
    """
    numbers = [int(part) for part in (version.split(".") + ["0", "0", "0", "0"])[:4]]
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=tuple(numbers), prodvers=tuple(numbers), mask=0x3F,
                          flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("080404B0", [
                StringStruct("CompanyName", "鲸鱼娘"),
                StringStruct("FileDescription", "鲸鱼娘 · 微信控制台"),
                StringStruct("FileVersion", version),
                StringStruct("InternalName", "WhaleGirlWeChat"),
                StringStruct("OriginalFilename", "WhaleGirlWeChat.exe"),
                StringStruct("ProductName", "鲸鱼娘 · 微信控制台"),
                StringStruct("ProductVersion", version),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0804, 1200])]),
        ],
    )

datas = [
    (os.path.join(ROOT, "styles"), "styles"),
    (os.path.join(ROOT, "assets"), "assets"),
    (os.path.join(ROOT, "src", "web", "static"), "src/web/static"),
    (os.path.join(ROOT, "icon.ico"), "."),
    # 保留原始脚本：源码方式调用（非冻结）与排错时都能直接看到它
    (os.path.join(ROOT, "ocr_worker.py"), "."),
]

hiddenimports = [
    "uvicorn", "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    "fastapi", "fastapi.responses", "starlette", "pydantic",
    "cryptography", "cryptography.fernet", "loguru", "yaml", "httpx", "requests",
    "PyQt6", "PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets", "PyQt6.sip",
    "ocr_worker",            # 冻结后 --ocr-worker 分支要 import 它
    "unittest.mock",         # --self-test 用
    "tkinter", "tkinter.filedialog",   # 插件安装的"浏览…"原生文件框
    "pysilk",                # 语音 SILK → PCM
    "comtypes", "uiautomation", "win32com", "win32com.client", "pythoncom", "pywintypes",
    "win32ui", "win32api", "win32gui", "win32con", "win32process",
    "wechatauto", "wxauto4",
    "faster_whisper", "ctranslate2", "onnxruntime", "tokenizers", "huggingface_hub", "av",
    "winsdk", "winrt",
]
# 1.3.8恢复全量支持模块，避免覆盖升级留下配置却丢失实现。
# 分类仍用于市场导出；正式冻结验收要求完整集合，精简试验不能冒充正式交付。
sys.path.insert(0, ROOT)
try:
    from src.plugin_catalog import MARKET_PLUGINS as _MARKET
except Exception:
    _MARKET = ()
if os.environ.get('WHALE_KEEP_ALL_PLUGINS', '1') == '1':
    _MARKET = ()
MARKET_EXCLUDES = {'src.plugins.' + name for name in _MARKET}

RETIRED = {'src.plugins.context_chime','src.plugins.repeat_echo','src.plugins.question_relay','src.plugins.at_mention'}
RETIRED.update('src.plugins.' + name for name in ('send_pacing','typo_sim','emotion_state',
    'mood_guidance','computer_helper','turn_complete','quote_reply','mention_nudge_guard','chatter_guard'))
hiddenimports += [name for name in collect_submodules("src")
                  if name not in RETIRED and name not in MARKET_EXCLUDES]

# 这几个包除了代码还有数据/原生依赖，交给官方 hook + collect_all 一起收，
# 漏一个就会在真机上表现为"某个功能静默不可用"。
for package in ("wechatauto", "wxauto4", "imageio_ffmpeg", "winsdk", "faster_whisper"):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception as exc:                     # 包没装时不能连打包一起失败
        print(f"[spec] collect_all({package}) 跳过：{exc}")
        continue
    datas += package_datas
    hiddenimports += package_hidden

# 系统目录里的 MFC 被默认扫描排除，构建机有它不代表用户电脑有。
# 从 win32ui 的真实依赖解析路径，检查架构后按应用本地运行库显式携带。
binaries = []
win32ui_path = importlib.util.find_spec('win32ui').origin
with pefile.PE(win32ui_path, fast_load=True) as extension:
    extension_machine = extension.FILE_HEADER.Machine
for name, resolved in get_imports(win32ui_path):
    if name.lower() == 'mfc140u.dll':
        if not resolved:
            raise RuntimeError('缺少 win32ui 的 MFC 运行库，不能发布依赖不完整的安装包')
        with pefile.PE(resolved, fast_load=True) as library:
            if library.FILE_HEADER.Machine != extension_machine:
                raise RuntimeError('MFC 与 Python 架构不匹配')
        binaries.append((resolved, '.'))
if not binaries:
    raise RuntimeError('无法确认 win32ui 的 MFC 运行库')
system32 = importlib.util.find_spec('pywin32_system32')
for folder in system32.submodule_search_locations:
    for stem in ('pythoncom', 'pywintypes'):
        library = Path(folder) / f'{stem}{sys.version_info.major}{sys.version_info.minor}.dll'
        if not library.is_file():
            raise RuntimeError(f'pywin32 安装不完整：{library.name}')
        binaries.append((str(library), 'pywin32_system32'))

a = Analysis(
    [os.path.join(ROOT, "gui_launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        *sorted(RETIRED),
        *sorted(MARKET_EXCLUDES),
        # 明显用不到的大件，剔掉能省不少体积（不影响功能）
        "matplotlib", "pandas", "scipy", "IPython", "jupyter", "notebook",
        "pytest", "sphinx", "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtMultimedia", "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.Qt3DCore",
        "pyttsx3", "edge_tts", "soundfile", "librosa", "pydub",
    ],
    noarchive=False,
    optimize=0,
)
# Qt 使用 Windows 自带的无版本后缀 ICU 接口。构建机 PATH 中的 Poppler
# 也带同名 DLL，但导出的是带版本号的接口，误收集会导致 QtCore 无法启动。
# 系统组件交给 Windows 加载，不把构建机第三方工具的同名副本带给用户。
windows_icu = {"icu.dll", "icuuc.dll", "icuin.dll"}
a.binaries = [entry for entry in a.binaries
              if not (os.path.basename(entry[0]).lower() in windows_icu
                      and os.path.isfile(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                                      "System32", os.path.basename(entry[0]))))]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WhaleGirlWeChat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "icon.ico"),
    version=_version_resource(VERSION),
    # 不在这里强制提权（uac_admin 会把整个 exe 变成"每次启动都要过 UAC"，
    # 而且打包后无法离线自检）。改用安装目录里的「启动（管理员）.cmd」：
    # 需要发送消息时用它，会自己走 UAC；普通启动用于改配置、看状态。
    # 界面在"无管理员权限"时会明确提示（见 wechat_gui 的控制台卡片）。
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WhaleGirlWeChat",
)
