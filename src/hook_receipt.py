"""Hook 文本发送的独立只读回执；接口返回成功不能替代新增自身服务器记录。"""
from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path


class ReceiptUnavailable(RuntimeError):
    pass


def _body_hash(text):
    if not isinstance(text, str):
        raise ReceiptUnavailable('回执正文格式无效')
    return hashlib.sha256(text.replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')).hexdigest()


def _file_state(path):
    try:
        value = path.stat()
        return (value.st_size, value.st_mtime_ns, value.st_ino)
    except FileNotFoundError:
        return None


def _committed_wal(path):
    """只验证 WAL 世代和提交边界；不实现或修补任何加密页合并。"""
    state = _file_state(path)
    if state is None or state[0] == 0:
        return
    size, _, _ = state
    if size < 32 or size > 256 * 1024 * 1024 or (size - 32) % 4120:
        raise ReceiptUnavailable('消息数据库 WAL 不是完整的受限快照')
    with path.open('rb') as stream:
        header = stream.read(32)
        if int.from_bytes(header[0:4], 'big') not in (0x377F0682, 0x377F0683):
            raise ReceiptUnavailable('消息数据库 WAL 头无法核验')
        if int.from_bytes(header[8:12], 'big') != 4096:
            raise ReceiptUnavailable('消息数据库 WAL 页大小不受支持')
        salt = header[16:24]
        last_commit = 0
        for index in range((size - 32) // 4120):
            stream.seek(32 + index * 4120)
            frame = stream.read(24)
            page = int.from_bytes(frame[0:4], 'big')
            if len(frame) != 24 or frame[8:16] != salt or not 1 <= page <= 10_000_000:
                raise ReceiptUnavailable('消息数据库 WAL 包含旧世代或无效帧')
            last_commit = int.from_bytes(frame[4:8], 'big')
        if size > 32 and not last_commit:
            raise ReceiptUnavailable('消息数据库 WAL 尾部事务尚未提交')
    if _file_state(path) != state:
        raise ReceiptUnavailable('消息数据库 WAL 在检查期间改变')


class _NativeSnapshots:
    def __init__(self, owner, work_dir):
        from wechatauto.db import WeChatDB, STAMP_VERSION
        self.owner = owner
        work_dir.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='receipt-', dir=work_dir)

        class PinnedDB(WeChatDB):
            def _try_other_accounts(self):
                return False

            def _stable_key_dirs(self):
                return []

            def _load_key_cache(self, _path):
                return {}

            def _save_keys(self):
                return None

            def _find_weixin_pids(self):
                return [owner._identity()['pid']]

            def _collect_db_files(self):
                return [row for row in super()._collect_db_files()
                        if re.fullmatch(r'message[\\/]message_\d+\.db', row[0])]

            def _open(self, rel):
                owner._identity()
                source = Path(self._db_path(rel))
                if not source.resolve().is_relative_to(owner.account_path / 'db_storage'):
                    raise ReceiptUnavailable('消息分片超出指定账号目录')
                wal = Path(str(source) + '-wal')
                states = (_file_state(source), _file_state(wal))
                _committed_wal(wal)
                connection = super()._open(rel)
                try:
                    stamp = Path(self.workdir) / (rel.replace(os.sep, '__') + '.stamp')
                    fields = stamp.read_text('ascii').split(',')
                    if (len(fields) != 6 or int(fields[0]) != STAMP_VERSION or int(fields[5]) < 0
                            or float(fields[1]) != source.stat().st_mtime
                            or int(fields[2]) != source.stat().st_size
                            or float(fields[3]) != (wal.stat().st_mtime if wal.exists() else 0)
                            or int(fields[4]) != (wal.stat().st_size if wal.exists() else 0)
                            or states != (_file_state(source), _file_state(wal))):
                        raise ReceiptUnavailable('消息分片快照过期或 WAL 未合并')
                    owner._identity()
                    return connection
                except BaseException:
                    connection.close()
                    raise

        try:
            self.db = PinnedDB(db_dir=str(owner.account_path.parent), account=owner.account_path.name,
                workdir=self.temp.name, keys_file=str(Path(self.temp.name) / 'unused-keys.json'))
            self._check_account()
        except BaseException:
            self.temp.cleanup()
            raise

    def _check_account(self):
        if (self.db.account != self.owner.account_path.name or self.db.wxid != self.owner.account
                or Path(self.db.account_dir).resolve() != self.owner.account_path
                or not self.db._db_files or self.db.unkeyed):
            raise ReceiptUnavailable('消息库账号或分片密钥无法完整核验')

    def snapshot(self, target, watermark=None):
        self._check_account()
        self.owner._identity()
        def collect(tables):
            maps = self.db._shard_maps(tables)
            rows = []
            for connection, table in tables:
                shard = self.db._shard_label(connection)
                if not shard or not maps.get(shard):
                    raise ReceiptUnavailable('消息分片发送者映射缺失')
                quoted = '"' + table.replace('"', '""') + '"'
                where, args = ('', ()) if watermark is None else (' WHERE sort_seq >= ?', (watermark,))
                cap = 100 if watermark is None else 1001
                fetched = connection.execute('SELECT local_id,server_id,real_sender_id,sort_seq,'
                    'local_type,message_content FROM ' + quoted + where +
                    ' ORDER BY sort_seq DESC,local_id DESC LIMIT ' + str(cap), args).fetchall()
                if watermark is not None and len(fetched) > 1000:
                    raise ReceiptUnavailable('新增消息数量超出回执核验范围')
                for item in fetched:
                    sender = maps[shard].get(int(item['real_sender_id'] or 0), '')
                    content = item['message_content']
                    if isinstance(content, bytes):
                        content = self.db._friendly_content(content, '文本')
                    rows.append({'shard': shard, 'local_id': item['local_id'],
                        'server_id': item['server_id'], 'sort_seq': item['sort_seq'],
                        'sender': sender, 'text': content if item['local_type'] == 1 else '',
                        'type': item['local_type']})
            return rows
        rows = self.db._run_msg_query(target, collect)
        self._check_account()
        self.owner._identity()
        if rows is None:
            raise ReceiptUnavailable('目标消息表不存在，无法建立发送前基线')
        return {'fresh': True, 'rows': rows}

    def close(self):
        self.temp.cleanup()


class NativeHookReceipt:
    def __init__(self, account_path, account, identity_probe, *, db_factory=None,
                 work_dir=None, poll_seconds=5):
        self.account_path = Path(account_path).resolve()
        self.account = str(account)
        if (not self.account.startswith('wxid_') or
                self.account_path.name not in (self.account, ) and
                re.sub(r'_\w{4}$', '', self.account_path.name) != self.account):
            raise ReceiptUnavailable('必须指定一致的微信稳定账号和账号目录')
        self.probe = identity_probe
        self.factory = db_factory
        self.work_dir = Path(work_dir or Path(os.environ.get('LOCALAPPDATA', Path.home())) /
                             'WhaleGirlWeChat/data/hook_receipts').resolve()
        if self.work_dir == self.account_path or self.work_dir.is_relative_to(self.account_path):
            raise ReceiptUnavailable('回执缓存不可写入微信账号目录')
        self.poll_seconds = min(8, max(0, float(poll_seconds)))
        self._binding = None
        self._provider = None
        self._tokens = {}
        self._lock = threading.RLock()

    def _identity(self):
        value = self.probe()
        if (not isinstance(value, dict) or value.get('verified') is not True
                or value.get('account') != self.account
                or Path(str(value.get('account_path') or '')).resolve() != self.account_path
                or type(value.get('pid')) is not int or value['pid'] <= 0
                or type(value.get('create_time')) not in (int, float)
                or not math.isfinite(value['create_time']) or value['create_time'] <= 0):
            raise ReceiptUnavailable('实际微信进程与账号无法核验')
        key = (value['pid'], value['create_time'], self.account, str(self.account_path))
        if self._binding is not None and self._binding != key:
            raise ReceiptUnavailable('微信进程或登录账号已经改变')
        self._binding = key
        return value

    def _snapshot(self, target, watermark=None):
        self._identity()
        if self._provider is None:
            self._provider = (self.factory(self) if self.factory else _NativeSnapshots(self, self.work_dir))
        result = self._provider.snapshot(target, watermark)
        self._identity()
        if not isinstance(result, dict) or result.get('fresh') is not True or not isinstance(result.get('rows'), list):
            raise ReceiptUnavailable('回执没有新鲜的只读消息快照')
        rows = result['rows']
        if len(rows) > 10000:
            raise ReceiptUnavailable('回执查询结果超出限制')
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('shard'), str) or not row['shard']
                    or any(type(row.get(k)) is not int or row[k] < 0
                           for k in ('local_id', 'server_id', 'sort_seq'))
                    or not isinstance(row.get('sender'), str) or not isinstance(row.get('text'), str)):
                raise ReceiptUnavailable('回执消息格式无效')
        return rows

    def prepare(self, account, target, text):
        if account != self.account or not isinstance(target, str) or not target or any(ord(c) < 32 for c in target):
            raise ReceiptUnavailable('回执目标或账号无效')
        digest = _body_hash(text)
        with self._lock:
            first = self._snapshot(target)
            second = self._snapshot(target)
            if first != second:
                raise ReceiptUnavailable('发送前消息基线尚未稳定')
            watermark = max((r['sort_seq'] for r in first), default=0)
            # 初次查询只保留每分片最近 100 条；最高序列可能被多条记录共享。
            # 发出前补齐该水位内的全部身份，不能把被截断的旧记录认成本次新增。
            complete = self._snapshot(target, watermark)
            if complete != self._snapshot(target, watermark):
                raise ReceiptUnavailable('发送前消息基线尚未稳定')
            if any(row['sort_seq'] > watermark for row in complete):
                raise ReceiptUnavailable('发送前出现新消息，请重新建立基线')
            now = time.monotonic()
            self._tokens = {k: v for k, v in self._tokens.items() if now - v['created'] <= 30}
            if len(self._tokens) >= 128:
                raise ReceiptUnavailable('回执基线数量达到上限')
            identifier = uuid.uuid4().hex
            self._tokens[identifier] = {'account': account, 'target': target, 'body_sha256': digest,
                'watermark': watermark,
                'existing': {(r['shard'], r['local_id']) for r in first + complete},
                'created': now, 'binding': self._binding}
            return {'id': identifier}

    def verify(self, token):
        with self._lock:
            baseline = self._tokens.pop(token.get('id'), None) if isinstance(token, dict) else None
            result = {'verified': False, 'account_matches': False, 'new_self_rows': 0,
                      'server_id': 0, 'recipient_read_verified': False}
            if baseline is None:
                return dict(result, reason='回执基线无效或已消费')
            result.update({k: baseline[k] for k in ('account', 'target', 'body_sha256')})
            if time.monotonic() - baseline['created'] > 30:
                return dict(result, reason='回执基线已过期')
            deadline = time.monotonic() + self.poll_seconds
            previous = None
            try:
                while True:
                    rows = self._snapshot(baseline['target'], baseline['watermark'])
                    matches = [row for row in rows if row['sort_seq'] >= baseline['watermark']
                        and (row['shard'], row['local_id']) not in baseline['existing']
                        and row['sender'] == self.account and row.get('type') == 1
                        and _body_hash(row['text']) == baseline['body_sha256']]
                    result.update(account_matches=self._binding == baseline['binding'], new_self_rows=len(matches))
                    if len(matches) > 1:
                        return dict(result, reason='出现多条相同自身消息，无法唯一核验')
                    if len(matches) == 1 and matches[0]['server_id'] > 0:
                        row = matches[0]
                        evidence = (row['shard'], row['local_id'], row['server_id'], row['sort_seq'])
                        if previous == evidence and result['account_matches']:
                            return dict(result, verified=True, server_id=row['server_id'], reason='新增自身服务器记录已核验')
                        previous = evidence
                    else:
                        previous = None
                    if time.monotonic() >= deadline:
                        return dict(result, reason='未在核验时限内取得唯一自身服务器记录')
                    time.sleep(min(.1, max(0, deadline - time.monotonic())))
            except Exception:
                return dict(result, reason='账号或消息快照核验未通过')

    def close(self):
        with self._lock:
            self._tokens.clear()
            if self._provider is not None:
                close = getattr(self._provider, 'close', None)
                if callable(close):
                    close()
                self._provider = None
            # disconnect 后允许同一固定账号的下一次连接建立新进程绑定；
            # 已消费/旧 token 已全部作废，不能跨进程拿旧基线作确认。
            self._binding = None
