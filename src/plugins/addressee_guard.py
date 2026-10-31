"""addressee_guard.py - 点名判定: hint when bot's name is mentioned without @.

被叫昵称来自 ``runtime.aliases()``（人设显示名 + 配置别名），不硬编码任何名字。
"""
from __future__ import annotations
from typing import Optional
from ..text_matching import contains_keyword
from . import PluginBase, PluginMeta, FieldSchema


class AddresseeGuard(PluginBase):
    META = PluginMeta(
        id="addressee_guard",
        name="点名判定",
        description="有人提到机器人名字但没有@时，提示AI考虑是否回应",
        category="intelligence",
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
        if message.get("at"):
            return None
        if message.get("kind") != "group":
            return None
        try:
            text = message.get("text", "")
            # 机器人被叫的昵称列表由运行时给人设显示名 + 配置别名，
            # 插件自己硬编码一份（以前的 ["鲸鱼娘","机器人"]）会在换人设后失效。
            bot_names = []
            try:
                provided = runtime.aliases()
                if isinstance(provided, (list, tuple)):
                    bot_names.extend(str(v).strip() for v in provided if str(v).strip())
            except Exception:
                pass
            if any(contains_keyword(text, n) for n in bot_names):
                return {
                    "extra_system": "有人提到了你的名字，你可以考虑是否回应。"
                }
        except Exception:
            pass
        return None
