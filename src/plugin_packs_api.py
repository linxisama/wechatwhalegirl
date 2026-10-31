"""插件包 HTTP 接口（1.3.63）：列表 / 重扫 / 安装 / 删除 / 启用开关。

挂载方式（由主程序在 create_app 里调用）：

    from src import plugin_packs_api
    plugin_packs_api.register(app, runtime)

设计口径：所有错误都用 {ok:false, reason, message} 表达，HTTP 状态码 200 为主，
只有「请求里该有的参数没有」才回 400。前端拿到 message 直接显示给用户。
"""
from __future__ import annotations

import base64
import binascii
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from . import plugin_packs
from .plugin_packs import PACK_ID_RE, MAX_PACK_BYTES

_NOT_READY = "插件系统还没准备好，请稍后再试"
_BAD_REQUEST = "请求缺少必要参数"


def _manager(app, runtime) -> Optional[Any]:
    """从 runtime 或 app.state 上找到 PluginManager；找不到返回 None。"""
    candidates = []
    if runtime is not None:
        candidates.append(getattr(runtime, "plugins", None))
        candidates.append(getattr(runtime, "manager", None))
        bot = getattr(runtime, "bot", None)
        if bot is not None:
            candidates.append(getattr(bot, "plugins", None))
    state = getattr(app, "state", None)
    bot = getattr(state, "bot", None) if state is not None else None
    if bot is not None:
        candidates.append(getattr(bot, "plugins", None))
    for item in candidates:
        if item is not None and hasattr(item, "pack_status"):
            return item
    return None


def _error(reason: str, message: str, **extra: Any) -> dict:
    payload = {"ok": False, "reason": reason, "message": message}
    payload.update(extra)
    return payload


def _pack_id(value: str) -> str:
    return str(value or "").strip()


def register(app, runtime=None) -> None:
    """把插件包相关路由注册到 FastAPI app 上（重复调用不会重复注册）。"""
    if getattr(app.state, "plugin_packs_registered", False):
        return
    app.state.plugin_packs_registered = True
    app.state.plugin_packs_runtime = runtime

    @app.get("/api/plugins/packs")
    async def list_plugin_packs():
        manager = _manager(app, runtime)
        if manager is None:
            return JSONResponse(_error("not_ready", _NOT_READY), status_code=200)
        try:
            status = manager.pack_status()
        except Exception as exc:  # 目录坏了也只回一条中文提示，不让接口 500
            return JSONResponse(_error("scan_failed", "插件包目录读取失败，请稍后再试",
                                       detail=str(exc)[:200]), status_code=200)
        payload = {"ok": True}
        payload.update(status)
        try:
            payload["plugins"] = manager.plugin_status()
        except Exception:
            payload["plugins"] = {}
        return JSONResponse(payload, status_code=200)

    @app.post("/api/plugins/packs/rescan")
    async def rescan_plugin_packs():
        manager = _manager(app, runtime)
        if manager is None:
            return JSONResponse(_error("not_ready", _NOT_READY), status_code=200)
        try:
            manager.load_packs()
            status = manager.pack_status()
        except Exception as exc:
            return JSONResponse(_error("scan_failed", "重新扫描插件包失败，请稍后再试",
                                       detail=str(exc)[:200]), status_code=200)
        payload = {"ok": True, "message": status.get("message", "已重新扫描插件包")}
        payload.update(status)
        return JSONResponse(payload, status_code=200)

    @app.post("/api/plugins/packs/install")
    async def install_plugin_pack(request: Request):
        manager = _manager(app, runtime)
        if manager is None:
            return JSONResponse(_error("not_ready", _NOT_READY), status_code=200)
        try:
            parsed = await request.json()
        except Exception:
            parsed = None
        data = parsed if isinstance(parsed, dict) else {}
        raw_path = data.get("path")
        raw_zip = data.get("zip_base64")
        if not raw_path and not raw_zip:
            return JSONResponse(_error("bad_request", "请提供插件包路径或压缩包内容"),
                                status_code=400)
        dest = manager.pack_install_dir()
        temp_dir = None
        source = None
        try:
            if raw_zip:
                if not isinstance(raw_zip, str):
                    return JSONResponse(_error("bad_request", "压缩包内容格式不对"),
                                        status_code=400)
                try:
                    blob = base64.b64decode(raw_zip, validate=True)
                except (binascii.Error, ValueError):
                    return JSONResponse(_error("bad_base64", "压缩包内容不是有效的 base64"),
                                        status_code=200)
                if len(blob) > MAX_PACK_BYTES:
                    return JSONResponse(_error("too_large", "插件包超过 20MB 上限"),
                                        status_code=200)
                temp_dir = Path(tempfile.mkdtemp(prefix="whale_pack_upload_"))
                source = temp_dir / "upload.zip"
                source.write_bytes(blob)
            else:
                if not isinstance(raw_path, str) or not raw_path.strip():
                    return JSONResponse(_error("bad_request", "插件包路径不能为空"),
                                        status_code=400)
                source = Path(raw_path.strip())
        except OSError as exc:
            return JSONResponse(_error("io_error", "插件包读取失败，请检查文件是否被占用",
                                       detail=str(exc)[:200]), status_code=200)
        try:
            result = plugin_packs.install_pack(source, dest)
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)
        if not result.get("ok"):
            payload = _error(str(result.get("reason") or "install_failed"),
                             str(result.get("message") or "插件包安装失败"))
            payload["errors"] = list(result.get("errors") or [])
            payload["id"] = result.get("id") or ""
            return JSONResponse(payload, status_code=200)
        try:
            manager.load_packs()
        except Exception:
            pass
        payload = {"ok": True, "id": result.get("id"), "version": result.get("version"),
                   "path": result.get("path"),
                   "message": result.get("message") or "插件包安装完成",
                   "pack": manager.pack_status()}
        return JSONResponse(payload, status_code=200)

    @app.delete("/api/plugins/packs/{pack_id}")
    async def delete_plugin_pack(pack_id: str):
        manager = _manager(app, runtime)
        if manager is None:
            return JSONResponse(_error("not_ready", _NOT_READY), status_code=200)
        pid = _pack_id(pack_id)
        if not PACK_ID_RE.match(pid):
            return JSONResponse(_error("bad_id", "插件包标识不合法"), status_code=200)
        result = plugin_packs.remove_pack(manager.pack_install_dir(), pid)
        if not result.get("ok"):
            return JSONResponse(_error(str(result.get("reason") or "remove_failed"),
                                       str(result.get("message") or "删除插件包失败")),
                                status_code=200)
        try:
            manager.load_packs()
        except Exception:
            pass
        payload = {"ok": True, "removed": True, "message": result.get("message") or "插件包已删除",
                   "pack": manager.pack_status()}
        return JSONResponse(payload, status_code=200)

    @app.post("/api/plugins/packs/{pack_id}/enabled")
    async def toggle_plugin_pack(pack_id: str, request: Request):
        manager = _manager(app, runtime)
        if manager is None:
            return JSONResponse(_error("not_ready", _NOT_READY), status_code=200)
        pid = _pack_id(pack_id)
        if not PACK_ID_RE.match(pid):
            return JSONResponse(_error("bad_id", "插件包标识不合法"), status_code=200)
        try:
            parsed = await request.json()
        except Exception:
            parsed = None
        data = parsed if isinstance(parsed, dict) else {}
        if "enabled" not in data:
            return JSONResponse(_error("bad_request", "请提供 enabled 开关值"), status_code=400)
        enabled = bool(data.get("enabled"))
        try:
            manager.update_settings(pid, {"enabled": enabled})
        except KeyError:
            return JSONResponse(_error("not_found", "没有找到这个插件包，可能还没安装"),
                                status_code=200)
        except ValueError as exc:
            return JSONResponse(_error("invalid", "这个插件包当前不可用，无法修改开关：" + str(exc)),
                                status_code=200)
        except Exception:
            return JSONResponse(_error("save_failed", "保存插件开关失败，请稍后再试"),
                                status_code=200)
        return JSONResponse({"ok": True, "id": pid, "enabled": enabled,
                             "message": "插件已启用" if enabled else "插件已停用"},
                            status_code=200)

    return None
