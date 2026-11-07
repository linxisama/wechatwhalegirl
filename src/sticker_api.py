"""本机 HTTP 接口：机器人本地表情库（列表 / 上传导入 / 改标签 / AI 识别 / 删除）。

对照 QQ 版 `sticker_api.py` 的接口形状（同一套字段名，界面才能照搬），差异有三处：

1. **缩略图用 Pillow 生成**，不在后端 import PyQt6 —— 后端是纯 HTTP 服务，
   拖进 GUI 框架会让冻结包缺图片插件时把合法图片判成损坏。
2. **上传中转目录改成数据目录下的 `uploads/sticker_admin/imports`**（QQ 版在项目目录里），
   与"可写数据一律在 %LOCALAPPDATA%"一致。
3. 全部路由都在既有的本机管理中间件之后（`wechat_app.local_admin`），
   非本机请求在更外层就被 403 挡掉。
"""
from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from .sticker_library import (MAX_BYTES, StickerLibrary, frames_for_vision,
                              inspect_image, thumbnail_png)

PAGE_SIZE = 12
# 单次导入总量上限：防止一次导入把请求体撑爆（单次上限 64MB）
IMPORT_TOTAL_BYTES = 64 * 1024 * 1024


def service(app):
    """取出运行时的表情库；被关掉或读不出来时给出可读的 4xx/5xx。"""
    bot = app.state.bot
    plugin = bot.plugins._plugins.get("sticker_admin")
    if plugin is None or not plugin.enabled:
        raise HTTPException(403, "表情包管理已关闭：请在插件中心启用「表情包管理」")
    store = getattr(bot, "stickers", None)
    if store is None:
        raise HTTPException(503, getattr(bot, "sticker_error", "")
                            or "本地表情库无法读取，请检查数据目录或从备份恢复")
    return bot, store


def vision_model(bot) -> str:
    """识图用哪个模型：插件里填了就用，没填则按服务商挑默认值。"""
    plugin = bot.plugins._plugins.get("image_understanding")
    if plugin is None or not plugin.enabled:
        raise HTTPException(400, "请先启用「图片与表情包识别」插件，AI 识别才有模型可用")
    configured = str(plugin.get("visionModel", "") or "").strip()
    if configured:
        return configured
    if urlsplit(str(plugin.get('visionApiUrl', '') or bot.ai.api_url or "")).hostname == "api.deepseek.com":
        return "deepseek-flash"
    return str(bot.ai.model or "")


def add_routes(app):
    @app.get('/api/stickers/annotation-jobs')
    async def annotation_jobs():
        bot, _ = service(app)
        return bot.image_chat.status()

    @app.post('/api/stickers/annotation-jobs')
    async def enqueue_annotations(request: Request):
        bot, store = service(app)
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {'keys', 'retry_failed'}:
            raise HTTPException(400, '标注任务格式无效')
        retry = body.get('retry_failed', False)
        if not isinstance(retry, bool):
            raise HTTPException(400, '重试选项必须是布尔值')
        keys = body.get('keys')
        if keys is None:
            failed = {row[0] for row in bot.db.execute("SELECT image_id FROM imagechat_jobs WHERE state='failed'")}
            keys = [entry['id'] for entry in store.entries()
                    if (entry['id'] in failed if retry else not entry.get('description') or not entry.get('auto_tags'))][:20]
        if not keys:
            return {'success': True, 'message': '没有需要补全的图片', 'queued': 0, 'reused': 0, 'skipped': 0}
        try:
            result = bot.image_chat.enqueue(keys, retry=retry)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from None
        return {'success': True, 'message': f"已排队{result['queued']}张，复用说明{result['reused']}张，跳过{result['skipped']}张", **result}

    @app.post('/api/stickers/annotation-jobs/cancel')
    async def cancel_annotations():
        bot, _ = service(app)
        await bot.image_chat.cancel()
        return {'success': True, 'message': '已取消标注任务；保留图片和已有说明'}

    @app.get("/api/stickers")
    async def library(q: str = "", page: int = 1):
        bot, store = service(app)
        if len(q) > 100 or page < 1:
            raise HTTPException(400, "搜索词或页码无效")
        from .image_chat_tags import searchable
        needle = q.lower()
        items = [entry for entry in store.entries()
                 if needle in searchable(entry).lower()]
        items.sort(key=lambda entry: float(entry.get("created") or 0), reverse=True)
        total = len(items)
        window = items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        for entry in window:
            try:
                entry["thumbnail"] = base64.b64encode(
                    thumbnail_png(store.data(entry["id"]))).decode("ascii")
            except (KeyError, OSError, ValueError):
                # 单张图坏了不该让整个列表打不开：这一格留空，其余照常显示
                entry["thumbnail"] = ""
        return {"items": window, "total": total, "page": page,
                "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
                "collecting": _collecting(bot), "annotation_auto": bot.image_chat.automatic(),
                "annotation_jobs": bot.image_chat.status()}

    @app.post("/api/stickers/import")
    async def import_images(request: Request):
        bot, store = service(app)
        body = await request.json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise HTTPException(400, "每次请选择1～20张图片")
        # 只接受"管理窗上传按钮"落盘的托管文件：不能让接口读任意本机路径。
        # 目录与界面里的 import_plugin_files(ROOT/'data/plugin_uploads', ...) 保持一致，
        # 否则会出现"上传成功但导入被拒"。
        managed = (Path(bot.root) / "data" / "plugin_uploads" / "sticker_admin").resolve()
        pending = []
        total = 0
        try:
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("上传记录无效")
                path = Path(str(item.get("path") or "")).resolve()
                # 只接受"管理窗上传按钮"落盘的托管文件：不能让接口读任意本机路径
                if not path.is_relative_to(managed) or not path.is_file():
                    raise ValueError("请使用管理窗的上传按钮选择图片")
                if path.stat().st_size > MAX_BYTES:
                    raise ValueError(f"单张图片不能超过{MAX_BYTES // 1024 // 1024}MB")
                data = path.read_bytes()
                total += len(data)
                if total > IMPORT_TOTAL_BYTES:
                    raise ValueError("本次导入不能超过64MB")
                # 先逐张解码校验，**一张都不入库**再统一写：否则坏图排在后面时，
                # 前面的好图已经进库了，用户看到的是"报错但库变了"。
                inspect_image(data)
                label = str(item.get("label") or "").strip()
                for char in "[]|\r\n":
                    label = label.replace(char, "_")
                pending.append((data, label[:40]))
            added, new_keys = 0, []
            for data, label in pending:
                entry, fresh = store.put(data, label)
                if fresh:
                    added += 1
                    new_keys.append(entry['id'])
            if new_keys and bot.image_chat.automatic():
                bot.image_chat.enqueue(new_keys, origin='auto')
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None
        return {"success": True, "message": f"已导入{added}张；重复图片已合并", "added": added}

    @app.post("/api/stickers/{key}")
    async def update(key: str, request: Request):
        _, store = service(app)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "设置格式无效")
        try:
            entry = store.update(key, body.get("label"), body.get("description"), tags=body.get('tags'))
        except KeyError:
            raise HTTPException(404, "表情不存在") from None
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None
        return {"success": True, "message": "标签和图片说明已保存", "item": entry}

    @app.delete("/api/stickers/{key}")
    async def remove(key: str):
        _, store = service(app)
        try:
            store.remove(key)
        except KeyError:
            raise HTTPException(404, "表情不存在") from None
        except OSError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"success": True, "message": "已删除机器人本地收藏，不影响「表情包与自定义图库」"}

    @app.post("/api/stickers/{key}/recognize")
    async def recognize(key: str):
        bot, store = service(app)
        try:
            data = store.data(key)
        except (KeyError, OSError):
            raise HTTPException(404, "表情图片不存在") from None
        try:
            saved = await bot.image_chat.recognize(key)
            if not saved:
                raise HTTPException(409, '识别期间人工编辑过说明，保留人工结果；请刷新')
        except HTTPException:
            raise
        except KeyError:
            raise HTTPException(404, '图片已被删除，请刷新列表') from None
        except Exception as exc:
            from .image_input import vision_failure
            raise HTTPException(400, vision_failure(bot.ai, exc)) from None
        entry = store.get(key)
        return {"success": True, "message": "识别完成，已更新图片说明",
                "description": entry.get("description", "")}


def _collecting(bot) -> bool:
    plugin = bot.plugins._plugins.get("sticker_steal")
    return bool(plugin and plugin.enabled)


# 供自检/测试直接构造一个只读写指定目录的库
def open_library(root) -> StickerLibrary:
    return StickerLibrary(root)
