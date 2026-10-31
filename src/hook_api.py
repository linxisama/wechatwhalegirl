"""本机受控协议配置与有限回调；不下载、安装、加载或升级微信组件。"""
import asyncio
import copy
import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent_control_api import require_bound
from .hook_identity import loopback_api, probe_hook_identity
from .hook_gateway import MAX_CALLBACK_BYTES, HookProtocolError, _payload

FIELDS = ('api_url', 'account', 'account_path', 'target')
CALLBACK_PREFIX = '/api/wechat/hook/callback/'


class HookSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    api_url: str = Field(default='http://127.0.0.1:30001', max_length=128)
    account: str = Field(pattern=r'^wxid_[A-Za-z0-9_]{1,120}$')
    account_path: str = Field(min_length=3, max_length=1024)
    target: str = Field(pattern=r'^(?:wxid_[A-Za-z0-9_]{1,120}|[A-Za-z0-9_]{1,128}@chatroom|filehelper)$')


class CallbackAccessFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn 将路径作为格式参数；随机回调凭据不能进入后台日志。
        if isinstance(record.args, tuple):
            record.args = tuple(CALLBACK_PREFIX + '[redacted]' if isinstance(value, str)
                and value.startswith(CALLBACK_PREFIX) else value for value in record.args)
        return True


def _settings(bot):
    value = bot.config.get('wechat.hook', {})
    return {key: str(value.get(key, '')) for key in FIELDS}


def _check(bot, actor):
    try:
        if require_bound(bot)['revision'] == actor['revision']:
            return
    except HTTPException:
        pass
    raise HTTPException(409, '管理员绑定已改变，未保存旧操作')


async def _probe(settings):
    try:
        loopback_api(settings['api_url'])
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    result = await asyncio.to_thread(probe_hook_identity, settings['api_url'],
                                    settings['account_path'], settings['account'])
    return {key: result.get(key) for key in ('ready', 'reason', 'waiting_code')}


def add_routes(app):
    access = logging.getLogger('uvicorn.access')
    if not any(isinstance(item, CallbackAccessFilter) for item in access.filters):
        access.addFilter(CallbackAccessFilter())

    @app.get('/api/wechat/hook')
    async def info():
        bot = app.state.bot
        bridge = bot.bridge
        environment = bot.config.get('wechat.environment', 'local')
        status = dict(bridge.send_environment) if environment == 'hook' else {
            'ready': False, 'reason': '候选协议通道尚未启用；原版模块缺少接口隔离，不能直接激活',
            'waiting_code': 'not_enabled'}
        return {'success': True, 'settings': _settings(bot), 'environment': environment,
                'status': status, 'candidate_only': True, 'running': bot.running}

    @app.post('/api/wechat/hook/probe')
    async def probe(body: HookSettings):
        bot = app.state.bot; actor = require_bound(bot)
        result = await _probe(body.model_dump())
        _check(bot, actor)
        return {'success': True, 'status': result, 'ready': result.get('ready') is True}

    @app.post('/api/wechat/hook/config')
    async def configure(body: HookSettings):
        bot = app.state.bot; actor = require_bound(bot)
        settings = body.model_dump()
        try:
            loopback_api(settings['api_url'])
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        async with bot.lock:
            _check(bot, actor)
            if bot.config.get('wechat.environment', 'local') == 'hook':
                raise HTTPException(409, '请先停用协议通道再修改账号或目标，避免旧任务沿用新路由')
            before = copy.deepcopy(bot.config.config)
            try:
                bot.config.set('wechat.hook', dict(settings, enabled=False))
                bot.config.save()
            except Exception:
                bot.config._config = before
                raise HTTPException(500, '协议配置未保存，请检查数据目录') from None
        return {'success': True, 'settings': settings, 'message': '已保存；未启用通道，未连接或修改微信'}

    @app.post('/api/wechat/hook/activate')
    async def activate():
        bot = app.state.bot; actor = require_bound(bot)
        async with bot.lock:
            _check(bot, actor)
            try:
                settings = HookSettings(**_settings(bot)).model_dump()
            except ValidationError:
                raise HTTPException(400, '请先保存完整有效的协议账号与固定目标') from None
            result = await _probe(settings)
            _check(bot, actor)
            if result.get('ready') is not True:
                raise HTTPException(409, str(result.get('reason') or '协议身份未通过核验'))
            before = copy.deepcopy(bot.config.config)
            try:
                await bot.switch_wechat_environment('hook', check_access=lambda: _check(bot, actor))
            except BaseException:
                bot.config._config = before
                raise
        return {'success': True, 'message': '已选择受控协议通道；机器人保持停止，须手动启动'}

    @app.post('/api/wechat/hook/deactivate')
    async def deactivate():
        bot = app.state.bot; actor = require_bound(bot)
        async with bot.lock:
            _check(bot, actor)
            if bot.config.get('wechat.environment', 'local') == 'hook':
                await bot.switch_wechat_environment('local', check_access=lambda: _check(bot, actor))
            before = copy.deepcopy(bot.config.config)
            try:
                bot.config.set('wechat.hook.enabled', False)
                bot.config.save()
            except Exception:
                bot.config._config = before
                raise HTTPException(500, '通道已停止，但停用设置未保存，请检查数据目录') from None
        return {'success': True, 'message': '协议通道已停用；未结束微信或修改客户端'}

    @app.post('/api/wechat/hook/callback/{token}')
    async def callback(token: str, request: Request):
        bot = app.state.bot
        bridge = bot.bridge
        credential = getattr(bridge, 'callback_token', None)
        peer = request.client.host if request.client else ''
        if (bot.config.get('wechat.environment', 'local') != 'hook' or not bot.running
                or credential is None or not credential.accepts(request.url.path, peer)):
            raise HTTPException(403, '回调未授权或通道已停止')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_CALLBACK_BYTES:
                raise HTTPException(413, '回调超过大小限制')
        try:
            _payload(raw)
        except HookProtocolError:
            raise HTTPException(400, '回调格式无效') from None
        if bot.bridge is not bridge or not bot.running or not credential.accepts(request.url.path, peer):
            raise HTTPException(409, '微信通道已经改变')
        accepted = await asyncio.to_thread(bridge.ingest_raw, bytes(raw))
        return {'accepted': bool(accepted)}
