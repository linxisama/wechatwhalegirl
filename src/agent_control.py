"""结构化控制台能力：复用原接口，不允许模型自行拼URL或调用发送通道。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import quote

import httpx

from .agent_authority import AuthorityError, authority


class ControlError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# 名称是长期接口，路径沿用控制台。未登记的接口永远不能通过此处调用。
_READS = {
    'workspace.info': '/api/agent/workspace', 'workspace.files': '/api/agent/workspace/files',
    'workspace.read': '/api/agent/workspace/note', 'workspace.memories': '/api/agent/workspace/memories',
    'workspace.tools': '/api/agent/workspace/tools', 'agent.history': '/api/agent/history',
    'workspace.diary.read': '/api/agent/workspace/diary',
    'console.status': '/api/status', 'console.settings': '/api/settings',
    'console.models': '/api/models', 'plugins.list': '/api/plugins',
    'plugins.health': '/api/plugins/health', 'personas.list': '/api/personas',
    'personas.read': '/api/personas/{name}', 'personas.export': '/api/persona-files/{name}/export',
    'dialogue.overview': '/api/dialogue', 'dialogue.rules': '/api/dialogue/rules',
    'archive.search': '/api/dialogue/archive', 'archive.export': '/api/dialogue/archive/export',
    'learning.search': '/api/dialogue/learning', 'learning.overview': '/api/dialogue/learning/overview',
    'skills.list': '/api/agent/skills', 'schedules.list': '/api/agent/schedules',
    'stickers.list': '/api/stickers', 'wechat.accounts': '/api/wechat/accounts',
    'stickers.jobs': '/api/stickers/annotation-jobs',
    'wechat.groups': '/api/wechat/groups', 'wechat.sessions': '/api/wechat/sessions',
    'weflow.config': '/api/weflow/config',
    'memory.guard': '/api/memory/guard', 'usage.report': '/api/usage/summary',
}
_WRITES = {
    'workspace.write': '/api/agent/workspace/note', 'workspace.remember': '/api/agent/workspace/memories',
    'workspace.forget': '/api/agent/workspace/forget', 'workspace.diary': '/api/agent/workspace/diary',
    'agent.chat': '/api/agent/chat',
    'agent.context.configure': '/api/agent/workspace/context',
    'console.settings.patch': '/api/settings', 'console.start': '/api/start',
    'console.stop': '/api/stop', 'console.test_model': '/api/test-ai',
    'console.test_wechat': '/api/test-wechat',
    'dsh.announce': '/api/dsh/announce', 'plugins.configure': '/api/plugins/{plugin_id}',
    'plugins.retry': '/api/plugins/retry-failed', 'plugins.inspect': '/api/plugins/inspect',
    'plugins.install': '/api/plugins/install', 'plugins.batch_inspect': '/api/plugins/batch-inspect',
    'plugins.batch_install': '/api/plugins/batch-install',
    'plugins.restore': '/api/plugins/{plugin_id}/restore',
    'personas.save': '/api/personas', 'personas.activate': '/api/personas/{name}/activate',
    'personas.import': '/api/persona-files/import', 'personas.analyze': '/api/persona-files/{name}/analyze',
    'dialogue.configure': '/api/dialogue/rules', 'memory.maintain': '/api/dialogue/memory/maintain',
    'memory.notes': '/api/dialogue/notes', 'memory.facts': '/api/dialogue/facts',
    'archive.import': '/api/dialogue/archive/import', 'archive.delete': '/api/dialogue/archive/delete',
    'learning.review': '/api/dialogue/learning', 'learning.bulk': '/api/dialogue/learning/bulk',
    'skills.preview': '/api/agent/skills/preview', 'skills.import': '/api/agent/skills/import',
    'skills.toggle': '/api/agent/skills/toggle', 'schedules.create': '/api/agent/schedules',
    'schedules.run': '/api/agent/schedules/run', 'schedules.toggle': '/api/agent/schedules/toggle',
    'stickers.import': '/api/stickers/import', 'stickers.configure': '/api/stickers/{key}',
    'stickers.recognize': '/api/stickers/{key}/recognize',
    'stickers.annotate': '/api/stickers/annotation-jobs',
    'stickers.cancel_annotations': '/api/stickers/annotation-jobs/cancel',
    'weflow.configure': '/api/weflow/config', 'weflow.probe': '/api/weflow/probe',
    'weflow.sessions': '/api/weflow/sessions', 'weflow.messages': '/api/weflow/messages',
    'weflow.import': '/api/weflow/import-history',
}
_DELETES = {'plugins.remove': '/api/plugins/{plugin_id}', 'personas.remove': '/api/personas/{name}',
            'skills.remove': '/api/agent/skills/{skill_id}', 'schedules.remove': '/api/agent/schedules/{task_id}',
            'stickers.remove': '/api/stickers/{key}'}
CATALOG = {name: {'action': name, 'method': method, 'path': path}
           for method, values in [('GET', _READS), ('POST', _WRITES), ('DELETE', _DELETES)]
           for name, path in values.items()}


def capability(spec):
    name, method = spec['action'], spec['method']
    if name.startswith('workspace.') or name == 'agent.history':
        return 'read_memory' if method == 'GET' else 'write_memory'
    if name == 'weflow.messages':
        return 'read_memory'
    if name == 'weflow.import':
        return 'write_memory'
    if name.startswith(('archive.', 'dialogue.', 'learning.', 'memory.')):
        return 'read_memory' if method == 'GET' else 'write_memory'
    if method != 'GET' and name.startswith(('plugins.install', 'plugins.batch_install',
                                            'plugins.remove', 'plugins.restore', 'skills.import', 'skills.remove')):
        return 'manage_extensions'
    return 'read_console' if method == 'GET' else 'manage_console'


def redacted(value):
    if isinstance(value, dict):
        return {k: ('[已隐藏]' if re.search(r'password|secret|api.?key|authorization|token|credential', k, re.I)
                    and not k.endswith(('_set', '_tokens')) else redacted(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v) for v in value]
    return value


class ConsoleControl:
    def __init__(self, app, bot):
        self.app, self.bot = app, bot
        self._serial = asyncio.Lock()
        bot.db.execute('''CREATE TABLE IF NOT EXISTS agent_operations(
            request_id TEXT PRIMARY KEY, actor TEXT, revision INTEGER, action TEXT,
            request_hash TEXT, state TEXT, started REAL, finished REAL, result TEXT)''')
        bot.db.commit()

    def catalog(self, actor=None):
        grants = set((actor or {}).get('capabilities', []))
        return [dict(spec, capability=capability(spec), available=capability(spec) in grants,
                     parameters=re.findall(r'{(\w+)}', spec['path'])) for spec in CATALOG.values()]

    @staticmethod
    def _request(raw):
        if not isinstance(raw, dict) or set(raw) - {'action', 'request_id', 'params', 'query', 'body'}:
            raise ControlError('操作字段无效')
        action, request_id = raw.get('action'), raw.get('request_id', '')
        if not isinstance(action, str) or action not in CATALOG:
            raise ControlError('未知控制台能力；请先读取能力目录')
        spec = CATALOG[action]
        if spec['method'] != 'GET' and (not isinstance(request_id, str)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,100}', request_id)):
            raise ControlError('写操作需要8～100位稳定request_id，重试时须使用同一标识')
        params, query, body = (raw.get(k, {}) for k in ('params', 'query', 'body'))
        if any(not isinstance(v, dict) for v in (params, query, body)):
            raise ControlError('params、query、body必须为对象')
        slots = set(re.findall(r'{(\w+)}', spec['path']))
        if set(params) != slots or any(not isinstance(v, str) or not v or len(v) > 100
                or any(ch in v for ch in '/\\?#\x00') or v in {'.', '..'} for v in params.values()):
            raise ControlError('操作目标标识无效')
        if len(query) > 20 or any(not isinstance(k, str) or len(k) > 80 or
                type(v) not in (str, int, bool, float) or len(str(v)) > 2000 for k, v in query.items()):
            raise ControlError('查询参数无效或过长')
        try:
            encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (ValueError, TypeError, RecursionError):
            raise ControlError('操作数据必须为有效JSON') from None
        if len(encoded.encode('utf-8')) > 512 * 1024:
            raise ControlError('单次操作数据超过512KB')
        path = spec['path'].format(**{k: quote(v, safe='') for k, v in params.items()})
        return spec, request_id, path, query, body, hashlib.sha256(encoded.encode()).hexdigest()

    def _guards(self, spec, params, body):
        if spec['action'] == 'plugins.configure':
            pid = params['plugin_id']
            if pid in {'robot_admin', 'group_ambient'}:
                if pid != 'robot_admin' or set(body) - {'paused'} or type(body.get('paused')) is not bool:
                    raise ControlError('管理员授权及主动外发不通过Agent修改', 403)
            if pid == 'chat_controls' and 'replyProbability' in body and body['replyProbability'] != 0:
                raise ControlError('未点名插话概率请在本机专用页面明确设置', 403)
        if spec['action'] == 'console.settings.patch':
            from .wechat_app import Settings
            if set(body) - set(Settings.model_fields):
                raise ControlError('未知设置字段')
            if (body.get('proactive_enabled') or body.get('send_compat') or body.get('auto_open_chat')
                    or body.get('hot_activate_uia')):
                raise ControlError('Agent不能开启主动外发、前台兼容、热激活UIA或自动切会话', 403)
            if (body.get('background_submit') == 'posted_enter' and
                    self.bot.config.get('wechat.background_submit', 'invoke') != 'posted_enter'):
                raise ControlError('实验投递通道须在本机设置页显式选择，Agent不能代为开启', 403)
            strict = self.bot.config.get('wechat.background_send', False)
            if strict and (body.get('background_send') is False or body.get('send_mode') in {'foreground', 'legacy'}):
                raise ControlError('Agent不能放宽当前严格后台发送限制', 403)

    async def run(self, raw, actor):
        spec, request_id, path, query, body, digest = self._request(raw)
        if capability(spec) not in actor.get('capabilities', []):
            raise ControlError('当前接入凭据没有此能力', 403)
        check_access = lambda: authority(self.bot).validate_actor(actor)
        check_access()
        self._guards(spec, raw.get('params', {}), body)
        if spec['method'] == 'GET':
            result = await self._invoke(spec, path, query, body)
            check_access()
            return result
        # 先落执行中状态，再调用已有接口；断电或取消后保留未知，不重做写操作。
        actor_key = json.dumps([actor['admin'], actor['revision'], actor.get('credential_id', actor['peer'])], sort_keys=True)
        async with self._serial:
            check_access()
            row = self.bot.db.execute('SELECT actor,request_hash,state,result FROM agent_operations WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row[0] != actor_key or row[1] != digest:
                    raise ControlError('request_id已用于不同操作或身份', 409)
                if row[2] not in {'done', 'failed'}:
                    raise ControlError('该操作仍执行中或结果未知，不会重复执行；请读取当前状态', 409)
                return json.loads(row[3]) | {'replayed': True}
            self.bot.db.execute('INSERT INTO agent_operations VALUES(?,?,?,?,?,?,?,?,?)',
                (request_id, actor_key, actor['revision'], spec['action'], digest, 'running', time.time(), 0, '{}'))
            self.bot.db.commit()
        # 幂等登记锁不能跨网络等待，停止操作不能被一次慢模型测试挡在后面。
        # 配置/插件/启停之间的业务互斥仍由原控制台接口自己的锁处理。
        try:
            check_access()
            if spec['action'] == 'workspace.diary':
                data=await self.bot.diary_writer.generate(grants=actor['capabilities'],check_access=check_access)
                result={'success':data['success'],'status':200,'data':data}
            elif spec['action'] == 'agent.chat':
                from .agent_workspace_api import ChatBody
                from .agent_workbench import chat
                checked = ChatBody(**body)
                data = await chat(self.bot, **checked.model_dump(), source=actor['peer'],
                                  grants=actor['capabilities'], check_access=check_access)
                result = {'success':data['success'],'status':200,'data':data}
            else:
                result = await self._invoke(spec, path, query, body)
            check_access()
        except asyncio.CancelledError:
            raise
        except AuthorityError:
            # 保留running收据；已发生的写入不能因撤销而被下一次重试执行。
            raise
        except Exception as exc:
            result = {'success': False, 'status': 503, 'detail': '控制操作失败：' + type(exc).__name__}
        self.bot.db.execute('UPDATE agent_operations SET state=?,finished=?,result=? WHERE request_id=?',
            ('done' if result['success'] else 'failed', time.time(), json.dumps(result, ensure_ascii=False), request_id))
        self.bot.db.commit()
        return result

    async def _invoke(self, spec, path, query, body):
        if spec['action'] == 'console.settings.patch':
            from .wechat_app import Settings
            base = {k: v for k, v in self.bot.public_settings().items() if k in Settings.model_fields}
            base['send_mode'] = 'background' if base.get('background_send') else 'legacy'
            body = base | body
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1') as client:
            response = await client.request(spec['method'], path, params=query,
                                            json=body if spec['method'] == 'POST' else None)
        try:
            data = response.json()
        except ValueError:
            data = {'detail': '目标接口没有返回JSON'}
        if spec['action'] == 'console.status' and isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in {'messages', 'events', 'recent'}}
        result = {'success': response.is_success and not (isinstance(data, dict) and data.get('success') is False),
                  'status': response.status_code, 'data': redacted(data)}
        return result
