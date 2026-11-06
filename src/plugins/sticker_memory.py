"""sticker_memory.py - 表情包识别缓存与纯逻辑指纹工具。

移植自 JS 插件 ``sticker-memory``（QQ Agent，MIT）。

它解决的问题：同一个表情包反复出现时不必每次都让模型看图（省时间、省 token）——
按图片指纹记住它的含义，下次直接复用。

微信侧没有 QQ 那种稳定的 ``file_uuid``/``sub_type``，图片只能先下载到本机，
所以缓存键改成**下载后图片内容的 sha256**（内联 base64 图片同样是字节哈希）。
这样同一张图无论转发多少次、出现在哪个群，指纹都一致；而两张不同的图绝不会撞键。
照片默认不缓存，多图消息仍逐次识别，不因部分缓存命中而整条跳过。
缓存有容量上限与复核期限；旧版纯逻辑接口保留供兼容调用。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema
from ..prompt_budget import clip_note, sticker_max_items, sticker_meaning_chars

# 存档/渲染里的媒体占位形式（见契约第 7 节）：[表情] [图片] [语音] [文件:名字]
_STICKER_TAG = re.compile(r'^\[(动画表情|表情|动图|GIF|贴纸)')
_IMAGE_TAG = re.compile(r'^\[(图片|照片|截图|视频|文件)')
# 表情包的典型尺寸：很小、且通常是方图。截图/照片会明显更大。
DEFAULT_STICKER_MAX_BYTES = 512 * 1024
DEFAULT_STICKER_MAX_SIDE = 400


def fingerprint(data: bytes) -> str:
    """按图片字节算指纹（与原版"按图片指纹记住"同口径）。"""
    if not data:
        return ""
    return hashlib.sha256(data).hexdigest()


def looks_like_sticker(tag: str = "", byte_size: Optional[int] = None,
                       width: Optional[int] = None, height: Optional[int] = None,
                       sticker_only: bool = True) -> bool:
    """判断这是不是"值得记住"的表情包。

    ``sticker_only=True``（默认）时只认表情包/动图：
    - 标签明确是表情/动图/贴纸 → 是；
    - 标签明确是图片/照片/截图/视频/文件 → 否（这些图再出现时内容可能已经变了，
      复用旧含义会答错）；
    - 没有明确的尺寸信息时 → **否**（宁可不记，也不要把照片当表情包）；
    - 有尺寸信息且不超过表情包的典型大小 → 是。

    ``sticker_only=False`` 时除明确排除的（照片/截图/视频/文件）之外都记。
    """
    tag = str(tag or "").strip()
    if _STICKER_TAG.match(tag):
        return True

    excluded = bool(_IMAGE_TAG.match(tag)) or tag.startswith("[视频") or tag.startswith("[文件")
    if excluded:
        # 「图片」在 sticker_only=False 时可以记；照片/截图/视频/文件一律不记
        if not (_IMAGE_TAG.match(tag) and tag.startswith("[图片")):
            return False
        if sticker_only:
            return False

    if not sticker_only:
        return True

    if byte_size is None and width is None and height is None:
        return False
    if byte_size is not None and byte_size > DEFAULT_STICKER_MAX_BYTES:
        return False
    if width is not None and width > DEFAULT_STICKER_MAX_SIDE:
        return False
    if height is not None and height > DEFAULT_STICKER_MAX_SIDE:
        return False
    return True


class StickerMemoryCache:
    """表情包记忆库：{指纹: {"meaning": str, "last_used": ts, "last_chat": str, "updated": ts}}。"""

    def __init__(self, max_entries: int = 2000):
        self.max_entries = max(1, int(max_entries))
        self._entries: dict = {}

    def __len__(self) -> int:
        return len(self._entries)

    def remember(self, digest: str, meaning: str, chat_key: str = "", now: Optional[float] = None) -> None:
        if not digest:
            return
        moment = time.time() if now is None else now
        meaning = str(meaning or "").strip()
        if not meaning:
            return
        self._entries[digest] = {
            "meaning": meaning,
            "last_used": moment,
            "last_chat": str(chat_key or ""),
            "updated": moment,
        }
        self._prune()

    def forget(self, digest: str) -> bool:
        return self._entries.pop(digest, None) is not None

    def lookup(self, digest: str) -> Optional[dict]:
        return self._entries.get(digest)

    def is_fresh(self, digest: str, chat_key: str, same_chat_hours: float = 24.0,
                 cross_chat_days: float = 7.0, now: Optional[float] = None) -> bool:
        """记忆是否还在"免看图"有效期内。

        同一个会话用 ``same_chat_hours``（默认 24 小时）；换到别的会话用
        ``cross_chat_days``（默认 7 天 —— 表情包通常跨群通用，所以给得更长）。
        两个窗口都填 0 表示永不重新确认（最省 token）。
        """
        entry = self._entries.get(digest)
        if not entry:
            return False
        moment = time.time() if now is None else now
        age = moment - float(entry.get("updated") or 0)
        if str(entry.get("last_chat") or "") == str(chat_key or ""):
            limit = max(0.0, float(same_chat_hours)) * 3600.0
        else:
            limit = max(0.0, float(cross_chat_days)) * 86400.0
        if limit <= 0:
            return True
        return age <= limit

    def touch(self, digest: str, chat_key: str = "", now: Optional[float] = None) -> None:
        entry = self._entries.get(digest)
        if not entry:
            return
        entry["last_used"] = time.time() if now is None else now
        if chat_key:
            entry["last_chat"] = str(chat_key)

    def hit_ratio(self, digests, chat_key: str, same_chat_hours: float = 24.0,
                  cross_chat_days: float = 7.0, now: Optional[float] = None) -> float:
        """一批图片里命中记忆库（且在有效期内）的比例。"""
        items = [d for d in (digests or []) if d]
        if not items:
            return 0.0
        hits = sum(1 for d in items if self.lookup(d) and self.is_fresh(
            d, chat_key, same_chat_hours, cross_chat_days, now))
        return hits / len(items)

    def should_skip_look(self, digests, chat_key: str, threshold: float = 0.5,
                          same_chat_hours: float = 24.0, cross_chat_days: float = 7.0,
                          now: Optional[float] = None) -> bool:
        """一条消息里命中比例达到门槛就整条跳过看图。"""
        return self.hit_ratio(digests, chat_key, same_chat_hours,
                              cross_chat_days, now) >= max(0.0, float(threshold))

    def known_notes(self, digests, chat_key: str, same_chat_hours: float = 24.0,
                    cross_chat_days: float = 7.0, now: Optional[float] = None,
                    meaning_chars: Optional[int] = None,
                    max_items: Optional[int] = None) -> list:
        """给出可直接写进上下文的"已知表情包"提示行。

        每条 meaning 与总条数都有上限（默认 40 字 / 3 条，可用 prompt_budget.
        stickerMeaningChars / stickerMaxItems 覆盖）。为什么必须在这里封顶：记下来的
        说明最长 3000 字，这条路径一旦接进提示词，20 张图就能把整段预算吃穿——
        与本轮表情包段落 800 字硬预算同源，所以两边用同一套上限。
        """
        limit_chars = sticker_meaning_chars() if meaning_chars is None else meaning_chars
        limit_items = sticker_max_items() if max_items is None else max_items
        notes = []
        for digest in digests or []:
            if not digest:
                continue
            entry = self.lookup(digest)
            if entry and self.is_fresh(digest, chat_key, same_chat_hours, cross_chat_days, now):
                meaning = clip_note(entry.get('meaning'), limit_chars)
                if meaning:
                    notes.append(f"[已知表情包：{meaning}]")
                self.touch(digest, chat_key, now)
                if len(notes) >= limit_items:
                    break
        return notes

    def export(self) -> dict:
        return {"entries": dict(self._entries)}

    def load(self, raw) -> None:
        """从存档恢复；损坏结构一律忽略（不能因为存档坏了就不工作）。"""
        try:
            entries = raw.get("entries") if isinstance(raw, dict) else None
            if not isinstance(entries, dict):
                return
            clean = {}
            for digest, entry in entries.items():
                if isinstance(digest, str) and isinstance(entry, dict) and str(entry.get("meaning") or "").strip():
                    clean[digest] = {
                        "meaning": str(entry.get("meaning")),
                        "last_used": float(entry.get("last_used") or 0),
                        "last_chat": str(entry.get("last_chat") or ""),
                        "updated": float(entry.get("updated") or 0),
                    }
            self._entries = clean
            self._prune()
        except Exception:
            return

    def _prune(self) -> None:
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries, key=lambda key: float(self._entries[key].get("last_used") or 0))
            self._entries.pop(oldest, None)


class StickerMemory(PluginBase):
    META = PluginMeta(
        id="sticker_memory",
        name="表情包识别记忆",
        description=(
            "缓存视觉模型成功识别的图片含义，按**图片内容指纹**（下载后字节的 sha256）复用。"
            "微信侧拿不到协议级的表情标记与图片尺寸，因此只按内容指纹去重："
            "同一张图重复出现时直接复用，绝不因文件名或消息号相同就误判成同一张。"
            "识别失败不写入；多图消息仍正常识图，避免漏看未缓存的图。"
        ),
        category="media",
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用表情包记忆库"),
            "stickerOnly": FieldSchema(
                type="bool", label="只记表情包（推荐）",
                description="截图、照片、带文字的图一律不记——这些图再出现时内容可能已经变了，复用旧含义反而会答错。"
                            "微信侧拿不到图片尺寸，所以识别链只按内容指纹记录确实被识别过的单张图片。",
            ),
            "maxCacheEntries": FieldSchema(
                type="int", label="记忆库上限（条）", min=10, max=100000,
                description="超过后先淘汰最久没被用到的条目，默认 2000。",
            ),
            "recheckSameChatHours": FieldSchema(
                type="float", label="同一个会话多久后重新确认（小时）", min=0, max=8760,
                description="默认 24。填 0 = 永不重新确认（最省 token）。",
            ),
            "recheckCrossChatDays": FieldSchema(
                type="float", label="跨会话多久后重新确认（天）", min=0, max=3650,
                description="默认 7。表情包通常跨群通用，所以给得更长。",
            ),
            "skipThreshold": FieldSchema(
                type="float", label="跳过识图的门槛（0~1）", min=0.0, max=1.0,
                description="保留旧设置兼容；当前收图链仅复用单图结果，多图不会整条跳过。",
            ),
        },
        default_settings={
            "enabled": True,
            "stickerOnly": True,
            "maxCacheEntries": 2000,
            "recheckSameChatHours": 24.0,
            "recheckCrossChatDays": 7.0,
            "skipThreshold": 0.5,
        },
    )

    STATE_FILE = "sticker_memory.json"

    def __init__(self, manager):
        super().__init__(manager)
        self._cache: Optional[StickerMemoryCache] = None

    # ── 记忆库 ────────────────────────────────────────────────────────────
    def cache(self) -> StickerMemoryCache:
        if self._cache is None:
            self._cache = StickerMemoryCache(int(self.get("maxCacheEntries", 2000) or 2000))
            self._cache.load(self.load_state(self.STATE_FILE, {}))
        self._cache.max_entries = int(self.get('maxCacheEntries',2000))
        self._cache._prune()
        return self._cache

    def persist(self) -> None:
        try:
            self.save_state(self.STATE_FILE, self.cache().export())
        except Exception:
            pass

    def remember(self, data: bytes, meaning: str, chat_key: str = "",
                 sticker_only: Optional[bool] = None) -> bool:
        """记下一个表情包的含义；不是表情包（按设置）就不记。

        ``sticker_only`` 省略时用设置里的 ``stickerOnly``（注意必须真的传下去，
        否则「关掉只记表情包」这个开关等于失效）。
        """
        digest = fingerprint(data)
        if not digest:
            return False
        if sticker_only is None:
            sticker_only = bool(self.get("stickerOnly", True))
        if not looks_like_sticker(sticker_only=sticker_only):
            return False
        self.cache().remember(digest, meaning, chat_key)
        self.persist()
        return True

    def notes_for(self, digests, chat_key: str, config=None) -> list:
        """给这批图片生成"已知表情包"提示（同时刷新使用时间）。

        ``config`` 是宿主的配置对象；传了就认 prompt_budget.stickerMeaningChars /
        stickerMaxItems 两个覆盖值，不传则用默认 40 字 / 3 条。
        """
        return self.cache().known_notes(
            digests, chat_key,
            float(self.get("recheckSameChatHours", 24.0) or 0),
            float(self.get("recheckCrossChatDays", 7.0) or 0),
            meaning_chars=sticker_meaning_chars(config),
            max_items=sticker_max_items(config),
        )

    def should_skip_look(self, digests, chat_key: str) -> bool:
        return self.cache().should_skip_look(
            digests, chat_key,
            float(self.get("skipThreshold", 0.5) or 0),
            float(self.get("recheckSameChatHours", 24.0) or 0),
            float(self.get("recheckCrossChatDays", 7.0) or 0),
        )

    def key_for(self, content):
        """按**图片内容**算缓存键。

        ``content`` 可以是 ``bytes``，也可以是 ``data:image/...;base64,...`` 字符串
        （识图插件下载完图片后交出来的形式）。QQ 版用的 ``sub_type`` / ``file_uuid``
        是协议专有字段，微信侧不存在，用了只会让每一张图都算出空键（等于缓存失效）。

        拿不到任何图片字节时返回空串——宁可不缓存，也不要拿"文件名/URL"这种会变的
        东西当键，那会把两张不同的图当成同一张、复用错误的含义。
        """
        if isinstance(content, (bytes, bytearray)):
            return fingerprint(bytes(content))
        text = str(content or "").strip()
        if text.startswith('data:'):
            _, _, encoded = text.partition(',')
            try:
                import base64
                return fingerprint(base64.b64decode(encoded, validate=True))
            except Exception:
                return ''
        return ''

    def _description_key(self, content, chat_key, variant=''):
        digest = self.key_for(content)
        if digest and variant:
            # 旧工具接口保留；视觉识别缓存显式绑定账号、会话和处理配置。
            digest = hashlib.sha256((digest + '\0' + chat_key + '\0' + variant).encode()).hexdigest()
        return digest

    def cached(self, content, chat_key, variant=''):
        digest = self._description_key(content, chat_key, variant)
        if not digest or not self.cache().is_fresh(digest,chat_key,
                float(self.get('recheckSameChatHours',24)),float(self.get('recheckCrossChatDays',7))):
            return ''
        entry = self.cache().lookup(digest)
        if entry:
            self.cache().touch(digest,chat_key)
            self.persist()
            return entry['meaning']
        return ''

    def record_description(self, content, meaning, chat_key, variant=''):
        digest = self._description_key(content, chat_key, variant)
        if digest and meaning:
            self.cache().remember(digest,str(meaning)[:3000],chat_key)
            self.persist()
