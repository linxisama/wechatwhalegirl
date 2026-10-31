"""插件分类；1.3.8安装包恢复全部受支持模块，保留分类供管理与市场使用。

为什么要有这张表（用户 2026-10-23 的 1.3.0 计划 P0-2）：
控制台一次性带 32 个插件，用户面对的选择太多，安装包也更大；QQ 5.0 的做法是
"基础能力内置 + 其余按需下载"。这里只做**数据**，不改变源码环境的行为——
`manager.PLUGIN_MODULES` 仍然是全量，测试也仍然覆盖全部插件。
1.3.0的冻结分层曾导致已启用的旧插件只剩设置却没有代码，升级后静默失效。
1.3.8默认完整打包，冻结与覆盖升级检查必须核对全部集合及关键词实际行为。
市场仍可单独导出插件包，不改变现有启用/移除选择。

划分原则见 `docs/插件分层清单-1.3.0.md`。
"""
from __future__ import annotations

# 核心配置面 + 回复质量链路 + 本地能力：缺一个控制台就不可用，必须内置。
BASIC_PLUGINS = (
    # 核心配置面（与 src/plugin_policy.py 的 CORE_PLUGINS 对齐）
    "reply_preferences", "robot_admin", "private_replies", "keyword_trigger",
    "group_monitor", "conversation_memory", "chat_controls",
    # 回复质量链路
    "addressee_guard", "anti_repeat", "burst_merge", "quick_reply", "reply_dedupe",
    "short_followup", "token_saver",
    # 本地能力（都不出本机）
    "image_ocr", "image_understanding", "speech_to_text_wechat",
    # 配图来源与本地群总结：用户常用，且被 GUI 自检直接引用，缺了会像"功能凭空消失"
    "image_library", "group_summary",
    # 发表情/配图是用户的常用功能：分层时一度划进市场包，结果装完"机器人不发表情了"。
    # 这三件是配图链路的完整闭环（管理界面 → 记住含义 → 按节奏发），必须有。
    "sticker_admin", "sticker_memory", "sticker_cadence",
    # 联网（时间/热榜 + 命中触发词时现查）
    "web_context", "web_lookup",
    # 群友外号：随安装包内置但默认关闭，属"记人"能力的补丁，不值得单开市场包
    "member_aliases",
)

# 可选的体验增强：放插件市场，用户按需下载。
MARKET_PLUGINS = (
    "group_ambient", "keyword_reply", "mention_gate",
    "minimal_mode", "reply_guards", "reply_pace", "sleep_mode",
    "sticker_steal",
)

# 每张市场插件卡片上的一句话说明（市场包的 plugin.json / 索引 README 用）。
MARKET_DESCRIPTIONS = {
    "group_ambient": "群聊主动发言：默认关闭，需同时满足 proactive.enabled 与群白名单，风险最高。",
    "keyword_reply": "旧口径的关键词回复，命中后直接回固定内容，不调用模型。",
    "mention_gate": "更严格的点名闸门：未在名单内的群一律不回，适合只看几个群。",
    "minimal_mode": "极简模式：只保留必要提示词，省 token。",
    "reply_guards": "复读接梗：群里复读时接一句，默认关闭。",
    "reply_pace": "回复节奏：开口前随机等几秒，降低「秒回」特征。",
    "sleep_mode": "睡眠时段：到点自动不回复，早上自动恢复。",
    "sticker_admin": "表情包管理界面：改标签与说明，供挑图时按名字+含义加权。",
    "sticker_cadence": "每 N 条回复配一张图：默认关闭，防止刷屏。",
    "sticker_memory": "记住表情包的含义，回复时更容易挑对图。",
    "sticker_steal": "回复时顺手收集群里的表情，攒进本地图库。",
}


def is_basic(plugin_id: str) -> bool:
    return plugin_id in BASIC_PLUGINS


def is_market(plugin_id: str) -> bool:
    return plugin_id in MARKET_PLUGINS


def market_spec(plugin_id: str) -> dict:
    """市场插件的一个条目；不在市场清单里时抛 KeyError。"""
    if plugin_id not in MARKET_PLUGINS:
        raise KeyError(plugin_id)
    return {"id": plugin_id, "description": MARKET_DESCRIPTIONS.get(plugin_id, "")}


def validate(available) -> list:
    """自检用：清单与真实插件是否一致（缺的、多的都报出来）。"""
    available = set(available)
    declared = set(BASIC_PLUGINS) | set(MARKET_PLUGINS)
    problems = []
    missing = sorted(declared - available)
    undeclared = sorted(available - declared)
    if missing:
        problems.append("清单里有、实际不存在：" + "、".join(missing))
    if undeclared:
        problems.append("实际存在但没归类：" + "、".join(undeclared))
    both = sorted(set(BASIC_PLUGINS) & set(MARKET_PLUGINS))
    if both:
        problems.append("同时出现在两张表：" + "、".join(both))
    return problems
