"""burst_merge.py - 连发合并: delay AI call when same sender just sent a message."""
from __future__ import annotations
import time
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class BurstMerge(PluginBase):
    META = PluginMeta(
        id="burst_merge",
        name="连发合并",
        description="同一人在短时间内连续发消息时，等待其发完再回复",
        category="rhythm",
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "capMs":   FieldSchema(type="int",  label="合并窗口(ms)", min=500, max=80000),
        },
        default_settings={
            "enabled": True,
            "capMs":   8000,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # key: (room_id, sender_id) -> last_seen timestamp (float)
        self._last_msg: dict = {}

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if message.get('_burst_handled'):
            return None
        if not self.get("enabled"):
            return None
        # 连发合并只对群聊生效 —— 私聊不需要等待对方"发完"
        if message.get("kind") != "group":
            return None
        try:
            cap_sec = self.get("capMs", 8000) / 1000.0
            room = message.get("target", "")
            sender_id = message.get("sender_id", "")
            key = (room, sender_id)
            now = time.time()
            last = self._last_msg.get(key)
            self._last_msg[key] = now
            if last is not None:
                elapsed = now - last
                if elapsed < cap_sec:
                    remaining = cap_sec - elapsed
                    return {"delay": remaining}
        except Exception:
            pass
        return None
