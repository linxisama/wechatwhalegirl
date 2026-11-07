"""角色心情（1.3.109）：随静置随机重置，只对主人的回复生效。

用户 2026-11-08 的原话：

  「每天心情会 4 小时不聊天随机重置一次，然后根据主人的 3 次绘画中 u 定心情，
   对只有主人的回话有用。」

为什么新建模块而不是改 src/plugins/emotion_state.py：
  那个插件在 src/plugin_policy.py 的 RETIRED_PLUGINS 里（与 context_chime、
  typo_sim 一起退役），**永远不会被加载**。往它里面加逻辑等于往死代码里加，
  跑起来也不生效，测试还测不到。

这个模块只做纯逻辑：给一个「上次互动时间」与「当前时间」，算出该不该重置、
重置成什么。不碰数据库、不碰文件、不 import Qt 或 FastAPI —— 这样它既能被
插件调用，也能被独立测试直接验证。
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

# 默认 4 小时（用户原话）。可配但必须有界：0 会让每次查询都重置，
# 大于 48 小时等于永不重置，两种都让这个功能形同虚设。
DEFAULT_IDLE_HOURS = 4
MIN_IDLE_HOURS = 1
MAX_IDLE_HOURS = 48

# 重置后的取值区间。心情落在 40-70、精力 70-90、压力 0-20 ——
# 都是「不好不坏」的中段，不回到固定值，避免用户看出这是个常量。
MOOD_RANGE = (40.0, 70.0)
ENERGY_RANGE = (70.0, 90.0)
STRESS_RANGE = (0.0, 20.0)


def clamp_idle_hours(value) -> int:
    """把配置值夹到合法区间；认不出来用默认值。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_IDLE_HOURS
    return max(MIN_IDLE_HOURS, min(MAX_IDLE_HOURS, number))


def _spread(seed: int) -> float:
    """把整数种子散成 0-1 的浮点。确定性：同一个种子永远同一个值。

    为什么要确定性：同一段静置时间反复查询必须拿到同一个心情。
    每次查询都随机的话，用户刷新一次界面心情就变一次，看起来像 bug。
    """
    return ((seed * 2654435761) % 1000) / 1000.0


def _between(low: float, high: float, ratio: float) -> float:
    return round(low + (high - low) * ratio, 1)


def should_reset(last_seen: Optional[float], now: Optional[float] = None,
                 idle_hours: int = DEFAULT_IDLE_HOURS) -> bool:
    """距上次互动是否已经超过 idle_hours。没有记录时返回 True（首次就是重置态）。"""
    if last_seen is None:
        return True
    try:
        elapsed = (time.time() if now is None else float(now)) - float(last_seen)
    except (TypeError, ValueError, OverflowError):
        return True
    return elapsed >= clamp_idle_hours(idle_hours) * 3600


def reset_values(last_seen: Optional[float], now: Optional[float] = None,
                 idle_hours: int = DEFAULT_IDLE_HOURS) -> Tuple[float, float, float]:
    """重置后的 (mood, energy, stress)。种子由 last_seen 与 idle_hours 决定。

    用 last_seen 而不是 now 做种子：这样「同一段静置」反复查询结果一致，
    而用户真的又聊了一句（last_seen 变了）才会换一个新心情。
    """
    base = int(last_seen or 0)
    seed = base ^ (clamp_idle_hours(idle_hours) * 7919)
    ratio = _spread(seed)
    return (_between(*MOOD_RANGE, ratio),
            _between(*ENERGY_RANGE, ratio),
            _between(*STRESS_RANGE, ratio))


def resolve(state: Optional[dict], now: Optional[float] = None,
            idle_hours: int = DEFAULT_IDLE_HOURS) -> dict:
    """给一份已存的状态，返回「现在应该是什么心情」。

    state 允许是 None 或残缺（缺 updated / 缺各字段），一律按默认处理 ——
    状态文件坏掉时不能让整轮回复挂掉。

    返回 dict 里 always 带 idle_reset 布尔，调用方据此知道要不要回写。
    """
    data = state if isinstance(state, dict) else {}
    last_seen = data.get("updated")
    hours = clamp_idle_hours(idle_hours)
    if should_reset(last_seen, now, hours):
        mood, energy, stress = reset_values(last_seen, now, hours)
        return {"updated": time.time() if now is None else float(now),
                "mood": mood, "energy": energy, "stress": stress,
                "idle_reset": True}
    def number(key: str, default: float) -> float:
        try:
            value = float(data.get(key, default))
            return value if value == value and abs(value) != float("inf") else default
        except (TypeError, ValueError, OverflowError):
            return default
    return {"updated": time.time() if now is None else float(now),
            "mood": max(0.0, min(100.0, number("mood", 60.0))),
            "energy": max(0.0, min(100.0, number("energy", 80.0))),
            "stress": max(0.0, min(100.0, number("stress", 10.0))),
            "idle_reset": False}


def label(mood: float) -> str:
    """心情数值 → 一个词。给提示词用，不暴露具体数字。"""
    if mood >= 70:
        return "开心"
    if mood >= 40:
        return "平静"
    return "有点低落"


def is_owner_message(message, runtime=None) -> bool:
    """这条消息是不是主人发的。**拿不准时返回 False**。

    为什么保守：心情注入会写进提示词。如果判错方向（把陌生人当主人），
    等于把角色状态泄露给群里的所有人；判错另一边（把主人当陌生人）
    只是少一句语气参考。前者不可接受，后者无所谓。
    """
    if isinstance(message, dict):
        flag = message.get("_is_owner")
        if isinstance(flag, bool):
            return flag
    checker = getattr(runtime, "is_owner", None)
    if callable(checker):
        try:
            return bool(checker(message))
        except Exception:
            return False
    return False