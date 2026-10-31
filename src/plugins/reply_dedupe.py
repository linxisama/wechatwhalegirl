"""reply_dedupe.py - 打断机器人自己"复读"：记住每个会话最近回过什么，
在下一轮生成前提醒模型"这些你已经说过，换个说法"，避免连发同一条回复。

旧版微信机器人靠 4 层防重复（指纹去重 / avoid_answer / 生成后查重改写 / 本地变体兜底）。
本插件走更简单的两层（够用且不碰封号红线）：
  1. on_sent：每成功发一条，把该会话最近 N 条回复存进 state.json（按会话隔离，带时间戳）；
  2. on_pre_ai：把"最近回过的"拼成 extra_system 注入，提示模型换说法。
窗口外自动过期；单会话上限 N 条，不无限膨胀。
"""
from __future__ import annotations

import time
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema


class ReplyDedupe(PluginBase):
    META = PluginMeta(
        id="reply_dedupe",
        name="回复去重",
        description="记住每个会话最近回过的内容，提示模型换说法，避免重复回复",
        category="rhythm",
        enabled_by_default=True,
        config_schema={
            "enabled":     FieldSchema(type="bool", label="启用"),
            "recentKeep":  FieldSchema(type="int", label="每个会话保留最近几条", min=1, max=50),
            "windowSec":   FieldSchema(type="int", label="统计窗口(秒)", min=60, max=86400),
        },
        default_settings={"enabled": True, "recentKeep": 8, "windowSec": 6 * 3600},
    )

    # {room: [[text, ts], ...]}  新→旧
    def __init__(self, manager):
        super().__init__(manager)
        self._store = self.load_state("state.json", {})

    def _room(self, message: dict) -> str:
        return f"{message.get('kind', '?')}:{message.get('target', '?')}"

    def _recent(self, room: str) -> list:
        keep = int(self.get("recentKeep", 8))
        window = float(self.get("windowSec", 6 * 3600))
        now = time.time()
        items = self._store.get(room, [])
        # 时间戳倒序过滤 + 截断
        items = [it for it in items
                 if isinstance(it, list) and len(it) >= 2
                 and now - float(it[1]) <= window]
        return items[:keep]

    def on_sent(self, message: dict, reply: str, runtime) -> None:
        if not self.get("enabled"):
            return
        text = str(reply or "").strip()
        if not text:
            return
        room = self._room(message)
        items = self._store.get(room, [])
        items.insert(0, [text, time.time()])
        items = items[: int(self.get("recentKeep", 8)) + 4]  # 留点余量再按窗口裁
        self._store[room] = items
        # 顺手清掉别的会话里过期太久的记录，避免 state 文件膨胀
        window = float(self.get("windowSec", 6 * 3600))
        now = time.time()
        for r in list(self._store):
            kept = [it for it in self._store[r]
                    if isinstance(it, list) and now - float(it[1]) <= window]
            if kept:
                self._store[r] = kept
            else:
                del self._store[r]
        try:
            self.save_state("state.json", self._store)
        except Exception:
            pass  # 写盘失败不阻断发送

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if not self.get("enabled"):
            return None
        recent = self._recent(self._room(message))
        if not recent:
            return None
        # 原来只放前 5 条、每条截 40 字 —— 聊天密集时提示词里只剩 200 字，
        # 对 0.95 的温度根本压不住复读。放大到 8 条 × 80 字。
        lines = "、".join(str(it[0])[:80] for it in recent[:8])
        return {"extra_system": (
            f"本会话你最近回过：{lines}。"
            "这次请换一种说法或补充新内容，不要原样重复上面任何一句。"
        )}

    def recent_replies(self, message: dict) -> list:
        """本会话最近回过的原文（新→旧）。供运行时做"生成后查重"。"""
        return [str(it[0]) for it in self._recent(self._room(message)) if str(it[0]).strip()]
