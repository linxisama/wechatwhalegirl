"""sticker_cadence.py - 每 N 次回复附一张真实图库图片（对齐 QQ 版「每三次表情」）。

跟旧版的区别：旧版只往提示词里塞一句"可以考虑加个 emoji"，**永远发不出图片**，
用户看到的就是"不触发自动回复表情"。现在按 QQ 版的语义做：

* 每个会话（同一群/同一好友，按账号隔离）独立计数；
* `on_sent` 里**发送成功才 +1**，失败/超时不算（下次该附图还是附）；
* 到点后从图库里**轮转**取一张真图附加到这条回复上（不是让模型描述图片）；
* 回复里已经有图（模型自己写了 `[[表情:标签]]`）就不再追加，避免一条回复两张图；
* 图库空/取不到图时只回文字，并且**不谎称**发了图。

主动外发的红线不受影响：图片只在"回复某人"的这条消息上附带，
本插件从不自己挑会话、也不定时外发。
"""
from __future__ import annotations

import re

from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema


def _tokens(text):
    """粗分词：中文取相邻两字，英文数字取整词。够用来做"像不像"的判断。"""
    raw = str(text or '').lower()
    words = re.findall(r'[a-z0-9]{2,}', raw)
    han = re.findall(r'[\u4e00-\u9fff]+', raw)
    grams = []
    for run in han:
        grams.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return set(words) | set(grams)


def rank_labels(labels, descriptions, text, start=0):
    """按"名字 + 含义"给图库候选排序。

    为什么：原来纯轮转，聊到"下雨"也可能配一张笑脸。名字命中权重最高（3），
    含义说明命中次之（1）；同分保持轮转顺序，用户仍能预期"下次大概换一张"。
    """
    order = list(labels)[start:] + list(labels)[:start]
    terms = _tokens(text)
    if not terms:
        return order
    scored = []
    for index, label in enumerate(order):
        score = 0
        score += 3 * len(terms & _tokens(label))
        score += len(terms & _tokens(descriptions.get(label, '')))
        scored.append((score, index, label))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored]


class StickerCadence(PluginBase):
    META = PluginMeta(
        id="sticker_cadence",
        name="每三次表情",
        description="每个会话每成功回复N次，附上一张机器人本地图库里的真实图片；发送失败不计数，图库为空时只回文字",
        category="personality",
        enabled_by_default=False,
        config_schema={
            "enabled":    FieldSchema(type="bool", label="启用"),
            "sendEveryN": FieldSchema(type="int",  label="每N条回复附图", min=1, max=100),
        },
        default_settings={"enabled": False, "sendEveryN": 3},
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {room: 成功回复次数}；落盘，重启后接着数（QQ 版同样持久化）
        self._counts: dict = self.load_state("counts.json", {})

    def _room(self, message: dict, runtime) -> str:
        try:
            from ..chat_state import scope_of
            return str(scope_of(message, runtime))
        except Exception:
            return f"{message.get('kind', '?')}:{message.get('target', '?')}"

    def _every(self) -> int:
        try:
            return max(1, int(self.get("sendEveryN", 3)))
        except (TypeError, ValueError):
            return 3

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if not self.get("enabled") or message.get("notice"):
            return None
        count = int(self._counts.get(self._room(message, runtime), 0))
        if (count + 1) % self._every():
            return None
        return {"extra_system": (
            "本轮会附上一张图库里的真实图片（由程序附在回复后面），"
            "所以你正常回文字即可，不要用颜文字冒充图片，也不要描述这张图。"
        )}

    async def on_post_reply(self, message: dict, reply: str, runtime):
        if not self.get("enabled") or message.get("notice"):
            return reply
        count = int(self._counts.get(self._room(message, runtime), 0))
        every = self._every()
        if (count + 1) % every:
            return reply
        existing = getattr(runtime, "reply_meta", None)
        if isinstance(existing, dict) and any(
                isinstance(item, dict) and item.get("type") == "image"
                for item in (existing.get("media") or [])):
            return reply
        library = runtime.plugins._plugins.get("image_library")
        if library is None or not library.enabled:
            return reply
        try:
            entries = library.all_entries(runtime)
        except Exception:
            return reply
        labels = list(entries)
        if not labels:
            return reply
        # 先按"名字+含义"跟当前话题的相关度排序，再按轮转顺序兜底；
        # 全都不相关时就是纯轮转，和以前一样。
        start = (count // every) % len(labels)
        descriptions = {}
        store_for_text = getattr(runtime, "stickers", None)
        if store_for_text:
            try:
                descriptions = {str(entry.get("label") or ""): str(entry.get("description") or "")
                                for entry in store_for_text.entries()}
            except Exception:
                descriptions = {}
        for label in rank_labels(labels, descriptions, message.get("text", ""), start):
            try:
                media = library.media_for(label, runtime)
            except (ValueError, OSError, KeyError):
                continue
            meta = dict(existing) if isinstance(existing, dict) else {}
            meta["media"] = list(meta.get("media") or []) + [media]
            store = getattr(runtime, "stickers", None)
            if store:
                try:
                    ids = [entry["id"] for entry in store.entries()
                           if str(entry.get("label")) == label]
                    if ids:
                        meta["library_ids"] = list(meta.get("library_ids") or []) + ids
                        message["_library_ids"] = ids
                except Exception:
                    pass
            runtime.reply_meta = meta
            return reply
        return reply

    def on_sent(self, message: dict, reply: str, runtime) -> None:
        if not self.get("enabled") or message.get("notice"):
            return
        room = self._room(message, runtime)
        self._counts[room] = int(self._counts.get(room, 0)) + 1
        # 只保留最近 2000 个会话，避免长时间运行后状态文件无限增长
        if len(self._counts) > 2000:
            self._counts = dict(list(self._counts.items())[-2000:])
        try:
            self.save_state("counts.json", self._counts)
        except Exception:
            pass
