"""外部微信收发协议的纯解析与持久收据；不加载 DLL、不启动微信。

回调中的账号字段不是可信的登录证明。调用者须先独立核验实际账号，
再把绑定账号传给解析器；随机回调路径仅限制本机入口，不能代替核验。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

MAX_CALLBACK_BYTES = 64 * 1024
MAX_TEXT_CHARS = 12000
MAX_PENDING_EVENTS = 1000
_STABLE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,255}\Z')
_REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_MESSAGE_ID = re.compile(r'[0-9]{1,24}\Z')


class HookProtocolError(ValueError):
    pass


class HookJournalError(RuntimeError):
    pass


def _stable(value, field='标识'):
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise HookProtocolError(field + '不是有效的稳定标识')
    return value


def _message_id(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not _MESSAGE_ID.fullmatch(value) or int(value) <= 0:
        raise HookProtocolError('缺少有效的原始消息 ID')
    return str(int(value))


def _json(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                         allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise HookProtocolError('回调格式无效') from None
    if len(raw) > MAX_CALLBACK_BYTES:
        raise HookProtocolError('回调超过大小限制')
    return raw


def _object_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise HookProtocolError('回调包含重复字段')
        value[key] = item
    return value


def _payload(payload):
    if isinstance(payload, (bytes, bytearray)):
        if not payload or len(payload) > MAX_CALLBACK_BYTES:
            raise HookProtocolError('回调超过大小限制或为空')
        try:
            payload = json.loads(bytes(payload).decode('utf-8'), object_pairs_hook=_object_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(HookProtocolError('无效数值')))
        except (ValueError, UnicodeError, RecursionError):
            raise HookProtocolError('回调不是有效的 UTF-8 JSON') from None
    if not isinstance(payload, dict) or len(payload) > 64:
        raise HookProtocolError('回调必须是有界对象')
    _json(payload)
    return payload


def normalize_callback(payload, *, account, now=None):
    """4.1.10.27 消息回调转原生 raw 字典；不猜账号、时间或缺失 ID。

    只接文本；微信团队安全卡片保持原文交给既有安全暂停入口。
    DLL 的 msgid 只用于事件去重，绝不冒充已验证服务器回执。
    """
    account = _stable(account, '绑定账号')
    item = _payload(payload)
    if type(item.get('event_type')) is not int or item['event_type'] != 1001:
        return None
    for field in ('self_wxid', 'account_wxid', 'own_wxid', 'self_id'):
        if field in item and item[field] != account:
            raise HookProtocolError('回调账号与已核验账号不一致')
    kind = item.get('type')
    if type(kind) is not int or kind not in (1, 49):
        return None
    message_id = _message_id(item.get('msgid'))
    timestamp = item.get('timestamp')
    current = time.time() if now is None else now
    if (type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp <= 0
            or type(current) not in (int, float) or not math.isfinite(current)
            or timestamp > current + 60):
        raise HookProtocolError('缺少有效的原始消息时间')
    content = item.get('content')
    if (not isinstance(content, str) or not content.strip() or len(content) > MAX_TEXT_CHARS
            or '\x00' in content):
        raise HookProtocolError('消息正文无效或过长')
    sender, room, peer = (item.get(key, '') for key in ('sender', 'roomid', 'wxid'))
    if any(not isinstance(value, str) for value in (sender, room, peer)):
        raise HookProtocolError('回调路由字段无效')
    if sender:
        _stable(sender, '发送者')
    if peer:
        _stable(peer, '会话')
    if sender == account or item.get('is_send') in (True, 1, '1'):
        return None
    if room:
        _stable(room, '群')
        if not room.endswith('@chatroom') or not sender or sender.endswith('@chatroom'):
            raise HookProtocolError('群消息缺少原始发送者或群 ID')
        target = room
    else:
        if peer and sender and peer != sender:
            raise HookProtocolError('私聊会话与发送者不一致，未猜测路由')
        target = peer or sender
        sender = sender or peer
        _stable(target, '会话')
        if target.endswith('@chatroom'):
            raise HookProtocolError('群消息缺少群路由字段')
    if target == account:
        return None
    # 仅剥离与独立 sender 字段精确相等的群正文前缀，不能按昵称猜成员。
    if room and content.startswith(sender + ':\n'):
        content = content[len(sender) + 2:]
        if not content.strip():
            raise HookProtocolError('群正文为空')
    raw = {'username': target, 'sender_username': sender, 'sender_id': sender,
           'type': '文本' if kind == 1 else '文件/链接/卡片', 'content': content,
           'create_time': float(timestamp), 'local_id': message_id,
           'shard': 'hook-v1:' + account, 'hook_msgid': message_id,
           '_hook_account': account, 'attr': 'friend'}
    if kind == 49:
        from .wechat_safety import security_notice
        if not security_notice(raw):
            return None
    return raw


class CallbackToken:
    """只接受回环地址与当前随机路径；轮换立即废止旧路径。"""
    def __init__(self, prefix='/api/wechat/hook/callback'):
        if (not isinstance(prefix, str) or not re.fullmatch(r'/[A-Za-z0-9/_-]{1,128}', prefix)
                or '//' in prefix):
            raise HookProtocolError('回调路径前缀无效')
        self._prefix = prefix.rstrip('/')
        self.rotate()

    def rotate(self):
        self.path = self._prefix + '/' + secrets.token_hex(32)
        return self.path

    def accepts(self, path, peer):
        if not isinstance(path, str) or not path.isascii() or not isinstance(peer, str) or '%' in peer:
            return False
        try:
            address = ipaddress.ip_address(peer)
            local = address.is_loopback or bool(getattr(address, 'ipv4_mapped', None)
                                                and address.ipv4_mapped.is_loopback)
        except ValueError:
            return False
        return bool(local and hmac.compare_digest(path, self.path))


def _path_without_links(path):
    path = Path(path)
    if '..' in path.parts:
        raise HookJournalError('收据路径包含上级目录')
    path = path.absolute()
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if ancestor.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise HookJournalError('收据路径不可包含链接')
    return path


class HookJournal:
    """按实际账号隔离的入站去重与单次发送消费记录；不自动清历史。"""
    def __init__(self, path, account):
        self.account = _stable(account, '绑定账号')
        self.path = _path_without_links(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _path_without_links(self.path)
        for suffix in ('-journal', '-wal', '-shm'):
            _path_without_links(self.path.with_name(self.path.name + suffix))
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self._db.execute('PRAGMA synchronous=FULL')
        try:
            with self._db:
                self._db.execute('CREATE TABLE IF NOT EXISTS hook_inbound ('
                    'account TEXT NOT NULL, msgid TEXT NOT NULL, fingerprint TEXT NOT NULL, '
                    'payload TEXT NOT NULL, received REAL NOT NULL, acked INTEGER NOT NULL DEFAULT 0, '
                    'PRIMARY KEY(account,msgid))')
                self._db.execute('CREATE TABLE IF NOT EXISTS hook_outbound ('
                    'account TEXT NOT NULL, request_id TEXT NOT NULL, target TEXT NOT NULL, '
                    'body_sha256 TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, '
                    'updated REAL NOT NULL, server_id TEXT, PRIMARY KEY(account,request_id))')
                self._db.execute('UPDATE hook_outbound SET state=\'uncertain\',updated=? '
                    'WHERE account=? AND state=\'started\'', (time.time(), self.account))
        except BaseException:
            self._db.close()
            raise

    def _check(self):
        if self._closed:
            raise HookJournalError('收据日志已关闭')

    def record_inbound(self, raw):
        if not isinstance(raw, dict) or raw.get('_hook_account') != self.account:
            raise HookProtocolError('入站记录不属于当前已核验账号')
        msgid = _message_id(raw.get('hook_msgid'))
        if raw.get('local_id') != msgid or raw.get('shard') != 'hook-v1:' + self.account:
            raise HookProtocolError('入站记录的消息身份不一致')
        fields = ('username', 'sender_username', 'sender_id', 'type', 'content', 'create_time',
                  'local_id', 'shard', 'hook_msgid', '_hook_account', 'attr')
        value = {field: raw[field] for field in fields if field in raw}
        _stable(value.get('username'), '会话')
        _stable(value.get('sender_username'), '发送者')
        if value.get('sender_id') != value['sender_username']:
            raise HookProtocolError('入站发送者身份不一致')
        if (value.get('type') not in ('文本', '文件/链接/卡片')
                or not isinstance(value.get('content'), str)
                or not value['content'].strip() or len(value['content']) > MAX_TEXT_CHARS
                or type(value.get('create_time')) not in (float, int)
                or not math.isfinite(value['create_time']) or value['create_time'] <= 0):
            raise HookProtocolError('入站记录未经有效解析')
        encoded = _json(value)
        digest = hashlib.sha256(encoded).hexdigest()
        with self._lock:
            self._check()
            with self._db:
                inserted = self._db.execute('INSERT OR IGNORE INTO hook_inbound '
                    '(account,msgid,fingerprint,payload,received) VALUES (?,?,?,?,?)',
                    (self.account, msgid, digest, encoded.decode('utf-8'), time.time()))
                if inserted.rowcount != 1:
                    found = self._db.execute('SELECT fingerprint FROM hook_inbound WHERE account=? AND msgid=?',
                                            (self.account, msgid)).fetchone()
                    if found is None or found[0] != digest:
                        raise HookJournalError('同一原始消息 ID 对应不同内容，已停止接收')
                    return False
                count = self._db.execute('SELECT COUNT(*) FROM hook_inbound WHERE account=? AND acked=0',
                                         (self.account,)).fetchone()[0]
                if count > MAX_PENDING_EVENTS:
                    raise HookJournalError('隔离入站队列已满，未丢弃旧消息')
                return True

    def pending_inbound(self, limit=100, *, target=None):
        if type(limit) is not int or not 1 <= limit <= MAX_PENDING_EVENTS:
            raise HookProtocolError('入站读取数量无效')
        if target is not None:
            target = _stable(target, '会话')
        with self._lock:
            self._check()
            # 先限定路由再取批次；改目标后保留的旧会话记录不能挡住新会话。
            # 官方安全通知仍必须恢复，不能被当前会话范围掩盖。
            scope = '' if target is None else "AND (json_extract(payload,'$.username')=? OR json_extract(payload,'$.username')='weixin') "
            args = (self.account, limit) if target is None else (self.account, target, limit)
            rows = self._db.execute('SELECT payload FROM hook_inbound WHERE account=? AND acked=0 '
                + scope + 'ORDER BY received,rowid LIMIT ?', args).fetchall()
            return [json.loads(row[0]) for row in rows]

    def ack_inbound(self, message_id):
        msgid = _message_id(message_id)
        with self._lock:
            self._check()
            with self._db:
                self._db.execute('UPDATE hook_inbound SET acked=1 WHERE account=? AND msgid=?',
                                 (self.account, msgid))

    def begin_send(self, request_id, target, text):
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise HookProtocolError('发送请求 ID 无效')
        target = _stable(target, '会话')
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise HookProtocolError('发送正文无效')
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        now = time.time()
        with self._lock:
            self._check()
            with self._db:
                inserted = self._db.execute('INSERT OR IGNORE INTO hook_outbound '
                    '(account,request_id,target,body_sha256,state,created,updated) VALUES (?,?,?,?,?,?,?)',
                    (self.account, request_id, target, digest, 'started', now, now))
                if inserted.rowcount == 1:
                    return True
                row = self._db.execute('SELECT target,body_sha256 FROM hook_outbound WHERE account=? AND request_id=?',
                                       (self.account, request_id)).fetchone()
                if row is None or row != (target, digest):
                    raise HookJournalError('发送请求 ID 已被其他内容占用，未重发')
                return False

    def get_send(self, request_id):
        with self._lock:
            self._check()
            row = self._db.execute('SELECT request_id,target,body_sha256,state,created,updated,server_id '
                'FROM hook_outbound WHERE account=? AND request_id=?', (self.account, request_id)).fetchone()
            if row is None:
                return None
            value = dict(zip(('request_id', 'target', 'body_sha256', 'state', 'created', 'updated', 'server_id'), row))
            value['account'] = self.account
            return value

    def finish_send(self, request_id, state, *, receipt=None):
        if state not in ('not_submitted', 'submitted', 'uncertain', 'confirmed'):
            raise HookProtocolError('发送状态无效')
        with self._lock:
            self._check()
            with self._db:
                row = self.get_send(request_id)
                if row is None:
                    raise HookJournalError('发送请求尚未被独占记录')
                if row['state'] == state:
                    return row
                server_id = row['server_id']
                if state == 'confirmed':
                    if row['state'] not in ('submitted', 'uncertain'):
                        raise HookJournalError('未提交记录不能直接确认为送达')
                    if (not isinstance(receipt, dict) or receipt.get('account') != self.account
                            or receipt.get('target') != row['target']
                            or receipt.get('body_sha256') != row['body_sha256']
                            or receipt.get('new_outgoing_record_verified') is not True):
                        raise HookProtocolError('回执未确认同账号、会话与完整正文')
                    server_id = _message_id(receipt.get('server_id'))
                elif row['state'] != 'started' and not (row['state'] == 'submitted' and state == 'uncertain'):
                    raise HookJournalError('已消费发送请求不可重新执行或改写结果')
                self._db.execute('UPDATE hook_outbound SET state=?,updated=?,server_id=? '
                    'WHERE account=? AND request_id=?', (state, time.time(), server_id, self.account, request_id))
                return self.get_send(request_id)

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._db.close()
