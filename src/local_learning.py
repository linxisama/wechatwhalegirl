"""有证据的本地学习：活动窗口有界，不修改人设，也不训练服务商模型。"""
import hashlib
import json
import re
import time

from .chat_state import safe_fact,person_of
from .style_manager import DEFAULT_PERSONA


PLACEHOLDER = re.compile(r'^\[(图片|表情|动画表情|语音|文件|文件/链接|链接|视频|位置|小程序|转账|红包|聊天记录|引用|拍一拍)\]$')


def meaningful_text(text):
    """这行字值不值得当"学到的表达"。

    真实群聊里大量是占位符和单号：`[图片]`、`[文件/链接]`、`YCNY20250101083`。
    它们被当成"表达例句"会让记忆库变成垃圾场，回复反而更机械 —— 所以要挡掉：
    至少两个汉字（或一段像样的英文），且数字占比不能过半。
    """
    raw = str(text or '').strip()
    if not raw or raw.startswith('[') or PLACEHOLDER.match(raw):
        return False
    # "拍了拍"是微信的系统提示，不是人说的话
    if '拍了拍' in raw:
        return False
    han = len(re.findall(r'[\u4e00-\u9fff]', raw))
    letters = len(re.findall(r'[A-Za-z]', raw))
    digits = sum(ch.isdigit() for ch in raw)
    if digits and digits / len(raw) > 0.5:
        return False
    if han >= 2:
        return True
    return letters >= 8 and len(raw) >= 8


# 学习库结构版本：每次改提炼规则就加一。旧机器升级后据此重新提炼一遍，
# 避免「归档有几千条、学习库空白」这种升级后遗症（1.3.76 用户反馈的就是这个）。
LEARN_SCHEMA_VERSION = '2'

SENDER_PREFIX = re.compile(r'^(?:wxid_[A-Za-z0-9_\-]{4,}|[A-Za-z0-9_\-]{4,40}@chatroom):\s*\n?\s*')


def strip_sender_prefix(text):
    """去掉归档里的 "wxid_xxx:\n" 前缀。

    群聊归档把发送者写在正文前面（形如 `wxid_2rc4z7dz591922:\n我手忙的发软了`）。
    学习逻辑原来遇到换行直接跳过 —— 于是**群聊一条都学不到**，记忆页自然空白。
    """
    return SENDER_PREFIX.sub('', str(text or ''), count=1).strip()


class LocalLearning:
    WINDOW=1000
    def __init__(self,db):
        self.db=db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS learning_messages(
          mode TEXT,account TEXT,kind TEXT,target TEXT,mid TEXT,subject TEXT,name TEXT,
          content TEXT,created REAL,PRIMARY KEY(mode,account,kind,target,mid));
        CREATE INDEX IF NOT EXISTS learning_window ON learning_messages(mode,account,kind,target,created);
        CREATE TABLE IF NOT EXISTS learning_items(
          id TEXT PRIMARY KEY,mode TEXT,account TEXT,kind TEXT,target TEXT,subject TEXT,
          category TEXT,content TEXT,status TEXT,hits INTEGER,evidence TEXT,created REAL,updated REAL,persona TEXT);
        CREATE INDEX IF NOT EXISTS learning_scope ON learning_items(mode,account,kind,target,updated);
        ''')
        if 'persona' not in {row[1] for row in db.execute('PRAGMA table_info(learning_items)')}:
            db.execute('ALTER TABLE learning_items ADD COLUMN persona TEXT')
            db.execute('UPDATE learning_items SET persona=? WHERE persona IS NULL',(DEFAULT_PERSONA,))

    @staticmethod
    def _settings(runtime):
        return getattr(getattr(runtime,'plugins',None),'_plugins',{}).get('conversation_memory')

    def _visibility(self,runtime,include_default=False):
        settings=self._settings(runtime)
        if settings is None or settings.get('personaIsolation',True):
            # 人设隔离必须严格：把别人设的条目混进来，等于把 A 的偏好当成 B 的记忆。
            # 切换人设后页面看起来空是"确实还没学"，由界面提示去补学，不能靠放宽隔离来填。
            return ' AND persona=?',(runtime.style.active_name,)
        return '',()

    @staticmethod
    def scope(runtime,kind,target):return runtime.mode,runtime.account(),kind,target

    def observe(self,message,runtime):
        scope=self.scope(runtime,message['kind'],str(message['target']))
        mid=str(message['id']);subject=person_of(message)
        text=strip_sender_prefix(message.get('text'))
        if message.get('_robot_admin') or message.get('_admin_session') or text.startswith(('管理员','/')):
            return
        oldest=self.db.execute('SELECT min(created),count(*) FROM learning_messages WHERE mode=? AND account=? AND kind=? AND target=?',scope).fetchone()
        if oldest[1]>=self.WINDOW and float(message.get('created') or time.time())<oldest[0]:return
        changed=self.db.execute('INSERT OR IGNORE INTO learning_messages VALUES(?,?,?,?,?,?,?,?,?)',
            scope+(mid,subject,str(message.get('sender_name') or message.get('sender') or '')[:80],text[:4000],float(message.get('created') or time.time()))).rowcount
        if changed and subject and not message.get('_history_self') and subject!=getattr(getattr(runtime,'bridge',None),'self_id',''):
            settings=self._settings(runtime)
            if settings is None or settings.enabled:
                self._extract(scope,mid,subject,text,message.get('_learning_persona') or runtime.style.active_name,
                              settings is None or settings.get('autoRemember',True))
        # 同时按时间与消息ID排序，导入乱序旧消息不能挤掉较新的上下文。
        self.db.execute('DELETE FROM learning_messages WHERE rowid IN (SELECT rowid FROM learning_messages '
            'WHERE mode=? AND account=? AND kind=? AND target=? ORDER BY created DESC,rowid DESC LIMIT -1 OFFSET ?)',scope+(self.WINDOW,))

    def _extract(self,scope,mid,subject,text,persona,auto_facts=True):
        if '\n' in text or not 3<=len(text)<=120 or '@' in text or re.search(r'https?://|[?？]|[吗么]$',text):return
        # 笑声、单字重复、纯表情这类"没有信息量"的话不该进学习库：
        # 它们会成为第一条"表达例句"，让记忆页看起来像垃圾堆（用户已经反馈过空白/无意义）。
        if len(set(text)) < 3 or re.fullmatch(r'[哈嘿嘻嗯哦噢呃啊嗷草6wzlh\s~～.!！。，、]+', text):return
        # 占位符、单号、纯符号不进学习库（真实数据里这类占了绝大多数）
        if not meaningful_text(text):return
        try:safe_fact(text)
        except ValueError:return
        fact=any(text.startswith(prefix) and len(text)>len(prefix) for prefix in
                 ('我喜欢','我不喜欢','我叫','我的昵称是','我住在','我的生日是','我正在学习'))
        category='fact' if fact else 'expression'
        if fact and not auto_facts:return
        if not fact and (len(text)>32 or text.startswith(('你','请','忽略','执行','系统','帮我'))):return
        identity=hashlib.sha256(json.dumps(scope+(subject,category,text,persona),ensure_ascii=False).encode()).hexdigest()
        row=self.db.execute('SELECT hits,evidence,status FROM learning_items WHERE id=?',(identity,)).fetchone()
        evidence=json.loads(row[1]) if row else []
        if mid in evidence:return
        hits=(row[0] if row else 0)+1;evidence=(evidence+[mid])[-20:]
        status=row[2] if row else 'candidate'
        if fact and hits>=2 and status=='candidate':status='approved'
        now=time.time()
        self.db.execute('INSERT INTO learning_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET hits=excluded.hits,evidence=excluded.evidence,status=excluded.status,updated=excluded.updated',
            (identity,)+scope+(subject,category,text,status,hits,json.dumps(evidence),now,now,persona))
        # 先淘汰未确认候选，保留有用条目；每个会话最多200条学习结果。
        self.db.execute('DELETE FROM learning_items WHERE id IN (SELECT id FROM learning_items WHERE mode=? AND account=? AND kind=? AND target=? '
            "ORDER BY (status='approved') DESC,updated DESC,id DESC LIMIT -1 OFFSET 200)",scope)

    def rebuild(self,runtime,kind,target,limit=None):
        """从本会话最近记录提炼；``limit`` 让调用方按块处理，别一次占住事件循环。"""
        rows,_=runtime.conversations.records(runtime,kind,target,limit=int(limit or self.WINDOW))
        for row in reversed(rows):
            self.observe(dict(id=row['id'],kind=kind,target=target,sender_id=row['sender_id'],
                # 旧行常常没有 persona 标记（功能上线前写的）。补学时按**当前生效人设**归属，
                # 否则这些语料全落到默认人设下，用户切到自己的"123"会看到空白。
                sender_name=row['sender_name'],text=row['content'],created=row['created'],
                _learning_persona=row['persona'] or getattr(getattr(runtime,'style',None),'active_name',None) or DEFAULT_PERSONA),runtime)
        return len(rows)

    def backfill_candidates(self, runtime, limit_rooms=30):
        """补学候选：只有"一条学习结果都没有、也没有活动窗口"的会话才需要补。

        拆出来是为了让调用方**逐个会话**处理并在中间让出事件循环 —— 之前整个过程
        塞进 `asyncio.to_thread`，关闭时线程还在跑，会让后台迟迟退不出去。
        """
        try:
            rooms = runtime.conversations.rooms(runtime) or []
        except Exception:
            return []
        out = []
        for room in rooms[:limit_rooms]:
            scope = self.scope(runtime, room['kind'], room['target'])
            try:
                learned = self.db.execute(
                    'SELECT count(*) FROM learning_items WHERE mode=? AND account=? AND kind=? AND target=?',
                    scope).fetchone()[0]
                if learned:
                    continue
            except Exception:
                continue
            # 1.3.75 及以前这里还要求「活动窗口为空」。可旧版会把消息写进窗口却一条也提炼不出来
            # （群聊正文带 wxid_xxx: 换行前缀，旧逻辑整条跳过），窗口一非空就永远不再补，
            # 用户只能自己点「一键学习」。现在只要一条学习结果都没有就补，靠 relearn 兜住窗口。
            out.append((room['kind'], room['target']))
        return out

    def backfill_one(self, runtime, kind, target, limit=None):
        """补一个会话；出错就当这次没补，绝不影响收发。"""
        try:
            scope = self.scope(runtime, kind, target)
            learned = self.db.execute(
                'SELECT count(*) FROM learning_items WHERE mode=? AND account=? AND kind=? AND target=?',
                scope).fetchone()[0]
            window = self.db.execute(
                'SELECT count(*) FROM learning_messages WHERE mode=? AND account=? AND kind=? AND target=?',
                scope).fetchone()[0]
            if not learned and window:
                return self.relearn(runtime, kind, target, limit=limit)
            return self.rebuild(runtime, kind, target, limit=limit)
        except Exception:
            return 0

    def relearn(self, runtime, kind, target, limit=None):
        """按归档原文重新提炼一遍，忽略「这条消息已经进过窗口」的去重。

        rebuild 走 observe，而 observe 用 INSERT OR IGNORE 判断新消息，老窗口里的消息一律
        changed=0，所以对升级前就写进窗口的语料 rebuild 永远是空转。这里直接调 _extract：
        它按（会话、发言人、分类、原文、人设）算 id，并用 mid 去重证据，天然幂等，重复跑不会翻倍。
        """
        scope = self.scope(runtime, kind, target)
        rows, _ = runtime.conversations.records(runtime, kind, target, limit=int(limit or self.WINDOW))
        settings = self._settings(runtime)
        if settings is not None and not settings.enabled:
            return 0
        auto_facts = settings is None or settings.get('autoRemember', True)
        active = getattr(getattr(runtime, 'style', None), 'active_name', None) or DEFAULT_PERSONA
        scanned = 0
        for row in reversed(rows):
            text = strip_sender_prefix(row['content'])
            if not text:
                continue
            subject = person_of({'sender_id': row['sender_id'], 'sender_name': row['sender_name']})
            if not subject:
                continue
            self._extract(scope, str(row['id']), subject, text, row['persona'] or active, auto_facts)
            scanned += 1
        return scanned

    def backfill_all(self, runtime, limit_rooms=30):
        """升级后把本地**已有**的聊天原文补学一遍。

        为什么需要：学习库是 1.2.56 才有的；老用户的 conversation_log 里躺着几百条
        原文，但 learning_items 是空的 —— 界面就显示"一大片空白"，用户会以为功能坏了。
        这里只补"一条学习结果都没有"的会话，天然幂等，重复执行不会重复计数。
        """
        done = []
        try:
            rooms = runtime.conversations.rooms(runtime) or []
        except Exception:
            return done
        for room in rooms[:limit_rooms]:
            kind, target = room['kind'], room['target']
            scope = self.scope(runtime, kind, target)
            try:
                learned = self.db.execute(
                    'SELECT count(*) FROM learning_items WHERE mode=? AND account=? AND kind=? AND target=?',
                    scope).fetchone()[0]
                if learned:
                    continue
                window = self.db.execute(
                    'SELECT count(*) FROM learning_messages WHERE mode=? AND account=? AND kind=? AND target=?',
                    scope).fetchone()[0]
                if window:
                    continue
                count = self.rebuild(runtime, kind, target)
            except Exception:
                continue
            if count:
                done.append((target, count))
        if done:
            try:
                runtime.note('已从本地聊天原文补学：' + '、'.join(f'{t}（{n} 条）' for t, n in done[:5])
                             + (f' 等 {len(done)} 个会话' if len(done) > 5 else ''))
            except Exception:
                pass
        return done

    def overview(self, runtime, limit_rooms=50):
        """所有会话的学习总览：让记忆页一进来就有东西看，而不是空白。"""
        rooms = []
        for room in (runtime.conversations.rooms(runtime) or [])[:limit_rooms]:
            kind, target = room['kind'], room['target']
            scope = self.scope(runtime, kind, target)
            try:
                window = self.db.execute('SELECT count(*) FROM learning_messages WHERE mode=? AND account=? '
                                         'AND kind=? AND target=?', scope).fetchone()[0]
                rows = self.db.execute('SELECT category,status,count(*) FROM learning_items WHERE mode=? '
                                       'AND account=? AND kind=? AND target=? GROUP BY category,status',
                                       scope).fetchall()
            except Exception:
                continue
            counts = {'fact': 0, 'expression': 0, 'approved': 0, 'candidate': 0}
            for category, status, number in rows:
                counts[category] = counts.get(category, 0) + number
                counts[status] = counts.get(status, 0) + number
            total = counts['fact'] + counts['expression']
            archive = int(room['count'] or 0)
            rooms.append(dict(kind=kind, target=target, archive=archive, window=window,
                              facts=counts['fact'], expressions=counts['expression'],
                              approved=counts['approved'], candidate=counts['candidate'],
                              # 归档里有原文却一条都没提炼出来时，页面要给出可点的下一步，不能只显示 0。
                              pending=0 if total else archive,
                              hint=('' if total else
                                    (f'有 {archive} 条聊天记录还没提炼，点“一键学习”' if archive
                                     else '这个会话还没有聊天记录'))))
        return rooms

    def bulk(self, runtime, action, kind=None, target=None, limit_rooms=30):
        """批量操作：一次性给所有（或指定）会话补学/批准表达，省得逐个点。"""
        if action not in ('learn-all', 'approve-expressions'):
            raise ValueError('请选择一键学习或批量批准')
        rooms = ([{'kind': kind, 'target': target}] if target and kind
                 else (runtime.conversations.rooms(runtime) or [])[:limit_rooms])
        learned = approved = 0
        for room in rooms:
            scope = self.scope(runtime, room['kind'], room['target'])
            try:
                if action == 'learn-all':
                    learned += self.rebuild(runtime, room['kind'], room['target'])
                else:
                    approved += self.db.execute(
                        "UPDATE learning_items SET status='approved',updated=? WHERE mode=? AND account=? "
                        "AND kind=? AND target=? AND category='expression' AND status='candidate'",
                        (time.time(),) + scope).rowcount
            except Exception:
                continue
        return dict(rooms=len(rooms), learned=learned, approved=approved)

    def view(self,runtime,kind,target):
        scope=self.scope(runtime,kind,target)
        fields=('id','subject','category','content','status','hits','evidence','created','updated','persona')
        clause,params=self._visibility(runtime)
        rows=self.db.execute('SELECT '+','.join(fields)+' FROM learning_items WHERE mode=? AND account=? AND kind=? AND target=?'+clause+' ORDER BY updated DESC',scope+params).fetchall()
        items=[dict(zip(fields,row)) for row in rows]
        for item in items:
            item['evidence']=json.loads(item['evidence'])
            name=self.db.execute('SELECT name FROM learning_messages WHERE mode=? AND account=? AND kind=? AND target=? AND subject=? ORDER BY created DESC LIMIT 1',scope+(item['subject'],)).fetchone()
            item['subject_name']=name[0] if name and name[0] else item['subject']
        count=self.db.execute('SELECT count(*) FROM learning_messages WHERE mode=? AND account=? AND kind=? AND target=?',scope).fetchone()[0]
        return dict(window_count=count,window_limit=self.WINDOW,items=items)

    def change(self,runtime,kind,target,identity,action):
        scope=self.scope(runtime,kind,target)
        if action not in ('approve','reject','delete'):raise ValueError('请选择批准、拒绝或删除')
        clause,params=self._visibility(runtime)
        if not self.db.execute('SELECT 1 FROM learning_items WHERE id=? AND mode=? AND account=? AND kind=? AND target=?'+clause,(identity,)+scope+params).fetchone():
            raise ValueError('学习条目不存在或属于其他会话')
        if action=='delete':self.db.execute('DELETE FROM learning_items WHERE id=?',(identity,))
        else:self.db.execute('UPDATE learning_items SET status=?,updated=? WHERE id=?',('approved' if action=='approve' else 'rejected',time.time(),identity))

    def recall(self,message,runtime,max_chars=700):
        scope=self.scope(runtime,message['kind'],str(message['target']))
        terms=runtime.conversations.search_terms(message.get('text'))
        clause,params=self._visibility(runtime)
        rows=self.db.execute("SELECT category,content,hits FROM learning_items WHERE mode=? AND account=? AND kind=? AND target=? AND subject=? AND status='approved'"+clause+' ORDER BY updated DESC LIMIT 100',scope+(person_of(message),)+params).fetchall()
        ranked=sorted(rows,key=lambda r:(sum(t in r[1].casefold() for t in terms),r[2]),reverse=True)
        result=[];used=0;seen=set()
        for category,content,hits in ranked:
            if (category,content) in seen:continue
            # 表达例句仅作语气参考，最多一条；事实只引用当前问题相关项。
            if category=='fact' and terms and not any(t in content.casefold() for t in terms):continue
            if category=='expression' and any(r['type']=='expression' for r in result):continue
            item=dict(type=category,text=content,evidence_count=hits)
            size=len(json.dumps(item,ensure_ascii=False))
            if used+size>max_chars:continue
            result.append(item);used+=size;seen.add((category,content))
            if len(result)>=4:break
        return result

    def clear(self,runtime,kind,target,mid=None):
        scope=self.scope(runtime,kind,target)
        if mid is None:
            for table in ('learning_messages','learning_items'):
                self.db.execute('DELETE FROM '+table+' WHERE mode=? AND account=? AND kind=? AND target=?',scope)
        else:
            self.db.execute('DELETE FROM learning_messages WHERE mode=? AND account=? AND kind=? AND target=? AND mid=?',scope+(mid,))
            # 删除证据后不能让对应学习条目残留并继续引用。
            for identity,evidence in self.db.execute('SELECT id,evidence FROM learning_items WHERE mode=? AND account=? AND kind=? AND target=?',scope).fetchall():
                if mid in json.loads(evidence):self.db.execute('DELETE FROM learning_items WHERE id=?',(identity,))
