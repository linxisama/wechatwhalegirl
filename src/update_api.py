"""更新相关 HTTP 接口。

所有异常都折叠成 HTTP 200 的结构化结果（reason + 中文 message），
避免控制台因为更新源离线而弹出一堆 500；真正的服务对象按 runtime 惰性创建。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import updater
from .update_service import UpdateService

_SERVICE_LOCK = threading.Lock()
_MISSING = object()


def _config_get(runtime: Any, key: str, default: Any) -> Any:
    config = getattr(runtime, 'config', None)
    if config is None:
        config = getattr(runtime, 'settings', None)
    if config is None:
        return default
    if isinstance(config, dict):
        value: Any = config
        for part in key.split('.'):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return default if value is None else value
    try:
        value = config.get(key, _MISSING)
        if value is not _MISSING:
            return default if value is None else value
    except Exception:
        pass
    return default


def _config_set(runtime: Any, key: str, value: Any) -> bool:
    """写入 runtime 的 config；兼容 ConfigManager.set 与测试里的普通 dict。"""
    config = getattr(runtime, 'config', None)
    if config is None:
        config = getattr(runtime, 'settings', None)
    if config is None:
        return False
    if isinstance(config, dict):
        current: Any = config
        parts = key.split('.')
        for part in parts[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                child = {}
                current[part] = child
            current = child
        current[parts[-1]] = value
        return True
    try:
        setter = getattr(config, 'set', None)
        if callable(setter):
            setter(key, value)
            return True
    except Exception:
        pass
    return False


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'on', '是'):
        return True
    if text in ('0', 'false', 'no', 'off', '否'):
        return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _current_version(runtime: Any) -> str:
    for name in ('app_version', 'current_version', 'version'):
        value = getattr(runtime, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        from .runtime_identity import APP_VERSION
        return str(APP_VERSION)
    except Exception:
        return '0.0.0'


def _data_dir(runtime: Any) -> Path:
    value = getattr(runtime, 'update_data_dir', None)
    if value:
        return Path(value)
    root = getattr(runtime, 'root', None) or getattr(runtime, 'data_root', None)
    if root:
        return Path(root) / 'data' / 'update'
    return Path.cwd() / 'data' / 'update'


def get_service(runtime: Any) -> UpdateService:
    """按 runtime 缓存一个 UpdateService；首次调用才读设置。"""
    service = getattr(runtime, 'update_service', None)
    if service is not None:
        return service
    with _SERVICE_LOCK:
        service = getattr(runtime, 'update_service', None)
        if service is not None:
            return service
        feed_url = _config_get(runtime, 'update.feed_url', updater.DEFAULT_FEED_URL)
        auto_check = _config_get(runtime, 'update.auto_check', True)
        interval = _config_get(runtime, 'update.interval_hours', 24)
        service = UpdateService(
            _data_dir(runtime),
            _current_version(runtime),
            feed_url=str(feed_url if feed_url is not None else ''),
            auto_check=_as_bool(auto_check, True),
            interval_hours=_as_float(interval, 24.0),
        )
        try:
            setattr(runtime, 'update_service', service)
        except Exception:
            pass
        return service


async def _read_body(request: Request) -> dict | None:
    try:
        raw = await request.body()
    except Exception:
        return None
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _ok(status: dict) -> dict:
    return {'ok': True, 'status': status}


def _fail(reason: str, message: str) -> dict:
    return {'ok': False, 'reason': str(reason), 'message': str(message)}


def _from_status(status: Any) -> dict:
    if not isinstance(status, dict):
        return _fail('internal', '更新服务返回了无法识别的结果')
    if status.get('ok') is False:
        return _fail(str(status.get('reason') or 'internal'),
                     str(status.get('message') or '更新操作失败，请重试'))
    if status.get('state') == 'error':
        error = status.get('error') or {}
        return _fail(str(error.get('reason') or 'internal'),
                     str(error.get('message') or '更新操作失败，请重试'))
    return _ok(status)


def register(app: FastAPI, runtime: Any) -> None:
    """把 /api/update/* 路由挂到现有 FastAPI app 上。"""

    def service() -> UpdateService:
        return get_service(runtime)

    @app.get('/api/update/status')
    async def update_status():
        try:
            current = service()
            maybe = getattr(current, 'maybe_auto_check', None)
            if callable(maybe):
                try:
                    maybe()
                except Exception:
                    pass
            return _ok(current.status())
        except Exception:
            return _fail('internal', '更新服务暂时不可用，请稍后重试')

    @app.post('/api/update/check')
    async def update_check(request: Request):
        try:
            body = await _read_body(request)
            if body is None:
                return _fail('bad_request', '请求内容不是有效的 JSON 对象')
            force = body.get('force', False)
            if not isinstance(force, bool):
                force = str(force).strip().lower() in ('1', 'true', 'yes', 'on')
            return _from_status(service().check(force=force))
        except Exception:
            return _fail('internal', '检查更新时发生内部错误，请稍后重试')

    @app.post('/api/update/download')
    async def update_download():
        try:
            return _from_status(service().download())
        except Exception:
            return _fail('internal', '下载更新时发生内部错误，请稍后重试')

    @app.post('/api/update/install')
    async def update_install():
        try:
            return _from_status(service().install())
        except Exception:
            return _fail('internal', '安装更新时发生内部错误，请稍后重试')

    @app.post('/api/update/skip')
    async def update_skip(request: Request):
        try:
            body = await _read_body(request)
            if body is None:
                return _fail('bad_request', '请求内容不是有效的 JSON 对象')
            current = service()
            if body.get('clear') is True:
                return _from_status(current.unskip())
            version = str(body.get('version') or '').strip()
            if not version:
                return _fail('bad_request', '请提供要跳过的版本号')
            return _from_status(current.skip(version))
        except Exception:
            return _fail('internal', '保存跳过版本时发生内部错误，请稍后重试')

    @app.post('/api/update/settings')
    async def update_settings(request: Request):
        try:
            body = await _read_body(request)
            if body is None:
                return _fail('bad_request', '请求内容不是有效的 JSON 对象')
            feed_url = body.get('feed_url', _MISSING)
            auto_check = body.get('auto_check', _MISSING)
            interval = body.get('interval_hours', _MISSING)
            if feed_url is not _MISSING and not isinstance(feed_url, str):
                return _fail('bad_request', '更新地址必须是文本')
            if auto_check is not _MISSING and not isinstance(auto_check, bool):
                return _fail('bad_request', '自动检查开关必须是布尔值')
            if interval is not _MISSING:
                try:
                    interval_value = float(interval)
                except (TypeError, ValueError):
                    return _fail('bad_request', '检查间隔必须是数字（小时）')
                if interval_value <= 0 or interval_value > 8760:
                    return _fail('bad_request', '检查间隔必须在 1 到 8760 小时之间')
            else:
                interval_value = None
            current = service()
            status = current.configure(
                feed_url=None if feed_url is _MISSING else str(feed_url).strip(),
                auto_check=None if auto_check is _MISSING else auto_check,
                interval_hours=interval_value,
            )
            if isinstance(status, dict) and status.get('ok') is False:
                return _from_status(status)

            if feed_url is not _MISSING:
                _config_set(runtime, 'update.feed_url', str(feed_url).strip())
            if auto_check is not _MISSING:
                _config_set(runtime, 'update.auto_check', bool(auto_check))
            if interval_value is not None:
                _config_set(runtime, 'update.interval_hours', interval_value)
            config = getattr(runtime, 'config', None)
            if config is not None and callable(getattr(config, 'save', None)):
                try:
                    config.save()
                except Exception:
                    return {'ok': False, 'reason': 'save_failed',
                            'message': '更新设置已生效，但写入配置文件失败，重启后可能丢失'}
            return _ok(current.status())
        except Exception:
            return _fail('internal', '保存更新设置时发生内部错误，请稍后重试')

    # 供测试或外部脚本直接拿服务，不属于 HTTP API。
    app.state.update_service_factory = service
