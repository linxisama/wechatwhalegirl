"""DSH桥的本机管理入口；群消息与bridge凭据不能改变接入设置。"""
import asyncio
import time

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from .agent_control_api import require_bound
from .dsh_bridge import DshError, error_detail, install_preset, local_url


class DshSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    base_url: str = Field(default='http://127.0.0.1:3080', max_length=200)
    model: str = Field(default='deepseek-flash', min_length=1, max_length=100)
    provider: str = Field(default='deepseek-official', min_length=1, max_length=100)
    reasoning_effort: Literal['', 'low', 'high', 'max'] = ''
    timeout: int = Field(default=30, ge=5, le=120)
    profile: Literal['desktop', 'web', 'web-desktop'] = 'desktop'


class DshAnnounce(BaseModel):
    """DSH主动上报只带地址；其余接入字段一律沿用已保存值，不在这里改。"""
    model_config = ConfigDict(extra='forbid')
    base_url: str | None = None


def announce_message(message, limit=160):
    text = str(message or '').strip()
    return (text or 'DSH上报未完成')[:limit]


def add_routes(app):
    def record_announce(bot, state, base_url, message):
        # 只存内存：控制台重启后回到null是预期，既不写配置也不进数据库。
        bot.dsh.last_announce = {'at': time.time(), 'state': state, 'base_url': base_url or '',
                                 'actor': 'dsh', 'message': announce_message(message)}

    async def persist(bot, actor, values):
        """设置页与主动上报共用同一条绑定校验、落盘与回滚路径，不复制第二份校验。"""
        async with bot.lock:
            if require_bound(bot)['revision'] != actor['revision']:
                raise HTTPException(409, '管理员绑定已改变，旧DSH设置未保存')
            # 与微信模式切换同样先终止旧任务，不把在途旧结果转入新通道。
            running = bot.running
            if running:
                await bot.stop()
            before = bot.config._config.copy()
            try:
                if require_bound(bot)['revision'] != actor['revision']:
                    raise HTTPException(409, '管理员绑定已改变，旧DSH设置未保存')
                bot.config._config = dict(before, dsh=values)
                bot.config.save()
            except Exception:
                bot.config._config = before
                raise
            finally:
                if running:
                    await bot.start()
        bot.dsh.stage='configured' if values['enabled'] else 'disabled'
        bot.dsh.last_error=''

    @app.post('/api/dsh/discover')
    async def discover():
        bot = app.state.bot
        require_bound(bot)
        from .dsh_discovery import discover as find
        try:
            async with __import__('asyncio').timeout(15): return await find(bot.dsh)
        except (DshError,TimeoutError):
            # 超时也是一次失败，必须给出和“没找到进程”同样的可执行下一步。
            return {'success':False,'candidates':[],'process_count':0,'checked_ports':0,
                    'unverified_ports':0,'state':'process_missing','drift':None,
                    'saved_base_url':bot.dsh.settings()['base_url'],
                    'hint':'未找到 DSH 进程，请先启动桌面 DSH',
                    'message':'DSH自动查找未在15秒内完成；请先启动并登录桌面 DSH 后重试，或手动填写地址'}

    @app.get('/api/dsh/catalog')
    async def catalog():
        bot = app.state.bot
        require_bound(bot)
        try:
            return await bot.dsh.catalog()
        except Exception:
            # 目录只是下拉框数据源，任何异常都不能变成HTTP 500堵住设置页。
            return {'success':False,'providers':[],
                    'message':'DSH提供商目录读取失败，请确认地址与登录态后重新探测'}
    @app.get('/api/dsh')
    async def status():
        service = app.state.bot.dsh
        return {'success': True, **service.settings(), 'stage': service.stage,
                'last_error': service.last_error, 'wechat_delivery_verified': False,
                'last_announce': getattr(service, 'last_announce', None)}

    @app.post('/api/dsh')
    async def save(req: DshSettings):
        bot = app.state.bot
        actor = require_bound(bot)
        try:
            values = req.model_dump()
            values['base_url'] = local_url(req.base_url)
            await persist(bot, actor, values)
            return {'success': True, 'message': 'DSH设置已保存；只读检测不代表对话或微信发送成功'}
        except DshError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post('/api/dsh/announce')
    async def announce(req: DshAnnounce):
        bot = app.state.bot
        actor = require_bound(bot)
        service = bot.dsh
        from .dsh_discovery import discover as find, normalized
        reported = (req.base_url or '').strip()
        try:
            # 一个请求只做一轮：probe 与 discovery 合计不超过20秒，超时按失败处理。
            async with asyncio.timeout(20):
                if reported:
                    target = local_url(reported)
                else:
                    async with asyncio.timeout(12):
                        found = await find(service)
                    rows = [row for row in found.get('candidates') or []
                            if isinstance(row, dict) and row.get('base_url')]
                    # 只接受唯一已验证候选：多个候选时替用户挑地址等于把错端口写进配置。
                    if len(rows) != 1:
                        message = f'DSH已上报，但自动发现得到 {len(rows)} 个已验证地址，未保存设置'
                        record_announce(bot, 'failed', '', message)
                        return JSONResponse(status_code=409, content={'detail': message,
                            'state': str(found.get('state') or ''), 'hint': str(found.get('hint') or '')})
                    target = local_url(rows[0]['base_url'])
                cfg = service.settings() | {'base_url': target}
                # probe 通过才落盘：校验失败时配置文件必须原样不动。
                probe = await service.probe(cfg=cfg)
                changed = normalized(service.settings()['base_url']) != target
                await persist(bot, actor, cfg)
                # 保存会把阶段写回 configured/disabled，这里恢复刚才真实探测到的阶段。
                service.stage='ready' if probe.get('preset_ready') else 'preset_missing'
                service.last_error=''
                record_announce(bot, 'connected', target, 'DSH已主动上报并保存本机地址；未启用DSH、未改提供商与模型')
                return {'success': True, 'state': 'connected', 'base_url': target,
                        'changed': changed, 'probe': probe}
        except HTTPException as exc:
            record_announce(bot, 'failed', reported, exc.detail)
            raise
        except (DshError, TimeoutError) as exc:
            # str(TimeoutError()) 是空串，超时也要给出一句可执行的中文。
            message = str(exc) or 'DSH上报超时（超过20秒），未修改DSH设置'
            record_announce(bot, 'failed', reported, message)
            raise HTTPException(400, message) from None

    async def discovery_hint(bot):
        # 连接失败时找一次本机端点：找到就把「端口已变更」交回面板，找不到就沉默。
        # 发现只是帮忙，任何异常都不许盖掉原来的失败原因。
        try:
            from .dsh_discovery import discover
            data = await discover(bot.dsh)
        except Exception:
            return {}
        drift = data.get('drift') if isinstance(data, dict) else None
        if not isinstance(drift, dict):
            return {}
        port = drift.get('to_port')
        # 只补 hint 与 drift：原来的失败原因必须原样留给用户，绝不覆盖 message。
        return {'state': 'drift', 'drift': drift, 'candidates': data.get('candidates') or [],
                'hint': '连接失败，但本机 DSH 在另一个端口（' + str(port) + '）；点「更新为 ' + str(port) + '」后重新检测。'}

    @app.post('/api/dsh/probe')
    async def probe():
        bot = app.state.bot
        require_bound(bot)
        try:
            return await bot.dsh.probe()
        except DshError as exc:
            body = {'success': False, 'message': str(exc), 'detail': error_detail(exc)}
            found = await discovery_hint(bot)
            if found:
                body.update(found)
            return body

    @app.post('/api/dsh/connect')
    async def connect(req: DshSettings):
        bot=app.state.bot;actor=require_bound(bot)
        try:
            values=req.model_dump();values['base_url']=local_url(req.base_url)
            result=await bot.dsh.probe(cfg=values)
            if require_bound(bot)['revision'] != actor['revision']:
                raise HTTPException(409, '管理员绑定已改变，旧DSH连接未保存或启用')
            if not result.get('preset_ready'):
                return {'success':False,'message':result.get('message') or 'DSH专用人设尚未就绪，未保存或启用'}
            saved=await save(DshSettings(**(values|{'enabled':True})))
            return saved|{'enabled':True,'message':'DSH对话引擎已保存并启用；微信发送端的控件状态仍单独检查。'}
        except DshError as exc:
            found = await discovery_hint(bot)
            return {'success':False,'message':str(exc)} | (found or {})

    @app.post('/api/dsh/install-preset')
    async def install():
        bot = app.state.bot
        require_bound(bot)
        try:
            return install_preset(bot.root, profile=bot.dsh.settings()['profile'])
        except (DshError, OSError) as exc:
            return {'success': False, 'message': str(exc) if isinstance(exc, DshError) else '专用人设写入失败，未启动DSH'}
