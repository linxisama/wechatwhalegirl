"""主人专属工作区：文件是真实本地文件，聊天和共享记忆不进入微信队列。"""
from __future__ import annotations

import hashlib
import json
import re
import stat
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .agent_authority import authority
from .persistence import atomic_text


class WorkspaceError(ValueError):
    pass


class AgentWorkspace:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS agent_dialogue(
          workspace TEXT,session TEXT,request TEXT,persona TEXT,role TEXT,content TEXT,created REAL);
        CREATE INDEX IF NOT EXISTS agent_dialogue_scope ON agent_dialogue(workspace,session,persona,created);
        CREATE INDEX IF NOT EXISTS agent_diary_day ON agent_dialogue(workspace,persona,created);
        CREATE TABLE IF NOT EXISTS agent_turns(
          workspace TEXT,request TEXT,digest TEXT,state TEXT,result TEXT,created REAL,
          PRIMARY KEY(workspace,request));
        CREATE TABLE IF NOT EXISTS agent_memories(
          workspace TEXT,id TEXT,persona TEXT,content TEXT,source TEXT,created REAL,active INTEGER,
          PRIMARY KEY(workspace,id));
        CREATE TABLE IF NOT EXISTS agent_note_versions(
          workspace TEXT,name TEXT,version TEXT,content TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS agent_diaries(
          workspace TEXT,day TEXT,content TEXT,updated REAL,PRIMARY KEY(workspace,day));
        CREATE TABLE IF NOT EXISTS agent_memory_meta(
          workspace TEXT,id TEXT,category TEXT,importance REAL,expires REAL,evidence TEXT,
          PRIMARY KEY(workspace,id));
        ''')
        self.db.commit()

    def identity(self):
        actor = authority(self.bot).local_actor()
        key = hashlib.sha256(json.dumps([actor['admin'], actor['revision']], sort_keys=True).encode()).hexdigest()[:24]
        return key, actor

    @staticmethod
    def _safe(path):
        # Windows junction不一定表现为symlink，因此同时检查reparse属性。
        if path.exists() or path.is_symlink():
            row = path.lstat()
            if path.is_symlink() or getattr(row, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
                raise WorkspaceError('工作区不能经过符号链接或目录联接')

    def folder(self):
        key, _ = self.identity()
        root = Path(self.bot.root).absolute()
        for ancestor in [*reversed(root.parents), root]:
            self._safe(ancestor)
        path = root
        for part in ('data', 'agent_workspaces', key, 'notes'):
            path = path / part
            self._safe(path)
            path.mkdir(exist_ok=True)
        return path

    def info(self):
        key, actor = self.identity()
        folder = self.folder()
        return {'workspace': key, 'path': str(folder.parent), 'notes_path': str(folder),
                'revision': actor['revision'], 'admin': actor['admin'],
                'file_limit_bytes': 65536, 'total_limit_bytes': 4194304,
                'permissions': ['notes.read', 'notes.write', 'memory.read', 'memory.write'],
                'desktop_execution': 'external_agent_only', 'wechat_connected_by_this_call': False}

    def file(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[\w\-\u4e00-\u9fff][\w.\-\u4e00-\u9fff ]{0,78}\.(?:txt|md)', name, re.I):
            raise WorkspaceError('笔记名只能使用文字、数字、空格和横线，以.txt或.md结尾')
        if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *('COM'+str(i) for i in range(10)), *('LPT'+str(i) for i in range(10))}:
            raise WorkspaceError('不能使用系统保留的文件名')
        path = self.folder() / name
        self._safe(path)
        if path.exists() and not path.is_file():
            raise WorkspaceError('笔记目标不是普通文件')
        return path

    def files(self):
        result = []
        for path in sorted(self.folder().iterdir()):
            if path.suffix.lower() not in {'.md', '.txt'}:
                continue
            self._safe(path)
            if path.is_file():
                result.append({'name': path.name, 'bytes': path.stat().st_size})
        return result

    def read(self, name):
        path = self.file(name)
        if not path.exists():
            return {'name': name, 'content': '', 'version': '', 'exists': False}
        if path.stat().st_size > 65536:
            raise WorkspaceError('笔记超过64KB，请先在本机整理')
        try:
            raw = path.read_bytes()
            content = raw.decode('utf-8-sig')
        except (OSError, UnicodeError):
            raise WorkspaceError('笔记必须是可读取的UTF-8文本') from None
        return {'name': name, 'content': content, 'version': hashlib.sha256(raw).hexdigest(), 'exists': True}

    def write(self, name, content, version):
        if not isinstance(content, str) or len(content.encode('utf-8')) > 65536 or '\x00' in content:
            raise WorkspaceError('笔记需要不超过64KB的文本')
        if not isinstance(version, str):
            raise WorkspaceError('写入需要读取时的version，新文件使用空字符串')
        current = self.read(name)
        if current['version'] != version:
            raise WorkspaceError('笔记已被其他Agent修改，请重新读取后再编辑')
        path = self.file(name)
        total = sum(row['bytes'] for row in self.files()) - (path.stat().st_size if path.exists() else 0)
        if total + len(content.encode('utf-8')) > 4194304:
            raise WorkspaceError('工作区笔记总量超过4MB，请先整理')
        key, _ = self.identity()
        if current['exists']:
            self.db.execute('INSERT INTO agent_note_versions VALUES(?,?,?,?,?)',
                            (key, name, version, current['content'], time.time()))
            self.db.execute('DELETE FROM agent_note_versions WHERE workspace=? AND name=? AND rowid NOT IN '
                '(SELECT rowid FROM agent_note_versions WHERE workspace=? AND name=? ORDER BY rowid DESC LIMIT 5)',
                (key,name,key,name))
            self.db.commit()
        atomic_text(path, content, backup=False)
        return self.read(name)

    def memories(self, persona='', query=''):
        key, _ = self.identity()
        rows = self.db.execute('SELECT m.id,m.persona,m.content,m.source,m.created FROM agent_memories m '
            'LEFT JOIN agent_memory_meta x ON x.workspace=m.workspace AND x.id=m.id '
            'WHERE m.workspace=? AND m.active=1 AND (?=\'*\' OR m.persona=\'\' OR m.persona=?) '
            'AND (coalesce(x.expires,0)=0 OR x.expires>?) ORDER BY m.created DESC LIMIT 1000',
            (key,persona,persona,time.time())).fetchall()
        out = [dict(zip(('id','persona','content','source','created'), row)) for row in rows]
        metadata = {r[0]:r[1:] for r in self.db.execute('SELECT id,category,importance,expires,evidence FROM agent_memory_meta WHERE workspace=?',(key,))}
        for row in out:
            meta = metadata.get(row['id'], ('fact',1.,0.,''))
            row.update(category=meta[0],importance=meta[1],expires=meta[2],evidence=meta[3])
        # 遗忘只停止引用过期学习条目；原文与人工明确记忆不自动删除。
        out = [r for r in out if not r['expires'] or r['expires'] > time.time()]
        if query:
            from .agent_context import relevance
            out = sorted((r for r in out if relevance(query,r['content'])),
                         key=lambda r:(-relevance(query,r['content']),-r['importance'],-r['created']))
        return out

    def remember(self, content, persona='', source='explicit'):
        if not isinstance(content, str) or not content.strip() or len(content) > 1000:
            raise WorkspaceError('记忆内容需要1～1000字')
        key, _ = self.identity()
        existing = self.db.execute('SELECT id FROM agent_memories WHERE workspace=? AND persona=? AND content=? AND active=1',
                                   (key,persona,content.strip())).fetchone()
        if existing:
            if source != 'learned:explicit-user':
                # 人工重新确认会解除自动过期；后续学习不能缩短人工记忆寿命。
                self.db.execute('UPDATE agent_memories SET source=? WHERE workspace=? AND id=?', (source,key,existing[0]))
                self.db.execute('DELETE FROM agent_memory_meta WHERE workspace=? AND id=?', (key,existing[0]))
                self.db.commit()
            return {'id':existing[0], 'content':content.strip(), 'persona':persona}
        if len(self.memories('*')) >= 1000:
            raise WorkspaceError('有效记忆已达1000条，请先忘记不再需要的内容')
        mid = uuid.uuid4().hex[:16]
        self.db.execute('INSERT INTO agent_memories VALUES(?,?,?,?,?,?,1)',
                        (key,mid,persona,content.strip(),source,time.time()))
        self.db.commit()
        return {'id':mid, 'content':content.strip(), 'persona':persona}

    def learn(self, text, persona, evidence):
        """只从主人本轮直接陈述提取明确偏好；不学模型生成内容或引用中的命令。"""
        if '\n' in text or len(text)>200 or text.startswith(('/', '>', '“', '"')):
            return []
        match = re.fullmatch(r'(我(?:不喜欢|喜欢|叫|希望)|以后(?:回复|回答))(\S.{1,150})[。！!]?\s*',text.strip())
        if not match: return []
        content = text.strip()
        if re.search(r'密码|密钥|token|api.?key|忽略.*规则|管理员|绑定|执行|运行命令',content,re.I): return []
        key,_ = self.identity()
        # 忘记后不能通过同一句自动学习悄悄复活。
        if self.db.execute('SELECT 1 FROM agent_memories WHERE workspace=? AND persona=? AND content=? AND active=0',(key,persona,content)).fetchone(): return []
        manual = self.db.execute('SELECT id FROM agent_memories WHERE workspace=? AND persona=? AND content=? '
            'AND active=1 AND source!=?', (key,persona,content,'learned:explicit-user')).fetchone()
        if manual: return []
        row = self.remember(content,persona,'learned:explicit-user')
        category = 'user' if match[1].startswith('我') else 'lesson'
        self.db.execute('INSERT OR REPLACE INTO agent_memory_meta VALUES(?,?,?,?,?,?)',
                        (key,row['id'],category,.8,time.time()+90*86400,evidence))
        self.db.commit()
        return [row]

    def forget(self, mid):
        key, _ = self.identity()
        changed = self.db.execute('UPDATE agent_memories SET active=0 WHERE workspace=? AND id=? AND active=1', (key,mid))
        self.db.commit()
        return {'forgotten': changed.rowcount == 1}

    def history(self, session, persona):
        key, _ = self.identity()
        rows = self.db.execute('SELECT role,content,created,persona,request FROM agent_dialogue WHERE workspace=? '
            'AND session=? AND (?=\'*\' OR persona=?) ORDER BY rowid DESC LIMIT 1000', (key,session,persona,persona)).fetchall()
        return [dict(zip(('role','content','created','persona','request'), row)) for row in reversed(rows)]

    def diary(self):
        key, _ = self.identity()
        now = datetime.now(timezone(timedelta(hours=8)))
        day = now.strftime('%Y-%m-%d')
        start = now.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        count = self.db.execute('SELECT count(*) FROM agent_dialogue WHERE workspace=? AND role=\'user\' AND created>=?', (key,start)).fetchone()[0]
        rows = self.db.execute('SELECT content FROM agent_memories WHERE workspace=? AND active=1 '
                               'AND created>=? ORDER BY created LIMIT 20', (key,start)).fetchall()
        text = f'# {day} 本地日记\n\n今天在Agent工作台记录了 {count} 次对话。\n'
        text += '\n明确记住的内容（原文记录，不代表已核实）：\n' + '\n'.join('- '+r[0] for r in rows) if rows else '\n今天没有新增明确记忆。'
        self.db.execute('INSERT OR REPLACE INTO agent_diaries VALUES(?,?,?,?)', (key,day,text,time.time()))
        self.db.commit()
        return {'day':day, 'content':text}


def workspace(bot):
    if getattr(bot, 'agent_workspace', None) is None:
        bot.agent_workspace = AgentWorkspace(bot)
    return bot.agent_workspace


class WorkspaceAccess:
    """给扩展传最小接口；运行中换绑后旧工具也不能写进新主人的工作区。"""
    def __init__(self, area, grants, check_access=None):
        self._area, self._grants = area, set(grants)
        self._key = area.identity()[0]
        self._check_access = check_access

    def _check(self, write=False):
        if self._check_access:
            self._check_access()
        if self._area.identity()[0] != self._key:
            raise WorkspaceError('主人绑定已变化，旧工具权限失效')
        if ('write_memory' if write else 'read_memory') not in self._grants:
            raise WorkspaceError('该工具没有工作区'+('写入' if write else '读取')+'权限')

    def files(self): self._check(); return self._area.files()
    def read(self, name): self._check(); return self._area.read(name)
    def write(self, name, content, version): self._check(True); return self._area.write(name,content,version)
    def memories(self, persona='', query=''): self._check(); return self._area.memories(persona,query)
    def remember(self, content, persona=''): self._check(True); return self._area.remember(content,persona,'plugin')
    def forget(self, mid): self._check(True); return self._area.forget(mid)
