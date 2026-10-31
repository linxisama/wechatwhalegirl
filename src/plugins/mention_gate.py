"""mention_gate.py - 点名闸门：只在被点名时开口 / 只回指定的人。

对齐原 JS 版 ``mention-gate`` 的三个功能（微信侧已去掉拍一拍那一档）：

① ``group_ids``：名单里的群，机器人只有被 @ 或**被叫到名字**（``runtime.aliases()``）
   才开口，其余消息当场丢弃，一次模型都不调。
② ``user_rules``：精确到"哪个群的哪个人"（一行 ``会话=发送者,发送者``）。写了这一项的群，
   名单上的人被忽略，同一群其他人照常回复。
③ ``mode``：``allowlist``（默认）= 名单之外的群完全不受影响；``only`` = 只服务名单里的群。

微信侧的判定依据是 ``message["at"]``（契约保证这个布尔值由宿主算好）与别名命中；
**不再解析数字 QQ 号**，也不再有 poke/notice 分支 —— 微信不产生通知事件
（契约第 1 节：``notice`` 恒为 None）。

降级点（相对 JS 版）：
- JS 版把"①"写成宿主的**分群档位**，因此连"是否建会话"都能省下；本项目只能在
  ingest 阶段丢弃消息（同样不调模型，但已经进过一次 ingest）。
- JS 版的"②"除了硬拦截发送工具，还会往提示词里加一句规则；本项目的 ``extra_system``
  与工具级硬拦截不等价（本项目没有 tool 层），因此只做消息级拦截 + 提示词说明。
- ``user_rules`` 里写昵称时无法区分重名的人（本项目拿不到群成员名单），
  建议填 wxid。
"""

from __future__ import annotations

import re
from typing import Optional

from ..text_matching import contains_keyword
from . import PluginBase, PluginMeta, FieldSchema

# 群/人标识抽取。微信侧标识是 username（``xxx@chatroom``）或 wxid（``wxid_xxx``），
# 都不是数字，所以只按分隔符切分、**不做数字校验**。旧配置里的数字群号照样能切出来。
_ID_SPLIT_RE = re.compile(r'[,，、;；\s]+')


def parse_ids(value: str) -> list:
    """从任意分隔的文本里抽出标识串，保持顺序去重。

    容忍中文逗号、顿号、分号、空格等一切分隔，也容忍旧配置里写的数字群号。
    """
    result = []
    for item in _ID_SPLIT_RE.split(str(value or "")):
        token = item.strip().strip('"\'“”')
        if token and token not in result:
            result.append(token)
    return result


def parse_user_rules(value: str) -> dict:
    """解析"会话=发送者,发送者"的多行规则，返回 ``{会话: [发送者, ...]}``。

    容忍空行、``#`` 注释行与全角等号；写错的行直接跳过（由调用方决定怎么提示）。
    """
    rules = {}
    for raw_line in str(value or "").replace("＝", "=").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        group_part, user_part = line.split("=", 1)
        groups = parse_ids(group_part)
        users = parse_ids(user_part)
        if not groups or not users:
            continue
        for group in groups:
            existing = rules.setdefault(group, [])
            for user in users:
                if user not in existing:
                    existing.append(user)
    return rules


def _identifiers(message: dict) -> list:
    """本条消息可用于比对"会话"的全部候选标识：username 与显示名。

    设置项承诺"可以填会话 username 或群名"，所以两处都要比对 ——
    只认 username 时，"填群名"会**静默失效**（界面看着配好了，闸门却从不生效），
    与 ``wechat_app.ingest`` 的群白名单口径保持一致。
    """
    values = [str(message.get("target") or ""), str(message.get("target_name") or "")]
    return [value for value in dict.fromkeys(values) if value]


class MentionGate(PluginBase):
    META = PluginMeta(
        id="mention_gate",
        name="点名闸门",
        description=(
            "给机器人加一道点名闸门：① 名单里的群只在被 @ 或被叫到名字时才开口"
            "（其余消息不调模型，降低风控）；② 精确到某个群的某个人（名单外的人照常回复）；"
            "③ 名单之外的群可以完全不受影响，也可以谁叫都不理。"
        ),
        category="message",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用点名闸门"),
            "group_ids": FieldSchema(
                type="string", label="① 整群只在被点名时回复",
                description="填会话 username（形如 12345@chatroom）或群名，多个用逗号隔开。留空 = 本项不启用。",
            ),
            "user_rules": FieldSchema(
                type="string", label="② 群内忽略名单（按会话，一行一个）", multiline=True,
                description="格式：会话=要忽略的发送者。例：12345@chatroom=wxid_abc,wxid_def。不回复名单上的人，其他人照常；建议填 wxid。",
            ),
            "mode": FieldSchema(
                type="select", label="③ 名单外群的待遇", values=["allowlist", "only"],
                description="allowlist = 名单之外的群保持你原来的设置；only = 只服务名单里的群。",
            ),
            "prompt_note": FieldSchema(
                type="bool", label="提示词里也写明本群规则",
                description="默认开：受管群会多一句规则说明，免得模型以为自己漏了消息。",
            ),
        },
        default_settings={
            "enabled": False,
            "group_ids": "",
            "user_rules": "",
            "mode": "allowlist",
            "prompt_note": True,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)

    def post_load(self):
        """迁移旧键。

        必须放在 ``post_load`` 而不是 ``__init__``：``__init__`` 跑在管理器把本实例
        放进 ``_plugins`` 之前，那时 ``update_settings`` 会抛
        ``Unknown plugin: mention_gate`` —— 这个迁移以前就是这么**静默失败**的。
        """
        self._migrate_legacy()

    # ── 旧键迁移 ─────────────────────────────────────────────────────────
    # 本项目早期版本用的是 onlyGroups（"生效群"）与 allowKeyword（"豁免关键词"），
    # 与 JS 的 groupIds（"这些群仅被点名才回"）语义**不同**：直接用新名字读旧值会把
    # 行为弄反。因此只做一次单向迁移：旧值存在且新键为空时，把旧标识搬过来并落盘，
    # 同时把差异写进日志。旧字段不再参与判定。（迁移目标键名保持不变，老配置能续用。）
    def _migrate_legacy(self):
        try:
            # 用 raw_settings：settings 会被 schema 过滤，看不见已废弃的旧键
            settings = dict(self.raw_settings)
            legacy_groups = settings.pop("onlyGroups", None)
            legacy_keyword = settings.pop("allowKeyword", None)
            changed = False
            if legacy_groups and not settings.get("group_ids"):
                ids = parse_ids(legacy_groups)
                if ids:
                    settings["group_ids"] = ",".join(ids)
                    changed = True
                    print(f"[plugins] mention_gate: 已把旧 onlyGroups 迁移为 group_ids（{len(ids)} 个会话）；"
                          f"注意新语义是“这些会话只在被点名时回复”")
            if legacy_keyword is not None:
                changed = True
                print("[plugins] mention_gate: 旧 allowKeyword 已停用；"
                      "新语义只认 @ 与别名，不再按关键词豁免")
            if changed:
                self._manager.update_settings(self.META.id, settings)
        except Exception as exc:
            print(f"[plugins] mention_gate 迁移失败（不影响运行）：{exc}")

    # ── 内部 ─────────────────────────────────────────────────────────────
    def _group_ids(self) -> list:
        return parse_ids(self.get("group_ids", ""))

    def _user_rules(self) -> dict:
        return parse_user_rules(self.get("user_rules", ""))

    @staticmethod
    def _aliases(runtime) -> list:
        """机器人被叫的昵称（来自运行时；拿不到就当作没有别名）。"""
        try:
            provided = runtime.aliases()
        except Exception:
            return []
        if not isinstance(provided, (list, tuple)):
            return []
        return [str(v).strip() for v in provided if str(v).strip()]

    def _addressed(self, message: dict, runtime) -> bool:
        """这条消息是不是在叫机器人：@ 或命中别名。"""
        if message.get("at") or message.get('_group_summary'):
            return True
        text = str(message.get("text") or "")
        return any(contains_keyword(text, name) for name in self._aliases(runtime))

    def _blocked_by_user_rule(self, group_id: str, message: dict) -> bool:
        """② 只回指定的人：名单上的人发言时忽略，同一群其他人照常。"""
        rule = self._user_rules().get(group_id)
        if not rule:
            return False
        sender_id = str(message.get("sender_id") or "").strip()
        if not sender_id:
            return False  # 拿不到发送者标识时不误伤（宁可按原设置回复）
        return sender_id in rule

    def on_block(self, message: dict, runtime) -> bool:
        if not self.get("enabled"):
            return False
        try:
            if message.get("kind") != "group":
                return False
            identifiers = _identifiers(message)
            managed = self._group_ids()
            rules = self._user_rules()
            in_group_list = any(value in managed for value in identifiers)
            # 命中规则的那个会话键（username 或群名）才是查"只回指定的人"的键
            rule_key = next((value for value in identifiers if value in rules), "")
            has_user_rule = bool(rule_key)

            # 名单之外：only 模式下一律不理；allowlist 模式完全不受影响。
            if not in_group_list and not has_user_rule:
                return str(self.get("mode", "allowlist")) == "only"

            # ② 优先于 ①：写了"只回指定的人"的群里，名单上的人被忽略。
            if has_user_rule and self._blocked_by_user_rule(rule_key, message):
                return True

            # ① 整群闸门：@ 或命中别名才放行，其余一律丢弃（不调模型）。
            if in_group_list:
                return not self._addressed(message, runtime)
            return False
        except Exception:
            return False

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        """受管群且开了 prompt_note 时，往系统提示里补一句规则说明。"""
        if not self.get("enabled") or not self.get("prompt_note", True):
            return None
        try:
            if message.get("kind") != "group":
                return None
            identifiers = _identifiers(message)
            rules = self._user_rules()
            if any(value in rules for value in identifiers):
                note = ("【点名闸门】本群忽略指定名单上的发送者；当前消息已通过忽略名单检查，"
                        "按当前提问正常回复，不要把忽略名单当成只允许名单。")
            elif any(value in self._group_ids() for value in identifiers):
                note = "【点名闸门】本群只在被 @ 或被叫到名字时才找你；能收到消息说明有人点名了，正常回即可。"
            else:
                return None
            return {"extra_system": note}
        except Exception:
            return None
