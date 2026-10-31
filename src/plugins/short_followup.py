"""short_followup.py - 短期跟聊: hint for briefer replies within a follow-up window."""
from __future__ import annotations
import time
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class ShortFollowup(PluginBase):
    META = PluginMeta(
        id="short_followup",
        name="短期跟聊",
        description=(
            "对方刚和你说过话时，提示 AI 用更短自然的方式回复。"
            "「句数上限」会真正写进提示词（以前那一项叫「最大token」，"
            "实际提示里写死了不超过 2 句，拨了没有任何效果）。"
        ),
        category="rhythm",
        enabled_by_default=True,
        config_schema={
            "enabled":           FieldSchema(type="bool", label="启用"),
            "followupWindowSec": FieldSchema(type="int",  label="跟聊窗口(秒)", min=30, max=86400),
            "maxSentences":      FieldSchema(
                type="int", label="跟聊时最多几句", min=1, max=10,
                description="默认 2。会写进给模型的提示词。",
            ),
            'strictLimit':FieldSchema(type='bool',label='严格限制跟聊回复句数',description='开启后按句末标点截取，默认关闭时仅提示模型控制句数。'),
        },
        default_settings={
            "enabled":           True,
            "followupWindowSec": 300,
            "maxSentences":      2,
            'strictLimit':False,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {(room, sender_id): last_reply_time}
        self._last_replied: dict = {}

    def sentence_cap(self) -> int:
        # 优先用新键；旧配置里存的是 maxTokens（当年叫"最大token"，实际当句数用），
        # 所以只有在新键缺省时才回退读它。不能写成 self.get("maxSentences", 回退)：
        # default_settings 里永远有 maxSentences，回退分支永远不会走到。
        settings = self.settings if isinstance(self.settings, dict) else {}
        value = settings.get("maxSentences")
        if value is None:
            value = settings.get("maxTokens", self.META.default_settings.get("maxSentences", 2))
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return 2

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        if not self.get("enabled"):
            return None
        try:
            room      = str(message.get("target", ""))
            sender_id = str(message.get("sender_id", ""))
            if not sender_id:
                return None
            real = runtime is not None and hasattr(runtime,'db') and hasattr(runtime,'style')
            if real:
                from ..chat_state import persona_key
                fingerprint=persona_key(runtime.style.get().prompt_text())
                key=(runtime.mode,runtime.account(),fingerprint,room,sender_id)
            else:
                key=(room,sender_id)
            now       = time.time()
            window    = self.get("followupWindowSec", 300)
            last      = self._last_replied.get(key)
            if real and last is None:
                row=runtime.db.execute("SELECT max(created) FROM inbox WHERE mode=? AND account=? AND room=? "
                    "AND state='sent' AND json_extract(payload,'$.sender_id')=? AND json_extract(payload,'$._persona_key')=?",
                    (runtime.mode,runtime.account(),message.get('kind','group')+':'+room,sender_id,fingerprint)).fetchone()
                last=row[0] if row else None
            if not real:
                self._last_replied[key] = now
            if last is not None and now - last < window:
                cap = self.sentence_cap()
                message['_short_followup_cap']=cap
                return {
                    "extra_system": f"对方刚才刚和你说过话，这是短期跟聊，回应要简短自然，不超过{cap}句。"
                }
        except Exception:
            pass
        return None

    def on_sent(self,message,reply,runtime):
        if not message.get('sender_id') or not message.get('_persona_key'):
            return
        # 发送期间可能切换人设，必须归属生成本条回复时的人设，不能读取当前选择。
        key=(runtime.mode,runtime.account(),message['_persona_key'],str(message.get('target','')),str(message.get('sender_id','')))
        self._last_replied[key]=time.time()
        if len(self._last_replied)>2000:
            cutoff=time.time()-float(self.get('followupWindowSec',300))
            self._last_replied={k:v for k,v in self._last_replied.items() if v>=cutoff}

    async def on_post_reply(self,message,reply,runtime):
        return self.limit_reply(message,reply)

    def limit_reply(self,message,reply):
        if not self.get('strictLimit',False) or not message.get('_short_followup_cap'):
            return reply
        import re
        parts=re.findall(r'.+?(?:[。！？!?]+(?:[”’」』])?|[.](?=\s|$)|$)',reply,re.S)
        return ''.join(parts[:message['_short_followup_cap']]) if parts else reply
