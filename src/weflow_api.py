"""WeFlow 本机只读工具；不调用登录、数据库写入或发送接口。"""
from __future__ import annotations

import copy
import ipaddress
import json
import math
import hashlib
import time
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field


DEFAULT_URL = 'http://127.0.0.1:5031'
MAX_BYTES = 4 * 1024 * 1024


def local_url(value: str) -> str:
    text = str(value or DEFAULT_URL).strip().rstrip('/')
    try:
        parts = urlsplit(text)
        host = parts.hostname or ''
        # 使用 IP 字面量，避免 localhost 被代理或 DNS 配置解析到其他机器。
        address = ipaddress.ip_address(host)
        if (parts.scheme != 'http' or not address.is_loopback or parts.username is not None
                or parts.password is not None or parts.path or parts.query or parts.fragment
                or not 1 <= (parts.port or 80) <= 65535):
            raise ValueError()
    except ValueError:
        raise ValueError('WeFlow 地址须为本机 HTTP 地址，例如 http://127.0.0.1:5031') from None
    return text


class Draft(BaseModel):
    url: str = Field(default=DEFAULT_URL, max_length=300)
    token: str = Field(default='', max_length=4096)
    clear_token: bool = False
    talker: str = Field(default='', max_length=300)


class HistoryDraft(Draft):
    account:str=Field(default='',max_length=256)


async def read_json(url: str, token: str, endpoint: str, params=None) -> dict:
    if endpoint not in {'/health', '/api/v1/sessions', '/api/v1/messages'}:
        raise ValueError('不支持的只读接口')
    base = local_url(url)
    if not token:
        raise ValueError('请填写 WeFlow 设置中的 API Token；健康检查不能代替鉴权验证')
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3), trust_env=False,
                                     follow_redirects=False) as client:
            async with client.stream('GET', base + endpoint,
                                     headers={'Authorization': 'Bearer ' + token}, params=params) as response:
                if response.status_code in (401, 403):
                    raise ValueError('WeFlow 拒绝鉴权，请检查 API Token 及 API 服务开关')
                if response.status_code != 200:
                    raise ValueError(f'WeFlow HTTP {response.status_code}；请检查版本和 API 服务状态')
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError('WeFlow 响应超过 4MB；请缩小查询范围')
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError):
            raise ValueError('WeFlow 返回的内容不是有效 JSON，请确认端口对应 WeFlow') from None
        if not isinstance(payload, dict):
            raise ValueError('WeFlow 响应结构不兼容')
        if payload.get('success') is False:
            # 服务端错误可能包含本地密钥或路径，不把原文透传给界面。
            raise ValueError('WeFlow 无法读取数据，请在 WeFlow 中检查数据库、账号和第三方组件')
        return payload
    except httpx.TimeoutException:
        raise ValueError('WeFlow 本机请求超时，请确认 API 服务和数据库可用') from None
    except httpx.HTTPError:
        raise ValueError('无法连接 WeFlow，请启动其 API 服务并检查本机地址和端口') from None


def _connection(bot, req: Draft):
    url = local_url(req.url)
    token = '' if req.clear_token else req.token.strip() or bot.secret('weflow.token')
    return url, token


def public_config(bot):
    return {'url': str(bot.config.get('weflow.url', DEFAULT_URL) or DEFAULT_URL),
            'account':bot.account(),
            'token_set': bool(bot.secret('weflow.token')), 'mode': 'read_only_tools',
            'message': '只读检查与历史导入；不替换当前微信收发方式。'}


def add_routes(app):
    @app.post('/api/weflow/import-history')
    async def import_history(req:HistoryDraft):
        bot=app.state.bot
        try:
            from .conversation_api import room
            target=req.talker.strip();kind='group' if target.endswith('@chatroom') else 'private'
            room(kind,target)
            if req.account!=bot.account():raise ValueError('账号已改变，请重新读取连接配置')
            owner=str(getattr(bot.bridge,'self_id','') or '')
            if not owner:raise ValueError('请先连接机器人账号以核对 WeFlow 数据所属账号')
            url,token=_connection(bot,req)
            # Pull端点按时间正序取最早批次；此处必须用默认倒序messages端点取最近1000条。
            payload=await read_json(url,token,'/api/v1/messages',{'talker':target,'limit':1000,
                'offset':0,'format':'chatlab','media':0,'end':int(time.time())})
            records=history_records(payload,owner,kind,target)
            async with bot.lock:
                if req.account!=bot.account() or owner!=str(bot.bridge.self_id):raise ValueError('读取期间账号改变，未导入')
                count=0
                with bot.db:
                    for message in records:
                        # 相同实时记录可能使用另一套消息ID，以时间/发送者/正文再去重。
                        duplicate=bot.db.execute('SELECT 1 FROM conversation_log WHERE mode=? AND account=? AND kind=? AND target=? AND sender_id=? AND created=? AND content=? LIMIT 1',
                            (bot.mode,bot.account(),kind,target,message['sender_id'],message['created'],message['text'])).fetchone()
                        if duplicate:continue
                        before=bot.db.total_changes
                        bot.conversations._insert(message,bot.mode,bot.account(),persona=bot.style.active_name)
                        count+=int(bot.db.total_changes>before)
                        bot.learning.observe(message,bot)
            return dict(success=True,imported=count,message=f'已导入 {count} 条文字历史并提取本地学习内容；未加入发送队列',window_limit=1000)
        except ValueError as exc:return dict(success=False,message=str(exc))

    @app.get('/api/weflow/config')
    async def config():
        return {'success': True, **public_config(app.state.bot)}

    @app.post('/api/weflow/config')
    async def save(req: Draft):
        bot = app.state.bot
        try:
            url, token = _connection(bot, req)
            # 与主设置复用同一锁，避免加密 Token 与其他配置同时保存时互相覆盖。
            async with bot.lock:
                old = copy.deepcopy(bot.config.config)
                try:
                    bot.config.set('weflow.url', url)
                    bot.config.set('weflow.token', '')
                    bot.config.set('weflow.token_encrypted', bot.config.encrypt_value(token) if token else '')
                    bot.config.save()
                except Exception:
                    bot.config._config = old
                    raise
            return {'success': True, **public_config(bot)}
        except ValueError as exc:
            return {'success': False, 'message': str(exc)}
        except Exception:
            return {'success': False, 'message': 'WeFlow 配置保存失败，原配置已保留'}

    @app.post('/api/weflow/probe')
    async def probe(req: Draft):
        try:
            url, token = _connection(app.state.bot, req)
            health = await read_json(url, token, '/health')
            if health.get('status') != 'ok':
                raise ValueError('WeFlow 健康检查格式不兼容')
            payload = await read_json(url, token, '/api/v1/sessions', {'limit': 1})
            if not isinstance(payload.get('sessions'), list):
                raise ValueError('WeFlow 会话接口格式不兼容')
            return {'success': True, 'read_state': 'available', 'login_state': 'unknown',
                    'send_state': 'not_provided',
                    'message': 'WeFlow 鉴权与会话查询成功；这不代表微信已登录或具备发送能力。'}
        except ValueError as exc:
            return {'success': False, 'message': str(exc)}

    @app.post('/api/weflow/sessions')
    async def sessions(req: Draft):
        try:
            url, token = _connection(app.state.bot, req)
            payload = await read_json(url, token, '/api/v1/sessions', {'limit': 100})
            if not isinstance(payload.get('sessions'), list):
                raise ValueError('WeFlow 会话接口格式不兼容')
            rows = []
            for row in payload['sessions'][:100]:
                if not isinstance(row, dict) or not isinstance(row.get('username'), str):
                    continue
                rows.append({key: row.get(key) for key in
                             ('username', 'displayName', 'type', 'lastTimestamp', 'unreadCount')})
            return {'success': True, 'sessions': rows, 'limit': 100}
        except ValueError as exc:
            return {'success': False, 'message': str(exc), 'sessions': []}

    @app.post('/api/weflow/messages')
    async def messages(req: Draft):
        try:
            if not req.talker.strip():
                raise ValueError('请先选择一个会话')
            url, token = _connection(app.state.bot, req)
            payload = await read_json(url, token, '/api/v1/messages',
                                      {'talker': req.talker.strip(), 'limit': 100, 'media': 0})
            if not isinstance(payload.get('messages'), list):
                raise ValueError('WeFlow 消息接口格式不兼容')
            rows = []
            for row in payload['messages'][:100]:
                if not isinstance(row, dict):
                    continue
                # API 默认只返回文字元数据；不携带媒体密钥、原始 XML 和本地文件路径。
                rows.append({key: row.get(key) for key in ('localId', 'serverId', 'createTime',
                             'localType', 'isSend', 'senderUsername', 'content', 'parsedContent')})
            return {'success': True, 'format': 'weflow-preview/1', 'talker': req.talker.strip(),
                    'messages': rows, 'hasMore': bool(payload.get('hasMore')), 'limit': 100}
        except ValueError as exc:
            return {'success': False, 'message': str(exc), 'messages': []}


def history_records(payload,owner,kind,target):
    """只接受具有来源账号证明的ChatLab记录；原生预览缺少owner不能静默归入当前账号。"""
    meta=payload.get('meta')
    if not isinstance(meta,dict) or meta.get('ownerId')!=owner:
        raise ValueError('WeFlow 未提供匹配的 ownerId，不能确认数据属于当前机器人账号')
    if meta.get('type')!=kind or kind=='group' and meta.get('groupId')!=target:
        raise ValueError('WeFlow 返回的会话不匹配，未导入')
    if any(meta.get(key) not in (None,'',target) for key in ('sessionId','talker')):
        raise ValueError('WeFlow 返回的会话标识不匹配，未导入')
    messages=payload.get('messages')
    if not isinstance(messages,list) or len(messages)>1000:raise ValueError('WeFlow 历史格式不兼容或超过1000条')
    result=[]
    for row in messages:
        if not isinstance(row,dict):raise ValueError('WeFlow 消息记录格式无效')
        if isinstance(row.get('type'),bool):raise ValueError('WeFlow 消息类型无效')
        if row.get('type')!=0:continue
        stamp=row.get('timestamp');content=row.get('content');sender=row.get('sender')
        if (not isinstance(stamp,(int,float)) or isinstance(stamp,bool) or not math.isfinite(stamp) or not 0<stamp<=time.time()+86400
                or not isinstance(content,str) or len(content)>65536 or not isinstance(sender,str) or not 1<=len(sender)<=256):
            raise ValueError('WeFlow 消息时间、内容或发送者无效，整批未导入')
        mid=str(row.get('platformMessageId') or hashlib.sha256(json.dumps([sender,stamp,content],ensure_ascii=False).encode()).hexdigest())
        if len(mid)>256:raise ValueError('WeFlow 消息ID超长')
        result.append(dict(id='weflow:'+mid,kind=kind,target=target,sender_id=sender,
            sender_name=str(row.get('accountName') or sender)[:80],text=content,created=float(stamp),_history_self=sender==owner))
    return sorted(result,key=lambda message:(message['created'],message['id']))
