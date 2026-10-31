"""仅本机绑定管理员可配对隔离微信；接口本身不会启动机器人。"""
import asyncio
import re
import sys
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .agent_control_api import require_bound
from .isolated_wechat.protocol import ProtocolError, load_pairing, live_service
from .runtime_identity import APP_VERSION
from . import sandbox_manager as manager


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=36, max_length=36)


class InstallBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(default='', max_length=36)
    installer: str = Field(min_length=1, max_length=1100)


class ActivateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    channel_id: str = Field(pattern=r'^[0-9a-f]{32}$')


def _check_actor(bot, actor):
    try:
        current = require_bound(bot)
    except HTTPException:
        raise HTTPException(409, '管理员绑定已改变，未保存旧操作') from None
    if current['revision'] != actor['revision']:
        raise HTTPException(409, '管理员绑定已改变，未保存旧操作')


def _check_not_preparing(bot):
    if getattr(bot, '_sandbox_preparing', False):
        raise HTTPException(409, '隔离后台正在配对，请等待本次结果后再切换通道')


def _preparing_current_sandbox(bot, sandbox_id):
    return (bot.config.get('wechat.environment', 'local') == 'sandbox'
            and str(bot.config.get('wechat.sandbox_id', '')).lower() == sandbox_id)


def _public(value, fields):
    """只公布已定义的显示字段，内部新增元数据也不会随响应泄露。"""
    return {key: value[key] for key in fields if key in value}


def _pair_public(value):
    """诊断只是有限标量，不能把恢复记录、路径或未知异常原文带出。"""
    result = _public(value, ('channel_id', 'sandbox_id', 'short_id', 'stage',
        'agent_online', 'wechat_online', 'app_version', 'reason'))
    enums = {'recovery_state': frozenset(('none', 'resumable', 'repairable', 'blocked', 'ready')),
             'recovery_stage': manager.RECOVERY_STAGES,
             'recovery_failed_operation': manager.RECOVERY_OPERATIONS,
             'initial_failed_operation': manager.RECOVERY_OPERATIONS,
             'recovery_reason': manager.RECOVERY_REASONS}
    for key, allowed in enums.items():
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            result[key] = item
    # 有字段却不是已定义状态，不能省略成旧版“首次恢复”的兼容语义。
    # 固定 blocked 不带回错误原文，同时让界面和后端保持保守一致。
    if 'recovery_state' in value and 'recovery_state' not in result:
        result['recovery_state'] = 'blocked'
    for prefix in ('recovery', 'initial'):
        code = value.get(prefix + '_hresult')
        if isinstance(code, str) and re.fullmatch(r'0x[0-9A-F]{8}', code):
            result[prefix + '_hresult'] = code
        number = value.get(prefix + '_returncode')
        if type(number) is int:
            result[prefix + '_returncode'] = number
    return result


def _operation_error(error):
    if isinstance(error, (manager.SandboxError, ProtocolError)):
        return str(error)
    return '隔离通道操作未完成，请检查沙盒状态后重试'


async def _stop_result(bot, result):
    try:
        # 不信任返回的绝对路径；只停止本机通道根目录内核验过的配对。
        path = manager.channel_path(bot.root, result.get('channel_id', ''))
        await asyncio.to_thread(manager.stop_guest, path)
    except (OSError, ValueError, RuntimeError):
        bot.note('旧配对代理未确认退出；未启用通道，未终止微信或沙盒')


def add_routes(app):
    @app.get('/api/sandbox/status')
    async def status():
        bot = app.state.bot
        try:
            result = await asyncio.to_thread(manager.paired_status, bot.root)
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(400, _operation_error(error)) from None
        value = _public(result, ('success', 'available', 'message'))
        value['sandboxes'] = [_public(row, ('id',)) for row in result.get('sandboxes', [])]
        value['pairs'] = [_pair_public(row) for row in result.get('pairs', [])]
        value.update(environment=bot.config.get('wechat.environment', 'local'),
            active_channel_id=bot.config.get('wechat.sandbox_channel_id', ''), running=bot.running,
            preparing=bool(getattr(bot, '_sandbox_preparing', False)))
        return value

    async def prepare_or_recover(req: PrepareBody, *, recovery=False):
        bot = app.state.bot
        actor = require_bound(bot)
        if getattr(bot, '_sandbox_preparing', False):
            raise HTTPException(409, '隔离后台正在配对，请等待本次结果')
        bot._sandbox_preparing = True
        abandoned = asyncio.Event()

        async def perform(sandbox_id):
            if abandoned.is_set():
                raise HTTPException(409, '配对请求已取消，未开始准备隔离通道')
            _check_actor(bot, actor)
            app_dir = (Path(sys.executable).parent if getattr(sys, 'frozen', False) else
                Path(__file__).resolve().parent.parent / 'dist/WhaleGirlWeChat')
            action = manager.recover_sandbox if recovery else manager.prepare_sandbox
            result = await asyncio.to_thread(action, bot.root, app_dir, sandbox_id)
            try:
                if abandoned.is_set():
                    raise HTTPException(409, '配对请求已取消，隔离通道未启用')
                _check_actor(bot, actor)
            except HTTPException:
                await _stop_result(bot, result)
                raise
            return _public(result, ('success', 'channel_id', 'sandbox_id', 'stage', 'message',
                                    'skipped_channels'))

        async def operation():
            try:
                sandbox_id = manager._sandbox_id(req.id)
                async with bot.lock:
                    _check_actor(bot, actor)
                    if recovery and (bot.running or getattr(bot, 'connecting', False)
                                     or getattr(bot, 'reconnecting', False)):
                        raise HTTPException(409, '请先停止机器人，再修复未完成的隔离配对')
                    if recovery or _preparing_current_sandbox(bot, sandbox_id):
                        if bot.running or getattr(bot, 'connecting', False):
                            raise HTTPException(409, '请先停止机器人，再更新正在使用的隔离通道')
                        # 当前已选但停止的通道准备期间不能被另一个启动请求使用。
                        # 其他环境的机器人不受此次配对影响，也不占用它的停止锁。
                        return await perform(sandbox_id)
                return await perform(sandbox_id)
            except (OSError, ValueError, RuntimeError) as error:
                raise HTTPException(400, _operation_error(error)) from None
            finally:
                bot._sandbox_preparing = False

        # HTTP 断连不能取消正在执行的 share，也不能提前放开第二次配对覆盖同一映射。
        task = asyncio.create_task(operation())
        bot._sandbox_prepare_task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            abandoned.set()
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            raise

    @app.post('/api/sandbox/launch')
    async def launch():
        bot = app.state.bot
        require_bound(bot)
        if getattr(bot, '_sandbox_preparing', False):
            raise HTTPException(409, '隔离后台正在配对，请等待本次结果后再打开沙盒')
        if getattr(bot, '_sandbox_launching', False):
            raise HTTPException(409, '正在等待 Windows 沙盒启动，请稍候刷新状态')
        # 先置位再建任务：两次连点也只可能启动一台沙盒。
        bot._sandbox_launching = True

        async def perform():
            try:
                result = await asyncio.to_thread(manager.launch_sandbox, bot.root)
            except manager.SandboxLaunchError as error:
                return {'success': False, 'reason': error.reason, 'message': str(error)}
            except (OSError, ValueError, RuntimeError) as error:
                return {'success': False, 'reason': 'launch_failed', 'message': _operation_error(error)}
            finally:
                bot._sandbox_launching = False
            return _public(result, ('success', 'sandbox_id', 'created', 'message'))

        # 启动与轮询在后台线程执行，不占用事件循环；HTTP 断连不取消已经发出的启动。
        task = asyncio.create_task(perform())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            raise


    @app.post('/api/sandbox/install-client')
    async def install_client(req: InstallBody):
        bot = app.state.bot
        require_bound(bot)
        if getattr(bot, '_sandbox_preparing', False):
            raise HTTPException(409, '隔离后台正在配对，请等待本次结果后再安装微信')
        if getattr(bot, '_sandbox_launching', False):
            raise HTTPException(409, '正在等待 Windows 沙盒启动，请稍候刷新状态后再安装微信')
        if getattr(bot, '_sandbox_installing', False):
            raise HTTPException(409, '正在沙盒内安装微信，请等待本次结果')
        # 先置位再建任务：两次连点也只可能跑一次安装包。
        bot._sandbox_installing = True

        async def perform():
            try:
                result = await asyncio.to_thread(manager.install_client, bot.root, req.id,
                    req.installer)
            except manager.SandboxInstallError as error:
                return {'success': False, 'installed': False, 'reason': error.reason,
                    'message': str(error)}
            except (OSError, ValueError, RuntimeError) as error:
                return {'success': False, 'installed': False, 'reason': 'exec_failed',
                    'message': _operation_error(error)}
            finally:
                bot._sandbox_installing = False
            return _public(result, ('success', 'installed', 'already_installed', 'executable',
                'message'))

        # 安装与轮询放在后台线程，不占用事件循环；HTTP 断连不取消已经发出的安装。
        task = asyncio.create_task(perform())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            raise

    @app.post('/api/sandbox/prepare')
    async def prepare(req: PrepareBody):
        return await prepare_or_recover(req)

    @app.post('/api/sandbox/recover')
    async def recover(req: PrepareBody):
        return await prepare_or_recover(req, recovery=True)

    @app.post('/api/sandbox/activate')
    async def activate(req: ActivateBody):
        bot = app.state.bot
        actor = require_bound(bot)
        def check():
            _check_actor(bot, actor)
            _check_not_preparing(bot)
        check()
        async with bot.lock:
            check()
            try:
                path = manager.channel_path(bot.root, req.channel_id)
                service = live_service(load_pairing(path))
                if service.get('app_version') != APP_VERSION:
                    raise manager.SandboxError('隔离代理版本不一致，请重新准备')
                pairs = await asyncio.to_thread(manager.paired_status, bot.root)
                check()
                selected = next((pair for pair in pairs.get('pairs', [])
                    if pair.get('channel_id') == req.channel_id), None)
                if (selected is None or selected.get('agent_online') is not True
                        or selected.get('stage') not in ('agent_ready', 'ready')):
                    raise manager.SandboxError('隔离配对尚未就绪，请检查沙盒并刷新状态')
                await bot.switch_wechat_environment('sandbox', req.channel_id,
                    str(selected.get('sandbox_id', '')), check_access=check)
            except (ValueError, OSError, RuntimeError) as error:
                raise HTTPException(400, _operation_error(error)) from None
        return {'success': True,
            'message': '已启用隔离后台通道；机器人保持停止，请检查账号后手动启动'}

    @app.post('/api/sandbox/deactivate')
    async def deactivate():
        bot = app.state.bot
        actor = require_bound(bot)
        def check():
            _check_actor(bot, actor)
            _check_not_preparing(bot)
        check()
        async with bot.lock:
            check()
            previous = bot.config.get('wechat.sandbox_channel_id', '')
            try:
                await bot.switch_wechat_environment('local', check_access=check)
            except (ValueError, OSError, RuntimeError) as error:
                raise HTTPException(400, _operation_error(error)) from None
            if previous:
                await _stop_result(bot, {'channel_id': previous})
        return {'success': True, 'message': '已切回本机通道；机器人保持停止，未关闭微信或沙盒'}
