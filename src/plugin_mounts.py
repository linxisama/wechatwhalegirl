"""插件挂载点（1.3.108）：把 8 个钩子归成三个「在链路的哪一步」。

为什么要有这一层：
  用户的诊断是「7 种钩子平铺」。问题不在钩子数量，而在于**没人说得清**
  「这个插件到底在链路的哪一步起作用」——插件作者不知道该实现哪个，
  用户装了十几个也看不出它们之间的先后关系。

  但钩子**不能直接改名合并**：它们的返回契约互不兼容。
    on_ingest 返回 str  = 立即回复并跳过模型
    on_block  返回 bool = 丢弃这条消息
    on_observed 返回 None = 只登记
  把这三个揉成一个 observe() 就要在里面靠 isinstance 猜返回值，
  那是把契约问题变成类型问题，更糟。

  所以这一层做的是**归类**而不是合并：钩子签名一个字不动（44 个内置插件、
  25 个测试模块零改动），但每个钩子归属一个挂载点，清单与控制台都能回答
  「这个插件在哪一步」。
"""
from __future__ import annotations

from typing import Dict, Iterable, Tuple

# 三个挂载点，按链路顺序
MOUNT_POINTS: Tuple[Tuple[str, str], ...] = (
    ("observe", "观察：消息进来、要不要拦、要不要直接回"),
    ("compose", "组装：拼提示词、预取外部数据、生成回复"),
    ("settle", "收尾：发出去了、改最终文本、周期后台任务"),
)

# 钩子 → 挂载点。键必须覆盖 PluginBase 的全部钩子，测试会核对。
HOOK_MOUNT: Dict[str, str] = {
    "on_observed": "observe",
    "on_ingest": "observe",
    "on_block": "observe",
    "on_pre_ai": "compose",
    "on_prepare": "compose",
    # describe / generate 不是 PluginBase 的方法（只有 image_understanding 与
    # chat_controls 各自实现），但它们是真实的能力名：清单里写这两个名字的插件
    # 确实在 compose 这一步起作用。PluginBase 的八个钩子 + 这两个 = 十个能力名。
    "describe": "compose",
    "generate": "compose",
    "on_sent": "settle",
    "on_post_reply": "settle",
    "tick": "settle",
}

# 不是钩子、但仍然要在清单里声明的能力（见 src/plugin_packs.py 的 CAPABILITY_KINDS）
NON_HOOK_KINDS: Tuple[str, ...] = ("settings", "helper")


def mount_of(hook: str) -> str:
    """这个钩子属于哪个挂载点；不是钩子时返回空串。"""
    return HOOK_MOUNT.get(str(hook).strip(), "")


def mount_points_of(capabilities: Iterable) -> Tuple[str, ...]:
    """把 capabilities 归成去重后的挂载点元组，按链路顺序排列。

    返回顺序固定为 observe → compose → settle，而不是按输入顺序 ——
    控制台直接拿它显示「先观察、再组装、后收尾」，不用自己排。
    """
    found = set()
    for item in capabilities or ():
        mount = mount_of(item)
        if mount:
            found.add(mount)
    return tuple(name for name, _label in MOUNT_POINTS if name in found)


def unknown_hooks(capabilities: Iterable) -> Tuple[str, ...]:
    """capabilities 里既不是已知钩子、也不是非钩子种类的名字。"""
    result = []
    for item in capabilities or ():
        text = str(item).strip()
        if text and text not in HOOK_MOUNT and text not in NON_HOOK_KINDS and text not in result:
            result.append(text)
    return tuple(result)


def describe(capabilities: Iterable) -> str:
    """一句话说明这个插件在哪几步起作用；给控制台的插件卡片用。"""
    mounts = mount_points_of(capabilities)
    labels = dict(MOUNT_POINTS)
    if mounts:
        return " / ".join(labels[name].split("：")[0] for name in mounts)
    for item in capabilities or ():
        if str(item) in NON_HOOK_KINDS:
            return "设置门" if str(item) == "settings" else "工具库"
    return "未声明"