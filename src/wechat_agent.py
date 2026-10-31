"""控制台 Agent：把"人话"翻成一组白名单操作，预览 → 应用 → 可撤销（1.3.0 P0-4）。

硬约束（计划第二节）：**Agent 不能发微信消息**。这里没有、也不允许出现
`queue_outgoing` / `send` / 微信库调用；它只能改控制台自己的配置、插件开关、人设、
启停机器人，以及读写本地记忆。
`tests/test_wechat_agent.py::SafetyTests` 用静态扫描钉死这条线。

为什么规则优先、不先上模型：Agent 一旦能动设置，误操作的代价比"不够聪明"大得多。
先用确定性规则把"能预览、能撤销、值被改过就拒绝撤销"跑通，模型只负责把话翻成动作名，
真值校验永远在本地做。
"""
from __future__ import annotations

import re
from typing import Any

from .plugin_catalog import BASIC_PLUGINS

# 允许改的插件：市场插件不在安装包里，Agent 不能凭空"打开"一个不存在的插件。
AGENT_PLUGINS = set(BASIC_PLUGINS)

ACTIONS = ("set_reply_length", "set_picture_reply", "toggle_plugin",
           "activate_persona", "control_service", "answer")


class AgentError(ValueError):
    """面向用户的可读错误（路由直接回给界面）。"""


# ── 动作实现 ────────────────────────────────────────────────────────────────
def _settings_plugin(bot, plugin_id: str):
    plugin = bot.plugins._plugins.get(plugin_id)
    if plugin is None:
        raise AgentError(f"插件 {plugin_id} 未安装")
    return plugin


def _apply_set_reply_length(bot, params: dict) -> dict:
    try:
        chars = int(params.get("chars"))
    except (TypeError, ValueError):
        raise AgentError("回复长度要是数字") from None
    if not 0 <= chars <= 500:
        raise AgentError("回复长度要在 0~500 之间")
    plugin = _settings_plugin(bot, "chat_controls")
    before = int(plugin.get("replyMaxChars", 80) or 0)
    bot.plugins.update_settings("chat_controls", {"replyMaxChars": chars})
    return {"before": before, "after": chars, "label": f"回复上限 {before} → {chars} 字"}


def _apply_set_picture_reply(bot, params: dict) -> dict:
    enabled = bool(params.get("enabled"))
    plugin = _settings_plugin(bot, "chat_controls")
    before = bool(plugin.get("pictureAlwaysReply", True))
    bot.plugins.update_settings("chat_controls", {"pictureAlwaysReply": enabled})
    return {"before": before, "after": enabled,
            "label": f"带图必回 {'开' if before else '关'} → {'开' if enabled else '关'}"}


def _apply_toggle_plugin(bot, params: dict) -> dict:
    plugin_id = str(params.get("plugin_id") or "").strip()
    if plugin_id not in AGENT_PLUGINS:
        raise AgentError(f"「{plugin_id}」不在可操作清单里（只允许基础插件）")
    plugin = _settings_plugin(bot, plugin_id)
    if "enabled" not in (plugin.META.config_schema or {}):
        raise AgentError(f"「{plugin_id}」没有启用开关")
    enabled = bool(params.get("enabled"))
    before = bool(plugin.get("enabled"))
    bot.plugins.update_settings(plugin_id, {"enabled": enabled})
    return {"before": before, "after": enabled,
            "label": f"插件 {plugin_id} {'开' if before else '关'} → {'开' if enabled else '关'}"}


def _apply_activate_persona(bot, params: dict) -> dict:
    name = str(params.get("name") or "").strip()
    if not name:
        raise AgentError("要指定人设名")
    before = bot.style.active_name
    if name == before:
        return {"before": before, "after": before, "label": f"人设已经是 {name}"}
    try:
        if hasattr(bot, 'activate_persona'):
            bot.activate_persona(name)
        elif not bot.style.set_active(name) and bot.style.active_name != name:
            raise AgentError('人设切换未生效')
    except Exception as exc:
        raise AgentError(f"切换人设失败：{exc}") from None
    return {"before": before, "after": bot.style.active_name,
            "label": f"人设 {before} → {bot.style.active_name}"}


def _apply_control_service(bot, params: dict) -> dict:
    want = bool(params.get("running"))
    before = bool(bot.running)
    if want and not before:
        raise AgentError("启动机器人要在界面上点「启动」：它需要微信连接，可能弹窗")
    if not want and before:
        raise AgentError("停止机器人请在界面上操作，避免把正在发送的回复掐断")
    return {"before": before, "after": before,
            "label": f"机器人状态未改动（{'运行中' if before else '已停止'}）"}


HANDLERS = {
    "set_reply_length": _apply_set_reply_length,
    "set_picture_reply": _apply_set_picture_reply,
    "toggle_plugin": _apply_toggle_plugin,
    "activate_persona": _apply_activate_persona,
    "control_service": _apply_control_service,
}


# ── 计划：把一句话翻成动作 ──────────────────────────────────────────────────
LENGTH_WORDS = ("太长", "啰嗦", "短一点", "简短", "说少点", "别写那么长")
PICTURE_WORDS = ("表情包", "图片", "带图")


def plan(bot, text: str) -> dict:
    """返回 {title, actions:[{action, params, label}]}；不确定就只给只读回答。"""
    raw = str(text or "").strip()
    if not raw:
        raise AgentError("请先说要改什么")
    actions = []

    number = re.search(r"(\d{2,3})", raw)
    if any(word in raw for word in LENGTH_WORDS):
        chars = int(number.group(1)) if number else 60
        actions.append({"action": "set_reply_length", "params": {"chars": chars},
                        "label": f"把回复上限改成 {chars} 字"})
    if any(word in raw for word in PICTURE_WORDS) and any(word in raw for word in ("不", "别", "关")):
        actions.append({"action": "set_picture_reply", "params": {"enabled": False},
                        "label": "带图/表情不再必回"})

    for plugin_id in AGENT_PLUGINS:
        if plugin_id not in raw:
            continue
        want = not any(word in raw for word in ("关闭", "关掉", "停用", "不要"))
        actions.append({"action": "toggle_plugin",
                        "params": {"plugin_id": plugin_id, "enabled": want},
                        "label": f"{'开启' if want else '关闭'}插件 {plugin_id}"})

    match = re.search(r"(?:换成|切换到|用)\s*([^\s，。]{1,20})\s*人设", raw)
    if match:
        actions.append({"action": "activate_persona", "params": {"name": match.group(1)},
                        "label": f"切换到人设 {match.group(1)}"})

    if not actions:
        return {"title": raw[:40], "actions": [{"action": "answer", "params": {}, "label": "回答当前状态"}]}
    return {"title": raw[:40], "actions": actions[:3]}


# ── 应用 / 撤销 ─────────────────────────────────────────────────────────────
def apply_card(bot, session, card: dict, sessions) -> dict:
    """逐条执行；任何一条失败就回滚已执行的，避免"改了一半"。"""
    if card.get("state") not in (None, "preview"):
        raise AgentError("这张卡片已经处理过了")
    done = []
    try:
        for action in card.get("actions") or []:
            name = action.get("action")
            if name not in ACTIONS:
                raise AgentError(f"不认识的操作：{name}")
            if name == "answer":
                done.append({"action": name, "undo": None,
                             "label": action.get("label", ""), "result": answer(bot)})
                continue
            result = HANDLERS[name](bot, action.get("params") or {})
            done.append({"action": name, "undo": {"before": result["before"],
                                                  "after": result["after"],
                                                  "params": action.get("params") or {}},
                         "label": result["label"]})
    except Exception:
        _rollback(bot, done)
        raise
    card = sessions.update_card(session["id"], card["id"], state="applied", done=done,
                                applied_at=__import__("time").time())
    return card


def _undo_one(bot, item: dict) -> None:
    """把一条已执行的动作还原到执行前的值。"""
    undo = item.get("undo") or {}
    params = undo.get("params") or {}
    name = item.get("action")
    if name == "set_reply_length":
        bot.plugins.update_settings("chat_controls", {"replyMaxChars": int(undo["before"])})
    elif name == "set_picture_reply":
        bot.plugins.update_settings("chat_controls", {"pictureAlwaysReply": bool(undo["before"])})
    elif name == "toggle_plugin":
        bot.plugins.update_settings(str(params.get("plugin_id")), {"enabled": bool(undo["before"])})
    elif name == "activate_persona":
        if hasattr(bot, 'activate_persona'):
            bot.activate_persona(str(undo['before']))
        else:
            bot.style.set_active(str(undo["before"]))


def _rollback(bot, done) -> None:
    for item in reversed(done):
        if item.get("undo"):
            try:
                _undo_one(bot, item)
            except Exception:
                pass


def _current_value(bot, item: dict):
    """撤销前的核对：当前值必须仍是"当初应用的那个值"。"""
    name, undo = item.get("action"), item.get("undo") or {}
    params = undo.get("params") or {}
    if name == "set_reply_length":
        return int(_settings_plugin(bot, "chat_controls").get("replyMaxChars", 80) or 0)
    if name == "set_picture_reply":
        return bool(_settings_plugin(bot, "chat_controls").get("pictureAlwaysReply", True))
    if name == "toggle_plugin":
        plugin = bot.plugins._plugins.get(str(params.get("plugin_id")))
        return None if plugin is None else bool(plugin.get("enabled"))
    if name == "activate_persona":
        return bot.style.active_name
    return None


def undo_card(bot, session, card: dict, sessions) -> dict:
    """撤销：先核对"当前值是否还是当初应用的值"，被别人改过就拒绝，避免覆盖别人的改动。"""
    if card.get("state") != "applied":
        raise AgentError("只有已应用的卡片才能撤销")
    for item in card.get("done") or []:
        if not item.get("undo"):
            continue
        current = _current_value(bot, item)
        if current != item["undo"].get("after"):
            raise AgentError(
                f"「{item.get('label')}」之后又被改过（当前 {current}），不能自动撤销，请手动确认")
    _rollback(bot, card.get("done") or [])
    return sessions.update_card(session["id"], card["id"], state="undone")


def answer(bot) -> str:
    """只读回答：当前状态一句话摘要（不调用模型，不花钱）。"""
    status = bot.status()
    memory = status.get("memory") or {}
    return (f"机器人{'运行中' if status.get('running') else '已停止'}；"
            f"插件 {status.get('plugins')} 个；模型 {status.get('model')}；"
            f"记忆 {memory.get('confirmed', 0) + memory.get('manual', 0)} 条，"
            f"学习 {memory.get('learned', 0)} 条。")
