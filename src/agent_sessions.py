"""Agent 多会话与任务卡片（1.3.0 P0-4）。

为什么不用数据库：会话是"操作记录"而不是聊天数据，用户可能想整段删掉/拷走；
放一个 JSON 文件最方便导出与人工检查，也避免污染 messages.sqlite3 的同步路径。

容量上限按计划：最多 50 段会话、每段 200 张卡片。超了从**最旧的会话/卡片**开始丢，
不会因为用户聊太多而把文件撑到几十兆。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

MAX_SESSIONS = 50
MAX_CARDS = 200


def _now() -> float:
    return time.time()


class AgentSessions:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── 存取 ──────────────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"sessions": []}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.path)

    # ── 会话 ──────────────────────────────────────────────────────────────
    def list_sessions(self) -> list:
        """列表只回摘要，不回整段卡片：会话多时列表会变得又大又慢。"""
        out = []
        for session in self._load()["sessions"]:
            out.append({"id": session["id"], "title": session.get("title", ""),
                        "created": session.get("created", 0), "updated": session.get("updated", 0),
                        "cards": len(session.get("cards") or [])})
        return sorted(out, key=lambda item: item.get("updated") or 0, reverse=True)

    def create(self, title: str = "新会话") -> dict:
        data = self._load()
        session = {"id": uuid.uuid4().hex[:12], "title": str(title or "新会话")[:60],
                   "created": _now(), "updated": _now(), "cards": []}
        data["sessions"].append(session)
        data["sessions"] = sorted(data["sessions"], key=lambda s: s.get("updated", 0),
                                  reverse=True)[:MAX_SESSIONS]
        self._save(data)
        return session

    def get(self, session_id: str) -> dict | None:
        for session in self._load()["sessions"]:
            if session["id"] == session_id:
                return session
        return None

    def delete(self, session_id: str) -> bool:
        data = self._load()
        before = len(data["sessions"])
        data["sessions"] = [s for s in data["sessions"] if s["id"] != session_id]
        if len(data["sessions"]) == before:
            return False
        self._save(data)
        return True

    # ── 卡片 ──────────────────────────────────────────────────────────────
    def add_card(self, session_id: str, card: dict) -> dict:
        data = self._load()
        for session in data["sessions"]:
            if session["id"] != session_id:
                continue
            card = dict(card)
            card.setdefault("id", uuid.uuid4().hex[:12])
            card.setdefault("created", _now())
            card.setdefault("state", "preview")
            session.setdefault("cards", []).append(card)
            session["cards"] = session["cards"][-MAX_CARDS:]
            session["updated"] = _now()
            self._save(data)
            return card
        raise KeyError("会话不存在")

    def update_card(self, session_id: str, card_id: str, **fields) -> dict:
        data = self._load()
        for session in data["sessions"]:
            if session["id"] != session_id:
                continue
            for card in session.setdefault("cards", []):
                if card.get("id") == card_id:
                    card.update(fields)
                    card["updated"] = _now()
                    session["updated"] = _now()
                    self._save(data)
                    return card
            raise KeyError("卡片不存在")
        raise KeyError("会话不存在")
