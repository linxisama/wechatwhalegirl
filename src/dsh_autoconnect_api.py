"""控制台↔DSH 零配置连接接口：状态、一键修复、守护线程启停。

统一返回 {ok:...}；出异常只回 {ok:false,reason,message}，不把裸异常抛给调用方。
守护线程的句柄挂在 runtime 上（属性名见 dsh_autoconnect.WATCH_ATTR），不占模块全局。
"""
from __future__ import annotations

import asyncio
import threading
import time

from pydantic import BaseModel, ConfigDict

from . import dsh_autoconnect as autoconnect

SERVICE_ATTR = 'dsh_connect_service'
RUNTIME_ATTR = 'dsh_connect_runtime'


class WatchBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = True


def _failure(reason, message, status=None):
    value = {'ok': False, 'reason': str(reason), 'message': str(message)}
    if status is not None:
        value['status'] = status
    return value


def _watch_view(state):
    return {'enabled': bool(state.get('enabled')),
            'interval': float(state.get('interval') or 0.0),
            'started_at': float(state.get('started_at') or 0.0),
            'last': state.get('last') if isinstance(state.get('last'), dict) else None}


class DshConnectService:
    """把纯逻辑模块包成接口能调的服务；status/ensure/instructions/watch 都可注入替身。"""

    def __init__(self, runtime=None, status=None, ensure=None, instructions=None, watch=None):
        self.runtime = runtime
        self._status = status or autoconnect.status
        self._ensure = ensure or autoconnect.ensure
        self._instructions = instructions or autoconnect.connect_instructions
        self._watch = watch or autoconnect.watch
        self.lock = threading.Lock()
        # 句柄同时留在服务上：宿主的 runtime 可能是延迟代理对象，setattr 不一定落到 bot 上。
        self._box = {}

    def status(self):
        return self._status(self.runtime)

    def ensure(self):
        return self._ensure(self.runtime)

    def instructions(self):
        return self._instructions()

    def watch_state(self):
        if isinstance(self._box, dict) and self._box:
            return self._box
        state = getattr(self.runtime, autoconnect.WATCH_ATTR, None)
        return state if isinstance(state, dict) else {}

    def start(self, interval=30.0):
        with self.lock:
            state = self.watch_state()
            thread = state.get('thread')
            if isinstance(thread, threading.Thread) and thread.is_alive():
                return {'ok': True, 'enabled': True, 'message': '守护线程已在运行', 'watch': _watch_view(state)}
            stop = threading.Event()
            box = {'enabled': True, 'stop': stop, 'started_at': time.time(),
                   'interval': float(interval), 'last': None, 'thread': None}

            def run():
                try:
                    box['last'] = self._watch(self.runtime, interval=float(interval), stop_event=stop)
                finally:
                    box['enabled'] = False

            thread = threading.Thread(target=run, name='dsh-autoconnect-watch', daemon=True)
            box['thread'] = thread
            self._box = box
            try:
                setattr(self.runtime, autoconnect.WATCH_ATTR, box)
            except Exception as error:
                return _failure('runtime_locked', '无法把守护句柄挂到运行时（' + type(error).__name__ + '）')
            thread.start()
            return {'ok': True, 'enabled': True, 'message': '守护线程已启动，每 ' + str(interval) + ' 秒刷新一次连接状态',
                    'watch': _watch_view(box)}

    def stop(self):
        with self.lock:
            state = self.watch_state()
            thread = state.get('thread')
            stop = state.get('stop')
            if not isinstance(thread, threading.Thread) or not thread.is_alive():
                state['enabled'] = False
                return {'ok': True, 'enabled': False, 'message': '守护线程本来就没在跑', 'watch': _watch_view(state)}
            if isinstance(stop, threading.Event):
                stop.set()
            thread.join(timeout=5.0)
            state['enabled'] = False
            alive = thread.is_alive()
            return {'ok': not alive, 'enabled': False,
                    'message': '守护线程已停止' if not alive else '守护线程还在退出，请稍后再试',
                    'watch': _watch_view(state)}


def _service(app):
    service = getattr(app.state, SERVICE_ATTR, None)
    if service is None:
        raise RuntimeError('dsh connect service 未注册')
    return service


def register(app, runtime) -> None:
    """挂载 /api/dsh/connect 三个端点；已注入 service 时保留替身（测试与宿主都能替换）。"""
    service = getattr(app.state, SERVICE_ATTR, None)
    if service is None:
        service = DshConnectService(runtime)
        setattr(app.state, SERVICE_ATTR, service)
    setattr(app.state, RUNTIME_ATTR, runtime)

    @app.get('/api/dsh/connect')
    async def dsh_connect_status():
        try:
            service_now = _service(app)
            value = await asyncio.to_thread(service_now.status)
            text = await asyncio.to_thread(service_now.instructions)
        except Exception as error:
            return _failure('internal', '读取 DSH 连接状态时出现意外错误（' + type(error).__name__ + '），请重试')
        if not isinstance(value, dict):
            return _failure('internal', 'DSH 连接状态返回格式异常，请重试')
        return {'ok': value.get('state') != 'error', 'status': value, 'instructions': str(text or '')}

    @app.post('/api/dsh/connect/repair')
    async def dsh_connect_repair():
        try:
            result = await asyncio.to_thread(_service(app).ensure)
        except Exception as error:
            return _failure('internal', '修复 DSH 连接时出现意外错误（' + type(error).__name__ + '），没有改动任何配置')
        if not isinstance(result, dict):
            return _failure('internal', '修复结果格式异常，请重试')
        steps = [step for step in (result.get('steps') or []) if isinstance(step, dict)]
        return {'ok': bool(result.get('ok')), 'state': str(result.get('state') or 'failed'),
                'steps': steps, 'message': str(result.get('message') or ''), 'at': result.get('at')}

    @app.post('/api/dsh/connect/watch')
    async def dsh_connect_watch(body: WatchBody):
        try:
            service_now = _service(app)
            value = await asyncio.to_thread(service_now.start if body.enabled else service_now.stop)
        except Exception as error:
            return _failure('internal', '切换 DSH 守护线程时出现意外错误（' + type(error).__name__ + '）')
        if not isinstance(value, dict):
            return _failure('internal', '守护线程返回格式异常，请重试')
        return value


def start_watch(app, interval=30.0):
    """宿主启动时调用：用 app 上注册的 service 起守护线程；失败只返回 dict，不抛。"""
    try:
        value = _service(app).start(interval)
    except Exception as error:
        return {'ok': False, 'reason': 'internal', 'enabled': False,
                'message': '启动 DSH 守护失败（' + type(error).__name__ + '）'}
    if not isinstance(value, dict):
        return {'ok': False, 'reason': 'internal', 'enabled': False, 'message': '守护线程返回格式异常，请重试'}
    return value


def stop_watch(app):
    """宿主关停时调用：停掉守护线程，等它收尾；同样绝不抛异常。"""
    try:
        value = _service(app).stop()
    except Exception as error:
        return {'ok': False, 'reason': 'internal', 'enabled': False,
                'message': '停止 DSH 守护失败（' + type(error).__name__ + '）'}
    if not isinstance(value, dict):
        return {'ok': False, 'reason': 'internal', 'enabled': False, 'message': '守护线程返回格式异常，请重试'}
    return value
