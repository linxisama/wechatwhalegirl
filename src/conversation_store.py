"""本地会话记录：可重启、可查询；和发送队列分离，导入不会发消息。"""
import hashlib
import json
import math
import re
import time
import unicodedata

from .chat_state import persona_key, person_of
from .style_manager import DEFAULT_PERSONA


class ConversationStore:
    def __init__(self, db):
        self.db = db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS conversation_log(
          key TEXT PRIMARY KEY,mode TEXT,account TEXT,kind TEXT,target TEXT,mid TEXT,
          sender_id TEXT,sender_name TEXT,content TEXT,reply TEXT,persona TEXT,
          fingerprint TEXT,created REAL,updated REAL);
        CREATE INDEX IF NOT EXISTS conversation_room ON conversation_log(mode,account,kind,target,created);
        CREATE UNIQUE INDEX IF NOT EXISTS conversation_mid ON conversation_log(mode,account,kind,target,mid);
        CREATE TABLE IF NOT EXISTS conversation_meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS conversation_deleted(
          mode TEXT,account TEXT,kind TEXT,target TEXT,mid TEXT,
          PRIMARY KEY(mode,account,kind,target,mid));
        ''')
        if not db.execute("SELECT 1 FROM conversation_meta WHERE key='backfill1'").fetchone():
            self._backfill()
            db.execute("INSERT INTO conversation_meta VALUES('backfill1','done')")
        db.commit()

    @staticmethod
    def identity(message, mode, account):
        return hashlib.sha256(json.dumps([mode, account, message['kind'], message['target'],
            str(message.get('source_event') or message['id'])], ensure_ascii=False).encode()).hexdigest()

    def _insert(self, message, mode, account, persona='', fingerprint='', reply=''):
        if message.get('_robot_admin') or message.get('_admin_session'):
            return
        if message.get('kind')=='private' and str(message.get('text') or '').startswith(('管理员','/管理员')):
            return
        self.db.execute('INSERT OR IGNORE INTO conversation_log VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            self.identity(message, mode, account), mode, account, message['kind'], str(message['target']),
            str(message['id']), person_of(message), str(message.get('sender_name') or message.get('sender') or '')[:80],
            str(message.get('text') or '')[:65536], str(reply or '')[:65536], persona, fingerprint,
            float(message.get('created') or time.time()), time.time()))

    def _backfill(self):
        # 仅从已有本机数据回填，既不遍历微信界面，也不把历史变成待发送消息。
        for scope, mid, subject, name, text, created in self.db.execute(
                'SELECT scope,mid,subject,name,text,created FROM chat_ambient').fetchall():
            parts = str(scope).split('|', 2)
            if len(parts) != 3 or ':' not in parts[2]:
                continue
            kind, target = parts[2].split(':', 1)
            if kind not in ('private', 'group'):
                continue
            self._insert(dict(id=mid, kind=kind, target=target, sender_id=subject,
                              sender_name=name, text=text, created=created), parts[0], parts[1])
        for mode, account, raw, reply in self.db.execute(
                "SELECT mode,account,payload,reply FROM inbox WHERE state='sent'").fetchall():
            try:
                message = json.loads(raw)
                if message.get('_robot_admin') or message.get('_management_account') and message.get('kind') == 'private' and str(message.get('text', '')).startswith(('管理员', '/管理员')):
                    continue
                self._insert(message, mode, account, fingerprint=message.get('_persona_key', ''), reply=reply)
                self.db.execute('UPDATE conversation_log SET reply=?,fingerprint=? WHERE mode=? AND account=? AND kind=? AND target=? AND mid=?', (
                    str(reply or '')[:65536], message.get('_persona_key', ''), mode,account,message['kind'],message['target'],str(message['id'])))
            except (ValueError, TypeError, KeyError):
                continue

    def observe(self, message, runtime):
        self._insert(message, runtime.mode, runtime.account(), runtime.style.active_name,
                     persona_key(runtime.style.get().prompt_text()))
        if getattr(runtime,'learning',None):
            scope=(runtime.mode,runtime.account(),message['kind'],str(message['target']))
            if not self.db.execute('SELECT 1 FROM learning_messages WHERE mode=? AND account=? AND kind=? AND target=? LIMIT 1',scope).fetchone():
                runtime.learning.rebuild(runtime,message['kind'],str(message['target']))
            runtime.learning.observe(message,runtime)
        self.db.commit()

    def sent(self, message, reply, runtime):
        from .image_input import message_text
        self.db.execute('UPDATE conversation_log SET reply=?,fingerprint=?,persona=?,content=?,updated=? WHERE key=?', (
            str(reply)[:65536], message.get('_persona_key', ''),
            message.get('_persona_name', runtime.style.active_name), message_text(message)[:65536], time.time(),
            self.identity(message, runtime.mode, runtime.account())))
        self.db.commit()

    def delete(self, runtime, kind, target, key):
        scope = (runtime.mode,runtime.account(),kind,target)
        row = self.db.execute('SELECT mid FROM conversation_log WHERE mode=? AND account=? AND kind=? AND target=? AND key=?',scope+(key,)).fetchone()
        if row:
            # 原始发送日志保留，但已删除的记忆不能再次通过旧历史查询进入提示词。
            with self.db:
                if getattr(runtime,'learning',None):runtime.learning.clear(runtime,kind,target,row[0])
                self.db.execute('INSERT OR IGNORE INTO conversation_deleted VALUES(?,?,?,?,?)',scope+(row[0],))
                self.db.execute('DELETE FROM conversation_log WHERE key=?',(key,))

    def reset_time(self, runtime, kind, target):
        key = f'reset:{runtime.mode}|{runtime.account()}|{kind}:{target}'
        row = self.db.execute('SELECT value FROM conversation_meta WHERE key=?', (key,)).fetchone()
        return float(row[0]) if row else 0

    def records(self, runtime, kind, target, *, query='', limit=100, offset=0, since=0, before=None):
        where = 'mode=? AND account=? AND kind=? AND target=? AND created>=?'
        args = [runtime.mode, runtime.account(), kind, target, since]
        if before is not None:
            where += ' AND created<=?'
            args.append(before)
        if query:
            where += ' AND (instr(content,?)>0 OR instr(sender_name,?)>0 OR instr(reply,?)>0)'
            args.extend([query]*3)
        total = self.db.execute('SELECT count(*) FROM conversation_log WHERE '+where, args).fetchone()[0]
        rows = self.db.execute('SELECT key,mid,sender_id,sender_name,content,reply,persona,fingerprint,created,updated '
            'FROM conversation_log WHERE '+where+' ORDER BY created DESC,key DESC LIMIT ? OFFSET ?',
            args+[min(2000, max(1, int(limit))), max(0, int(offset))]).fetchall()
        fields = ('key','id','sender_id','sender_name','content','reply','persona','fingerprint','created','updated')
        return [dict(zip(fields, row), role='user', kind=kind, target=target) for row in rows], total

    def rooms(self, runtime):
        return [dict(kind=k, target=t, count=n) for k,t,n in self.db.execute(
            'SELECT kind,target,count(*) FROM conversation_log WHERE mode=? AND account=? '
            'GROUP BY kind,target ORDER BY max(created) DESC', (runtime.mode, runtime.account())).fetchall()]

    @staticmethod
    def _excluded(message, excluded_ids=()):
        return {str(message['id']), *map(str, message.get('_merged_ids', [])), *map(str, excluded_ids)}

    @staticmethod
    def _entry(record, fingerprint, width=1500):
        from .image_input import safe_image_history
        content=safe_image_history(record['content'])
        failure=content!=record['content']
        entry = {'id':record['id'], 'time':record['created'], 'sender_id':record['sender_id'],
                 'sender':record['sender_name'], 'user':content[:width]}
        if not failure and record['fingerprint'] == fingerprint and record['reply']:
            entry['assistant_to_this_sender'] = record['reply'][:width]
        return entry

    def context(self, message, runtime, turns=10, max_chars=8000, excluded_ids=()):
        if turns <= 0 or max_chars <= 0:
            return []
        records, _ = self.records(runtime, message['kind'], message['target'], limit=turns+10,
                                  before=float(message.get('created') or time.time()))
        excluded = self._excluded(message, excluded_ids)
        fingerprint = persona_key(runtime.style.get().prompt_text())
        result, used = [], 0
        for record in records:
            if record['id'] in excluded or str(record['content']).startswith('/'):
                continue
            entry = self._entry(record, fingerprint)
            size = len(json.dumps(entry, ensure_ascii=False))
            if len(result) >= turns:
                break
            if used+size > max_chars:
                continue
            result.append(entry)
            used += size
        return list(reversed(result))

    @staticmethod
    def search_terms(text):
        """本地词面召回，不花模型请求；去掉寻址和常见问句，避免所有旧记录都命中。"""
        text = unicodedata.normalize('NFKC', str(text or '')).casefold()
        text = re.sub(r'@[^\s@，,。！？!?]+', ' ', text)
        stop = {'之前','以前','记得','什么','怎么','那个','这个','我们','你们','还有','现在',
                '一下','多少','时候','可以','的是','我的','你的','请问','帮我','the','was','what','with','this','that'}
        words = re.findall(r'[a-z0-9_][a-z0-9_./-]{1,39}|[\u4e00-\u9fff]+', text)
        terms = []
        for word in words:
            if re.fullmatch(r'[\u4e00-\u9fff]+',word):
                for filler in sorted(stop,key=len,reverse=True):
                    word = word.replace(filler,' ')
                values = [part[i:i+2] for part in word.split() for i in range(len(part)-1)]
            else:
                values = [word]
            terms.extend(v for v in values if v not in stop)
        return list(dict.fromkeys(terms))[:16]

    def recall(self, message, runtime, limit=3, max_chars=2000, excluded_ids=()):
        """检索整个当前会话；只输出有词面证据的片段，磁盘原文保持不变。"""
        terms = self.search_terms(message.get('text'))
        if not terms or limit <= 0 or max_chars < 200:
            return []
        fingerprint = persona_key(runtime.style.get().prompt_text())
        # 排序计算在 SQLite 完成，避免把整个群的原文全部搬进 Python 或模型上下文。
        scores = ['(CASE WHEN instr(lower(content),?)>0 THEN 2 ELSE 0 END + '
                  'CASE WHEN fingerprint=? AND instr(lower(reply),?)>0 THEN 1 ELSE 0 END)' for _ in terms]
        args = [value for term in terms for value in (term,fingerprint,term)]
        fields = ('id','sender_id','sender_name','content','reply','fingerprint','created')
        window=(' AND mid IN (SELECT mid FROM learning_messages WHERE mode=conversation_log.mode '
                'AND account=conversation_log.account AND kind=conversation_log.kind AND target=conversation_log.target) '
                if getattr(runtime,'learning',None) and message.get('kind')=='group' else '')
        rows = self.db.execute('SELECT mid,sender_id,sender_name,content,reply,fingerprint,created,('
            + '+'.join(scores)+') AS relevance FROM conversation_log '
            'WHERE mode=? AND account=? AND kind=? AND target=? AND created<=? '
            'AND substr(content,1,1)<>\'/\' AND relevance>0 '+window+' ORDER BY relevance DESC,created DESC LIMIT 100',
            args+[runtime.mode,runtime.account(),message['kind'],message['target'],
                  float(message.get('created') or time.time())]).fetchall()
        excluded = self._excluded(message, excluded_ids)
        result, seen, used = [], set(), 0
        for row in rows:
            record = dict(zip(fields,row))
            if record['id'] in excluded:
                continue
            entry = self._entry(record, fingerprint, width=600)
            for field in ('user','assistant_to_this_sender'):
                from .image_input import safe_image_history
                source = safe_image_history(record['content']) if field=='user' else record['reply']
                if field not in entry:
                    continue
                positions = [source.casefold().find(term) for term in terms if term in source.casefold()]
                start = max(0,min(positions)-100) if positions else 0
                entry[field] = ('…' if start else '')+source[start:start+600]
            signature = (entry['sender_id'],entry['user'],entry.get('assistant_to_this_sender',''))
            size = len(json.dumps(entry,ensure_ascii=False))+2
            if signature in seen or used+size>max_chars:
                continue
            seen.add(signature);result.append(entry);used+=size
            if len(result)>=min(10,limit):
                break
        return sorted(result,key=lambda item:item['time'])

    def export(self, runtime, kind, target):
        records, total = self.records(runtime, kind, target, limit=2000)
        return dict(schema='WhaleGirlWeChat.conversations', version=1, account=runtime.account(),
                    kind=kind, target=target, exported_at=time.time(), total=total,
                    truncated=total>len(records), records=list(reversed(records)),
                    training_samples=[{'persona':r['persona'], 'messages':[
                        {'role':'user','content':r['content']}, {'role':'assistant','content':r['reply']}]
                    } for r in reversed(records) if r['reply'] and r['fingerprint']])

    def restore(self, runtime, kind, target, bundle, mode='merge'):
        if not isinstance(bundle, dict) or bundle.get('schema') != 'WhaleGirlWeChat.conversations' or bundle.get('version') != 1:
            raise ValueError('不是受支持的本地对话库 JSON')
        if bundle.get('account') != runtime.account() or bundle.get('kind') != kind or bundle.get('target') != target:
            raise ValueError('备份账号或会话与当前选择不一致，不能跨群导入')
        if mode not in ('merge','replace') or mode == 'replace' and bundle.get('truncated'):
            raise ValueError('请选择合并或覆盖；截断备份只能合并')
        records = bundle.get('records')
        if not isinstance(records, list) or len(records)>2000:
            raise ValueError('一次最多导入 2000 条记录')
        validated=[]
        for r in records:
            if not isinstance(r,dict) or r.get('kind')!=kind or r.get('target')!=target or r.get('role')!='user':
                raise ValueError('记录的角色或会话字段无效')
            for field, maximum in [('id',300),('sender_id',256),('sender_name',80),('content',65536),('reply',65536),('persona',48),('fingerprint',64)]:
                if not isinstance(r.get(field),str) or len(r[field])>maximum:
                    raise ValueError('记录字段无效：'+field)
            if not r['id'] or not isinstance(r.get('created'),(int,float)) or isinstance(r['created'],bool) or not math.isfinite(r['created']) or not 0<r['created']<=time.time()+86400:
                raise ValueError('记录时间或消息标识无效')
            validated.append(r)
        # 所有校验在删除之前完成；SQLite 事务使覆盖失败时能够完整回滚。
        with self.db:
            if mode=='replace':
                self.clear(runtime,kind,target,commit=False)
            for r in validated:
                self.db.execute('DELETE FROM conversation_deleted WHERE mode=? AND account=? AND kind=? AND target=? AND mid=?',
                    (runtime.mode,runtime.account(),kind,target,r['id']))
                message=dict(id=r['id'],kind=kind,target=target,sender_id=r['sender_id'],
                             sender_name=r['sender_name'],text=r['content'],created=r['created'],_learning_persona=r['persona'] or DEFAULT_PERSONA)
                self._insert(message,runtime.mode,runtime.account(),r['persona'],r['fingerprint'],r['reply'])
                if getattr(runtime,'learning',None):runtime.learning.observe(message,runtime)
        return len(validated)

    def clear(self, runtime, kind, target, commit=True):
        if getattr(runtime,'learning',None):runtime.learning.clear(runtime,kind,target)
        self.db.execute('DELETE FROM conversation_log WHERE mode=? AND account=? AND kind=? AND target=?',
                        (runtime.mode,runtime.account(),kind,target))
        self.db.execute('INSERT OR REPLACE INTO conversation_meta VALUES(?,?)',
            (f'reset:{runtime.mode}|{runtime.account()}|{kind}:{target}', str(time.time())))
        if commit:
            self.db.commit()
