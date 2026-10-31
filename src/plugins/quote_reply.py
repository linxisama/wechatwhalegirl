"""quote_reply.py - 引用规范: hint AI to use 「」 style for quotations."""
from __future__ import annotations
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class QuoteReply(PluginBase):
    META = PluginMeta(
        id="quote_reply",
        name="引用规范",
        description="提示AI使用「」引号格式而非>引号",
        category="style",
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
        },
        default_settings={
            "enabled": True,
        },
    )

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if not self.get("enabled"):
            return None
        return {
            "extra_system": "如果引用对方的话，用「...」格式，不要用>引号。"
        }
