"""机器/环境特征探测：搬到另一台电脑前，先让程序自己说清楚这台机器哪里不兼容。

只读模块。唯一的写操作是「数据根写一个临时文件再立刻删掉」——那是可写性探测本身。

**硬约束**：绝不调用任何「设置进程 DPI 感知」的 API。那一族函数会把整个控制台进程切到
另一套 DPI 虚拟化，坐标通道和截图尺寸会跟着变 —— 等于探测把被测对象改坏了。本模块只读：
只用 GetDpiForSystem / GetDpiForWindow 取 DPI，绝不往系统里写任何进程级状态。

七项探测各自独立 try：任何一项抛异常都降级成 status='unknown'，绝不让 HTTP 接口 500。
返回值里不出现用户名、wxid、聊天内容——窗口标题只在确认是「微信主窗口默认标题」时才带出。
"""
import ctypes
import os
import platform
import shutil
import socket
import sys
import time
import json
from datetime import datetime
from pathlib import Path

from .runtime_identity import APP_VERSION, PORT

STATUSES = ('ok', 'warn', 'unknown')

# 顺序固定：界面和测试都按这个顺序读
ITEM_ORDER = ('scale', 'screen', 'wechat', 'ocr', 'windows_sandbox', 'port', 'privilege', 'dsh', 'data_root')
_PROBE_NAMES = {key: '_probe_' + key for key in ITEM_ORDER}

# collect() 的整体预算：超过就跳过剩下的项，避免界面点了按钮一直转圈
BUDGET_SECONDS = 3.0
HTTP_TIMEOUT = 1.5

SM_CXSCREEN, SM_CYSCREEN = 0, 1
SPI_GETWORKAREA = 48  # SystemParametersInfo 的工作区参数，不是 GetSystemMetrics 的编号
SM_CMONITORS = 80

_WECHAT_EXE_NAMES = ('weixin.exe', 'wechat.exe')
# 微信主窗口停在默认标题时才是应用名；一旦切到某个会话，标题就变成联系人昵称
_WECHAT_WINDOW_TITLES = ('微信', 'weixin', 'wechat')
_HIDDEN_TITLE = '<窗口标题已隐藏>'
# 缺中文 OCR 包时给的是一条能照着点的路径，不是「去设置里装一下」
_OCR_ZH_INSTALL = ('设置 → 时间和语言 → 语言和区域 → 中文 → 语言选项 → 可选功能里安装'
                   '「中文（简体）光学字符识别」')


# ── 通用小工具 ────────────────────────────────────────────────────────────────
def _item(key, status, detail, data=None):
    if status not in STATUSES:
        status = 'unknown'
    return {'key': key, 'status': status, 'detail': str(detail), 'data': dict(data or {})}


def _unknown(key, reason):
    return _item(key, 'unknown', reason)


def _redact(text):
    """把用户主目录换成 ~：探测结果要能直接贴到 issue 里，不能顺手带出用户名。"""
    value = str(text or '')
    for candidate in (str(Path.home()), os.environ.get('USERPROFILE', '')):
        if candidate and len(candidate) > 3:
            value = value.replace(candidate, '~')
    return value


def _mask_title(title):
    """返回 (可公开的标题, 是否隐藏)。会话标题就是联系人昵称，属于用户名。"""
    text = str(title or '').strip()
    if not text:
        return '', False
    if text.lower() in _WECHAT_WINDOW_TITLES:
        return text, False
    return _HIDDEN_TITLE, True


def data_root():
    """与控制台同一套规则：冻结态写 LOCALAPPDATA，源码态就是工程根。"""
    if getattr(sys, 'frozen', False):
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'WhaleGirlWeChat'
    return Path(__file__).resolve().parent.parent


# ── 1. DPI 与缩放 ─────────────────────────────────────────────────────────────
def _probe_scale():
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    system_dpi = int(user32.GetDpiForSystem())
    if not system_dpi:
        raise RuntimeError('GetDpiForSystem 返回 0')
    # 取前台窗口的 DPI 只用于对照：探测跑在 HTTP 线程里，拿不到本进程的 Qt 窗口句柄
    hwnd = int(user32.GetForegroundWindow() or 0)
    window_dpi = None
    if hwnd:
        try:
            shcore = ctypes.WinDLL('shcore', use_last_error=True)
            window_dpi = int(shcore.GetDpiForWindow(ctypes.c_void_p(hwnd))) or None
        except (OSError, AttributeError):
            window_dpi = None  # Win10 1607 以前没有这个 API，跳过即可
    dpi = window_dpi or system_dpi
    scale = round(dpi / 96.0, 2)
    percent = int(round(scale * 100))
    data = {'system_dpi': system_dpi, 'window_dpi': window_dpi,
            'scale': scale, 'scale_percent': percent}
    if percent == 100:
        return _item('scale', 'ok', f'系统缩放 100%（DPI {dpi}），坐标通道不用换算', data)
    return _item('scale', 'warn',
                 f'系统缩放 {percent}%（DPI {dpi}），坐标要按 {scale} 倍换算，像素通道在非 100% 缩放下容易点偏', data)


# ── 2. 屏幕与工作区 ───────────────────────────────────────────────────────────
def _probe_screen():
    user32 = ctypes.WinDLL('user32', use_last_error=True)

    def metric(index, label):
        value = int(user32.GetSystemMetrics(index))
        if value <= 0:
            raise RuntimeError(f'{label} 读不到（GetSystemMetrics({index})={value}）')
        return value

    width, height = metric(SM_CXSCREEN, '屏幕宽'), metric(SM_CYSCREEN, '屏幕高')
    # 工作区不能用 GetSystemMetrics(48/49)：那两个编号在 GetSystemMetrics 里并不是
    # SM_CXWORKAREA/SM_CYWORKAREA（实测返回 34x16 这种无意义值）。48 是
    # SystemParametersInfo 的 SPI_GETWORKAREA，工作区只有它取得对。
    from ctypes import wintypes
    work = wintypes.RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0):
        raise RuntimeError('SPI_GETWORKAREA 读取工作区失败')
    work_width = int(work.right - work.left)
    work_height = int(work.bottom - work.top)
    if work_width <= 0 or work_height <= 0:
        raise RuntimeError(f'工作区尺寸异常（{work_width}x{work_height}）')
    monitors = int(user32.GetSystemMetrics(SM_CMONITORS)) or 1
    data = {'width': width, 'height': height, 'work_width': work_width,
            'work_height': work_height, 'monitors': monitors}
    if monitors > 1:
        # 多屏本身不是风险：点击通道按虚拟桌面换算（MOUSEEVENTF_VIRTUALDESK），窗口摆在哪块屏都点得准。
        # 真会跨屏的只有「窗口比主屏还宽」，可这里拿不到微信窗口矩形，就不臆造这条判据。
        return _item('screen', 'ok',
                     f'主屏 {width}x{height}，检测到 {monitors} 台显示器；点击坐标已按虚拟桌面换算'
                     f'（MOUSEEVENTF_VIRTUALDESK），不会落到别的屏', data)
    return _item('screen', 'ok',
                 f'屏幕 {width}x{height}，工作区 {work_width}x{work_height}，单显示器', data)


# ── 3. 微信客户端 ─────────────────────────────────────────────────────────────
def _wechat_processes():
    """微信 4.x 是多进程（主窗口、登录窗口、托盘各自独立），必须全收齐再挑主窗口。"""
    import psutil
    found = []
    for process in psutil.process_iter(['name', 'exe']):
        name = str(process.info.get('name') or '')
        if name.lower() in _WECHAT_EXE_NAMES:
            found.append({'pid': int(process.pid), 'name': name,
                          'exe': str(process.info.get('exe') or '')})
    return found


def _file_version(path):
    """读 exe 文件版本。

    语言块必须先从 '\\VarFileInfo\\Translation' 里取，写死 040904B0 在中文系统上
    会查到空字符串（表现为版本号变成 NA）。
    """
    if not path or not Path(path).is_file():
        return ''
    version = ctypes.WinDLL('version', use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    version.GetFileVersionInfoSizeW.restype = ctypes.c_ulong
    version.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = ctypes.c_int
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
    version.VerQueryValueW.restype = ctypes.c_int

    handle = ctypes.c_ulong()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(handle))
    if not size:
        return ''
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return ''
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not version.VerQueryValueW(buffer, '\\VarFileInfo\\Translation',
                                  ctypes.byref(pointer), ctypes.byref(length)) or not pointer.value:
        return ''
    words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))
    language, codepage = int(words[0]), int(words[1])
    for field in ('FileVersion', 'ProductVersion'):
        block = f'\\StringFileInfo\\{language:04x}{codepage:04x}\\{field}'
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        if version.VerQueryValueW(buffer, block, ctypes.byref(pointer), ctypes.byref(length)) and pointer.value:
            raw = ctypes.wstring_at(pointer, int(length.value or 0))
            text = raw.split('\x00')[0].strip()
            if text:
                return text
    return ''


def _find_window(pid):
    """按 pid 找微信可见窗口，取面积最大的那个当主窗口。"""
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    found = []

    def callback(hwnd, _param):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) != int(pid) or not user32.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        buffer = ctypes.create_unicode_buffer(max(length + 1, 2))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        found.append({'width': int(rect.right - rect.left), 'height': int(rect.bottom - rect.top),
                      'left': int(rect.left), 'top': int(rect.top), 'title': buffer.value})
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback), 0)
    if not found:
        return None
    return max(found, key=lambda window: window['width'] * window['height'])


def _version_parts(text):
    """容错解析版本号：'4.1.15.56'→(4,1,15,56)，'4.1.15'→(4,1,15)，空/非数字→()。绝不抛。"""
    parts = []
    for chunk in str(text or '').replace(',', '.').split('.'):
        digits = ''.join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _wechat_version_gate(version):
    """返回 (status, 说明)。只有 4.1.x 是实机验证过的通道；其余版本只提示未验证，不放行也不否决。"""
    parts = _version_parts(version)
    if not parts:
        return 'warn', f'版本号「{version or "空"}」解析不出来，无法判断兼容性'
    major, minor = parts[0], (parts[1] if len(parts) > 1 else 0)
    if (major, minor) == (4, 1):
        return 'ok', ''
    if (major, minor) == (4, 0):
        return 'warn', '4.0.x 未验证，发送通道可能失效，请先在自检通过后测试一次'
    return 'warn', f'{major}.{minor}.x 未验证（程序只在 4.1.x 上验证过），发送通道可能失效'


def _probe_wechat():
    processes = _wechat_processes()
    if not processes:
        return _item('wechat', 'warn', '微信进程没在跑；版本和窗口尺寸无法核对，先打开微信再自检',
                     {'running': False})
    file_version = ''
    version_note = ''
    for process in processes:
        try:
            file_version = _file_version(process['exe'])
        except (OSError, AttributeError, ValueError) as exc:
            version_note = f'（版本读取失败：{type(exc).__name__}）'
            file_version = ''
        if file_version:
            break
    windows = []
    for process in processes:
        try:
            window = _find_window(process['pid'])
        except (OSError, AttributeError):
            window = None
        if window:
            windows.append(window)
    window = max(windows, key=lambda w: w['width'] * w['height']) if windows else None
    data = {'running': True, 'process': processes[0]['name'],
            'exe': _redact(next((p['exe'] for p in processes if p['exe']), '')),
            'version': file_version}
    if window:
        title, hidden = _mask_title(window['title'])
        data['window'] = {'width': window['width'], 'height': window['height'],
                          'left': window['left'], 'top': window['top'],
                          'title': title, 'title_hidden': hidden}
    else:
        data['window'] = None
    size_note = f"，主窗口 {data['window']['width']}x{data['window']['height']}" if data['window'] else ''
    if not file_version:
        return _item('wechat', 'warn',
                     f'微信正在运行但读不到文件版本{version_note}；版本不明时别假设接口可用，'
                     f'请手动确认是 4.1.x 再测试一次发送{size_note}', data)
    status, note = _wechat_version_gate(file_version)
    data['version_ok'] = status == 'ok'
    if status == 'ok':
        return _item('wechat', 'ok', f'微信 {file_version} 正在运行{size_note}', data)
    return _item('wechat', 'warn', f'微信 {file_version} 正在运行{size_note}；{note}', data)


# ── 4. 系统 OCR ───────────────────────────────────────────────────────────────
def _ocr_languages():
    from winsdk.windows.media.ocr import OcrEngine
    tags = []
    for language in OcrEngine.available_recognizer_languages:
        tag = str(getattr(language, 'language_tag', '') or '')
        if tag:
            tags.append(tag)
    return tags


def _probe_ocr():
    tags = _ocr_languages()
    chinese = [tag for tag in tags if tag.lower().startswith('zh')]
    data = {'languages': tags[:8], 'total': len(tags), 'chinese': chinese[:8]}
    if chinese:
        return _item('ocr', 'ok', f'系统 OCR 有中文语言包（{chinese[0]}），共 {len(tags)} 种可用', data)
    return _item('ocr', 'warn',
                 f'系统 OCR 可用但只有 {len(tags)} 种语言、没有中文识别包；{_OCR_ZH_INSTALL}，'
                 f'装完重开控制台再自检', data)


# ── 5. Windows 与沙盒 ─────────────────────────────────────────────────────────
def _registry_edition():
    import winreg
    path = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    values = {}
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        for name in ('EditionID', 'ProductName', 'DisplayVersion', 'CurrentBuildNumber'):
            try:
                values[name] = str(winreg.QueryValueEx(key, name)[0])
            except OSError:
                continue
    return values


def _build_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _windows_name(build_int, product_name, edition_id):
    """大版本按 build 判断：注册表 ProductName 在 Windows 11 上至今仍写 'Windows 10'。"""
    number = _build_int(build_int)
    if number >= 22000:
        return 'Windows 11'
    if number >= 10240:
        return 'Windows 10'
    # build 读不到才退回注册表发行名：宁可不写版本，也不能把 11 写成 10
    return str(product_name or '').strip() or (f'Windows（{edition_id}）' if edition_id else 'Windows（版本未知）')


def _windows_label(edition, edition_id):
    """拼成「Windows 11（build 26200，25H2，Professional）」；DisplayVersion 有才带。"""
    build = _build_int(edition.get('CurrentBuildNumber', ''))
    name = _windows_name(build, edition.get('ProductName', ''), edition_id)
    parts = [f'build {build}'] if build else []
    display = str(edition.get('DisplayVersion', '') or '').strip()
    if display:
        parts.append(display)
    if edition_id:
        parts.append(edition_id)
    return f'{name}（{"，".join(parts)}）' if parts else name


def _resolve_wsb_exe(system_root=None):
    r"""找 Windows 沙盒的命令行 wsb.exe，顺序与 sandbox_provision 保持一致。

    1.3.76 之前只认 %SystemRoot%\System32\wsb.exe，但较新的 Windows 把 wsb.exe
    装成 %LOCALAPPDATA%\Microsoft\WindowsApps\wsb.exe 这个应用执行别名（本机 25H2 实测），
    于是自检面板报「没有 wsb.exe 命令行」而命令行其实能用——两个面板口径不一致。
    返回 (路径, 是否存在, 来源)，来源取值 system32 / windowsapps / none。
    """
    root = system_root or os.environ.get('SystemRoot') or r'C:\Windows'
    candidates = [('system32', Path(root) / 'System32' / 'wsb.exe')]
    local = os.environ.get('LOCALAPPDATA') or ''
    if local:
        candidates.append(('windowsapps', Path(local) / 'Microsoft' / 'WindowsApps' / 'wsb.exe'))
    for source, path in candidates:
        try:
            if path.is_file():
                return path, True, source
        except OSError:
            continue
    return candidates[0][1], False, 'none'


def _probe_windows_sandbox():
    edition = _registry_edition()
    system_root = os.environ.get('SystemRoot', r'C:\Windows')
    sandbox_exe = Path(system_root) / 'System32' / 'WindowsSandbox.exe'
    exe_exists = sandbox_exe.is_file()
    # 家庭版没有 Windows 沙盒这个可选功能，光有 exe 也开不起来
    edition_id = edition.get('EditionID', '')
    supported_edition = edition_id.lower() in {'professional', 'enterprise', 'education',
                                               'professionaln', 'enterprisen', 'educationn'}
    # 自动建隔离通道还要命令行 wsb.exe：Windows 10 只有 WindowsSandbox.exe，没有 wsb.exe，
    # 光看 exe 会误报「可以开沙盒」，所以两个判据都要看。
    wsb_exe, wsb_exists, wsb_source = _resolve_wsb_exe(system_root)
    available = bool(exe_exists and supported_edition and wsb_exists)
    data = {'windows_version': platform.version(), 'edition_id': edition_id,
            'product_name': edition.get('ProductName', ''),
            'display_version': edition.get('DisplayVersion', ''),
            'build': edition.get('CurrentBuildNumber', ''),
            'sandbox_exe': str(sandbox_exe), 'sandbox_exe_exists': exe_exists,
            'wsb_exe': str(wsb_exe), 'wsb_exe_exists': wsb_exists,
            'wsb_exe_source': wsb_source,
            'sandbox_available': available}
    name = _windows_label(edition, edition_id)
    if available:
        return _item('windows_sandbox', 'ok', f'{name}，可以开 Windows 沙盒', data)
    if exe_exists and supported_edition:
        return _item('windows_sandbox', 'warn',
                     f'{name} 能开 Windows 沙盒窗口，但 System32 与 WindowsApps 里都没有 wsb.exe 命令行，'
                     '自动建隔离通道用不了；先用本机模式，或换一台带 wsb.exe 的机器', data)
    if exe_exists:
        return _item('windows_sandbox', 'warn',
                     f'{name} 不是专业版/企业版，Windows 沙盒不可用；隔离后台跑不了', data)
    return _item('windows_sandbox', 'warn',
                 f'{name} 没有 WindowsSandbox.exe，Windows 沙盒不可用', data)


# ── 6. 端口 ───────────────────────────────────────────────────────────────────
def _runtime_identity(port):
    """问本机控制台端口上的东西是谁；通不通都要在 1.5 秒内给结论。"""
    import urllib.error
    import urllib.request
    url = f'http://127.0.0.1:{port}/api/runtime'
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
            raw = response.read(4096)
        body = json.loads(raw.decode('utf-8', 'replace'))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return body if isinstance(body, dict) else None


def _port_free(port):
    """能 bind 才算空闲。不加 SO_REUSEADDR：Windows 上它会让 bind 在端口被占用时也成功。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(('127.0.0.1', int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _probe_port():
    port = int(PORT)
    identity = _runtime_identity(port)
    ours = bool(identity and 'version' in identity)
    free = _port_free(port)
    data = {'port': port, 'self_running': ours, 'bindable': free,
            'version': identity.get('version', '') if ours else ''}
    if ours:
        return _item('port', 'ok', f'{port} 是本控制台后台在用（v{identity.get("version")}）', data)
    if not free:
        return _item('port', 'warn',
                     f'{port} 被别的进程占着、又不像本控制台后台；先关掉占用者，否则后台起不来', data)
    return _item('port', 'ok', f'{port} 空闲，后台可以正常监听', data)


# ── 6.5 管理员权限与桌面 DSH ────────────────────────────────────────────────
def _probe_privilege():
    """有没有管理员权限。非管理员时微信 4.x 能收不能发，用户只能自己去找「以管理员身份运行」。"""
    try:
        from .elevation import is_elevated
        elevated = bool(is_elevated())
    except Exception:
        return _unknown('privilege', '无法判断当前权限')
    data = {'elevated': elevated}
    if elevated:
        return _item('privilege', 'ok', '当前以管理员身份运行，微信发送可用', data)
    return _item('privilege', 'warn',
                 '当前不是管理员：微信 4.x 可能能收不能发；点「以管理员身份重启」即可', data)


def _probe_dsh():
    """桌面 DSH 装没装、在不在跑。没有 DSH 也能用本地模型，所以缺了只是警告。"""
    from . import dsh_discovery
    home = dsh_discovery.dsh_home()
    try:
        installed = home.is_dir()
    except OSError:
        installed = False
    running = False
    if installed:
        try:
            import psutil
            for proc in psutil.process_iter(['name']):
                name = str((proc.info or {}).get('name') or '').casefold()
                if name in dsh_discovery.ROOT_NAMES:
                    running = True
                    break
        except Exception:
            running = False
    data = {'installed': installed, 'running': running}
    if not installed:
        return _item('dsh', 'warn',
                     '这台电脑没找到桌面 DSH（没有 ~/.dsh 目录）；可以先用本地模型，接 DSH 前要先安装它',
                     data)
    if running:
        return _item('dsh', 'ok', '桌面 DSH 已安装并正在运行', data)
    return _item('dsh', 'warn', '桌面 DSH 已安装但没在运行；用 DSH 对话前先打开它', data)


# ── 7. 数据根与路径 ───────────────────────────────────────────────────────────
def _probe_data_root():
    root = data_root()
    writable = False
    probe_file = root / f'.site_profile_{os.getpid()}_{int(time.time() * 1000)}.tmp'
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe_file.write_text('ok', encoding='utf-8')
        writable = probe_file.is_file()
    except OSError:
        writable = False
    finally:
        try:
            probe_file.unlink()
        except OSError:
            pass
    usage = shutil.disk_usage(str(root))
    free_gb = round(usage.free / float(1024 ** 3), 2)
    frozen = bool(getattr(sys, 'frozen', False))
    data = {'root': _redact(root), 'writable': writable, 'free_gb': free_gb,
            'frozen': frozen, 'executable': _redact(sys.executable)}
    if not writable:
        return _item('data_root', 'warn', f'数据目录写不进去（{data["root"]}）；配置和聊天记录都存不下来', data)
    if free_gb < 1.0:
        return _item('data_root', 'warn', f'数据目录可写，但磁盘只剩 {free_gb} GB；记录和日志很快写满', data)
    mode = '安装版' if frozen else '源码运行'
    return _item('data_root', 'ok', f'数据目录可写（{data["root"]}），剩余 {free_gb} GB，{mode}', data)


# ── 汇总 ─────────────────────────────────────────────────────────────────────
def _platform_info():
    return {'app_version': APP_VERSION, 'port': int(PORT),
            'frozen': bool(getattr(sys, 'frozen', False)),
            'executable': _redact(sys.executable),
            'python': platform.python_version(), 'os': platform.platform(),
            'machine': platform.machine()}


def _summary(items, counts):
    total = len(items)
    if counts['unknown']:
        names = '、'.join(item['key'] for item in items if item['status'] == 'unknown')
        return (f'{total} 项探测：{counts["ok"]} 项正常，{counts["warn"]} 项警告，'
                f'{counts["unknown"]} 项探测不出（{names}）；先把探测不出的查清楚再搬机器')
    if counts['warn']:
        return (f'{total} 项探测：{counts["ok"]} 项正常，{counts["warn"]} 项警告；'
                f'这台机器能跑，但警告项会影响坐标/OCR 精度')
    return f'{total} 项探测全部正常，这台机器可以直接跑'


def collect():
    """一次机器自检。任何一项失败都只降级成 unknown，绝不抛给调用方。"""
    started = time.monotonic()
    items = []
    for key in ITEM_ORDER:
        if time.monotonic() - started > BUDGET_SECONDS:
            items.append(_unknown(key, f'整体探测超过 {BUDGET_SECONDS:.0f} 秒，这一项被跳过，请单独重试'))
            continue
        probe = globals().get(_PROBE_NAMES[key])
        if not callable(probe):
            items.append(_unknown(key, '探测函数缺失'))
            continue
        try:
            item = probe()
        except Exception as exc:  # 探测层不许把异常抛出去，接口必须 200
            items.append(_unknown(key, f'探测失败：{type(exc).__name__}: {exc}'))
            continue
        if not isinstance(item, dict) or item.get('key') != key:
            items.append(_unknown(key, '探测返回了非预期结构'))
            continue
        items.append(item)
    counts = {status: sum(1 for item in items if item['status'] == status) for status in STATUSES}
    return {'ok': counts['unknown'] == 0,
            'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'platform': _platform_info(),
            'items': items,
            'summary': _summary(items, counts),
            'elapsed_seconds': round(time.monotonic() - started, 3)}
