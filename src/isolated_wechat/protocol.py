"""本机共享目录上的有界、签名消息协议；不接受代码或任意文件路径。"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

VERSION = 1
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
DIRECTORIES = ('requests', 'responses', 'consumed', 'events', 'control')
ACTIONS = frozenset(('connect', 'disconnect', 'start_listening', 'stop_listening', 'poll',
                     'inspect_environment', 'send_text', 'display_name', 'resolve_session',
                     'member_names', 'resolve_chat_name', 'resume_sends',
                     'video_frames'))
UUID32 = re.compile(r'[0-9a-f]{32}\Z')


class ProtocolError(RuntimeError):
    pass


class Pairing(dict):
    def __init__(self, value, root):
        super().__init__(value)
        self.root = Path(root)


def _no_links(path):
    """共享树及其父目录都不跟随 junction/symlink。"""
    path = Path(path)
    # Path.absolute/relative_to 不折叠 '..'；先拒绝才能保证后面的边界检查有效。
    if '..' in path.parts:
        raise ProtocolError('隔离协议路径包含上级目录')
    path = path.absolute()
    for item in reversed((path, *path.parents)):
        try:
            stat = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
            raise ProtocolError('隔离通道路径包含链接，已拒绝访问')
    return path


def checked_path(path, pairing):
    root = _no_links(pairing.root)
    path = _no_links(path)
    try:
        path.relative_to(root)
    except ValueError:
        raise ProtocolError('隔离协议路径越界') from None
    return path


def _bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                          allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise ProtocolError('隔离协议文档格式无效') from None


def request_digest(value):
    """摘要绑定完整已核验载荷；签名字段本身不参与内容身份。"""
    payload = dict(value)
    payload.pop('signature', None)
    return hashlib.sha256(_bytes(payload)).hexdigest()


def _open_shared_read(path):
    """Windows 读取不能禁止同目录发布者原子替换心跳或响应文件。"""
    if os.name != 'nt':
        return path.open('rb')
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    # CRT 的普通 open 仅共享读写；缺少 DELETE 会让只读者令 replace
    # 抛出 sharing violation，客体遂按真实持久化失败停止收发。
    handle = create(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        # open_osfhandle 成功后CRT拥有handle，file关闭时仅通过descriptor释放一次。
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def _read_json(path, limit):
    path = _no_links(path)
    try:
        with _open_shared_read(path) as stream:
            raw = stream.read(int(limit) + 1)
        if len(raw) > limit:
            raise ProtocolError('隔离协议文档超过大小限制')
        value = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(value, dict):
            raise ProtocolError('隔离协议文档必须是对象')
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ProtocolError('隔离协议文档无法解析') from None


def load_pairing(channel):
    root = _no_links(channel)
    if not root.is_dir():
        raise ProtocolError('尚未准备隔离通道')
    value = _read_json(root / 'pairing.json', 8192)
    if (value.get('version') != VERSION or type(value.get('version')) is not int
            or not UUID32.fullmatch(str(value.get('channel_id', '')))
            or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('secret', '')))):
        raise ProtocolError('隔离通道配对信息无效')
    pairing = Pairing({key: value[key] for key in ('version', 'channel_id', 'secret')}, root)
    for name in DIRECTORIES:
        folder = checked_path(root / name, pairing)
        folder.mkdir(exist_ok=True)
        _no_links(folder)
    return pairing


def signed(pairing, payload):
    if not isinstance(payload, dict) or 'signature' in payload:
        raise ProtocolError('隔离协议载荷无效')
    value = dict(payload)
    if value.get('version', VERSION) != VERSION or value.get('channel_id', pairing['channel_id']) != pairing['channel_id']:
        raise ProtocolError('隔离协议配对不一致')
    value.update(version=VERSION, channel_id=pairing['channel_id'])
    value['signature'] = hmac.new(bytes.fromhex(pairing['secret']), _bytes(value), hashlib.sha256).hexdigest()
    return value


def verify(pairing, envelope):
    if not isinstance(envelope, dict):
        raise ProtocolError('隔离协议文档无效')
    value = dict(envelope)
    signature = value.pop('signature', None)
    if (value.get('version') != VERSION or type(value.get('version')) is not int
            or value.get('channel_id') != pairing['channel_id'] or not isinstance(signature, str)):
        raise ProtocolError('隔离协议配对不一致')
    expected = hmac.new(bytes.fromhex(pairing['secret']), _bytes(value), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ProtocolError('隔离协议签名不匹配')
    return value


def read_document(path, pairing, max_bytes=MAX_DOCUMENT_BYTES, *, retry_deadline=None):
    if (retry_deadline is not None and (type(retry_deadline) not in (int, float)
            or not math.isfinite(retry_deadline))):
        raise ProtocolError('隔离协议读取截止时间无效')
    path = Path(path)
    root = Path(pairing.root).absolute()
    candidate = path.absolute()
    # OCR 心跳反复发布，结果槽也先由客体写 pending、再由宿主发布一次结果。
    # 只有固定目录里的精确名字可重查，不能把不可变业务响应/请求一并放宽。
    ocr_mutable = (candidate == root / 'ocr/relay.json'
        or (candidate.parent == root / 'ocr' and candidate.name.endswith('.result.json')
            and UUID32.fullmatch(candidate.name.removesuffix('.result.json')) is not None))
    mutable = (candidate == root / 'service.json'
               or candidate in (root / 'control/cancel.json', root / 'control/stop.json',
                                root / 'control/request.json')
               or ocr_mutable)
    deadline = None
    while True:
        try:
            return verify(pairing, _read_json(checked_path(path, pairing), max_bytes))
        except OSError as error:
            # ReplaceFileW发布可变槽时，Windows可短暂返回缺名/共享冲突。
            # 仅在同一次只读内重查；每轮路径、大小、JSON和签名仍完整核验。
            # 一次发布的业务响应/事件/消费与配对不等候，任何RPC、OCR及发送都不重投。
            if os.name != 'nt' or not mutable or getattr(error, 'winerror', None) not in (2, 32):
                raise
            if deadline is None:
                deadline = time.monotonic() + .05
                if retry_deadline is not None:
                    deadline = min(deadline, retry_deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(.005, remaining))
            if time.monotonic() >= deadline:
                raise


def _replace_shared_document(source, target):
    """使用本机及Sandbox映射都支持的替换；不先删除、不复制或重发消息。"""
    if os.name != 'nt':
        source.replace(target)
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    replace = kernel.ReplaceFileW
    replace.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
    replace.restype = wintypes.BOOL
    # os.replace拒绝打开的目标；InfoEx/POSIX虽在NTFS可用，Sandbox映射明确
    # 返回87，不能把它当生产发布方式。ReplaceFileW保留旧读句柄的完整版本。
    if replace(str(target), str(source), None, 0, None, None):
        return
    error = ctypes.get_last_error()
    if error in (2, 3):
        source.replace(target)
        return
    raise ctypes.WinError(error)


def write_document(path, pairing, payload, exclusive=False):
    path = checked_path(path, pairing)
    raw = _bytes(signed(pairing, payload))
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ProtocolError('隔离协议文档超过大小限制')
    if exclusive:
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return path
    temporary = checked_path(path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp'), pairing)
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
        _no_links(path)
        _replace_shared_document(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def valid_epoch(value):
    return isinstance(value, str) and bool(UUID32.fullmatch(value))


def live_service(pairing, *, now=None, max_age=8.0):
    value = read_document(pairing.root / 'service.json', pairing, 256 * 1024)
    now = time.time() if now is None else float(now)
    heartbeat = value.get('heartbeat')
    if (not valid_epoch(value.get('epoch')) or value.get('active') is not True
            or type(heartbeat) not in (float, int) or not math.isfinite(heartbeat)
            or not -2 <= now - heartbeat <= max_age):
        raise ProtocolError('隔离微信代理未运行或心跳已过期')
    return value


def validate_request(value, epoch, *, now=None):
    now = time.time() if now is None else float(now)
    created, deadline = value.get('created'), value.get('deadline')
    if (value.get('epoch') != epoch or not valid_epoch(value.get('request_id'))
            or value.get('action') not in ACTIONS or not isinstance(value.get('args'), dict)
            or type(created) not in (int, float) or type(deadline) not in (int, float)
            or not math.isfinite(created) or not math.isfinite(deadline)
            or not 0 < deadline - created <= 30 or not -2 <= now - created <= 32
            or now > deadline):
        raise ProtocolError('隔离请求过期、身份不符或动作不受支持')
    return value


def validate_control(value, epoch, action, *, now=None):
    now = time.time() if now is None else float(now)
    created, deadline = value.get('created'), value.get('deadline')
    return (value.get('epoch') == epoch and value.get('action') == action
            and valid_epoch(value.get('request_id')) and type(created) in (int, float)
            and type(deadline) in (int, float) and math.isfinite(created) and math.isfinite(deadline)
            and 0 < deadline - created <= 30 and -2 <= now - created <= 32 and now <= deadline)
