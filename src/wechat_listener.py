"""在注册监听前过滤会话，避免每轮遍历所有不需要回复的群。"""
import io
import re
import time
from xml.etree import ElementTree as ET


def mention_ids(source):
    """source 可是压缩二进制；限制解压，且只相信明确的 atuserlist。"""
    limit = 65536
    if isinstance(source, memoryview):
        source = source.tobytes()
    if isinstance(source, bytes):
        if len(source) > limit:
            raise ValueError('mention source too large')
        if source.startswith(b'\x28\xb5\x2f\xfd'):
            import zstandard
            with zstandard.ZstdDecompressor(max_window_size=1024).stream_reader(io.BytesIO(source)) as reader:
                source = reader.read(limit + 1)
        if len(source) > limit:
            raise ValueError('mention source too large')
        source = source.decode('utf-8-sig')
    if not isinstance(source, str) or not source:
        return []
    if len(source) > limit or len(source.encode('utf-8')) > limit:
        raise ValueError('mention source too large')
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', source, re.I):
        raise ValueError('unsafe mention XML')
    root = ET.fromstring(source)
    if root.tag != 'msgsource':
        return []
    ids = []
    for node in root.iter('atuserlist'):
        for value in re.split(r'[,;\s]+', node.text or ''):
            if re.fullmatch(r'[A-Za-z0-9_@.\-]{1,128}', value) and value not in ids:
                ids.append(value)
    return ids


class ListenerDatabase:
    def __init__(self, db, groups, listen_private=True, listen_groups=True, note=None):
        self._db = db
        self._groups = set(groups or [])
        self._listen_private = listen_private
        self._listen_groups = listen_groups
        self._names = {}
        self._note = note
        self._mention_errors = {}
        # 库在 add_all 里会先握一次会话全量，这里借那次调用记下「启动时就存在」的会话：
        # 只有运行中新出现的会话才需要修补初始水位（见 get_messages）。
        self._startup_users = None

    def __getattr__(self, name):
        return getattr(self._db, name)

    def _source(self, user, raw):
        local_id, seq, shard = raw.get('local_id'), raw.get('sort_seq'), raw.get('shard')
        if local_id is None or seq is None or not isinstance(shard, str) or not shard:
            return None
        queryer = getattr(self._db, '_run_msg_query', None)
        shard_label = getattr(self._db, '_shard_label', None)
        if callable(queryer) and callable(shard_label):
            def query(tables):
                found = []
                for conn, table in tables:
                    if shard_label(conn) != shard:
                        continue
                    # 同一分片和序号确认归属，CAST 避免库的 text_factory 损坏压缩字节。
                    safe_table = '"' + table.replace('"', '""') + '"'
                    rows = conn.execute('SELECT CAST(source AS BLOB) AS source FROM ' + safe_table +
                                        ' WHERE local_id=? AND sort_seq=? LIMIT 2', (local_id, seq)).fetchall()
                    found.extend(row['source'] for row in rows)
                    if len(found) > 1:
                        break
                return found
            found = queryer(user, query)
            return found[0] if len(found) == 1 else None
        getter = getattr(self._db, 'get_message_row', None)
        if callable(getter):
            meta = getter(user, local_id, shard=shard)
            if isinstance(meta, dict) and meta.get('shard') == shard and meta.get('sort_seq') == seq:
                return meta.get('source')
        return None

    def get_messages(self, user, limit=20, offset=0):
        """把运行中新出现的会话那一条「让会话被发现的」消息从初始水位里抠出来。

        库给新会话定水位时取 `get_messages(user, limit=1)[0]['sort_seq']`，而增量查询是
        `sort_seq > 水位`：让会话出现的那条消息恰好等于水位，于是永久取不到（用户看到的是
        新加的人/新群第一次说话机器人不理）。这里只在这种「取单条最新消息当水位」的调用上
        把 sort_seq 减 1，让初始水位落在该消息之前，增量查询就会取到它，且只取一次
        （投递后水位照常推进到真实 sort_seq）。启动时就已存在的会话原样透传，仍不重放历史；
        真实数据不改动，返回的是副本。
        """
        rows = self._db.get_messages(user, limit=limit, offset=offset)
        if limit != 1 or offset != 0 or not isinstance(rows, list) or not rows:
            return rows
        if self._startup_users is None or user in self._startup_users:
            return rows
        newest = rows[0]
        if not isinstance(newest, dict) or not isinstance(newest.get('sort_seq'), int):
            return rows
        return [dict(newest, sort_seq=newest['sort_seq'] - 1)]

    def get_new_messages(self, user, *args, **kwargs):
        rows = self._db.get_new_messages(user, *args, **kwargs)
        if not str(user).endswith('@chatroom') or not isinstance(rows, list):
            return rows
        enriched = []
        for raw in rows:
            if isinstance(raw, dict) and not (raw.get('at_ids') or raw.get('atuserlist')) and \
                    re.search(r'[@＠]', str(raw.get('content') or '')):
                try:
                    ids = mention_ids(self._source(user, raw))
                    if ids:
                        raw = dict(raw, at_ids=ids)
                except Exception as exc:
                    # 元数据不可用不丢消息，不在日志中泄露原文、账号或压缩内容。
                    kind = type(exc).__name__
                    now = time.monotonic()
                    if callable(self._note) and now - self._mention_errors.get(kind, -float('inf')) >= 30:
                        self._mention_errors[kind] = now
                        try:
                            self._note(f'群聊 @ 元数据读取失败（{kind}），保留原消息并按现有触发规则处理')
                        except Exception:
                            pass
            enriched.append(raw)
        return enriched

    def get_sessions(self, limit=500):
        sessions = list(self._db.get_sessions(limit=limit))
        if self._startup_users is None:
            # 第一次取会话就是库启动时的全量快照（db.Listener.add_all 紧接着会再取一次做发现）。
            # 名单按未过滤的全量 username 记，避免开关过滤条件变化时把老会话错当新会话。
            self._startup_users = {str(session.get("username") or "") for session in sessions}
        accepted = []
        for session in sessions:
            user = str(session.get("username") or "")
            if not user.endswith("@chatroom"):
                if self._listen_private:
                    accepted.append(session)
                continue
            if not self._listen_groups:
                continue
            if not self._groups or user in self._groups:
                accepted.append(session)
                continue
            cached = self._names.get(user)
            if cached is None or time.monotonic() - cached[0] > 60:
                # 群名可变化，缓存定期失效；查名发生在监听线程而非 HTTP 事件循环。
                try:
                    name = self._db.group_id_to_name(user)
                except Exception:
                    name = user
                cached = self._names[user] = (time.monotonic(), name)
            if cached[1] in self._groups:
                accepted.append(session)
        return accepted
