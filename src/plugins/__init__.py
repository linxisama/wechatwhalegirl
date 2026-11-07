"""Plugin system for the WeChat robot - 触发节奏 trigger rhythm plugins."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class FieldSchema:
    type: str
    label: str
    description: str = ""
    group: str = ""
    values: List[str] = field(default_factory=list)
    options: List[Dict] = field(default_factory=list)
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    multiline: bool = False
    file_picker: str = ""  # file / files / labeled_files / directory
    file_extensions: List[str] = field(default_factory=list)
    file_max_mb: int = 6
    editor: str = ""


@dataclass
class PluginMeta:
    id: str
    name: str
    description: str
    category: str = "utility"
    enabled_by_default: bool = True
    config_schema: Dict[str, FieldSchema] = field(default_factory=dict)
    default_settings: Dict[str, Any] = field(default_factory=dict)
    # 以下四个字段**全部有默认值**：存量插件一个都不用改就能继续加载。
    # 只有插件清单（plugin.json）或作者显式写 META 时才会填上，宿主不靠它们跑逻辑。
    version: str = ""          # 插件自身版本，例 "1.2.0"
    api_version: str = ""      # 插件声明的宿主 API 版本，空=按宿主默认
    author: str = ""           # 作者，单行文本
    min_host_version: str = "" # 要求的最低宿主版本，空=不限制
    # 竞标权重（1.3.104）：只在**排他钩子**（on_ingest / on_block）上起作用。
    # 排他钩子的语义是「一个插件说话，其余闭嘴」：on_ingest 返回字符串 = 立即回复并
    # 跳过模型，on_block 返回 True = 丢弃这条消息。多个插件同时命中时，过去按注册
    # 顺序先到先得 —— 谁先被 import 谁赢，用户完全无从判断为什么是它在回。
    # 现在按 (权重降序, 注册顺序) 排序后依次问，第一个给出结果的赢，其余记进
    # runtime.bid_trace 备查。权重相同仍按注册顺序，所以不填权重的存量插件行为不变。
    capability_weight: int = 0
    # 权限声明（1.3.105）：插件在清单或 META 里写自己要用哪些能力。
    # 五项：net（联网）/ fs_read（读本机文件）/ fs_write（写本机文件）/
    #       exec（起子进程）/ wechat_send（绕过宿主闸门直接发消息）。
    # 空列表 = 什么都不声明。声明是**诚实义务**：不声明的插件用不到对应能力，
    # 用到时宿主会拒绝并记一次故障（见 src/plugin_guard.py）。
    # 为什么不强制所有存量插件补声明：44 个内置插件里有 7 个真的联网或读写文件，
    # 一刀切会让它们在升级当天全部被隔离。所以**默认只审计不拦截**，
    # 由 plugin_guard.enforce 开关控制（默认 False），与 1.3.103 的双档清单同一个思路。
    permissions: tuple = ()
    def schema_dict(self) -> dict:
        result = {}
        for k, v in self.config_schema.items():
            d = {"type": v.type, "label": v.label, "description": v.description}
            if v.group:
                d["group"] = v.group
            if v.values:
                d["values"] = v.values
            if v.options:
                d["options"] = v.options
            if v.min is not None:
                d["min"] = v.min
            if v.max is not None:
                d["max"] = v.max
            if v.step is not None:
                d["step"] = v.step
            if v.multiline:
                d["multiline"] = True
            if v.file_picker:
                d.update(file_picker=v.file_picker, file_extensions=list(v.file_extensions), file_max_mb=v.file_max_mb)
            if v.editor:
                d['editor']=v.editor
            result[k] = d
        return result


class PluginBase:
    META: PluginMeta = None

    def __init__(self, manager):
        self._manager = manager

    @property
    def settings(self) -> dict:
        return self._manager.get_settings(self.META.id)

    @property
    def raw_settings(self) -> dict:
        """已保存的**原始**设置，不做 schema 过滤。

        ``settings`` 只返回 schema 里已知的键，所以**看不见已废弃的旧键**
        （例如 mention_gate 早期的 ``onlyGroups``）。做"旧键迁移"必须用这个，
        否则旧值在读取阶段就被过滤掉了，迁移拿不到东西。
        """
        stored = self._manager._settings.get(self.META.id)
        return dict(stored) if isinstance(stored, dict) else {}

    @property
    def enabled(self) -> bool:
        if getattr(self._manager, 'is_quarantined', lambda pid: False)(self.META.id):
            return False
        if self.META.id in getattr(self._manager,'_removed',set()):
            return False
        return bool(self.settings.get("enabled", self.META.enabled_by_default))

    def get(self, key, default=None):
        s = self.settings
        if key in s:
            return s[key]
        return self.META.default_settings.get(key, default)

    def on_observed(self, message: dict, runtime) -> None:
        """只登记已观察消息；下载等异步工作放在 on_prepare 或 tick。"""

    def agent_tools(self) -> list:
        """可选工作台工具声明：name/description/capability；旧插件无需实现。"""
        return []

    async def agent_invoke(self, name: str, arguments: dict, workspace) -> str:
        """只在主人显式/技能命令后调用，workspace提供有界笔记/记忆读写。"""
        raise ValueError('此插件没有Agent工具')

    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        """Return a str to immediately reply (skip AI), None to continue."""
        return None

    def on_block(self, message: dict, runtime) -> bool:
        """Return True to silently drop this message."""
        return False

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        """Return dict with optional keys: delay(s), block, extra_system."""
        return None

    def validate_settings(self, settings: dict) -> None:
        """Optional validation of structured values after schema validation."""

    async def on_prepare(self, message: dict, runtime) -> None:
        """Await protocol lookups before constructing the model conversation."""

    def on_sent(self, message: dict, reply: str, runtime) -> None:
        """Record only successfully sent results."""

    async def on_post_reply(self, message: dict, reply: str, runtime) -> str:
        """Transform the final reply string (send-pacing uses this)."""
        return reply

    async def tick(self, runtime) -> None:
        """Periodic background task called every ~60s when bot is running."""

    def post_load(self) -> None:
        """在**实例已被管理器注册之后**调用一次；默认什么都不做。

        为什么需要这个钩子：插件的 ``__init__`` 跑在管理器把实例放进
        ``_plugins`` **之前**，所以 ``__init__`` 里调
        ``self._manager.update_settings(...)`` 会抛
        ``KeyError: Unknown plugin: <id>``。
        ``mention_gate`` 的旧键迁移就是这么静默失败的（日志里只有一行
        "迁移失败（不影响运行）"）。需要落盘改设置的插件请把逻辑放这儿。
        """

    def state_file(self, name: str) -> Path:
        p = self._manager.state_dir / self.META.id
        p.mkdir(parents=True, exist_ok=True)
        return p / name

    def load_state(self, name: str, default=None):
        f = self.state_file(name)
        try:
            return json.loads(f.read_text("utf-8"))
        except Exception:
            return {} if default is None else default

    def save_state(self, name: str, data):
        # 状态文件写坏后 load_state 会静默回落成默认值，插件看上去「重置了」，
        # 用户无从察觉。走 atomic_text 先写临时文件再替换，失败时旧状态还在。
        from ..persistence import atomic_text
        atomic_text(self.state_file(name),
                    json.dumps(data, ensure_ascii=False, indent=2), backup=False)
