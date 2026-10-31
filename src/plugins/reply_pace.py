"""回复节奏：收到消息后先"想一会儿"再回，别秒回。

为什么需要：秒回是最明显的机器人特征。真人看到消息、读完、想一下、打字，
通常有几秒间隔；同一个群里连续"零延迟"回复，既不像人，也更容易被平台按行为模式识别。
这里只做**等待**，不改发送内容、不改频率上限，也不碰封号红线里的任何接口。

与"回复速度三档"的关系：三档（极速/快速/正常）说的是"取多少上下文、留多少输出预算"，
影响的是生成耗时；本插件控制的是**开口前的等待**，两者互不冲突。
"""
from __future__ import annotations

import random

from . import PluginBase, PluginMeta, FieldSchema


class ReplyPace(PluginBase):
    META = PluginMeta(
        id="reply_pace",
        name="回复节奏（别秒回）",
        description="收到消息后随机等一小会儿再回，降低「秒回」这种机器人特征。"
                    "只影响等待时间，不改内容、不改发送上限；极速档下不生效。",
        category="utility",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用回复节奏"),
            "minDelay": FieldSchema(type="int", label="最少等几秒", min=0, max=60),
            "maxDelay": FieldSchema(type="int", label="最多等几秒", min=0, max=120),
            "longTextExtra": FieldSchema(type="int", label="对方消息较长时再多等几秒", min=0, max=30,
                                         description="超过 30 个字的提问通常要想一想，加一点更像真人在读。"),
        },
        default_settings={"enabled": False, "minDelay": 2, "maxDelay": 6, "longTextExtra": 1},
    )

    def on_pre_ai(self, message, conversation, runtime):
        if not self.get("enabled"):
            return None
        try:
            low = max(0, int(self.get("minDelay", 2) or 0))
            high = max(low, int(self.get("maxDelay", 6) or 0))
            extra = max(0, int(self.get("longTextExtra", 1) or 0))
        except (TypeError, ValueError):
            low, high, extra = 2, 6, 1
        delay = random.uniform(low, high) if high > low else float(low)
        if extra and len(str(message.get("text") or "")) > 30:
            delay += random.uniform(0, extra)
        if delay <= 0:
            return None
        return {"delay": round(delay, 2)}
