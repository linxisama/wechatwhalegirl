"""机器人本地表情库：按内容去重的收藏夹（索引 + 图片文件）。

与 QQ 版 `sticker_library.py` 的语义保持一致（同一份 index/images 结构、同一个
sha256 主键口径、同样的"发送成功才计次"），但有四处**刻意的差异**：

1. **不依赖 PyQt6**。QQ 版在服务端用 `QImage` 做缩略图与抽帧；微信版的后端
   （`src/wechat_app.py`）是纯 HTTP 服务，把 GUI 框架拖进后端既慢又容易在冻结
   打包时缺图片插件。这里改用 Pillow。
2. **可写数据落在 `%LOCALAPPDATA%\\WhaleGirlWeChat`**（由调用方传入 root），
   不写项目目录 —— 安装目录可能只读。
3. **索引带 `.bak`**：QQ 版索引损坏会让整个库不可用，这里先尝试从上一版备份恢复，
   恢复不了才报错。
4. 下载入口是**注入的协程**（`resolve(item, download)`）：微信的图片要从本地数据库
   按 `shard/local_id` 解密导出，那属于传输层的事，存储层不该知道微信怎么取图。

对外只做"字节进、字节出"，失败一律抛 `ValueError`/`OSError`（上层转成界面提示）。
"""
from __future__ import annotations

import asyncio
import copy
import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from PIL import Image

# 单张图片上限。上传、下载、入库三处必须用同一个值，否则会出现"传上去了却入不了库"。
# 图片读取/储藏的统一上限。历史上这里是 6MB，而识图插件早已放宽到 12MB，
# 结果 6~12MB 的图在取图阶段被接受、保存阶段被拒绝，用户看到的是「识别失败」。
# 现在两边共用这一个常量（识图插件直接 import 它），不要再各写一份。
MAX_BYTES = 12 * 1024 * 1024
# 解码像素上限：防止"几 KB 的解压炸弹"把内存打满。
MAX_PIXELS = 16_000_000
# 提取给视觉模型的帧数上限（与契约里"每次最多 3 张图"一致）。
MAX_FRAMES = 3
ALLOWED_EXT = {'png': 'PNG', 'jpg': 'JPEG', 'jpeg': 'JPEG', 'gif': 'GIF', 'webp': 'WEBP'}
ID = re.compile(r'^[0-9a-f]{64}$')


def _open(data: bytes) -> Image.Image:
    """字节 → PIL 图片；顺便做大小与像素校验（不信任外部图片）。"""
    if not data:
        raise ValueError('图片为空')
    if len(data) > MAX_BYTES:
        raise ValueError(f'图片超过 {MAX_BYTES // 1024 // 1024}MB')
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            raise ValueError('图片像素过大')
        image.load()
    except Exception as exc:
        raise ValueError('图片无法解析或格式不受支持') from exc
    return image


def inspect_image(data: bytes) -> Dict[str, Any]:
    """返回 ``{ext,width,height,animated,frames}``；解不开就抛 ValueError。"""
    image = _open(data)
    fmt = str(image.format or 'PNG').upper()
    ext = next((k for k, v in ALLOWED_EXT.items() if v == fmt), None)
    if ext is None:
        raise ValueError('图片格式不受支持（仅 PNG/JPG/GIF/WebP）')
    if ext == 'jpeg':
        ext = 'jpg'
    frames = int(getattr(image, 'n_frames', 1) or 1)
    return {'ext': ext, 'width': image.width, 'height': image.height,
            'animated': frames > 1, 'frames': frames}


def thumbnail_png(data: bytes, size: int = 96) -> bytes:
    """缩略图（PNG 字节）。列表页每行一张，不能让客户端去拉原图。"""
    image = _open(data).convert('RGBA')
    image.thumbnail((size, size), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, 'PNG')
    return out.getvalue()


def frames_for_vision(data: bytes, want: int = 2, max_side: int = 1024) -> List[str]:
    """把图片转成给视觉模型的 data URL 列表；动图最多抽 ``want`` 帧。

    为什么要抽帧：以图片形式收到或手动上传的 GIF/WebP 动图，视觉模型不一定支持，
    只喂第一帧经常会漏掉"动作"这个最关键的信息（例如拍水花）。抽帧后逐帧说明。
    """
    image = _open(data)
    count = max(1, min(int(want or 1), MAX_FRAMES))
    sources: List[str] = []
    total = int(getattr(image, 'n_frames', 1) or 1)
    count = min(total, count)
    for index in range(count):
        # 均匀取帧，避免前几帧全是开场空白，漏掉后半段的动作与文字。
        image.seek(round(index * (total - 1) / max(1, count - 1)))
        rgba = image.convert('RGBA')
        # 抽帧尺寸跟识图上限走：用户嫌"压得太小、看不清"时可以把上限调大。
        side = max(256, int(max_side or 1024))
        if rgba.width > side or rgba.height > side:
            rgba.thumbnail((side, side), Image.LANCZOS)
        out = io.BytesIO()
        rgba.save(out, 'PNG')
        sources.append('data:image/png;base64,' + base64.b64encode(out.getvalue()).decode('ascii'))
    return sources


def _public_host(host: str) -> bool:
    """只允许解析到公网地址的图片链接。

    为什么必须查：库里的图片可能是"别人发来的链接"，若允许内网/本机地址，
    就等于给了插件一个打本机服务的入口（SSRF）。任何一条解析结果不是全局地址就拒绝。
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        address = info[4][0]
        try:
            candidate = ipaddress.ip_address(address.split('%')[0])
        except ValueError:
            return False
        if not candidate.is_global:
            return False
    return True


async def fetch_image(url: str, limit: int = MAX_BYTES) -> bytes:
    """下载一张图片（SSRF 防护 + 大小上限 + 不跟代理）。"""
    if str(url).startswith('data:'):
        encoded = str(url).split(',', 1)[1] if ',' in url else ''
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError('图片内联数据无法解析') from exc
        if len(raw) > limit:
            raise ValueError('图片超过大小上限')
        return raw
    current = str(url)
    async with httpx.AsyncClient(timeout=8.0, trust_env=False, follow_redirects=False) as client:
        for _ in range(4):
            host = httpx.URL(current).host or ''
            if not _public_host(host):
                raise ValueError('图片链接不能指向本机或内网')
            response = await client.get(current)
            if response.status_code in (301, 302, 303, 307, 308):
                target = response.headers.get('location')
                if not target:
                    raise ValueError('图片跳转没有目标地址')
                current = str(httpx.URL(current).join(target))
                continue
            response.raise_for_status()
            raw = response.content
            if len(raw) > limit:
                raise ValueError('图片超过大小上限')
            return raw
    raise ValueError('图片跳转次数过多')


class StickerLibrary:
    """内容寻址的本地表情库；``root`` 下是 ``index.json`` 与 ``images/``。"""

    def __init__(self, root, max_items: int = 800, max_mb: int = 96):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.images = self.root / 'images'
        self.images.mkdir(exist_ok=True)
        self.index = self.root / 'index.json'
        self.backup = self.root / 'index.json.bak'
        self.max_items = max(10, int(max_items or 800))
        self.max_mb = max(6, int(max_mb or 96))
        self._entries: Dict[str, dict] = self._load()
        self._meanings: Dict[str, str] = {}
        self._sources: Dict[str, str] = {}
        self._recent: Dict[str, bytes] = {}
        self._md5_cache: Dict[str,str] = {}
        self._pending_annotations: Dict[str, dict] = {}

    # ── 索引读写 ────────────────────────────────────────────────────────────
    def _read_index(self, path: Path) -> Dict[str, dict]:
        raw = json.loads(path.read_text('utf-8'))
        if not isinstance(raw, dict):
            raise ValueError('索引不是对象')
        return {k: v for k, v in raw.items() if ID.fullmatch(str(k)) and isinstance(v, dict)}

    def _load(self) -> Dict[str, dict]:
        for path in (self.index, self.backup):
            try:
                if path.is_file():
                    return self._read_index(path)
            except (OSError, ValueError):
                continue
        if self.index.exists():
            raise ValueError('表情库索引损坏，且没有可用备份；请删除或恢复 data 下的 stickers 目录')
        return {}

    def _save(self, entries: Optional[Dict[str, dict]] = None) -> None:
        data = self._entries if entries is None else entries
        payload = json.dumps(data, ensure_ascii=False, indent=1)
        temporary = self.index.with_suffix('.json.tmp')
        try:
            if self.index.is_file():
                # 写之前留一份上一版：索引损坏时还能救回来（QQ 版这里会整个库不可用）。
                self.backup.write_bytes(self.index.read_bytes())
            temporary.write_text(payload, encoding='utf-8')
            os.replace(temporary, self.index)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    # ── 查询 ────────────────────────────────────────────────────────────────
    def entries(self) -> List[dict]:
        return copy.deepcopy(list(self._entries.values()))

    def get(self, key: str) -> dict:
        return copy.deepcopy(self._entries[self.path(key).stem])

    def path(self, key: str) -> Path:
        key = str(key or '')
        if not ID.fullmatch(key):
            raise KeyError(key)
        entry = self._entries.get(key)
        if not entry:
            raise KeyError(key)
        target = (self.images / f"{key}.{entry.get('ext', 'png')}").resolve()
        # 目录穿越防护：文件名由 sha256 组成，但 ext 来自索引文件，仍要确认没跑出去。
        if target.parent != self.images.resolve():
            raise KeyError(key)
        return target

    def data(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    def matching_md5(self, value: str) -> bytes:
        """仅匹配已登记的同一原文件；MD5 是定位线索，SHA256 仍是内容校验。"""
        wanted = str(value or '').casefold()
        if not re.fullmatch(r'[0-9a-f]{32}',wanted):
            return b''
        for key,entry in self._entries.items():
            known = str(entry.get('md5') or self._md5_cache.get(key) or '')
            if known and known!=wanted:
                continue
            try:
                target = self.path(key)
                if not 0<target.stat().st_size<=MAX_BYTES:
                    continue
                raw = target.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=key:
                    continue
                actual = hashlib.md5(raw).hexdigest()
                self._md5_cache[key] = actual
                if actual==wanted:
                    inspect_image(raw)
                    return raw
            except (OSError,KeyError,ValueError):
                continue
        return b''

    def total_bytes(self) -> int:
        return sum(int(item.get('bytes') or 0) for item in self._entries.values())

    # ── 写入 ────────────────────────────────────────────────────────────────
    @staticmethod
    def validate_label(label) -> str:
        label = str(label or '').strip()
        if not label or len(label) > 40 or any(c in label for c in '[]|\r\n'):
            raise ValueError('标签需为1～40字，不能包含方括号、竖线或换行')
        return label

    def put(self, data: bytes, label: str = '', max_items: Optional[int] = None,
            max_mb: Optional[int] = None) -> Tuple[dict, bool]:
        """入库一张图；同一张图（按字节）只留一条，重复的累加 ``seen``。"""
        info = inspect_image(data)
        digest = hashlib.sha256(data).hexdigest()
        now = time.time()
        existing = self._entries.get(digest)
        if existing:
            entries = dict(self._entries)
            entries[digest] = dict(existing, last_seen=now,
                                   seen=int(existing.get('seen') or 1) + 1)
            self._save(entries)
            self._entries = entries
            return self.get(digest), False
        label = self.validate_label(label or ('收集_' + digest[:10]))
        if any(item.get('label') == label for item in self._entries.values()):
            label = f'{label}_{digest[:10]}'[:40]
        target = self.images / f"{digest}.{info['ext']}"
        temporary = target.with_suffix(target.suffix + '.tmp')
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        entry = {'id': digest, 'md5':hashlib.md5(data).hexdigest(), 'label': label, 'description': '', 'ext': info['ext'],
                 'bytes': len(data), 'created': now, 'last_seen': now,
                 'last_used': 0, 'uses': 0, 'seen': 1,
                 'width': info['width'], 'height': info['height'],
                 'animated': info['animated']}
        if digest in self._pending_annotations:
            entry.update(self._pending_annotations.pop(digest))
            entry.update(annotation_source='chat', annotation_updated=now)
        entries = dict(self._entries)
        entries[digest] = entry
        entries = self._evict(entries, max_items=max_items, max_mb=max_mb)
        try:
            self._save(entries)
        except OSError:
            # 索引没写成 → 刚落的图也要撤掉，否则留下永远引用不到的孤儿文件。
            try:
                target.unlink()
            except OSError:
                pass
            raise
        self._entries = entries
        return self.get(digest), True

    def _evict(self, entries: Dict[str, dict], max_items=None, max_mb=None) -> Dict[str, dict]:
        """超出数量/容量时按 (最近使用, 创建时间) 淘汰 —— 越久没用越先走。"""
        limit = max(10, int(max_items or self.max_items))
        cap = max(6, int(max_mb or self.max_mb)) * 1024 * 1024
        while len(entries) > limit or sum(int(i.get('bytes') or 0) for i in entries.values()) > cap:
            victim = min(entries.values(),
                         key=lambda i: (float(i.get('last_used') or 0), float(i.get('created') or 0)))
            entries = {k: v for k, v in entries.items() if k != victim.get('id')}
            try:
                (self.images / f"{victim.get('id')}.{victim.get('ext', 'png')}").unlink()
            except OSError:
                pass
        return entries

    def update(self, key: str, label, description, *, tags=None) -> dict:
        self.path(key)
        clean = self.validate_label(label)
        if any(k != key and item.get('label') == clean for k, item in self._entries.items()):
            raise ValueError('标签重复')
        text = '' if description is None else str(description)
        if len(text) > 1000:
            raise ValueError('图片说明最多1000字')
        entries = dict(self._entries)
        entries[key] = dict(entries[key], label=clean, description=text)
        entries[key]['_edit_revision'] = int(entries[key].get('_edit_revision') or 0) + 1
        if tags is not None:
            from .image_chat_tags import clean_tags
            entries[key]['manual_tags'] = clean_tags(tags)
        self._save(entries)
        self._entries = entries
        return self.get(key)

    def annotate(self, key, result, source='model', expected_revision=None, overwrite=False):
        """原子保存AI说明和维度；任务期间人工编辑过就不回写旧结果。"""
        current = self.get(key)
        if expected_revision is not None and int(current.get('_edit_revision') or 0) != expected_revision:
            return False
        if current.get('description') and not overwrite:
            return False
        entries = dict(self._entries)
        entries[key] = dict(current, **copy.deepcopy(result), annotation_source=source,
                            annotation_updated=time.time())
        self._save(entries)
        self._entries = entries
        return True

    def remove(self, key: str) -> None:
        target = self.path(key)
        entries = {k: v for k, v in self._entries.items() if k != key}
        self._save(entries)
        self._entries = entries
        try:
            target.unlink()
        except OSError:
            pass

    def used(self, keys) -> None:
        """记一次"这张图真的发出去了"。只应由发送成功的回调调用。"""
        now = time.time()
        entries = dict(self._entries)
        changed = False
        for key in keys or []:
            if key in entries:
                entries[key] = dict(entries[key], last_used=now,
                                    uses=int(entries[key].get('uses') or 0) + 1)
                changed = True
        if changed:
            self._save(entries)
            self._entries = entries

    # ── 出站：给聊天用 ──────────────────────────────────────────────────────
    def label_of(self, key: str) -> str:
        return str(self._entries.get(str(key), {}).get('label') or '')

    def path_for(self, key: str) -> Optional[Path]:
        """可发送的本机文件路径；文件不在就返回 None（不谎称能发）。"""
        try:
            target = self.path(key)
        except KeyError:
            return None
        return target if target.is_file() else None

    def record_description(self, key: str, text: str) -> None:
        """把识别结果回填到条目（识别可能晚于收集完成）。"""
        from .image_chat_tags import annotation
        if not ID.fullmatch(str(key or '')) or not str(text or '').strip():
            return
        result = annotation(str(text)[:1000])
        if key in self._entries:
            self.annotate(key, result, source='chat')
        else:
            # 识图和收集并行；只保留有界临时说明，之后同字节入库才回填，绝不自行收图。
            self._pending_annotations[key] = result
            while len(self._pending_annotations) > 50:
                self._pending_annotations.pop(next(iter(self._pending_annotations)))

    async def resolve(self, item: dict, download: Callable[[dict], Any]) -> Dict[str, Any]:
        """消息里的媒体段 → ``{'data','width','height','animated','frames'}``。

        ``download`` 由调用方注入（微信要按 shard/local_id 从本地库解密导出），
        存储层只负责缓存与校验；同一个媒体段并发只下载一次。
        """
        key = self.source_key(item)
        cached = self._recent.get(key)
        if cached is not None:
            info = inspect_image(cached)
            return dict(info, data=cached)
        if key in self._sources and self._sources[key] in self._entries:
            entry_id = self._sources[key]
            path = self.path_for(entry_id)
            if path:
                raw = path.read_bytes()
                self._remember(key, raw)
                return dict(inspect_image(raw), data=raw)
        inflight = getattr(self, '_inflight', None)
        if inflight is None:
            inflight = self._inflight = {}
        task = inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch_item(item, download))
            inflight[key] = task
        try:
            raw = await asyncio.shield(task)
        finally:
            inflight.pop(key, None)
        info = inspect_image(raw)
        self._remember(key, raw)
        return dict(info, data=raw)

    async def _fetch_item(self, item: dict, download: Callable[[dict], Any]) -> bytes:
        url = str(item.get('url') or item.get('link') or '')
        if not url:
            raw = await download(item) if asyncio.iscoroutinefunction(download) else download(item)
        else:
            raw = await fetch_image(url)
        if not raw:
            raise ValueError('图片不可读或已过期')
        return bytes(raw)

    def _remember(self, key: str, data: bytes) -> None:
        self._recent[key] = data
        while len(self._recent) > 8:
            self._recent.pop(next(iter(self._recent)))

    @staticmethod
    def source_key(item: dict) -> str:
        """媒体段的稳定指纹：优先用微信的 shard+local_id，其次 URL。"""
        item = item or {}
        shard, local = str(item.get('shard') or ''), str(item.get('local_id') or '')
        if shard or local:
            return f'{shard}:{local}'
        raw = str(item.get('url') or item.get('file') or item.get('name') or '')
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]

    def bind(self, item: dict, entry_id: str) -> None:
        """记住"这个媒体段对应库里哪一条"，供使用计次与说明回填。"""
        key = self.source_key(item)
        self._sources[key] = entry_id
        while len(self._sources) > 2000:
            self._sources.pop(next(iter(self._sources)))
        text = self._meanings.get(key)
        if text:
            self.record_description(entry_id, text)
