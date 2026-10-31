"""Windows 沙盒启用接口：状态、异步启用、核对提权结果、创建隔离通道、禁用方法。

四个动作都要么立刻返回（启用是异步的，进度靠 status 轮询），要么把慢活扔进线程，
绝不让 HTTP 请求挂住界面。统一返回 {ok, status:{...}}；出异常只回 {ok:false,reason,message}，
不把裸异常抛给调用方。
"""
from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from . import sandbox_provision as provision

SERVICE_ATTR = 'sandbox_provision_service'
TASK_ATTR = 'sandbox_provision_task'
LAST_ATTR = 'sandbox_provision_last'
RUNTIME_ATTR = 'sandbox_provision_runtime'


class CheckBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    log_path: str = Field(default='', max_length=1100)


class SandboxProvisionService:
    """把纯逻辑模块包成面板/接口能调的服务；runner 与 log_dir 都可注入，测试用替身。"""

    def __init__(self, runtime=None, runner=None, log_dir=None, elevation=None):
        self.runtime = runtime
        self.runner = runner
        self.log_dir = log_dir
        self.elevation = elevation

    def status(self):
        return provision.status(runner=self.runner, log_dir=self.log_dir)

    def enable(self):
        return provision.enable(runner=self.runner, elevation=self.elevation,
                                log_dir=self.log_dir)

    def last_log_path(self):
        try:
            folder = provision._resolve_log_dir(self.log_dir)
            record = provision._read_record(folder / provision.RECORD_NAME)
        except Exception:
            return ''
        return str(record.get('log_path') or '')

    def check(self, log_path=None):
        path = str(log_path or '') or self.last_log_path()
        if not path:
            return {'ok': False, 'state': 'pending', 'reason': 'log_missing', 'log_path': '',
                    'message': '还没有启用记录：请先点“启用 Windows 沙盒”。'}
        return provision.check_elevation_result(path)

    def create_channel(self):
        return provision.create_channel(self.runtime, manager=None, runner=self.runner)

    def rollback_text(self):
        return provision.rollback_text()


def _failure(reason, message, status=None):
    value = {'ok': False, 'reason': reason, 'message': message}
    if status is not None:
        value['status'] = status
    return value


def _error_message(action, error):
    return action + '时出现意外错误（' + type(error).__name__ + '），未做任何改动，请重试'


def _service(app):
    service = getattr(app.state, SERVICE_ATTR, None)
    if service is None:
        raise RuntimeError('sandbox provision service 未注册')
    return service


def register(app, runtime=None):
    """挂载 /api/sandbox/provision/*；已注入 service 时保留替身（测试与宿主都能替换）。"""
    service = getattr(app.state, SERVICE_ATTR, None)
    if service is None:
        service = SandboxProvisionService(runtime)
        setattr(app.state, SERVICE_ATTR, service)
    setattr(app.state, RUNTIME_ATTR, runtime)

    @app.get('/api/sandbox/provision/status')
    async def provision_status():
        try:
            value = await asyncio.to_thread(_service(app).status)
        except Exception as error:
            return _failure('internal', _error_message('读取沙盒状态', error))
        if not isinstance(value, dict):
            return _failure('internal', '沙盒状态返回格式异常，请重试')
        if value.get('ok') is False:
            return _failure(str(value.get('reason') or 'internal'),
                            str(value.get('message') or '读取沙盒状态失败'), value)
        return {'ok': True, 'status': value}

    @app.post('/api/sandbox/provision/enable')
    async def provision_enable():
        service = _service(app)
        task = getattr(app.state, TASK_ATTR, None)
        if task is not None and not task.done():
            return {'ok': True, 'status': {'state': 'running',
                    'message': '启用流程正在进行，请稍候刷新状态'}}

        async def perform():
            try:
                result = await asyncio.to_thread(service.enable)
            except Exception as error:
                result = {'state': 'failed', 'reason': 'internal',
                          'message': _error_message('启用 Windows 沙盒', error)}
            if not isinstance(result, dict):
                result = {'state': 'failed', 'reason': 'internal',
                          'message': '启用返回格式异常，请重试'}
            try:
                setattr(app.state, LAST_ATTR, result)
            except Exception:
                pass
            return result

        task = asyncio.create_task(perform())
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        setattr(app.state, TASK_ATTR, task)
        return {'ok': True, 'status': {'state': 'started',
                'message': '已开始启用 Windows 沙盒：如果弹出管理员授权窗口，请点“是”'}}

    @app.post('/api/sandbox/provision/check')
    async def provision_check(body: CheckBody | None = None):
        log_path = (body.log_path if body is not None else '') or None
        try:
            value = await asyncio.to_thread(_service(app).check, log_path)
        except Exception as error:
            return _failure('internal', _error_message('核对启用结果', error))
        if not isinstance(value, dict):
            return _failure('internal', '核对结果格式异常，请重试')
        if value.get('ok') is True:
            return {'ok': True, 'status': value}
        return _failure(str(value.get('reason') or 'pending'),
                        str(value.get('message') or '还没有启用结果'), value)

    @app.post('/api/sandbox/provision/channel')
    async def provision_channel():
        try:
            value = await asyncio.to_thread(_service(app).create_channel)
        except Exception as error:
            return _failure('internal', _error_message('创建隔离通道', error))
        if not isinstance(value, dict):
            return _failure('internal', '创建结果格式异常，请重试')
        if value.get('ok'):
            return {'ok': True, 'status': value}
        return _failure(str(value.get('reason') or 'internal'),
                        str(value.get('message') or '隔离通道没有创建完成'), value)

    @app.get('/api/sandbox/provision/rollback')
    async def provision_rollback():
        try:
            text = await asyncio.to_thread(_service(app).rollback_text)
        except Exception as error:
            return _failure('internal', _error_message('读取禁用方法', error))
        if not isinstance(text, str) or not text.strip():
            return _failure('internal', '禁用方法文本为空，请重试')
        return {'ok': True, 'status': {'text': text,
                'message': '把下面的命令复制到“以管理员身份运行”的命令提示符里执行即可禁用'}}

    return None


__all__ = ['register', 'SandboxProvisionService', 'CheckBody', 'SERVICE_ATTR']
