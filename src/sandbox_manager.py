"""为已有 Windows Sandbox 准备受限微信代理，不启动微信或机器人。"""
from __future__ import annotations

import json
import base64
import hashlib
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .runtime_identity import APP_VERSION
from . import sandbox_acl_recovery as acl_recovery
from .private_directory import create_private_directory

CHANNEL_ID = re.compile(r'[0-9a-f]{32}\Z')
PROGRAM_KEY = re.compile(r'[0-9a-f]{20}\Z')
GUEST_APP = r'C:\WhaleApp'
GUEST_CHANNEL = r'C:\WhaleChannel'
APP_EXEC_LINK_TAG = 0x8000001B
_PROGRAM_LOCK = threading.Lock()
_PREPARE_LOCK = threading.RLock()
DEFAULT_UPGRADE_TIMEOUT = 45.0
# 能读到但无法核验的通道元数据一律整体失败，不新建也不覆盖任何映射。
_UNVERIFIED_CHANNEL = '现有隔离通道元数据无法核验，请先检查配对记录；未覆盖任何映射'


def _upgrade_timeout(name, environ=None):
    """升级等待默认 45 秒；慢机器冷启动超出旧 20 秒会假失败，仅允许环境变量覆盖。"""
    values = os.environ if environ is None else environ
    try:
        value = float(values.get(name) or DEFAULT_UPGRADE_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_UPGRADE_TIMEOUT
    return value if math.isfinite(value) and value > 0 else DEFAULT_UPGRADE_TIMEOUT


UPGRADE_STOP_TIMEOUT = _upgrade_timeout('WHALE_SANDBOX_UPGRADE_STOP_TIMEOUT')
UPGRADE_START_TIMEOUT = _upgrade_timeout('WHALE_SANDBOX_UPGRADE_START_TIMEOUT')
RECOVERY_STAGES = frozenset(('reserved', 'mapping_program', 'mapping_channel', 'channel_mapped',
    'launching', 'ready', 'failed', 'preflight', 'program_mapped', 'verifying', 'committing',
    'repair_staging', 'repair_archiving', 'repair_installing'))
RECOVERY_OPERATIONS = frozenset(('share_program', 'share_channel', 'process_probe', 'launch',
    'verify_heartbeat', 'commit_session', 'preflight', 'complete', 'session_probe',
    'repair_preflight', 'stage_channel', 'archive_channel', 'install_channel'))
RECOVERY_REASONS = frozenset((
    '尚无恢复记录，可显式修复失败配对',
    '原映射已确认，可在重新核验后继续恢复；不会重复映射通道',
    '恢复证据不足或映射结果未知；保留记录，不能重复映射或启动',
    '配对恢复已经完成',
    '恢复记录无法核验；保留原始证据', acl_recovery.REPAIRABLE_REASON, acl_recovery.BLOCKED_REASON))


@contextmanager
def _prepare_process_lock(root, sandbox_id):
    """同资料目录、同沙盒的跨进程互斥；进程崩溃仍由持久阶段阻止未知重投。"""
    if os.name != 'nt':
        # 非 Windows 本就不能操作沙盒；离线替身仍保留同一进程的闸门。
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    identity = hashlib.sha256((str(Path(root).resolve()).casefold() + '|' + sandbox_id).encode()).hexdigest()
    handle = kernel.CreateMutexW(None, False, 'Local\\WhaleSandboxPreparation-' + identity)
    if not handle:
        raise SandboxError('无法取得隔离配对操作锁，未更改映射')
    held = False
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        held = result in (0, 0x80)  # abandoned 仅取得锁，不抹除未知持久记录。
        if not held:
            raise SandboxError('此沙盒正在由另一进程准备，请等待原操作完成')
        yield
    finally:
        if held:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def _session_probe(sandbox_id):
    """只核验 ExistingLogin 的交互用户；先于创建配对和映射，不能改用 System。"""
    code = "# WHALE_SESSION_PROBE\nif ($env:USERNAME -cne 'WDAGUtilityAccount') { exit 2 }; exit 0"
    encoded = base64.b64encode(code.encode('utf-16le')).decode('ascii')
    command = (r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe '
        '-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand ' + encoded)
    try:
        value = _run_wsb(['exec', '--id', sandbox_id, '--run-as', 'ExistingLogin', '--command', command], timeout=25)
    except SandboxError as error:
        if error.hresult == '0x80070520':
            raise SandboxError('沙盒交互会话尚未就绪，请先连接并打开沙盒桌面，再检查配对',
                operation='exec', returncode=error.returncode, hresult=error.hresult) from None
        raise
    if type(value.get('ExitCode')) is not int or value['ExitCode'] != 0:
        raise SandboxError('无法核验沙盒桌面交互用户，未创建或映射配对', operation='exec',
            guest_exit_code=value.get('ExitCode') if type(value.get('ExitCode')) is int else None)


class SandboxError(ValueError):
    def __init__(self, message, *, operation=None, returncode=None, hresult=None, guest_exit_code=None):
        super().__init__(message)
        self.operation = operation
        self.returncode = returncode
        self.hresult = hresult
        self.guest_exit_code = guest_exit_code


def _error_scalars(error):
    """仅保存已知操作及数字；CLI 原文可能包含路径或凭据。"""
    value = {}
    if getattr(error, 'operation', None) in ('list', 'share', 'exec'):
        value['operation'] = error.operation
    if type(getattr(error, 'returncode', None)) is int:
        value['returncode'] = error.returncode
    if type(getattr(error, 'guest_exit_code', None)) is int:
        value['guest_exit_code'] = error.guest_exit_code
    code = getattr(error, 'hresult', None)
    if isinstance(code, str) and re.fullmatch(r'0x[0-9A-F]{8}', code):
        value['hresult'] = code
    return value


def _reject_links(path, *, allow_app_alias=False):
    path = Path(path).absolute()
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        alias = (item == path and allow_app_alias
                 and getattr(info, 'st_reparse_tag', None) == APP_EXEC_LINK_TAG)
        if not alias and (item.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise SandboxError('隔离后台路径包含链接，已拒绝访问')
    return path


def _sandbox_id(value):
    text = str(value or '')
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError):
        raise SandboxError('请选择有效的 Windows Sandbox 实例') from None
    if str(parsed) != text.lower():
        raise SandboxError('沙盒实例编号格式不正确')
    return str(parsed)


def _program_tree(app_dir):
    """程序树也不能借嵌套链接把原用户目录映射进沙盒。"""
    app_dir = _reject_links(app_dir)
    pending = [app_dir]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise SandboxError('程序目录包含链接，不能映射到隔离后台')
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
        except OSError:
            raise SandboxError('程序目录无法完整读取，未映射到隔离后台') from None
    return app_dir


def packaged_version(executable):
    try:
        import win32api
        info = win32api.GetFileVersionInfo(str(executable), '\\')
        high, low = info['FileVersionMS'], info['FileVersionLS']
        return f'{high >> 16}.{high & 65535}.{low >> 16}'
    except Exception:
        raise SandboxError('无法核验隔离程序版本，请先重新构建或覆盖安装当前版本') from None


def _runtime_fingerprint(executable, internal):
    digest = hashlib.sha256()
    with executable.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    size = executable.stat().st_size
    for path in sorted(internal.rglob('*')):
        _reject_links(path)
        if not path.is_file():
            continue
        info = path.stat()
        digest.update(path.relative_to(internal).as_posix().encode('utf-8'))
        digest.update(f':{info.st_size}\n'.encode('ascii'))
        # 沙盒会执行这些依赖；大小和时间戳都能被保留，不能用元数据代替内容核验。
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        size += info.st_size
    return digest.hexdigest(), size


def _cached_program_tree(target):
    target = _program_tree(target)
    if {path.name for path in target.iterdir()} != {'WhaleGirlWeChat.exe', '_internal', 'runtime.json'}:
        # 覆盖升级遗留的配置只要混进缓存根目录，也不能随整个目录映射给沙盒。
        raise SandboxError('隔离程序缓存包含额外文件，未映射；请重新构建后准备')
    return target


def _prepare_program(root, app_dir):
    """只暂存 PyInstaller 运行目录，不分享覆盖升级保留的旧配置与聊天。"""
    app_dir = _reject_links(app_dir)
    executable = _reject_links(app_dir / 'WhaleGirlWeChat.exe')
    internal = _reject_links(app_dir / '_internal')
    if not executable.is_file() or not internal.is_dir():
        raise SandboxError('隔离后台需要已构建的程序目录，请先生成安装包或使用安装版控制台')
    if packaged_version(executable) != APP_VERSION:
        raise SandboxError('程序目录版本与控制台不一致，请先构建当前版本再准备隔离后台')
    _program_tree(internal)
    fingerprint, size = _runtime_fingerprint(executable, internal)
    if not re.fullmatch(r'\d+\.\d+\.\d+', APP_VERSION):
        raise SandboxError('当前程序版本格式不支持隔离运行')
    cache_root = _reject_links(Path(root) / 'data' / 'isolated_wechat_programs' / APP_VERSION)
    cache_root.mkdir(parents=True, exist_ok=True)
    _reject_links(cache_root)
    target = _reject_links(cache_root / fingerprint[:20])
    with _PROGRAM_LOCK:
        if target.is_dir():
            manifest = _read_small(target / 'runtime.json')
            if manifest.get('fingerprint') == fingerprint and manifest.get('app_version') == APP_VERSION:
                _cached_program_tree(target)
                if _runtime_fingerprint(target / executable.name, target / '_internal')[0] != fingerprint:
                    raise SandboxError('隔离程序缓存内容已改变，保留原目录，请重新构建后准备')
                return target, fingerprint[:20]
            raise SandboxError('隔离程序缓存无法验证，保留旧缓存，请重新构建后准备')
        if shutil.disk_usage(cache_root).free < size + 64 * 1024 * 1024:
            raise SandboxError('磁盘空间不足，无法准备隔离程序；原配置与旧缓存保留')
        stage = _reject_links(cache_root / ('.preparing-' + uuid.uuid4().hex))
        create_private_directory(stage)
        try:
            shutil.copy2(executable, stage / executable.name)
            shutil.copytree(internal, stage / '_internal')
            _program_tree(stage)
            if _runtime_fingerprint(executable, internal)[0] != fingerprint:
                raise SandboxError('复制期间程序发生变化，请在更新结束后重新准备')
            if _runtime_fingerprint(stage / executable.name, stage / '_internal')[0] != fingerprint:
                raise SandboxError('隔离程序复制核验失败，未映射到沙盒')
            _write_session(stage / 'runtime.json',
                           {'app_version': APP_VERSION, 'fingerprint': fingerprint})
            stage.replace(target)
        except BaseException:
            # 只清理本次在版本缓存内创建的临时目录，不触碰旧缓存和用户数据。
            if stage.exists():
                checked = _reject_links(stage)
                if checked.parent != cache_root or not checked.name.startswith('.preparing-'):
                    raise SandboxError('隔离程序临时目录边界异常，未清理')
                shutil.rmtree(checked)
            raise
    return target, fingerprint[:20]


def find_wsb():
    """只使用 Windows 的固定 CLI 位置，避免从任意 PATH 程序启动。"""
    candidates = []
    if os.environ.get('SystemRoot'):
        candidates.append(Path(os.environ['SystemRoot']) / 'System32' / 'wsb.exe')
    if os.environ.get('LOCALAPPDATA'):
        candidates.append(Path(os.environ['LOCALAPPDATA']) / 'Microsoft' / 'WindowsApps' / 'wsb.exe')
    for path in candidates:
        try:
            if path.is_file():
                return _reject_links(path, allow_app_alias=True)
        except OSError:
            continue
    raise SandboxError('未找到 Windows Sandbox 命令行，请先启用并打开 Windows 沙盒')


def _run_wsb(arguments, *, timeout=20):
    command = [str(find_wsb()), *map(str, arguments), '--raw']
    operation = str(arguments[0]) if arguments and arguments[0] in ('list', 'share', 'exec') else None
    try:
        # CLI 打开的窗口可能继承标准输出句柄。PIPE 在超时杀掉直接进程后仍会
        # 等待窗口关闭才得到 EOF，导致 timeout 失效；临时文件不等待后代句柄。
        # Windows 删除式临时句柄也会影响后续窗口的继承；使用普通私有文件。
        # 用户窗口仍持有句柄时只尝试清理，不等待它关闭，也不终止该窗口。
        with tempfile.TemporaryDirectory(prefix='whale-wsb-cli-', ignore_cleanup_errors=True) as folder:
            with (Path(folder) / 'stdout').open('w+b') as output, (Path(folder) / 'stderr').open('w+b') as errors:
                result = subprocess.run(command, stdout=output, stderr=errors,
                    timeout=timeout, check=False,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                limit = 2 * 1024 * 1024
                output.seek(0, os.SEEK_END)
                errors.seek(0, os.SEEK_END)
                if output.tell() + errors.tell() > limit:
                    raise SandboxError('Windows Sandbox 返回结果超出读取上限')
                output.seek(0)
                raw = output.read(limit + 1)
                if len(raw) > limit:
                    raise SandboxError('Windows Sandbox 返回结果超出读取上限')
                if result.returncode:
                    errors.seek(0)
                    error_raw = errors.read(limit + 1)
                    # 只解释固定格式错误码，绝不把异常正文或命令带入界面。
                    codes = re.findall(rb'(?<![A-Za-z0-9])0x[0-9a-fA-F]{8}(?![A-Za-z0-9])', raw + error_raw)
                    hresult = codes[0].decode('ascii').upper().replace('0X', '0x') if codes else None
                    suffix = f'（退出码 {result.returncode}' + (f'，{hresult}' if hresult else '') + '）'
                    raise SandboxError('Windows Sandbox 操作失败，请检查沙盒是否正常、共享目录是否被占用' + suffix,
                        operation=operation, returncode=result.returncode, hresult=hresult)
    except subprocess.TimeoutExpired:
        raise SandboxError('Windows Sandbox 操作超时，请检查沙盒是否仍在运行', operation=operation) from None
    except OSError:
        raise SandboxError('无法运行 Windows Sandbox 命令行') from None
    raw = raw.decode('utf-8', 'replace').lstrip('\ufeff').strip()
    if not raw:
        return {}  # 部分系统的 share 成功只用退出码确认，没有 JSON 正文。
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise SandboxError('Windows Sandbox 返回了无法识别的结果') from None
    if not isinstance(value, dict):
        raise SandboxError('Windows Sandbox 返回格式不正确')
    return value


def list_sandboxes():
    result = _run_wsb(['list'])
    rows = result.get('WindowsSandboxEnvironments')
    if not isinstance(rows, list):
        raise SandboxError('Windows Sandbox 实例清单格式不正确')
    identifiers = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ident = _sandbox_id(row.get('Id'))
        except SandboxError:
            continue
        if ident not in identifiers:
            identifiers.append(ident)
    return [{'id': ident} for ident in identifiers]


SANDBOX_EXE_NAME = 'WindowsSandbox.exe'
LAUNCH_TIMEOUT_SECONDS = 120.0
LAUNCH_POLL_SECONDS = 2.0
LAUNCH_CONFIG_MAX_AGE = 600.0
SANDBOX_LAUNCH_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Configuration>
  <MemoryInMB>4096</MemoryInMB>
  <Networking>Enable</Networking>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <PrinterRedirection>Disable</PrinterRedirection>
  <vGPU>Disable</vGPU>
</Configuration>
"""


class SandboxLaunchError(SandboxError):
    """启动沙盒的稳定失败原因：reason 供界面与回归直接匹配，语句面向用户。"""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def find_sandbox_executable():
    """只使用 Windows 固定位置，避免从任意 PATH 启动同名程序。"""
    path = Path(os.environ.get('SystemRoot') or r'C:\Windows') / 'System32' / SANDBOX_EXE_NAME
    try:
        if path.is_file():
            return path
    except OSError:
        pass
    raise SandboxLaunchError('sandbox_exe_missing',
        '未找到 Windows Sandbox 启动程序，请先启用并打开 Windows 沙盒')


def _discard_config(path):
    """只删除本进程刚写下的那一份；清理失败不能盖住原始错误。"""
    try:
        path.unlink()
    except OSError:
        pass


def _purge_stale_configs(folder):
    """清掉更早启动遗留的配置；本次要用的那一份留给沙盒读取，启动期间不删。"""
    now = time.time()
    for path in folder.glob('*.wsb'):
        try:
            if now - path.stat().st_mtime > LAUNCH_CONFIG_MAX_AGE:
                path.unlink()
        except OSError:
            continue


def _launch_config(root):
    """配置只写到数据目录，工程目录不落临时文件。"""
    folder = _reject_links(Path(root)) / 'data' / 'sandbox_launch'
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise SandboxLaunchError('config_write_failed',
            '无法创建沙盒配置目录，未启动沙盒；请检查数据目录是否可写') from None
    _purge_stale_configs(folder)
    return folder / ('sandbox-' + uuid.uuid4().hex + '.wsb')


def _write_config(path):
    try:
        path.write_text(SANDBOX_LAUNCH_CONFIG, 'utf-8')
    except OSError:
        _discard_config(path)
        raise SandboxLaunchError('config_write_failed',
            '无法写入沙盒配置文件，未启动沙盒；请检查数据目录是否可写') from None


def launch_sandbox(root):
    """控制台自己打开一台 Windows 沙盒：已有实例直接复用，绝不启动第二台。"""
    existing = list_sandboxes()
    if existing:
        return {'success': True, 'sandbox_id': existing[0]['id'], 'created': False,
            'message': '已有运行中的 Windows 沙盒，未重复启动；请直接使用该实例'}
    executable = find_sandbox_executable()
    config = _launch_config(root)
    _write_config(config)
    known = {row['id'] for row in existing}
    try:
        # 独立进程：不等待、不回收窗口；标准句柄不继承，控制台退出也带不走它。
        subprocess.Popen([str(executable), str(config)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0))
    except OSError:
        _discard_config(config)
        raise SandboxLaunchError('launch_failed',
            '无法启动 Windows Sandbox 进程，请检查沙盒功能是否正常') from None
    deadline = time.monotonic() + LAUNCH_TIMEOUT_SECONDS
    while True:
        try:
            rows = list_sandboxes()
        except SandboxError:
            rows = []  # 轮询期间的 CLI 抖动不改变“等不到实例”的结论。
        for row in rows:
            if row['id'] not in known:
                return {'success': True, 'sandbox_id': row['id'], 'created': True,
                    'message': '已由控制台打开 Windows 沙盒；请先在沙盒内安装并登录微信'}
        if time.monotonic() >= deadline:
            break
        time.sleep(LAUNCH_POLL_SECONDS)
    raise SandboxLaunchError('launch_timeout',
        '等待沙盒实例超时：%d 秒内没有出现新的 Windows 沙盒，可能仍在初始化，请稍后刷新清单'
        % max(1, round(LAUNCH_TIMEOUT_SECONDS)))


GUEST_INSTALL = r'C:\WhaleInstall'
GUEST_OUT = r'C:\WhaleOut'
GUEST_POWERSHELL = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
CLIENT_EXECUTABLE = r'C:\Program Files\Tencent\Weixin\Weixin.exe'
INSTALL_TIMEOUT_SECONDS = 300.0
INSTALL_POLL_SECONDS = 5.0
INSTALL_EXEC_TIMEOUT = 60.0
CLIENT_PROBE = 'cmd /c if exist "%s" (exit 0) else (exit 7)' % CLIENT_EXECUTABLE
INSTALL_MESSAGES = {
    'installer_missing': '未找到可用的微信安装包，请选择本机已存在的 .exe 文件，不要用快捷方式或相对路径',
    'sandbox_not_running': '沙盒未打开：请先打开 Windows Sandbox，等它进入桌面并在下拉框里选中实例后再安装微信',
    'share_failed': '无法把目录共享进沙盒；请确认安装包所在位置可读、沙盒已进入桌面后重试',
    'exec_failed': '沙盒拒绝执行安装脚本；请确认沙盒已进入桌面且没有关闭后重试',
    'install_timeout': '等待微信安装完成超时：%d 秒内没有出现微信主程序，请检查沙盒窗口里的安装进度后重试',
    'already_installed': '沙盒内已安装微信，未重复运行安装包',
}


class SandboxInstallError(SandboxError):
    """安装微信的稳定失败原因：reason 供界面与回归直接匹配，语句面向用户。"""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _install_error(reason, message=None):
    """默认文案里只有安装超时带秒数；%d 在抛出时才填。"""
    text = message or INSTALL_MESSAGES[reason]
    if '%d' in text:
        text = text % max(1, round(INSTALL_TIMEOUT_SECONDS))
    return SandboxInstallError(reason, text)


def _install_sandbox_id(value):
    """空编号表示面板还没选中实例；只有唯一一台在运行时才替用户选中它。"""
    wanted = str(value or '').strip().lower()
    try:
        running = [row['id'] for row in list_sandboxes()]
    except SandboxError:
        raise _install_error('sandbox_not_running') from None
    if not wanted and len(running) == 1:
        return running[0]
    if wanted not in running:
        raise _install_error('sandbox_not_running')
    return wanted


def _installer_file(value):
    """安装包只做只读映射：必须是已存在的绝对 .exe，链接与相对路径一律拒绝。"""
    path = Path(str(value or ''))
    if not path.is_absolute() or path.suffix.lower() != '.exe':
        raise _install_error('installer_missing')
    try:
        path = _reject_links(path)
    except SandboxError:
        raise _install_error('installer_missing') from None
    if not path.is_file():
        raise _install_error('installer_missing')
    return path


def _install_out(root):
    """宿主可写目录：客体只往这里写日志，安装包目录本身保持只读。"""
    folder = _reject_links(Path(root)) / 'data' / 'sandbox_install'
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise _install_error('share_failed',
            '无法创建安装日志目录，未在沙盒内安装微信；请检查数据目录是否可写') from None
    return folder


def _install_script(name):
    """客体脚本只负责静默启动；进度写进共享目录，因为 exec 只回退出码。"""
    return '\n'.join((
        "$ErrorActionPreference = 'Stop'",
        "$log = '" + GUEST_OUT + "\\install.log'",
        'function Write-Log($text) { Add-Content -LiteralPath $log -Value $text -Encoding ASCII }',
        "Write-Log 'installing'",
        "$installer = '" + GUEST_INSTALL + '\\' + str(name).replace("'", "''") + "'",
        "if (-not (Test-Path -LiteralPath $installer)) { Write-Log 'installer_missing'; exit 2 }",
        "$process = Start-Process -FilePath $installer -ArgumentList '/S' -WindowStyle Hidden -PassThru",
        "Write-Log ('started ' + [string]$process.Id)",
        "Write-Log 'launched'",
        'exit 0',
    )) + '\n'


def _share_install(sandbox_id, host_path, guest_path, *, writable=False):
    """映射成功只用退出码确认；失败不落记录，可以直接重试。"""
    arguments = ['share', '--id', sandbox_id, '--host-path', str(host_path),
                 '--sandbox-path', guest_path]
    if writable:
        arguments.append('--allow-write')
    try:
        _run_wsb(arguments)
    except SandboxError:
        raise _install_error('share_failed') from None


def _install_exec(sandbox_id):
    """必须执行脚本文件：从宿主 shell 拼内联脚本会被展开 $ 变量而改坏内容。"""
    command = (GUEST_POWERSHELL + ' -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
               + GUEST_OUT + '\\install.ps1')
    try:
        result = _run_wsb(['exec', '--id', sandbox_id, '--run-as', 'ExistingLogin',
                           '--command', command], timeout=INSTALL_EXEC_TIMEOUT)
    except SandboxError:
        raise _install_error('exec_failed') from None
    code = result.get('ExitCode')
    if type(code) is not int:
        raise _install_error('exec_failed')
    if code != 0:
        raise _install_error('exec_failed',
            INSTALL_MESSAGES['exec_failed'] + '（客体退出码 %d）' % code)


def _client_present(sandbox_id):
    """客体探针只回退出码：if exist 命中为 0；轮询期间的 CLI 抖动不改结论。"""
    try:
        result = _run_wsb(['exec', '--id', sandbox_id, '--run-as', 'ExistingLogin',
                           '--command', CLIENT_PROBE], timeout=25)
    except SandboxError:
        return False
    return type(result.get('ExitCode')) is int and result['ExitCode'] == 0


def install_client(root, sandbox_id, installer_path):
    """在所选沙盒内静默安装微信：已装则短路，否则映射、执行并轮询安装结果。"""
    ident = _install_sandbox_id(sandbox_id)
    if _client_present(ident):
        return {'success': True, 'installed': True, 'already_installed': True,
                'reason': 'already_installed', 'executable': CLIENT_EXECUTABLE,
                'message': INSTALL_MESSAGES['already_installed']}
    installer = _installer_file(installer_path)
    out = _install_out(root)
    try:
        (out / 'install.ps1').write_text(_install_script(installer.name), encoding='utf-8-sig')
    except OSError:
        raise _install_error('share_failed',
            '无法写入安装脚本，未在沙盒内安装微信；请检查数据目录是否可写') from None
    _share_install(ident, installer.parent, GUEST_INSTALL)
    _share_install(ident, out, GUEST_OUT, writable=True)
    _install_exec(ident)
    deadline = time.monotonic() + INSTALL_TIMEOUT_SECONDS
    while True:
        if _client_present(ident):
            return {'success': True, 'installed': True, 'already_installed': False,
                    'executable': CLIENT_EXECUTABLE,
                    'message': '已在该沙盒内装好微信；请手动登录后再准备隔离通道'}
        if time.monotonic() >= deadline:
            break
        time.sleep(INSTALL_POLL_SECONDS)
    raise _install_error('install_timeout')


def _channel_root(root, create=False):
    root = _reject_links(root)
    path = _reject_links(root / 'data' / 'isolated_wechat')
    if create:
        path.mkdir(parents=True, exist_ok=True)
        _reject_links(path)
    return path


def channel_path(root, channel_id):
    if not CHANNEL_ID.fullmatch(str(channel_id or '')):
        raise SandboxError('隔离通道编号无效')
    acl_recovery.check_channel(root, channel_id)
    path = _reject_links(_channel_root(root) / channel_id)
    if not path.is_dir():
        raise SandboxError('隔离通道不存在，请重新配对')
    from .isolated_wechat.protocol import load_pairing
    pairing = load_pairing(path)
    if pairing['channel_id'] != channel_id:
        raise SandboxError('隔离通道目录与配对身份不一致')
    return path


def _read_small(path, limit=8192):
    path = _reject_links(path)
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise SandboxError('隔离通道元数据过大')
    value = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(value, dict):
        raise SandboxError('隔离通道元数据格式无效')
    return value


def _write_session(path, value):
    from .persistence import atomic_text
    _reject_links(path)
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2), backup=False)


def _resume_evidence(root, path, original, *, hash_runtime=False):
    """只承认已完成两份映射、且从未进入 launch 的恢复；不修改原证据。"""
    record = _read_small(_reject_links(path / 'recovery.json'), limit=64 * 1024)
    if (type(record.get('version')) is not int or record['version'] != 1
            or record.get('channel_id') != path.name or record.get('sandbox_id') != original['sandbox_id']
            or record.get('original_session') != original or record.get('channel_mapping') != 'mapped'
            or record.get('stage') != 'failed' or record.get('failed_operation') != 'process_probe'
            or 'launch_started' in record or record.get('rpc_requests') != 0
            or type(record.get('rpc_requests')) is not int):
        raise SandboxError('此配对已有恢复记录，映射结果可能不明确；未重复恢复，请保留诊断记录')
    version, key = record.get('app_version'), record.get('program_key')
    if (not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version)
            or not isinstance(key, str) or not PROGRAM_KEY.fullmatch(key)):
        raise SandboxError('恢复记录的程序身份无法核验，未继续恢复')
    registry = _read_program_registry(path, original['sandbox_id'])
    mapping = registry['mappings'].get(key)
    if not mapping or mapping['stage'] != 'mapped' or mapping['app_version'] != version:
        raise SandboxError('原程序映射尚未确认，未继续恢复')
    cache = _reject_links(Path(root) / 'data/isolated_wechat_programs' / version / key)
    manifest = _read_small(cache / 'runtime.json')
    if manifest != {'app_version': version, 'fingerprint': mapping['fingerprint']}:
        raise SandboxError('原程序完整指纹与映射不符，未继续恢复')
    if hash_runtime:
        _cached_program_tree(cache)
        if _runtime_fingerprint(cache / 'WhaleGirlWeChat.exe', cache / '_internal')[0] != mapping['fingerprint']:
            raise SandboxError('原程序缓存内容已改变，未继续恢复')
    journals = _resume_journal_records(path, original)
    if any(journal.get('stage') != 'failed'
           or journal.get('failed_operation') not in ('preflight', 'session_probe', 'process_probe')
           or journal.get('launch_started') is not None for journal in journals):
        raise SandboxError('此前续接存在未确认副作用或证据不符，未重复映射或启动')
    return record, journals



def _resume_journal_records(path, original):
    records = []
    digest = hashlib.sha256((path / 'recovery.json').read_bytes()).hexdigest()
    for journal_path in sorted(path.glob('resume-*.json')):
        if not re.fullmatch(r'resume-[0-9a-f]{32}\.json', journal_path.name):
            raise SandboxError('续接记录名称无法核验，未继续恢复')
        journal = _read_small(_reject_links(journal_path), limit=64 * 1024)
        if (type(journal.get('version')) is not int or journal['version'] != 1
                or journal.get('channel_id') != path.name or journal.get('sandbox_id') != original['sandbox_id']
                or journal.get('original_session') != original or journal.get('recovery_sha256') != digest
                or not isinstance(journal.get('program_key'), str) or not PROGRAM_KEY.fullmatch(journal['program_key'])
                or not isinstance(journal.get('app_version'), str) or not re.fullmatch(r'\d+\.\d+\.\d+', journal['app_version'])
                or type(journal.get('created')) not in (int, float) or not math.isfinite(journal['created'])):
            raise SandboxError('续接记录身份或指纹无法核验，未继续恢复')
        records.append(journal)
    return records

def _recovery_diagnostic(root, path, session):
    """状态读取只核有限元数据；真正续接前仍完整核验缓存，不每四秒重哈希程序。"""
    result = {}
    operation = session.get('failed_operation')
    if isinstance(operation, str) and operation in RECOVERY_OPERATIONS:
        result['initial_failed_operation'] = operation
    def failure_scalars(prefix, value):
        if not isinstance(value, dict):
            return
        if type(value.get('returncode')) is int:
            result[prefix + '_returncode'] = value['returncode']
        code = value.get('hresult')
        if isinstance(code, str) and re.fullmatch(r'0x[0-9A-F]{8}', code):
            result[prefix + '_hresult'] = code
    failure_scalars('initial', session.get('failure'))
    try:
        repaired = acl_recovery.diagnostic(root, path.name, session)
        if repaired is not None:
            return dict(result, **repaired)
    except (OSError, ValueError, RuntimeError):
        return dict(result, recovery_state='blocked', recovery_reason=acl_recovery.BLOCKED_REASON)
    record_path = _reject_links(path / 'recovery.json')
    if not record_path.exists():
        return dict(result, recovery_state='none', recovery_reason='尚无恢复记录，可显式修复失败配对')
    try:
        record = _read_small(record_path, limit=64 * 1024)
        if record.get('channel_id') != path.name or record.get('sandbox_id') != session['sandbox_id']:
            raise SandboxError('record identity')
        result.update(recovery_state='blocked', recovery_reason='恢复证据不足或映射结果未知；保留记录，不能重复映射或启动')
        if record.get('stage') in RECOVERY_STAGES:
            result['recovery_stage'] = record['stage']
        if record.get('failed_operation') in RECOVERY_OPERATIONS:
            result['recovery_failed_operation'] = record['failed_operation']
        failure_scalars('recovery', record.get('failure'))
        prior_session = record.get('original_session')
        if not isinstance(prior_session, dict) or prior_session.get('channel_id') != path.name or prior_session.get('sandbox_id') != session['sandbox_id']:
            raise SandboxError('original recovery identity')
        journals = _resume_journal_records(path, prior_session)
        latest = max(journals, key=lambda row: row['created']) if journals else record
        if latest.get('stage') in RECOVERY_STAGES:
            result['recovery_stage'] = latest['stage']
        if latest.get('failed_operation') in RECOVERY_OPERATIONS:
            result['recovery_failed_operation'] = latest['failed_operation']
        if journals:
            result.pop('recovery_hresult', None); result.pop('recovery_returncode', None)
            failure_scalars('recovery', latest.get('failure'))
        if (session.get('stage') == 'prepared' and latest.get('stage') == 'ready'
                and latest.get('program_key') == session.get('program_key')
                and latest.get('app_version') == session.get('app_version')):
            result.update(recovery_state='ready', recovery_reason='配对恢复已经完成')
            return result
        try:
            acl_recovery.eligible(root, path, session)
        except (OSError, ValueError, RuntimeError, TypeError, KeyError):
            pass
        else:
            return dict(result, recovery_state='repairable', recovery_reason=acl_recovery.REPAIRABLE_REASON)
        _resume_evidence(root, path, session)
        result.update(recovery_state='resumable', recovery_reason=
            '原映射已确认，可在重新核验后继续恢复；不会重复映射通道')
        return result
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        return dict(result, recovery_state='blocked', recovery_reason='恢复记录无法核验；保留原始证据')


def _version_text(value):
    """只回显有界的版本串；未知或异常内容不进入界面文案。"""
    return value if isinstance(value, str) and re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z.+-]{0,23}', value) else ''


def _pair_status(root, ident, running_ids):
    """仅导出显示字段；不把 secret、原始消息或完整路径交给界面。"""
    path = channel_path(root, ident)
    session = _read_small(path / 'session.json')
    if session.get('channel_id') != ident:
        raise SandboxError('隔离通道元数据身份不一致')
    _session_launch_key(session)  # 状态候选也须通过准备时的版本/构建/映射结构核验。
    _session_channel_key(session)
    sandbox = _sandbox_id(session.get('sandbox_id'))
    result = {'channel_id': ident, 'sandbox_id': sandbox, 'short_id': ident[:12],
              'stage': 'prepared', 'agent_online': False, 'wechat_online': False,
              'app_version': str(session.get('app_version') or ''),
              'reason': '已准备，尚未检测到隔离微信代理'}
    result.update(_recovery_diagnostic(root, path, session))
    if sandbox not in running_ids:
        result.update(stage='sandbox_closed', reason='沙盒已关闭；请重新打开、登录并配对')
        return result
    stage = session.get('stage')
    if stage not in ('preparing', 'prepared', 'failed'):
        raise SandboxError('隔离配对准备状态无法核验')
    if stage != 'prepared':
        # 启动超时或登记失败时代理仍可能在线；在线心跳不能代替映射提交，
        # 更不能让尚未确认退出的失败候选成为activate的可选目标。
        result.update(stage=stage, reason='上次配对未完成，请检查沙盒后显式恢复'
                      if stage == 'failed' else '隔离配对尚未完成，不能启用')
        return result
    try:
        from .isolated_wechat.protocol import live_service, load_pairing
        service = live_service(load_pairing(path))
        key = session.get('program_key')
        if (('program_key' in service and service['program_key'] != key)
                or (session.get('program_mapping') == 'by_build' and service.get('program_key') != key)):
            # 旧版本也须先核对原配对构建；不能将身份冲突解释为可直接升级。
            result.update(stage='build_mismatch', reason='隔离代理构建不一致，请重新准备并核验')
        elif session.get('app_version') != APP_VERSION or service.get('app_version') != APP_VERSION:
            # 心跳已由 live_service 核验；版本不符不等于代理离线，界面据此提供原地升级。
            current = (_version_text(service.get('app_version'))
                       or _version_text(session.get('app_version')) or '未知版本')
            result.update(stage='version_mismatch', agent_online=True,
                reason='隔离代理在线，版本 %s 与控制台 %s 不一致；可原地升级代理，升级不会启动机器人'
                       % (current, APP_VERSION))
        else:
            status = service.get('status')
            online = isinstance(status, dict) and status.get('online') is True
            result.update(stage='ready' if online else 'agent_ready', agent_online=True,
                          wechat_online=online,
                          reason='微信读取已连接；只回复当前已打开会话' if online else
                                 '隔离代理已运行；启用后再由启动按钮连接微信')
    except (OSError, ValueError, RuntimeError):
        if session.get('stage') == 'failed':
            result.update(stage='failed', reason='上次配对未完成，请检查沙盒后重新准备')
    return result


def paired_status(root):
    try:
        sandboxes = list_sandboxes()
        available, message = True, '仅支持当前已打开会话；启动机器人仍需单独操作'
    except SandboxError as error:
        sandboxes = []
        available, message = False, str(error)
    running = {row['id'] for row in sandboxes}
    pairs = []
    folder = _channel_root(root)
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if not CHANNEL_ID.fullmatch(path.name):
                continue
            try:
                pairs.append(_pair_status(root, path.name, running))
            except (OSError, ValueError, RuntimeError):
                # 无法验证的目录不允许成为启用候选，也不披露其内容。
                continue
    try:
        repair_rows = acl_recovery.status_rows(root, running)
        ids = {row['channel_id'] for row in repair_rows}
        pairs = [row for row in pairs if row['channel_id'] not in ids] + repair_rows
        acl_recovery.check_pending(root)
    except (OSError, ValueError, RuntimeError):
        available = False
        message = acl_recovery.BLOCKED_REASON
    return {'success': True, 'available': available, 'sandboxes': sandboxes,
            'pairs': pairs, 'message': message}


def _build_guest_app(program_key):
    if not isinstance(program_key, str) or not PROGRAM_KEY.fullmatch(program_key):
        raise SandboxError('隔离程序构建编号无效')
    return GUEST_APP + '-' + program_key


def _session_launch_key(session):
    if type(session.get('version')) is not int or session['version'] != 1:
        raise SandboxError('隔离通道元数据版本无效，未启动代理')
    key = session.get('program_key')
    _build_guest_app(key)  # 旧路径也必须保留有效的构建编号，不接受路径字段控制命令。
    mapping = session.get('program_mapping', 'legacy')
    if mapping not in ('legacy', 'by_build'):
        raise SandboxError('隔离程序映射元数据无效，未启动代理')
    return key if mapping == 'by_build' else None


def _build_guest_channel(channel_id):
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise SandboxError('隔离通道映射编号无效，未启动代理')
    return GUEST_CHANNEL + '-' + channel_id


def _session_channel_key(session):
    mapping = session.get('channel_mapping', 'legacy')
    if mapping not in ('legacy', 'by_channel'):
        raise SandboxError('隔离通道映射元数据无效，未启动代理')
    if mapping == 'by_channel':
        _build_guest_channel(session.get('channel_id'))
        return session['channel_id']
    return None


def _launch(sandbox_id, program_key=None, *, channel_id=None):
    app = GUEST_APP if program_key is None else _build_guest_app(program_key)
    channel = GUEST_CHANNEL if channel_id is None else _build_guest_channel(channel_id)
    command = (app + r'\WhaleGirlWeChat.exe --sandbox-agent-launch --channel ' + channel)
    result = _run_wsb(['exec', '--id', sandbox_id, '--run-as', 'ExistingLogin',
                       '--command', command], timeout=25)
    if type(result.get('ExitCode')) is not int or result['ExitCode'] != 0:
        code = result.get('ExitCode') if type(result.get('ExitCode')) is int else None
        suffix = f'（客体退出码 {code}）' if code is not None else ''
        raise SandboxError('隔离代理未能启动，请确认沙盒已进入桌面且程序映射完整' + suffix,
                           operation='exec', guest_exit_code=code)


def _launch_session(sandbox_id, program_key, session):
    channel_id = _session_channel_key(session)
    arguments = {} if channel_id is None else {'channel_id': channel_id}
    if program_key is None:
        return _launch(sandbox_id, **arguments)
    return _launch(sandbox_id, program_key=program_key, **arguments)


def _upgrade_service(pairing):
    from .isolated_wechat.protocol import read_document, valid_epoch
    value = read_document(pairing.root / 'service.json', pairing, 256 * 1024)
    if (not valid_epoch(value.get('epoch')) or type(value.get('active')) is not bool
            or not isinstance(value.get('app_version'), str)
            or not re.fullmatch(r'\d+\.\d+\.\d+', value['app_version'])
            or type(value.get('heartbeat')) not in (int, float)
            or not math.isfinite(value['heartbeat']) or value['heartbeat'] > time.time() + 2
            or ('program_key' in value and value['program_key'] != ''
                and (not isinstance(value['program_key'], str)
                     or not PROGRAM_KEY.fullmatch(value['program_key'])))):
        raise SandboxError('隔离代理停止证据格式无效，未升级程序')
    return value


def _stop_upgrade_agent(channel):
    """只接受真实清理完成的签名记录，不能把失联或删心跳当作已停止。"""
    from .isolated_wechat.protocol import load_pairing, write_document
    pairing = load_pairing(channel)
    try:
        initial = _upgrade_service(pairing)
    except (OSError, ValueError, RuntimeError):
        raise SandboxError('无法核验原配对代理状态，未升级；请先确认原代理已正常停止') from None
    if not initial['active']:
        return pairing, initial['epoch']
    now = time.time()
    write_document(pairing.root / 'control/stop.json', pairing,
                   {'epoch': initial['epoch'], 'action': 'stop', 'request_id': uuid.uuid4().hex,
                    'created': now, 'deadline': now + 30})
    deadline = time.monotonic() + UPGRADE_STOP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            value = _upgrade_service(pairing)
        except (OSError, ValueError, RuntimeError):
            raise SandboxError('原代理停止证据无法核验，保留原通道，未升级') from None
        if value['epoch'] != initial['epoch'] or value['app_version'] != initial['app_version']:
            raise SandboxError('等待停止时隔离代理身份已改变，未升级')
        if not value['active'] and value['heartbeat'] >= initial['heartbeat']:
            return pairing, initial['epoch']
        time.sleep(.1)
    raise SandboxError('尚未确认原代理完成退出，未升级；微信与原通道信息保留')


def _read_program_registry(channel, sandbox_id):
    registry_path = channel / 'program-mappings.json'
    registry = {'version': 1, 'channel_id': channel.name, 'sandbox_id': sandbox_id, 'mappings': {}}
    if registry_path.exists():
        registry = _read_small(registry_path, limit=64 * 1024)
        mappings = registry.get('mappings')
        if (type(registry.get('version')) is not int or registry['version'] != 1
                or registry.get('channel_id') != channel.name
                or registry.get('sandbox_id') != sandbox_id or not isinstance(mappings, dict)
                or len(mappings) > 100 or any(not isinstance(key, str) or not PROGRAM_KEY.fullmatch(key)
                                            for key in mappings)):
            raise SandboxError('隔离程序映射记录无效，未添加新映射')
        for key, value in mappings.items():
            if (not isinstance(value, dict) or set(value) != {'app_version', 'fingerprint', 'stage'}
                    or not isinstance(value.get('app_version'), str)
                    or not re.fullmatch(r'\d+\.\d+\.\d+', value['app_version'])
                    or not isinstance(value.get('fingerprint'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', value['fingerprint'])
                    or not value['fingerprint'].startswith(key)
                    or value.get('stage') not in ('mapped', 'uncertain')):
                raise SandboxError('隔离程序映射记录无效，未添加新映射')
    return registry


def _share_build_program(channel, sandbox_id, app_dir, program_key):
    """独立记录不可变映射；share 超时不赌其结果，也不覆盖旧固定映射。"""
    registry_path = channel / 'program-mappings.json'
    registry = _read_program_registry(channel, sandbox_id)
    manifest = _read_small(app_dir / 'runtime.json')
    fingerprint = manifest.get('fingerprint')
    if (manifest.get('app_version') != APP_VERSION or not isinstance(fingerprint, str)
            or not re.fullmatch(r'[0-9a-f]{64}', fingerprint) or fingerprint[:20] != program_key):
        raise SandboxError('隔离程序构建清单不符，未映射')
    record = {'app_version': APP_VERSION, 'fingerprint': fingerprint, 'stage': 'mapped'}
    existing = registry['mappings'].get(program_key)
    if existing is not None:
        if existing == record:
            return
        raise SandboxError('此构建的隔离映射尚未确认，未重复映射；原通道信息保留')
    if len(registry['mappings']) >= 100:
        raise SandboxError('此沙盒保留的构建映射已达上限，未添加新映射')
    registry['mappings'][program_key] = dict(record, stage='uncertain')
    _write_session(registry_path, registry)
    _run_wsb(['share', '--id', sandbox_id, '--host-path', str(app_dir),
              '--sandbox-path', _build_guest_app(program_key)])
    registry['mappings'][program_key] = record
    _write_session(registry_path, registry)


def _wait_upgrade_agent(pairing, old_epoch, program_key, *, minimum_heartbeat=None):
    deadline = time.monotonic() + UPGRADE_START_TIMEOUT
    while time.monotonic() < deadline:
        try:
            value = _upgrade_service(pairing)
        except FileNotFoundError:
            time.sleep(.1)
            continue
        except (OSError, ValueError, RuntimeError):
            raise SandboxError('新代理心跳无法核验，未登记升级成功') from None
        if value['epoch'] != old_epoch and value['active']:
            if (value['app_version'] != APP_VERSION or value.get('program_key') != program_key
                    or not -2 <= time.time() - value['heartbeat'] <= 8
                    or (minimum_heartbeat is not None and value['heartbeat'] < minimum_heartbeat - 2)):
                raise SandboxError('新代理构建身份或心跳不符，未登记升级成功')
            return
        time.sleep(.1)
    raise SandboxError('未确认新构建代理上线，保留原通道信息；不会自动启动机器人')


def _cancel_upgrade_candidate(pairing, old_epoch, program_key):
    from .isolated_wechat.protocol import write_document
    try:
        value = _upgrade_service(pairing)
        if (value['active'] and value['epoch'] != old_epoch and value['app_version'] == APP_VERSION
                and value.get('program_key') == program_key):
            now = time.time()
            write_document(pairing.root / 'control/stop.json', pairing,
                           {'epoch': value['epoch'], 'action': 'stop', 'request_id': uuid.uuid4().hex,
                            'created': now, 'deadline': now + 30})
    except (OSError, ValueError, RuntimeError):
        pass  # 只请求停止能确认为本次新构建的代理；不动其他身份或微信。


def _upgrade_pair(path, session, app_dir, program_key):
    pairing, old_epoch = _stop_upgrade_agent(path)
    sandbox_id = session['sandbox_id']
    _read_program_registry(path, sandbox_id)
    _session_probe(sandbox_id)
    try:
        _share_build_program(path, sandbox_id, app_dir, program_key)
        _launch_session(sandbox_id, program_key, session)
        _wait_upgrade_agent(pairing, old_epoch, program_key)
        if _read_small(path / 'session.json') != session:
            raise SandboxError('升级期间通道元数据已改变，未登记升级成功')
        updated = dict(session, app_version=APP_VERSION, program_key=program_key,
                       program_mapping='by_build', stage='prepared', upgraded_at=time.time())
        _write_session(path / 'session.json', updated)
    except BaseException:
        _cancel_upgrade_candidate(pairing, old_epoch, program_key)
        raise
    return {'success': True, 'channel_id': session['channel_id'], 'channel_dir': str(path),
            'sandbox_id': sandbox_id, 'stage': 'agent_ready',
            'message': '隔离代理已安全更新，微信登录及当前会话保留；尚未自动启动机器人'}


def prepare_sandbox(root, app_dir, sandbox_id):
    sandbox_id = _sandbox_id(sandbox_id)
    with _PREPARE_LOCK, _prepare_process_lock(root, sandbox_id):
        return _prepare_sandbox(root, app_dir, sandbox_id)


def _recovery_idle_channel(pairing):
    """没有旧心跳时，只接受从未留下业务证据的原配对，不删除任何证据。"""
    for name in ('requests', 'responses', 'consumed', 'events', 'ocr'):
        folder = _reject_links(pairing.root / name)
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            raise SandboxError('隔离通道已有业务或消费记录，不能使用无心跳恢复；原记录保留')


def _recovery_process_probe(pairing, sandbox_id):
    """固定只读脚本只统计代理/worker；签名与随机挑战绑定本次新映射。"""
    from .isolated_wechat.protocol import read_document
    nonce = uuid.uuid4().hex
    guest = _build_guest_channel(pairing['channel_id'])
    filename = 'recovery-proof-' + nonce + '.json'
    # 配对密钥仅在客体内读取，不拼进命令行，也不查询微信控件或进程内存。
    code = r'''
$ErrorActionPreference='Stop'
if ($env:USERNAME -ne 'WDAGUtilityAccount') { exit 2 }
$folder='__CHANNEL__'
$pair=Get-Content -LiteralPath ($folder+'\pairing.json') -Raw | ConvertFrom-Json
if ($pair.channel_id -cne '__ID__' -or $pair.secret -cnotmatch '^[0-9a-f]{64}$') { exit 3 }
$rows=@(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
if (-not $rows.Count -or @($rows | Where-Object { -not $_.Name }).Count) { exit 4 }
$count=@($rows | Where-Object {
  $_.Name -ieq 'WhaleGirlWeChat.exe' -or
  ($_.CommandLine -and $_.CommandLine -match '(?i)--(?:sandbox-agent(?:-launch)?|wechat-send-worker)(?:\s|$)')
}).Count
$value=[ordered]@{agent_process_count=[int]$count;channel_id='__ID__';nonce='__NONCE__';process_query_ok=$true;version=1}
$plain=$value | ConvertTo-Json -Compress
$key=New-Object byte[] 32
for($i=0;$i -lt 32;$i++) { $key[$i]=[Convert]::ToByte($pair.secret.Substring($i*2,2),16) }
$hmac=New-Object System.Security.Cryptography.HMACSHA256
try {
  $hmac.Key=$key
  $signature=-join ($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($plain)) | ForEach-Object { $_.ToString('x2') })
} finally { $hmac.Dispose();[Array]::Clear($key,0,$key.Length) }
$value.signature=$signature
$value | ConvertTo-Json -Compress | Set-Content -LiteralPath ($folder+'\__FILE__') -Encoding UTF8
'''.replace('__CHANNEL__', guest).replace('__ID__', pairing['channel_id']).replace('__NONCE__', nonce).replace('__FILE__', filename)
    encoded = base64.b64encode(code.encode('utf-16le')).decode('ascii')
    command = (r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe '
               '-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand ' + encoded)
    value = _run_wsb(['exec', '--id', sandbox_id, '--run-as', 'ExistingLogin', '--command', command], timeout=25)
    if type(value.get('ExitCode')) is not int or value['ExitCode'] != 0:
        code = value.get('ExitCode') if type(value.get('ExitCode')) is int else None
        suffix = f'（客体退出码 {code}）' if code is not None else ''
        raise SandboxError('无法完成隔离代理只读进程核验，未启动代理' + suffix,
                           operation='exec', guest_exit_code=code)
    try:
        proof = read_document(pairing.root / filename, pairing, 8192)
    except (OSError, ValueError, RuntimeError):
        raise SandboxError('隔离代理只读进程回执无法核验，未启动代理') from None
    if (proof.get('nonce') != nonce or proof.get('process_query_ok') is not True
            or type(proof.get('agent_process_count')) is not int or proof['agent_process_count'] != 0):
        raise SandboxError('客体仍有代理或发送进程，或只读证据不足；未启动新代理')
    return {'process_query_ok': True, 'agent_process_count': 0, 'nonce': nonce}


def recover_sandbox(root, app_dir, sandbox_id):
    """显式恢复失败配对；不覆盖旧映射、不创建第二身份、不启动微信或机器人。"""
    sandbox_id = _sandbox_id(sandbox_id)
    with _PREPARE_LOCK, _prepare_process_lock(root, sandbox_id):
        return _recover_sandbox(root, app_dir, sandbox_id)


def _recover_sandbox(root, app_dir, sandbox_id):
    from .isolated_wechat.protocol import load_pairing
    acl_recovery.check_pending(root)
    sandbox_id = _sandbox_id(sandbox_id)
    running = {row['id'] for row in list_sandboxes()}
    if sandbox_id not in running:
        raise SandboxError('所选沙盒已关闭，不能恢复原登录环境')
    pairs = _existing_pairs_for_prepare(root, sandbox_id, running)
    if len(pairs) != 1:
        raise SandboxError('仅能恢复所选沙盒唯一的失败配对；未更改任何映射')
    path = channel_path(root, pairs[0]['channel_id'])
    original = _read_small(path / 'session.json')
    if (original.get('channel_id') != path.name or original.get('sandbox_id') != sandbox_id
            or original.get('stage') != 'failed'):
        raise SandboxError('失败配对归属或状态已改变，未恢复')
    _session_launch_key(original)
    _session_channel_key(original)
    pairing = load_pairing(path)
    record_path = _reject_links(path / 'recovery.json')
    if record_path.exists():
        try:
            acl_recovery.eligible(root, path, original)
        except (OSError, ValueError, RuntimeError, TypeError, KeyError):
            pass
        else:
            return acl_recovery.repair(root, app_dir, sandbox_id, path, original, pairing)
        return _resume_recovery(root, app_dir, sandbox_id, path, original, pairing)
    service_path = _reject_links(path / 'service.json')
    if service_path.exists():
        pairing, old_epoch = _stop_upgrade_agent(path)
        evidence = 'signed_inactive'
    else:
        _recovery_idle_channel(pairing)
        old_epoch = ''
        evidence = 'no_service_no_business'
    app_dir, program_key = _prepare_program(root, app_dir)
    _session_probe(sandbox_id)
    record = {'version': 1, 'channel_id': path.name, 'sandbox_id': sandbox_id,
              'app_version': APP_VERSION, 'program_key': program_key,
              'original_session': original, 'original_service_evidence': evidence,
              'created': time.time(), 'stage': 'reserved', 'rpc_requests': 0}
    # 独占保留首次尝试，另一进程不能覆盖尚未完成的映射证据。
    try:
        with record_path.open('x', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise SandboxError('此配对已开始恢复，未重复映射') from None
    operation = 'share_program'
    try:
        record.update(stage='mapping_program', current_operation=operation)
        _write_session(record_path, record)
        _share_build_program(path, sandbox_id, app_dir, program_key)
        operation = 'share_channel'
        record.update(stage='mapping_channel', current_operation=operation, channel_mapping='uncertain')
        _write_session(record_path, record)
        _run_wsb(['share', '--id', sandbox_id, '--host-path', str(path),
                  '--sandbox-path', _build_guest_channel(path.name), '--allow-write'])
        record.update(stage='channel_mapped', channel_mapping='mapped')
        _write_session(record_path, record)
        operation = 'process_probe'
        record['process_probe'] = _recovery_process_probe(pairing, sandbox_id)
        # 进程采样不宣称“从未启动”；最终仍由相同channel互斥锁及新签名代次限制实例。
        if evidence == 'no_service_no_business':
            _recovery_idle_channel(pairing)
            if service_path.exists():
                raise SandboxError('只读核验期间原代理状态已改变，未启动新代理')
        elif _upgrade_service(pairing).get('active') is not False:
            raise SandboxError('只读核验期间原代理重新运行，未启动新代理')
        operation = 'launch'
        started = time.time()
        record.update(stage='launching', current_operation=operation, launch_started=started)
        _write_session(record_path, record)
        _launch(sandbox_id, program_key=program_key, channel_id=path.name)
        operation = 'verify_heartbeat'
        _wait_upgrade_agent(pairing, old_epoch, program_key, minimum_heartbeat=started)
        if _read_small(path / 'session.json') != original:
            raise SandboxError('恢复期间原配对元数据已改变，未登记成功')
        updated = dict(original, app_version=APP_VERSION, program_key=program_key,
                       program_mapping='by_build', channel_mapping='by_channel',
                       stage='prepared', recovered_at=time.time())
        record.update(stage='ready', current_operation='complete', completed=time.time())
        _write_session(record_path, record)
        # 最后一次可失败的持久操作才切换有效session；失败前始终保留原文。
        _write_session(path / 'session.json', updated)
    except BaseException as error:
        if 'launch_started' in record:
            _cancel_upgrade_candidate(pairing, old_epoch, program_key)
        record.update(stage='failed', failed_operation=operation, failure=_error_scalars(error))
        try:
            _write_session(record_path, record)
        except (OSError, ValueError, RuntimeError):
            pass
        raise
    return {'success': True, 'channel_id': path.name, 'sandbox_id': sandbox_id,
            'stage': 'agent_ready', 'message': '失败配对已恢复，微信登录保留；机器人保持停止，未发送消息'}


def _resume_recovery(root, app_dir, sandbox_id, path, original, pairing):
    """显式续接只读检查失败；旧 record 不变，新 journal 先落盘再做副作用。"""
    _resume_evidence(root, path, original, hash_runtime=True)
    app_dir, program_key = _prepare_program(root, app_dir)
    recovery_digest = hashlib.sha256((path / 'recovery.json').read_bytes()).hexdigest()
    journal_path = _reject_links(path / ('resume-' + uuid.uuid4().hex + '.json'))
    journal = {'version': 1, 'channel_id': path.name, 'sandbox_id': sandbox_id,
        'app_version': APP_VERSION, 'program_key': program_key, 'original_session': original,
        'recovery_sha256': recovery_digest, 'stage': 'preflight', 'created': time.time(), 'rpc_requests': 0}
    with journal_path.open('x', encoding='utf-8') as stream:
        json.dump(journal, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    operation, old_epoch = 'preflight', ''
    try:
        # 重新核验旧记录与所有既有 journal；当前独占 journal 单独验证，不把自己误作旧未知结果。
        if _read_small(path / 'session.json') != original:
            raise SandboxError('续接期间原配对已改变，未启动代理')
        operation = 'session_probe'
        _session_probe(sandbox_id)
        # 续接只补未完成的管理步骤，不能穿过任何已有业务/消费证据。
        # 已签名退出也不代表遗留业务可以由本次恢复处置。
        _recovery_idle_channel(pairing)
        if (path / 'service.json').exists():
            pairing, old_epoch = _stop_upgrade_agent(path)
            evidence = 'signed_inactive'
        else:
            _recovery_idle_channel(pairing)
            evidence = 'no_service_no_business'
        operation = 'process_probe'
        proof = _recovery_process_probe(pairing, sandbox_id)
        journal['process_probe'] = proof
        _recovery_idle_channel(pairing)
        if evidence == 'no_service_no_business':
            if (path / 'service.json').exists():
                raise SandboxError('只读核验期间原代理状态已改变，未启动新代理')
        elif _upgrade_service(pairing).get('active') is not False:
            raise SandboxError('只读核验期间原代理重新运行，未启动新代理')
        # 旧通道 share 已确认，永不重复；新版本仅添加新的只读构建映射。
        operation = 'share_program'
        journal.update(stage='mapping_program', current_operation=operation)
        _write_session(journal_path, journal)
        _share_build_program(path, sandbox_id, app_dir, program_key)
        operation = 'launch'
        started = time.time()
        journal.update(stage='launching', current_operation=operation, launch_started=started)
        _write_session(journal_path, journal)
        _launch(sandbox_id, program_key=program_key, channel_id=path.name)
        operation = 'verify_heartbeat'
        _wait_upgrade_agent(pairing, old_epoch, program_key, minimum_heartbeat=started)
        operation = 'commit_session'
        if (_read_small(path / 'session.json') != original
                or hashlib.sha256((path / 'recovery.json').read_bytes()).hexdigest() != recovery_digest):
            raise SandboxError('续接期间原配对证据已改变，未登记成功')
        updated = dict(original, app_version=APP_VERSION, program_key=program_key,
            program_mapping='by_build', channel_mapping='by_channel', stage='prepared', recovered_at=time.time())
        journal.update(stage='ready', current_operation='complete', completed=time.time())
        _write_session(journal_path, journal)
        _write_session(path / 'session.json', updated)
    except BaseException as error:
        if 'launch_started' in journal:
            _cancel_upgrade_candidate(pairing, old_epoch, program_key)
        journal.update(stage='failed', failed_operation=operation, failure=_error_scalars(error))
        try:
            _write_session(journal_path, journal)
        except (OSError, ValueError, RuntimeError):
            pass
        raise
    return {'success': True, 'channel_id': path.name, 'sandbox_id': sandbox_id, 'stage': 'agent_ready',
        'message': '原配对已继续恢复，未重复映射通道；机器人保持停止，未发送消息'}


def _existing_pairs_for_prepare(root, sandbox_id, running, skipped=None):
    # 状态页可以隐藏坏记录；准备映射却不能将它解释成“此沙盒从未配对”，
    # 否则会新建通道并覆盖仍被旧 producer 使用的 C:\WhaleChannel。
    # 两类失败必须分开：目录本身读不到（旧版本遗留的 ACL 损坏目录）只跳过并记名，
    # 能读到却结构不符的记录仍然整体失败；不给 skipped 列表的入口（恢复）保持原样。
    acl_recovery.check_pending(root)
    folder = _channel_root(root)
    if not folder.is_dir():
        return []
    pairs = []
    for path in sorted(folder.iterdir()):
        if not CHANNEL_ID.fullmatch(path.name):
            continue
        name = path.name
        try:
            path = channel_path(root, name)
            session = _read_small(path / 'session.json')
        except (FileNotFoundError, NotADirectoryError):
            # 目录还在而现成元数据缺失：记录确实不完整，不能当作“读不到”。
            raise SandboxError(_UNVERIFIED_CHANNEL) from None
        except OSError:
            if skipped is None:
                raise SandboxError(_UNVERIFIED_CHANNEL) from None
            skipped.append(name)
            continue
        except (ValueError, RuntimeError):
            raise SandboxError(_UNVERIFIED_CHANNEL) from None
        try:
            if (type(session.get('version')) is not int or session['version'] != 1
                    or session.get('channel_id') != path.name):
                raise SandboxError('pair identity mismatch')
            owner = _sandbox_id(session.get('sandbox_id'))
            if owner == sandbox_id:
                pairs.append(_pair_status(root, path.name, running))
        except (OSError, ValueError, RuntimeError):
            raise SandboxError(_UNVERIFIED_CHANNEL) from None
    return pairs


def _prepare_sandbox(root, app_dir, sandbox_id):
    acl_recovery.check_pending(root)
    sandbox_id = _sandbox_id(sandbox_id)
    app_dir, program_key = _prepare_program(root, app_dir)
    running = {row['id'] for row in list_sandboxes()}
    if sandbox_id not in running:
        raise SandboxError('所选沙盒已关闭，请先打开 Windows Sandbox 并手动登录微信')
    # 每个存活沙盒复用同一专属映射，避免 C:\WhaleChannel 被第二个目录覆盖。
    # 读不到的旧通道目录只记名并透出，不阻断本次准备。
    skipped = []
    pairs = _existing_pairs_for_prepare(root, sandbox_id, running, skipped)
    if len(pairs) > 1:
        raise SandboxError('此沙盒存在多个配对记录，无法唯一确认原代理，未准备新映射')
    for pair in pairs:
        path = channel_path(root, pair['channel_id'])
        old_session = _read_small(path / 'session.json')
        if old_session.get('channel_id') != pair['channel_id'] or old_session.get('sandbox_id') != sandbox_id:
            raise SandboxError('隔离通道归属已改变，未准备新映射')
        launch_key = _session_launch_key(old_session)
        _session_channel_key(old_session)
        if pair['stage'] in ('failed', 'preparing'):
            raise SandboxError('此沙盒已有未完成的目录映射，请重新打开沙盒后配对')
        if pair['stage'] == 'build_mismatch':
            raise SandboxError('隔离代理构建与原配对不一致，未升级或重启；请先核验原配对记录')
        if old_session.get('app_version') != APP_VERSION or old_session.get('program_key') != program_key:
            return dict(_upgrade_pair(path, old_session, app_dir, program_key),
                        skipped_channels=skipped)
        if not pair['agent_online']:
            _session_probe(sandbox_id)
            _launch_session(sandbox_id, launch_key, old_session)
        return {'success': True, 'channel_id': pair['channel_id'], 'channel_dir': str(path),
                'sandbox_id': sandbox_id, 'stage': pair['stage'], 'skipped_channels': skipped,
                'message': '已复用此沙盒的专属通道，尚未自动启动机器人'}

    _session_probe(sandbox_id)
    ident = uuid.uuid4().hex
    path = _reject_links(_channel_root(root, create=True) / ident)
    create_private_directory(path)
    _reject_links(path)
    pairing = {'version': 1, 'channel_id': ident, 'secret': secrets.token_hex(32)}
    with (path / 'pairing.json').open('x', encoding='utf-8') as stream:
        json.dump(pairing, stream, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    session = {'version': 1, 'channel_id': ident, 'sandbox_id': sandbox_id,
               'app_version': APP_VERSION, 'program_key': program_key,
               'program_mapping': 'legacy',
               'created': time.time(), 'stage': 'preparing'}
    _write_session(path / 'session.json', session)
    operation = 'share_program'
    try:
        _run_wsb(['share', '--id', sandbox_id, '--host-path', str(app_dir),
                  '--sandbox-path', GUEST_APP])
        operation = 'share_channel'
        _run_wsb(['share', '--id', sandbox_id, '--host-path', str(path),
                  '--sandbox-path', GUEST_CHANNEL, '--allow-write'])
        operation = 'launch'
        _launch(sandbox_id)
    except SandboxError as error:
        session['stage'] = 'failed'
        session['failed_operation'] = operation
        session['failure'] = _error_scalars(error)
        try:
            _write_session(path / 'session.json', session)
        except (OSError, ValueError, RuntimeError):
            pass
        # exec 超时不代表代理未启动；只向本次新配对写签名停止请求，不终止微信。
        try:
            stop_guest(path)
        except (OSError, ValueError, RuntimeError):
            pass
        raise
    session['stage'] = 'prepared'
    _write_session(path / 'session.json', session)
    return {'success': True, 'channel_id': ident, 'channel_dir': str(path),
            'sandbox_id': sandbox_id, 'stage': 'prepared', 'skipped_channels': skipped,
            'message': '隔离通道已准备；请启用此通道后再点击启动机器人'}


def stop_guest(channel):
    """只请求本次配对代理退出，不关闭沙盒、不结束微信进程。"""
    from .isolated_wechat.protocol import load_pairing, live_service, write_document
    pairing = load_pairing(_reject_links(channel))
    try:
        service = live_service(pairing)
    except (OSError, ValueError, RuntimeError):
        return {'success': True, 'stopped': False, 'message': '隔离代理当前未运行'}
    now = time.time()
    write_document(pairing.root / 'control' / 'stop.json', pairing,
                   {'epoch': service['epoch'], 'action': 'stop', 'request_id': uuid.uuid4().hex,
                    'created': now, 'deadline': now + 30})
    return {'success': True, 'stopped': False, 'requested': True,
            'message': '已请求隔离代理停止；微信与沙盒保持运行'}
