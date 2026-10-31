"""member_aliases.py - 群友外号对照：进提示词让模型认人。

移植自 QQ Agent 社区插件 member-aliases v1.0.0（原版是纯 hooks：谁在说话就
告诉他这人还叫什么，消息里出现某人的外号就告诉他指的是谁）。命中才加行，
没命中一个字都不加，不白花 prompt 预算。

和原版的差异都是刻意的：
- 注入走 on_pre_ai 的 extra_system，由 src/prompt_budget.py 当"plugins"
  部分计费封顶；不动 system 前缀，改人设不会连带把这段缓存作废。
- 只认第一人称自述（"我叫X"/"叫我X"/"我的外号是X"）。群里"他叫X"多半是
  玩笑或点名别人，记下来就是往表里塞垃圾，所以默认不收。
- 默认关闭（enabled_by_default=False）：外号表会把昵称写进提示词，交不交给
  模型是用户的选择。
- 表存在插件 state 目录 member_aliases.json，形状同原版
  {"aliases": {"<id>": ["外号", ...]}}；文件坏了按空表处理，绝不因为一个
  坏文件把插件永久打挂（坏文件留在原地，用户随时能修）。
- 口令只在主人私聊里认（借 robot_admin.is_admin）：外号 看表、
  外号 加 <id> <外号>、外号 删 <id> [外号]。
"""

from __future__ import annotations

import re

from . import PluginBase, PluginMeta, FieldSchema

HEADER = "【称呼对照】群友的外号（只是帮你认人，别在回复里刻意解释这些对应关系）："
STATE_NAME = "member_aliases.json"

MAX_ALIASES_PER_MEMBER = 12      # 一个人最多记 12 个外号
MAX_ALIAS_LEN = 12               # 单个外号最多 12 字
MAX_MEMBERS = 300                # 表里最多 300 人
MAX_HINT_LINES = 5               # 一次最多注入 5 行（封顶，别把提示词撑爆）
NAME_KEEP = 6                    # 名字/id 只留前 6 字，别把整串 wxid 写进提示词

COMMAND_RE = re.compile(r"^\s*[/\\]?\s*外号\s*(?P<rest>.*)$")
_TAIL = r"([^\s，,。！!？?、；;：:]{1,12})"
# 第一人称自述：我叫X / 叫我X / 我的外号是X / 你可以叫我X
LEARN_RES = (
    re.compile(r"(?:你可以|可以|都|就)?叫我\s*" + _TAIL),
    re.compile(r"(?:我|咱)(?:就)?的?外号是\s*" + _TAIL),
    re.compile(r"(?:^|[，,。！!？?\s])(?:我|咱)(?:就)?叫\s*" + _TAIL),
)
# 候选外号以这些字开头的一律不要："我叫你别走""我叫了外卖"都不是自我介绍
_REJECT_PREFIX = ("你", "他", "她", "它", "别", "不", "没", "了", "过", "着", "错")
# 语气词尾巴不算外号："叫我海哥吧" 记的是"海哥"，不是"海哥吧"
_TAIL_PARTICLES = ("吧", "啊", "呀", "哦", "嘛", "呢", "啦", "呗", "哈", "噢", "喔")

_ASCII = re.compile(r"[0-9A-Za-z]")


def text_has_alias(text, alias) -> bool:
    """文本里是否出现了这个外号。

    中文不用空格分词，所以分两档（照搬原版）：纯中文或长度 >=3 的外号直接
    包含判断（"某某今天怎么没来"要能命中）；含 ASCII 字母数字的短外号
    （"99"）只在 ASCII 字母数字边界上判断，否则 "99" 会命中 "1990"；中文紧邻
    不算越界（"99来了"照样命中）。
    """
    body = str(text or "")
    word = str(alias or "").strip()
    if not body or not word:
        return False
    if not _ASCII.search(word) or len(word) >= 3:
        return word in body
    pattern = r"(?<![0-9A-Za-z])" + re.escape(word) + r"(?![0-9A-Za-z])"
    return re.search(pattern, body, re.IGNORECASE) is not None


def clean_aliases(value) -> list:
    """规整一个人的外号列表：去空、去重、限长、限量。"""
    if isinstance(value, str):
        items = re.split(r"[,，、;；\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        return []
    result, seen = [], set()
    for item in items:
        word = str(item or "").strip()[:MAX_ALIAS_LEN]
        if not word or word in seen:
            continue
        seen.add(word)
        result.append(word)
        if len(result) >= MAX_ALIASES_PER_MEMBER:
            break
    return result


def _trim_particles(word: str) -> str:
    """去掉句末语气词（最多两层），别把"海哥吧"记成外号。"""
    for _ in range(2):
        if len(word) > 1 and word.endswith(_TAIL_PARTICLES):
            word = word[:-1]
        else:
            break
    return word


def learn_aliases(text) -> list:
    """从一句话里抽出"自称"的外号候选（只认第一人称，抽不到返回空列表）。"""
    body = str(text or "")
    if not body:
        return []
    found = []
    for pattern in LEARN_RES:
        for match in pattern.finditer(body):
            word = _trim_particles(match.group(1).strip())
            if not word or len(word) > MAX_ALIAS_LEN:
                continue
            if word.startswith(_REJECT_PREFIX):
                continue
            if word not in found:
                found.append(word)
    return found


def _short(value) -> str:
    """名字/id 只留前几个字：完整 wxid 进提示词既占额度又没信息量。"""
    text = str(value or "").strip()
    return text if len(text) <= NAME_KEEP else text[:NAME_KEEP] + "…"


def build_hint(message, table, limit=MAX_HINT_LINES) -> str:
    """按原版顺序拼"称呼对照"块：先本轮说话的人，再被提到的外号；没命中返回空串。"""
    if not isinstance(table, dict) or not table:
        return ""
    text = str(message.get("text") or "")
    lines = []

    speaker = str(message.get("sender_id") or "")
    if speaker and table.get(speaker):
        words = clean_aliases(table.get(speaker))
        if words:
            lines.append("· 这轮说话的是 " + _short(speaker) + "（外号：" + "、".join(words) + "）")

    if text:
        for uid, value in table.items():
            words = clean_aliases(value)
            if not words or uid == speaker:
                continue
            hit = next((word for word in words if text_has_alias(text, word)), "")
            if hit:
                lines.append("· 有人提到「" + hit + "」= " + _short(uid) + "（外号：" + "、".join(words) + "）")
            if len(lines) >= max(1, int(limit)):
                break

    if not lines:
        return ""
    return HEADER + "\n" + "\n".join(lines[:max(1, int(limit))])


class MemberAliases(PluginBase):
    META = PluginMeta(
        id="member_aliases",
        name="群友外号对照",
        description="记下群友的自称外号，命中才往提示词里加几行，让模型认人；默认关闭",
        category="knowledge",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema("bool", "启用群友外号", "关闭后既不记外号也不注入提示词"),
            "rememberAliases": FieldSchema("bool", "自动记外号",
                                           "只在有人说「我叫X」「叫我X」时记下来"),
            "injectAliases": FieldSchema("bool", "注入提示词",
                                         "命中才加行，关闭后只记不用"),
            "maxPerGroup": FieldSchema("int", "每人最多记几个", "1~300", min=1, max=300, step=1),
        },
        default_settings={"enabled": False, "rememberAliases": False,
                          "injectAliases": True,
                          "maxPerGroup": MAX_ALIASES_PER_MEMBER},
    )

    # ── 表 ────────────────────────────────────────────────────────────────
    def table(self) -> dict:
        """读外号表；结构不对或文件坏了都当空表（坏文件原地留着，用户能修）。"""
        raw = self.load_state(STATE_NAME, {})
        aliases = raw.get("aliases") if isinstance(raw, dict) else None
        if not isinstance(aliases, dict):
            return {}
        table = {}
        for uid, value in aliases.items():
            words = clean_aliases(value)
            if words:
                table[str(uid)] = words
            if len(table) >= MAX_MEMBERS:
                break
        return table

    def save_table(self, table) -> None:
        clean = {str(uid): clean_aliases(value) for uid, value in dict(table or {}).items()}
        self.save_state(STATE_NAME, {"aliases": {uid: words for uid, words in clean.items() if words}})

    def add_alias(self, uid, alias) -> bool:
        uid, word = str(uid or "").strip(), str(alias or "").strip()[:MAX_ALIAS_LEN]
        if not uid or not word:
            return False
        table = self.table()
        words = clean_aliases(table.get(uid) or [])
        if word in words:
            return False
        words.append(word)
        table[uid] = words[:self._per_member()]
        self.save_table(dict(list(table.items())[:MAX_MEMBERS]))
        return True

    def remove_alias(self, uid, alias="") -> bool:
        uid, word = str(uid or "").strip(), str(alias or "").strip()
        table = self.table()
        if uid not in table:
            return False
        if not word:
            table.pop(uid, None)
            self.save_table(table)
            return True
        words = [item for item in table[uid] if item != word]
        if len(words) == len(table[uid]):
            return False
        if words:
            table[uid] = words
        else:
            table.pop(uid, None)
        self.save_table(table)
        return True

    def _per_member(self) -> int:
        """每人上限：设置值夹在 1..300；读坏了退回默认，不让设置把表写残。"""
        try:
            value = int(self.get("maxPerGroup", MAX_ALIASES_PER_MEMBER))
        except (TypeError, ValueError):
            value = MAX_ALIASES_PER_MEMBER
        return max(1, min(300, value))

    # ── 钩子 ──────────────────────────────────────────────────────────────
    def on_ingest(self, message, runtime):
        text = str(message.get("text") or "")
        if not text:
            return None
        command = COMMAND_RE.match(text)
        if command and self._is_keeper(message, runtime):
            return self._handle_command(command.group("rest"))
        if message.get("kind") == "group" and self.get("rememberAliases") and message.get("sender_id"):
            for word in learn_aliases(text):
                self.add_alias(message.get("sender_id"), word)
        return None

    def on_pre_ai(self, message, conversation, runtime):
        if not self.get("injectAliases"):
            return None
        if message.get("kind") != "group":
            return None          # 私聊里模型本来就知道在跟谁说话，不必花这笔额度
        hint = build_hint(message, self.table())
        if not hint:
            return None
        return {"extra_system": "\n" + hint}

    # ── 口令 ──────────────────────────────────────────────────────────────
    def _is_keeper(self, message, runtime) -> bool:
        """只有主人能看/改表；没装 robot_admin 时退化成"私聊就当主人"。"""
        if message.get("kind") != "private":
            return False
        control = getattr(getattr(runtime, "plugins", None), "_plugins", {}).get("robot_admin")
        if control is None:
            return True
        try:
            return bool(control.is_admin(message, runtime))
        except Exception:
            return False

    def _handle_command(self, rest) -> str:
        table = self.table()
        parts = str(rest or "").split()
        if not parts:
            if not table:
                return "外号表是空的。用法：外号 加 <id> <外号>"
            lines = ["外号表："]
            for uid, words in list(table.items())[:50]:
                lines.append("· " + _short(uid) + "：" + "、".join(words))
            return "\n".join(lines)

        action, rest_parts = parts[0], parts[1:]
        if action in ("加", "添加", "记") and len(rest_parts) >= 2:
            uid = rest_parts[0]
            alias = " ".join(rest_parts[1:])
            if self.add_alias(uid, alias):
                return "已记下：" + _short(uid) + " 的「" + alias + "」"
            return "这条已经在表里了（或者是空的）。"
        if action in ("删", "删除", "去掉") and rest_parts:
            uid = rest_parts[0]
            alias = " ".join(rest_parts[1:])
            if self.remove_alias(uid, alias):
                return "已删掉：" + _short(uid) + (" 的「" + alias + "」" if alias else " 的全部外号")
            return "没找到这条外号。"
        return "用法：外号 / 外号 加 <id> <外号> / 外号 删 <id> [外号]"
