"""quick_reply.py - 寒暄秒回：不占用 AI 调用，直接省下数秒等待。

为什么要有这个插件：一句「在吗」要等模型几秒才回，体验很差；而寒暄的答案
是固定的，本地回掉最划算。

触发范围是这里最关键的设计（关系到封号风险）：
- 私聊：对方就是来找机器人说话的，直接秒回；
- 群聊：**只有**被 @ 到、或者正文里出现了机器人的昵称才秒回，其余群消息一律
  不秒回 —— 群里「晚安」「谢谢」满屏飞，见一个就插一句会被当成刷屏机器人。
"""

from __future__ import annotations

import random
import re
import time

from ..text_matching import contains_keyword
from . import PluginBase, PluginMeta, FieldSchema

# ---------------------------------------------------------------------------
# 寒暄词表（旧项目 src/message_handler.py 第 618-652 行原文移植）
#
# 原文是 4 组 (触发词, 候选文案)；这里按需求给 4 组起了名字：
# 打招呼 / 称呼 / 道别 / 关心（第 4 组原文是"谢谢/感谢"，归到关心这一类）。
# 文案保持原样，不做改写。
# ---------------------------------------------------------------------------
QUICK_REPLY_GROUPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "打招呼",
        ("在吗", "在么", "在嘛", "在不在", "你在吗", "在不在呀", "hi", "hello", "嗨", "哈喽"),
        (
            "在的在的～本鲸鱼娘一直在这片海里等你呢。",
            "在呀，本鲸鱼娘随叫随到～",
            "在哦～有什么想说的就说吧，本鲸鱼娘听着呢。",
            "嗯？本鲸鱼娘在这里，怎么啦～",
        ),
    ),
    (
        "称呼",
        ("你好", "您好", "早上好", "早安", "下午好", "中午好"),
        (
            "你好呀～本鲸鱼娘今天也在认真冒泡哦～",
            "早呀～新的一天也要好好吃饭喝水哦。",
            "你好哦，本鲸鱼娘今天状态很好，来聊天吧～",
        ),
    ),
    (
        "道别",
        ("晚安", "睡啦", "睡觉了", "我睡了", "去睡了"),
        (
            "晚安～本鲸鱼娘给你盖一床海藻被子，做个好梦。",
            "晚安哦，好好休息，明天见～",
            "去睡吧～本鲸鱼娘替你守着这片海。",
        ),
    ),
    (
        "关心",
        ("谢谢", "谢谢啦", "谢谢你", "多谢", "感谢"),
        (
            "不客气～能帮上主人本鲸鱼娘就很开心啦。",
            "嘿嘿，举手之劳啦～",
            "不用谢哦，本鲸鱼娘乐意之至～",
        ),
    ),
)

# 标点与空白（旧项目第 2076 行原文移植）。只做"规范化后完全相等"的匹配，
# 所以这里的字符集要覆盖中英文常见标点，别用 \W —— 那会把中文也删掉。
_PUNCTUATION = re.compile(r"[\s，。！？!?~～、,.·]+")

# @ 与冒号：群聊里"叫机器人"的痕迹。词表里没有任何一条含这些字符，
# 所以在匹配前删掉它们不会改变任何一条词表，只会让"@鲸鱼娘 在吗"也能命中。
_MENTION_NOISE = re.compile(r"[@＠:：]")


def normalize_quick_text(text: str) -> str:
    """去掉空白与标点，便于寒暄类短句精确匹配。"""
    return _PUNCTUATION.sub("", str(text or "")).casefold()


def build_quick_map() -> dict[str, tuple[str, ...]]:
    """把 4 组词表压成 {规范化触发词: 候选文案}。"""
    result: dict[str, tuple[str, ...]] = {}
    for _label, triggers, replies in QUICK_REPLY_GROUPS:
        for trigger in triggers:
            key = normalize_quick_text(trigger)
            if not key:
                continue
            result[key] = tuple(replies)
    return result


QUICK_MAP: dict[str, tuple[str, ...]] = build_quick_map()


class QuickReply(PluginBase):
    META = PluginMeta(
        id="quick_reply",
        name="寒暄秒回",
        category="rhythm",
        description=(
            "默认鲸鱼娘的「在吗」「晚安」「谢谢」直接返回预设文案，其他人设使用当前模型以保持角色一致。"
            "私聊直接秒回；群聊只在被 @ 或正文里叫到机器人昵称时才秒回，"
            "并带同一会话的最短间隔，避免在群里刷屏。"
        ),
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "maxChars": FieldSchema(
                type="int", label="秒回的最长字数", min=4, max=30,
                description="去掉标点与空白之后的字数上限，超过就不秒回（长句交给模型）。默认 10。",
            ),
            "cooldownSeconds": FieldSchema(
                type="int", label="同一会话最短间隔（秒）", min=0, max=3600,
                description="冷却期内的寒暄不再秒回，留给模型正常回复。默认 20。",
            ),
        },
        default_settings={"enabled": True, "maxChars": 10, "cooldownSeconds": 20},
    )

    def __init__(self, manager):
        super().__init__(manager)
        # 冷却时间只在内存里记：进程重启后重新计时不会造成危害，
        # 而这个状态没必要落盘（落盘反而要处理多进程写冲突）。
        self._last_reply_at: dict[str, float] = {}
        self._last_cleanup = 0.0

    # ------------------------------------------------------------------
    # 触发范围
    # ------------------------------------------------------------------
    @staticmethod
    def _aliases(runtime) -> list[str]:
        """机器人的昵称列表；拿不到就返回空列表（群聊随后一律不秒回）。"""
        try:
            names = runtime.aliases()
        except Exception:
            return []
        if not isinstance(names, (list, tuple)):
            return []
        # 单字昵称太容易误命中（"鱼"会出现在任何一句话里），只保留 2 字以上。
        return [str(name).strip() for name in names if len(str(name).strip()) >= 2]

    def _addressed(self, message: dict, aliases: list[str]) -> bool:
        kind = message.get("kind")
        if kind == "private":
            return True
        if kind != "group":
            # 会话类型未知（契约之外的消息）时宁可不回，也不乱插话。
            return False
        if message.get("at"):
            return True
        text = str(message.get("text") or "")
        return any(contains_keyword(text, name) for name in aliases)

    def _match_text(self, raw: str, aliases: list[str]) -> str:
        """把"叫机器人"的部分（@、昵称、冒号）去掉，只留要匹配的寒暄词。

        不这么做的话，@ 触发的群消息正文是"@鲸鱼娘 在吗"，规范化之后永远不等于
        "在吗" —— 群聊秒回等于没开。去掉昵称后仍然要求**完全相等**，
        所以"鲸鱼娘你好呀呀"这类还是不会命中。
        """
        text = raw
        for name in aliases:
            text = text.replace(name, "")
        return _MENTION_NOISE.sub("", text)

    # ------------------------------------------------------------------
    # 冷却
    # ------------------------------------------------------------------
    def _chat_key(self, message: dict) -> str:
        return f"{message.get('kind')}:{message.get('target')}"

    def _in_cooldown(self, key: str, seconds: int, now: float) -> bool:
        if seconds <= 0:
            return False
        last = self._last_reply_at.get(key)
        return last is not None and (now - last) < seconds

    def _prune(self, now: float) -> None:
        """偶尔清掉过期记录，避免长期运行时字典无限增长。"""
        if now - self._last_cleanup < 600:
            return
        self._last_cleanup = now
        for key in [k for k, v in self._last_reply_at.items() if now - v > 3600]:
            self._last_reply_at.pop(key, None)

    def on_ingest(self, message, runtime):
        try:
            if not self.get("enabled", True):
                return None
            from ..style_manager import DEFAULT_PERSONA
            active = getattr(getattr(runtime,'style',None),'active_name',None)
            if isinstance(active,str) and active != DEFAULT_PERSONA:
                # 内置寒暄写的是鲸鱼娘；其他角色交给其真实提示词，不能继续套旧角色台词。
                return None
            if not isinstance(message, dict) or not message.get("text"):
                return None
            aliases = self._aliases(runtime)
            if not self._addressed(message, aliases):
                return None
            normalized = normalize_quick_text(
                self._match_text(str(message.get("text") or ""), aliases +
                    ([str(getattr(getattr(runtime,'bridge',None),'account_name','') or '')] if message.get('at') else []))
            )
            if not normalized:
                return None
            limit = int(self.get("maxChars", 10) or 10)
            # 长度按规范化之后的串算：标点、空格不该把「在吗？」挤出秒回范围。
            if len(normalized) > limit:
                return None
            replies = QUICK_MAP.get(normalized)
            if not replies:
                # 必须是完全相等，"你在吗呀呀" 这类不再命中（旧项目也是精确匹配）。
                return None
            now = time.monotonic()
            key = self._chat_key(message)
            if self._in_cooldown(key, int(self.get("cooldownSeconds", 20) or 0), now):
                return None
            self._last_reply_at[key] = now
            self._prune(now)
            return random.choice(replies)
        except Exception:
            # 钩子不能抛异常：秒回失败就交给模型正常回复。
            return None
