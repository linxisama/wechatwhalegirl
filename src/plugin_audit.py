"""插件审计：每次插件调入写一行 JSONL，出问题能倒查"哪个插件、哪个钩子、多久、成没成"。

移植自 QQ 版 plugin_core 的审计思路（那边也是追加式 JSONL + 只记元信息），
但按微信端的隐私规矩收了口径：

- **绝不记消息正文、会话名、回复内容**。只记插件 id、钩子名、耗时、成功与否、
  异常摘要（截断 200 字、换行压成空格）。这条与 src/logging_setup.py 的
  "正文绝不落盘"是同一套红线，审计不能成为例外。
- 每行独立可解析；写失败一律吞掉（审计坏了不能把插件链路带下水）。
- 单文件到 2MB 就滚一次，只留上一份（plugin_audit.jsonl.1），不会无限长大。
- 关掉：环境变量 WECHAT_PLUGIN_AUDIT=0/off/false。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

FILE_NAME = "plugin_audit.jsonl"
MAX_BYTES = 2 * 1024 * 1024          # 单个文件上限：到点滚一次，别让审计把磁盘吃满
ERROR_KEEP = 200                     # 异常摘要最多留 200 字
ENV_SWITCH = "WECHAT_PLUGIN_AUDIT"
_OFF_VALUES = ("0", "off", "false", "no")

_lock = threading.Lock()
_path_override = None


def enabled() -> bool:
    """审计默认开着；显式设成 0/off/false/no 才关。"""
    return str(os.environ.get(ENV_SWITCH, "")).strip().lower() not in _OFF_VALUES


def set_path(path):
    """测试/特殊部署用：把审计文件指到别处（None 恢复默认）。"""
    global _path_override
    _path_override = Path(path) if path is not None else None


def audit_path(fallback=None) -> Path:
    """审计文件落在日志目录（与 backend.log 同级），拿不到就退回 fallback。"""
    if _path_override is not None:
        return _path_override
    try:
        from .logging_setup import backend_log_dir
        return backend_log_dir(fallback=fallback) / FILE_NAME
    except Exception:
        base = Path(fallback) if fallback is not None else Path.cwd() / "logs"
        return base / FILE_NAME


def hook_label(method) -> str:
    name = str(getattr(method, "__name__", "") or "").strip()
    return name or "?"


def _safe_error(exc) -> str:
    """异常摘要：类型 + 首 200 字，换行压空格（一行一条记录）。"""
    if exc is None:
        return ""
    try:
        text = str(exc)
    except Exception:
        text = ""
    text = " ".join(text.split())
    label = type(exc).__name__
    return (label + (": " + text if text else ""))[:ERROR_KEEP]


def _rotate(path: Path) -> None:
    """到上限就滚一次：旧文件改名成 .1（覆盖更旧的一份）。"""
    try:
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            os.replace(str(path), str(path) + ".1")
    except Exception:
        pass


def record(plugin, hook, ok, ms, error=None, stage=None) -> None:
    """记一条；**任何异常都不许冒泡**（审计只是旁路观测）。"""
    if not enabled():
        return
    try:
        row = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "plugin": str(plugin or "unknown"),
            "hook": str(hook or "?"),
            "ok": bool(ok),
            "ms": int(max(0.0, float(ms))),
        }
        if stage:
            row["stage"] = str(stage)
        if not ok:
            row["error"] = _safe_error(error)
        line = json.dumps(row, ensure_ascii=False)
        path = audit_path()
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate(path)
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
    except Exception:
        return


def read_records(limit=50, path=None) -> list:
    """从尾部读最近若干条（坏行直接跳过）；用于接口/测试查看，不参与判定。"""
    target = Path(path) if path is not None else audit_path()
    try:
        lines = target.read_text("utf-8", errors="replace").splitlines()
    except Exception:
        return []
    rows = []
    for line in lines[-max(1, int(limit)):]:
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def timed(stage_hint=None):
    """一秒级小工具：给调用点算耗时用（保留 perf_counter 精度）。"""
    return time.perf_counter()
