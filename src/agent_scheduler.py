"""定时任务（1.3.0 P1-2）：只做本地检查与本地日报，**绝不发微信**。

动作白名单只有两个：
- `connection_check`：读一次 `bot.status()`，把结论写进任务记录（失败不自动重连、不外发）；
- `daily_digest`：把近 24 小时的收发/失败/token 写成 `data/agent_reports/daily-YYYYMMDD.txt`。

为什么这么窄：定时任务是"无人看管还会自己跑"的东西，一旦允许它发消息，
等于绕过了被动回复的全部闸门（@、白名单、频控、25 秒超时），所以这里连发送函数都不导入。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

ACTIONS = ("connection_check", "daily_digest")
MIN_INTERVAL = 5
MAX_INTERVAL = 1440
DEFAULT_INTERVAL = 60


class SchedulerError(ValueError):
    """可直接展示给用户的错误。"""


class AgentScheduler:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "agent_schedules.json"
        self.reports = Path(data_dir) / "agent_reports"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── 存储 ────────────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"tasks": []}

    def _save(self, data: dict) -> None:
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.path)

    def tasks(self) -> list:
        return self._load()["tasks"]

    def add(self, name: str, action: str, every_minutes: int = DEFAULT_INTERVAL,
            enabled: bool = True) -> dict:
        if action not in ACTIONS:
            raise SchedulerError(f"不支持的动作：{action}")
        try:
            minutes = int(every_minutes)
        except (TypeError, ValueError):
            raise SchedulerError("间隔要填数字（分钟）") from None
        if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
            raise SchedulerError(f"间隔要在 {MIN_INTERVAL}~{MAX_INTERVAL} 分钟之间")
        task = {"id": uuid.uuid4().hex[:12], "name": str(name or action)[:40],
                "action": action, "every_minutes": minutes, "enabled": bool(enabled),
                "last_run": 0, "last_result": ""}
        data = self._load()
        data["tasks"].append(task)
        self._save(data)
        return task

    def update(self, task_id: str, **fields) -> dict:
        data = self._load()
        for task in data["tasks"]:
            if task["id"] != task_id:
                continue
            if "every_minutes" in fields:
                minutes = int(fields["every_minutes"])
                if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
                    raise SchedulerError(f"间隔要在 {MIN_INTERVAL}~{MAX_INTERVAL} 分钟之间")
                task["every_minutes"] = minutes
            if "enabled" in fields:
                task["enabled"] = bool(fields["enabled"])
            if "name" in fields:
                task["name"] = str(fields["name"])[:40]
            # tick() 跑完要把运行时间与结果写回，否则下一轮永远"到期"，会连刷
            if "last_run" in fields:
                task["last_run"] = float(fields["last_run"] or 0)
            if "last_result" in fields:
                task["last_result"] = str(fields["last_result"])[:200]
            self._save(data)
            return task
        raise SchedulerError("任务不存在")

    def delete(self, task_id: str) -> bool:
        data = self._load()
        before = len(data["tasks"])
        data["tasks"] = [task for task in data["tasks"] if task["id"] != task_id]
        if len(data["tasks"]) == before:
            return False
        self._save(data)
        return True

    # ── 调度 ────────────────────────────────────────────────────────────
    @staticmethod
    def due(task: dict, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if not task.get("enabled"):
            return False
        last = float(task.get("last_run") or 0)
        return now - last >= max(MIN_INTERVAL, int(task.get("every_minutes") or DEFAULT_INTERVAL)) * 60

    def tick(self, bot, now: float | None = None) -> list:
        """跑到期的任务；返回本次执行记录（供日志与测试断言）。"""
        now = time.time() if now is None else now
        executed = []
        for task in self.tasks():
            if not self.due(task, now):
                continue
            try:
                result = self.run(bot, task["action"], now=now)
            except Exception as exc:
                result = f"失败：{type(exc).__name__}"
            self.update(task["id"], last_run=now, last_result=str(result)[:200])
            executed.append({"id": task["id"], "action": task["action"], "result": result})
        return executed

    # ── 动作 ────────────────────────────────────────────────────────────
    def run(self, bot, action: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        if action == "connection_check":
            status = bot.status()
            state = "运行中" if status.get("running") else "已停止"
            return (f"连接检查：机器人{state}；待处理 {status.get('pending', 0)} 条；"
                    f"模型 {status.get('model')}；插件 {status.get('plugins')} 个")
        if action == "daily_digest":
            return self.digest(bot, now=now)
        raise SchedulerError(f"不支持的动作：{action}")

    def digest(self, bot, now: float | None = None) -> str:
        """写本地日报；返回一行摘要（内容同时落盘，便于用户点开看）。"""
        now = time.time() if now is None else now
        since = now - 86400
        counts = {}
        try:
            rows = bot.db.execute(
                "SELECT state, COUNT(*) FROM inbox WHERE created>? GROUP BY state", (since,)).fetchall()
            counts = {str(state): int(number) for state, number in rows}
        except Exception:
            pass
        usage = {}
        try:
            usage = bot.ai.usage.summary
        except Exception:
            pass
        memory = {}
        try:
            memory = bot.memory_overview()
        except Exception:
            pass
        lines = [
            f"# 微信机器人日报 {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}",
            "",
            f"统计窗口：最近 24 小时",
            "",
            "## 收发",
            f"- 已回复：{counts.get('sent', 0)}",
            f"- 处理失败：{counts.get('failed', 0) + counts.get('uncertain', 0)}",
            f"- 过期未回：{counts.get('expired', 0)}",
            f"- 排队中：{counts.get('pending', 0) + counts.get('processing', 0) + counts.get('sending', 0)}",
            "",
            "## 用量",
            f"- 调用次数：{usage.get('request_count', 0)}",
            f"- token 合计：{usage.get('total_tokens', 0)}"
            f"（提示 {usage.get('prompt_tokens', 0)} / 生成 {usage.get('completion_tokens', 0)}）",
            "",
            "## 记忆",
            f"- 已确认：{memory.get('confirmed', 0)}；手动：{memory.get('manual', 0)}；"
            f"候选：{memory.get('candidate', 0)}；学习结果：{memory.get('learned', 0)}",
            "",
            "> 本报告只写在本机，不会发送到微信。",
        ]
        text = "\n".join(lines)
        self.reports.mkdir(parents=True, exist_ok=True)
        path = self.reports / f"daily-{time.strftime('%Y%m%d', time.localtime(now))}.txt"
        path.write_text(text, "utf-8")
        return f"日报已写入 {path.name}（已回复 {counts.get('sent', 0)} 条）"
