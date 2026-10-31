"""冻结程序的沙盒专用入口；不打开控制台、不登录或启动微信。"""
import argparse
import ctypes
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

from ..desktop_startup import worker_command
from ..logging_setup import setup_logging
from ..runtime_identity import APP_VERSION
from .protocol import (ProtocolError, load_pairing, live_service, read_document,
                       valid_epoch)


def require_sandbox_user():
    # 此导入只核验身份；真正创建桥接器在切换到来宾私有工作目录之后。
    from .guest_service import require_sandbox_user as check
    check()


def serve(channel, data_root):
    from .guest_service import main as run
    return run(channel, data_root)


def runtime_program_key():
    """构建身份只来自本冻结程序旁的只读运行清单，不接受启动参数冒充。"""
    if not getattr(sys, 'frozen', False):
        return ''  # 源码测试/旧代理没有可用于升级验收的冻结构建身份。
    manifest = Path(sys.executable).absolute().parent / 'runtime.json'
    try:
        info = manifest.lstat()
        if manifest.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked manifest')
        with manifest.open('rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError('oversized manifest')
        value = json.loads(raw.decode('utf-8-sig'))
        fingerprint = value.get('fingerprint') if isinstance(value, dict) else None
        if (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)
                or value.get('app_version') != APP_VERSION):
            raise ValueError('invalid build identity')
        return fingerprint[:20]
    except (OSError, ValueError, UnicodeError):
        raise ProtocolError('无法核验隔离程序构建清单，未启动代理') from None


def _private_root(pairing):
    local = os.environ.get('LOCALAPPDATA', '')
    if not local or not Path(local).is_absolute() or '..' in Path(local).parts:
        raise ProtocolError('无法确认来宾私有数据目录')
    root = Path(local) / 'WhaleGirlWeChatGuest'
    # 来宾环境也可能有可写共享映射；不能因环境变量被改写便将驱动日志
    # 放到程序映射或签名通道里，更不能跟随 junction 指到宿主目录。
    for path in (root, *root.parents):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ProtocolError('来宾私有数据目录包含链接')
    forbidden = [pairing.root.absolute()]
    if getattr(sys, 'frozen', False):
        forbidden.append(Path(sys.executable).absolute().parent)
    for path in forbidden:
        if root == path or root.is_relative_to(path) or path.is_relative_to(root):
            raise ProtocolError('来宾私有数据目录与共享映射重叠')
    root.mkdir(parents=True, exist_ok=True)
    return root


def _already_running(pairing):
    try:
        service = live_service(pairing)
    except FileNotFoundError:
        return False
    except ProtocolError:
        # 只有已通过签名和结构核验的过期/已停止心跳允许重新启动。
        # 无效签名不是“代理离线”，不能被新代理悄悄覆盖。
        service = read_document(pairing.root / 'service.json', pairing, 256 * 1024)
        heartbeat = service.get('heartbeat')
        if (not valid_epoch(service.get('epoch')) or type(service.get('active')) is not bool
                or not isinstance(service.get('app_version'), str) or not service['app_version']
                or type(heartbeat) not in (int, float) or not math.isfinite(heartbeat)):
            raise ProtocolError('隔离代理心跳结构无效')
        return False
    if service.get('app_version') != APP_VERSION:
        raise ProtocolError('旧版隔离代理仍在运行，请先停用原配对')
    key = runtime_program_key()
    if key and service.get('program_key') != key:
        raise ProtocolError('另一构建的隔离代理仍在运行，请先停止原配对')
    return True


def _agent(pairing, data_root):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    from ctypes import wintypes
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, True, 'Local\\WhaleGirlSandboxAgent-' + pairing['channel_id'])
    error = ctypes.get_last_error()
    if not handle:
        raise ProtocolError('无法创建隔离代理实例锁')
    if error == 183:
        kernel.CloseHandle(handle)
        return 0
    try:
        setup_logging(data_root / 'logs')
        return serve(pairing.root, data_root)
    finally:
        kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def main(argv):
    if not argv or argv[0] not in ('--sandbox-agent-launch', '--sandbox-agent'):
        raise ProtocolError('未知隔离启动模式')
    mode = argv[0]
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', required=True)
    options = parser.parse_args(argv[1:])
    require_sandbox_user()
    pairing = load_pairing(options.channel)
    if mode == '--sandbox-agent-launch' and _already_running(pairing):
        return 0
    data_root = _private_root(pairing)
    previous = Path.cwd()
    os.chdir(data_root)
    try:
        if mode == '--sandbox-agent-launch':
            subprocess.Popen(worker_command('--sandbox-agent', '--channel', str(pairing.root)),
                             cwd=str(data_root),
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return 0
        return _agent(pairing, data_root)
    finally:
        # 源码测试和嵌入调用返回后恢复调用者；代理运行期间始终留在私有目录。
        try:
            os.chdir(previous)
        except OSError:
            pass
