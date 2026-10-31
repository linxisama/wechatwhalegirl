"""Agent 单一管理员与跨端凭据；显示名不能充当跨平台身份。"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from pathlib import Path

from loguru import logger

from .persistence import atomic_text

CAPABILITIES = frozenset({'read_console', 'manage_console', 'read_memory',
                          'write_memory', 'manage_extensions'})


class AuthorityError(ValueError):
    pass


class AgentAuthority:
    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        if not self.path.exists():
            return {'revision': 0, 'admin': None, 'credentials': []}
        try:
            if self.path.stat().st_size > 256 * 1024:
                raise ValueError()
            data = json.loads(self.path.read_text('utf-8'))
            if (not isinstance(data, dict) or type(data.get('revision')) is not int
                    or data['revision'] < 0 or 'admin' not in data
                    or not isinstance(data.get('credentials'), list) or len(data['credentials']) > 20):
                raise ValueError()
            if data.get('admin') is not None:
                self._identity(data['admin'])
            for row in data['credentials']:
                if (not isinstance(row, dict) or not isinstance(row.get('digest'), str)
                        or not re.fullmatch(r'[a-f0-9]{64}', row['digest'])
                        or not isinstance(row.get('id'), str) or not isinstance(row.get('peer'), str)
                        or type(row.get('expires')) not in (int, float) or not math.isfinite(row['expires'])
                        or not isinstance(row.get('capabilities'), list)
                        or any(not isinstance(c, str) or c not in CAPABILITIES for c in row['capabilities'])
                        # 生命周期字段允许旧记录缺失；一旦存在就必须是数字，避免界面渲染出虚假时间。
                        or any(type(row[key]) not in (int, float) or not math.isfinite(row[key])
                               for key in ('issued', 'last_used') if key in row)):
                    raise ValueError()
            return data
        except (OSError, ValueError, TypeError, RecursionError):
            raise AuthorityError('Agent管理员记录损坏，请在本机绑定页修复；未启用控制权限') from None

    @staticmethod
    def _identity(raw):
        if not isinstance(raw, dict) or set(raw) != {'platform', 'account', 'subject'}:
            raise AuthorityError('管理员需要平台、机器人账号和稳定用户标识')
        platform, account, subject = (raw[k] for k in ('platform', 'account', 'subject'))
        if platform not in ('wechat', 'qq', 'desktop'):
            raise AuthorityError('管理员平台须为wechat、qq或desktop')
        if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.@:-]{1,100}', v)
               for v in (account, subject)):
            raise AuthorityError('账号和管理员须为稳定标识，不能填昵称或路径')
        if platform == 'qq' and any(not re.fullmatch(r'\d{5,15}', v) for v in (account, subject)):
            raise AuthorityError('QQ账号与管理员QQ须为5～15位数字')
        return dict(platform=platform, account=account, subject=subject)

    def _save(self, data):
        atomic_text(self.path, json.dumps(data, ensure_ascii=False, indent=2), backup=False)

    def status(self):
        try:
            data = self._load()
        except AuthorityError as exc:
            return {'bound': False, 'invalid': True, 'message': str(exc)}
        return {'bound': bool(data['admin']), 'invalid': False, 'revision': data['revision'],
                'admin': copy.deepcopy(data['admin']),
                'credentials': [{k: row[k] for k in ('id', 'peer', 'capabilities', 'expires',
                                                    'issued', 'last_used') if k in row}
                                for row in data['credentials']]}

    def bind(self, identity, revision=None):
        admin = self._identity(identity)
        data = self._load()
        if revision is not None and revision != data['revision']:
            raise AuthorityError('管理员绑定已变化，请刷新后重试')
        # 换绑必定换代：旧Agent会话/bridge凭据不能继续借用新管理员身份。
        self._save({'revision': data['revision'] + 1, 'admin': admin, 'credentials': []})
        return self.status()

    def unbind(self, revision=None):
        data = self._load()
        if revision is not None and revision != data['revision']:
            raise AuthorityError('管理员绑定已变化，请刷新后重试')
        self._save({'revision': data['revision'] + 1, 'admin': None, 'credentials': []})
        return self.status()

    def repair(self):
        if not self.status().get('invalid'):
            raise AuthorityError('绑定记录正常，不需要重置')
        # 只在本机显式修复时另存损坏记录，不将损坏自动当作免授权。
        preserved = self.path.with_name(self.path.stem + '.corrupt-' + secrets.token_hex(6) + '.json')
        self.path.replace(preserved)
        try:
            self._save({'revision': 0, 'admin': None, 'credentials': []})
        except OSError:
            preserved.replace(self.path)
            raise
        return self.status()

    def local_actor(self):
        data = self._load()
        if not data['admin']:
            raise AuthorityError('请先在Agent页绑定一个管理员，再操作控制台')
        return {'admin': copy.deepcopy(data['admin']), 'revision': data['revision'],
                'peer': 'local_console', 'capabilities': sorted(CAPABILITIES)}

    def issue(self, peer, capabilities, days=30):
        data = self._load()
        self.local_actor()
        if not isinstance(peer, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{1,40}', peer):
            raise AuthorityError('接入端名称格式无效')
        if (not isinstance(capabilities, list) or not capabilities
                or any(not isinstance(c, str) or c not in CAPABILITIES for c in capabilities)):
            raise AuthorityError('请选择已知能力范围')
        if type(days) is not int or not 1 <= days <= 90:
            raise AuthorityError('凭据有效期须为1～90天')
        if len(data['credentials']) >= 20:
            raise AuthorityError('最多20个接入凭据，请先撤销旧凭据')
        token = secrets.token_urlsafe(32)
        row = {'id': secrets.token_hex(8), 'peer': peer,
               'capabilities': sorted(set(capabilities)), 'expires': time.time() + days * 86400,
               'issued': int(time.time()),
               'digest': hashlib.sha256(token.encode()).hexdigest()}
        data['credentials'].append(row)
        self._save(data)
        return {k: v for k, v in row.items() if k != 'digest'} | {'token': token}

    def revoke(self, credential_id):
        data = self._load()
        self.local_actor()
        data['credentials'] = [row for row in data['credentials'] if row.get('id') != credential_id]
        self._save(data)

    def authenticate(self, token):
        data = self._load()
        if not data['admin'] or not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise AuthorityError('bridge凭据无效或管理员未绑定')
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = next((row for row in data['credentials'] if hmac.compare_digest(str(row.get('digest', '')), digest)), None)
        if not row or row.get('expires', 0) <= time.time():
            raise AuthorityError('bridge凭据无效、已撤销或已过期')
        self._touch(data, row)
        return {'admin': copy.deepcopy(data['admin']), 'revision': data['revision'],
                'peer': row['peer'], 'credential_id': row['id'],
                'capabilities': list(row['capabilities'])}

    def _touch(self, data, row):
        """记下最近使用时间；60秒内不重复写盘，写盘失败只记日志、绝不改变鉴权结果。"""
        now = int(time.time())
        if now - int(row.get('last_used') or 0) < 60:
            return
        row['last_used'] = now
        try:
            self._save(data)
        except OSError:
            logger.warning('凭据使用时间写入失败，本次鉴权结果不受影响')

    def validate_actor(self, actor):
        """异步等待后重验身份快照，撤销无需等下一条HTTP请求才生效。"""
        data = self._load()
        if (not data['admin'] or actor.get('revision') != data['revision']
                or actor.get('admin') != data['admin']):
            raise AuthorityError('管理员已换绑或解除，当前操作权限失效')
        cid = actor.get('credential_id')
        if cid:
            row = next((r for r in data['credentials'] if r['id'] == cid), None)
            if (not row or row['expires'] <= time.time() or row['peer'] != actor.get('peer')
                    or not set(actor.get('capabilities', [])) <= set(row['capabilities'])):
                raise AuthorityError('bridge凭据已撤销、到期或权限变化，本次操作已停止')
        elif actor.get('peer') != 'local_console':
            raise AuthorityError('没有可验证的Agent接入身份')
        return actor


def authority(bot):
    return AgentAuthority(bot.root / 'data' / 'agent_authority.json')
