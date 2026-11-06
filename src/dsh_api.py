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


def _stage_code(service):
    """桥的阶段 → 连接码；只有明确的失败/就绪阶段给码，其余交给 last_error 归类。"""
    stage = str(getattr(service, 'stage', '') or '')
    return {'failed': 'dsh_unreachable', 'preset_missing': 'dsh_preset_missing',
            'ready': 'dsh_ok'}.get(stage, '')


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
        from .dsh_discovery import CODE_HINT, CODE_TEXT, code_for_state, discover as find
        try:
            # 发现自己带 12 秒预算并会返回 timeout 结果；这里 15 秒只是最后一道闸。
            async with __import__('asyncio').timeout(15):
                return await find(bot.dsh)
        except (DshError,TimeoutError):
            # 超时不是「没找到进程」：说成没启动会让用户去开一个已经在跑的程序。
            code = 'dsh_timeout'
            return {'success':False,'candidates':[],'process_count':0,'checked_ports':0,
                    'unverified_ports':0,'state':'timeout','drift':None,'code':code,
                    'code_text':CODE_TEXT[code],'code_hint':CODE_HINT[code],
                    'saved_base_url':bot.dsh.settings()['base_url'],
                    'hint':CODE_HINT[code],
                    'message':'DSH自动查找未在15秒内完成（' + CODE_TEXT[code] + '）；'
                              + CODE_HINT[code]}

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
        from .dsh_discovery import (CODE_HINT, CODE_TEXT, code_for_message,
                                    describe_code, hint_for_code)
        # 界面过去只拿得到 stage 与一句英文异常类型；这里把「是什么/怎么办」两句话一起给出来。
        # 同样是 stage=failed，鉴权失败与端口没开不是一回事：先按 last_error 归类，认不出来才退回阶段码。
        code = code_for_message(getattr(service, 'last_error', '')) or _stage_code(service)
        return {'success': True, **service.settings(), 'stage': service.stage,
                'last_error': service.last_error, 'wechat_delivery_verified': False,
                'last_announce': getattr(service, 'last_announce', None),
                'code': code, 'code_text': describe_code(code) or CODE_TEXT.get(code, ''),
                'code_hint': hint_for_code(code) or CODE_HINT.get(code, ''),
                'checked_at': getattr(service, 'checked_at', None)}

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
        from .dsh_discovery import code_for_message, describe_code, discover as find, hint_for_code, normalized
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
        from .dsh_discovery import CODE_HINT, code_for_message, describe_code, hint_for_code
        try:
            result = await bot.dsh.probe()
            # 探测成功也要记一次心跳：界面靠这个时间戳区分「刚验过」和「上次的旧结论」。
            bot.dsh.checked_at = time.time()
            return result
        except DshError as exc:
            bot.dsh.checked_at = time.time()
            # 码只加字段，不动 message：既有测试与上报文案都依赖那句原文。
            code = code_for_message(exc) or 'dsh_unreachable'
            body = {'success': False, 'message': str(exc), 'detail': error_detail(exc),
                    'code': code, 'code_text': describe_code(code), 'code_hint': hint_for_code(code),
                    'checked_at': bot.dsh.checked_at}
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
            # 失败也带码与两句中文，界面不必再从异常文本里猜是哪一类故障。
            code = code_for_message(exc) or 'dsh_unreachable'
            return {'success':False,'message':str(exc),'code':code,
                    'code_text':describe_code(code),'code_hint':hint_for_code(code)} | (found or {})

    @app.post('/api/dsh/install-preset')
    async def install():
        bot = app.state.bot
        require_bound(bot)
        try:
            return install_preset(bot.root, profile=bot.dsh.settings()['profile'])
        except (DshError, OSError) as exc:
            return {'success': False, 'message': str(exc) if isinstance(exc, DshError) else '专用人设写入失败，未启动DSH'}
