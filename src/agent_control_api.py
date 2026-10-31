"""Agent控制面与鉴权握手；管理绑定接口不进入工具目录。"""
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .agent_authority import AuthorityError, authority
from .agent_control import ConsoleControl, ControlError
from .runtime_identity import APP_ID, APP_VERSION


class BindBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    platform: str
    account: str
    subject: str
    revision: int | None = None


class CredentialBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    peer: str = Field(min_length=2, max_length=41)
    capabilities: list[str] = Field(default_factory=lambda: ['read_console', 'read_memory'])
    days: int = Field(default=30, ge=1, le=90, strict=True)


def require_bound(bot):
    try:
        return authority(bot).local_actor()
    except AuthorityError as exc:
        raise HTTPException(403, str(exc)) from None


def add_routes(app):
    def control():
        bot = app.state.bot
        if getattr(bot, 'console_control', None) is None:
            bot.console_control = ConsoleControl(app, bot)
        return bot.console_control

    def actor(request, bridge=False):
        header = request.headers.get('authorization', '')
        if bridge or header:
            if not header.startswith('Bearer '):
                raise HTTPException(401, 'bridge需要Bearer凭据')
            try:
                return authority(app.state.bot).authenticate(header[7:])
            except AuthorityError as exc:
                raise HTTPException(401, str(exc)) from None
        return require_bound(app.state.bot)

    @app.get('/api/agent/authority')
    async def binding_status():
        bot = app.state.bot
        admin = bot.plugins._plugins.get('robot_admin')
        account = bot.management_account()
        owners = admin.owners(account) if admin else []
        return {'success': True, **authority(bot).status(), 'known_admins': [
            {'platform': 'wechat', 'account': account, 'subject': subject} for subject in owners]}

    @app.post('/api/agent/authority')
    async def bind(body: BindBody):
        try:
            result = authority(app.state.bot).bind(body.model_dump(exclude={'revision'}), body.revision)
        except AuthorityError as exc:
            raise HTTPException(400, str(exc)) from None
        return {'success': True, **result}

    @app.delete('/api/agent/authority')
    async def unbind(revision: int):
        try:
            result = authority(app.state.bot).unbind(revision)
        except AuthorityError as exc:
            raise HTTPException(409, str(exc)) from None
        return {'success': True, **result}

    @app.post('/api/agent/authority/repair')
    async def repair():
        try:
            return {'success': True, **authority(app.state.bot).repair()}
        except AuthorityError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/agent/credentials')
    async def issue(body: CredentialBody):
        try:
            result = authority(app.state.bot).issue(body.peer, body.capabilities, body.days)
        except AuthorityError as exc:
            raise HTTPException(400, str(exc)) from None
        return {'success': True, 'credential': result,
                'message': '凭据只显示本次，请保存在接入端；可随时撤销'}

    @app.delete('/api/agent/credentials/{credential_id}')
    async def revoke(credential_id: str):
        try:
            authority(app.state.bot).revoke(credential_id)
        except AuthorityError as exc:
            raise HTTPException(403, str(exc)) from None
        return {'success': True}

    @app.get('/api/agent/control/catalog')
    async def catalog(request: Request):
        status = authority(app.state.bot).status()
        who = actor(request) if status.get('bound') else None
        return {'success': True, 'bound': bool(who), 'operations': control().catalog(who)}

    @app.get('/api/agent/bridge/handshake')
    async def handshake(request: Request):
        who = actor(request, bridge=True)
        bot = app.state.bot
        return {'success': True, 'protocol': 'whale-agent-bridge/1', 'app': APP_ID, 'version': APP_VERSION,
                'source': {'platform': 'wechat', 'account': bot.management_account(),
                           'persona': bot.style.active_name},
                'principal': who['admin'], 'revision': who['revision'], 'peer': who['peer'],
                'capabilities': who['capabilities'], 'operations': control().catalog(who)}

    async def execute(request, bridge):
        who = actor(request, bridge=bridge)
        raw = await request.body()
        if len(raw) > 512 * 1024:
            raise HTTPException(413, '单次控制操作超过512KB')
        import json
        try:
            result = await control().run(json.loads(raw), who)
        except (ControlError, AuthorityError) as exc:
            raise HTTPException(getattr(exc, 'status', 403), str(exc)) from None
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, '控制操作JSON格式无效') from None
        return result

    @app.post('/api/agent/control/execute')
    async def local_execute(request: Request):
        return await execute(request, False)

    @app.post('/api/agent/bridge/execute')
    async def bridge_execute(request: Request):
        return await execute(request, True)

    @app.get('/api/agent/control/audit')
    async def audit(request: Request, limit: int = 30):
        who = actor(request)
        if 'read_console' not in who['capabilities']:
            raise HTTPException(403, '没有读取控制记录的能力')
        service = control()
        rows = service.bot.db.execute('SELECT request_id,action,state,started,finished FROM agent_operations '
                                      'ORDER BY started DESC LIMIT ?', (max(1, min(limit, 100)),)).fetchall()
        return {'success': True, 'items': [dict(zip(('request_id', 'action', 'state', 'started', 'finished'), row)) for row in rows]}
