"""只读核验外部微信接口的进程、版本、模块及账号；不加载或安装 DLL。"""
from __future__ import annotations

import hashlib
import ipaddress
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

HOOK_VERSION = '4.1.10.27'
HOOK_DLL_SHA256 = '23948e7d933ff40735ddc0cc516641fd9f935d67ffdec476bb8db61b79940f29'
# 原版同时包含反撤回与模块隐藏，绝不能因监听被改成回环就放行。
# 这里只登记经过独立审计的加固构建；目前没有满足条件的构建。
APPROVED_PROVIDER_SHA256 = frozenset()


def loopback_api(value):
    if not isinstance(value, str) or len(value) > 128 or any(ord(char) < 33 for char in value):
        raise ValueError('微信接口地址格式无效')
    parsed = urlsplit(str(value))
    try:
        address = ipaddress.ip_address(parsed.hostname or '')
        port = parsed.port
    except ValueError:
        raise ValueError('微信接口必须使用明确的回环 IP 和端口') from None
    if (parsed.scheme != 'http' or not address.is_loopback or not port
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('微信接口只允许本机 HTTP 回环地址，不接受代理、凭据或任意路径')
    return parsed.hostname, port


def _plain_path(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('账号目录必须为明确的绝对路径')
    for item in (path, *path.parents):
        try:
            stat = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
            raise ValueError('微信路径不能包含链接')
    return path.resolve()


def validate_snapshot(snapshot, account):
    """所有证据必须来自监听接口的同一进程；缺少正向证据不能靠配置补齐。"""
    from .wechat_bridge import account_wxid
    if not isinstance(snapshot, dict):
        return {'ready': False, 'reason': '协议进程证据格式无效', 'waiting_code': 'identity_unverified'}
    if snapshot.get('version') != HOOK_VERSION:
        return {'ready': False, 'reason': '协议接口要求微信 ' + HOOK_VERSION, 'waiting_code': 'version_mismatch'}
    if snapshot.get('dll_loaded') is not True:
        return {'ready': False, 'reason': '协议模块来源或加载状态未通过核验', 'waiting_code': 'module_unverified'}
    digest = snapshot.get('dll_sha256')
    if not isinstance(digest, str) or digest == HOOK_DLL_SHA256 or digest not in APPROVED_PROVIDER_SHA256:
        return {'ready': False, 'reason': '尚无审计通过的加固协议模块；原版包含反撤回和模块隐藏，未启用',
                'waiting_code': 'provider_unapproved'}
    if snapshot.get('api_port_owned') is not True or type(snapshot.get('pid')) is not int or snapshot['pid'] <= 0:
        return {'ready': False, 'reason': '无法将接口监听端口绑定到唯一微信进程', 'waiting_code': 'process_unverified'}
    if snapshot.get('loopback_listener') is not True:
        return {'ready': False, 'reason': '原接口监听非回环地址且缺少鉴权，未启用；不会修改防火墙或微信设置', 'waiting_code': 'unsafe_listener'}
    accounts = snapshot.get('open_accounts')
    if (not isinstance(accounts, list) or len(accounts) != 1 or not account
            or not isinstance(accounts[0], str) or not accounts[0]
            or account_wxid(Path(accounts[0]).name) != account or snapshot.get('account') != account
            or type(snapshot.get('process_create_time')) not in (int, float)
            or not math.isfinite(snapshot['process_create_time']) or snapshot['process_create_time'] <= 0):
        return {'ready': False, 'reason': '无法由接口进程唯一确认所选微信账号', 'waiting_code': 'account_unverified'}
    return dict(snapshot, ready=True, verified=True, waiting_code='', reason='协议进程与所选微信账号已只读核验')


def probe_hook_identity(api_url, account_path, account):
    try:
        host, port = loopback_api(api_url)
        if os.name != 'nt':
            raise ValueError('微信协议进程核验仅支持 Windows')
        expected = _plain_path(account_path)
        database = expected / 'db_storage'
        if not database.is_dir():
            raise ValueError('所选账号没有 db_storage 目录')
        import psutil
        import win32api
        listeners = [row for row in psutil.net_connections(kind='tcp')
            if row.status == psutil.CONN_LISTEN and row.laddr and row.laddr.port == port]
        if len(listeners) != 1 or not listeners[0].pid:
            raise ValueError('接口端口归属不唯一或无法读取')
        listener = listeners[0]
        process = psutil.Process(listener.pid)
        created = process.create_time()
        executable = _plain_path(process.exe())
        if process.name().lower() != 'weixin.exe' or executable.name.lower() != 'weixin.exe':
            raise ValueError('监听接口的进程不是微信')
        version = win32api.GetFileVersionInfo(str(executable), '\\')
        ms, ls = version['FileVersionMS'], version['FileVersionLS']
        version_text = '.'.join(str(v) for v in (ms >> 16, ms & 65535, ls >> 16, ls & 65535))
        dll = _plain_path(executable.parent / 'version.dll')
        if dll.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('协议模块超过核验大小限制')
        digest = hashlib.sha256(dll.read_bytes()).hexdigest()
        loaded = any(_plain_path(row.path) == dll for row in process.memory_maps(grouped=True)
                     if row.path and row.path.lower().endswith('version.dll'))
        observed = set()
        for opened in process.open_files():
            path = _plain_path(opened.path)
            for parent in path.parents:
                if parent.name.lower() == 'db_storage':
                    observed.add(parent.parent)
                    break
        if process.create_time() != created or not process.is_running():
            raise ValueError('核验期间微信进程已变化')
        same_account = observed == {expected}
        return validate_snapshot({'version': version_text, 'dll_sha256': digest,
            'dll_loaded': loaded, 'api_port_owned': True, 'pid': process.pid,
            'process_create_time': created, 'account': account if same_account else '',
            'create_time': created, 'account_path': str(expected),
            'open_accounts': sorted(str(path) for path in observed),
            'loopback_listener': ipaddress.ip_address(listener.laddr.ip).is_loopback
                                 and listener.laddr.ip == host}, account)
    except Exception as error:
        return {'ready': False, 'waiting_code': 'identity_unverified',
                'reason': '协议进程未通过只读核验（' + type(error).__name__ + '）；未连接或发送'}
