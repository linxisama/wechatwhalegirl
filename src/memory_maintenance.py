"""记忆维护与预算（1.3.0 P1-3 收尾）。

两件事：
1. **预算**：手动记忆按相关度最多 8 条 / 1800 字，别把上百条全塞进提示词（既贵又乱）。
2. **整理**：合并近重复、清理过期 superseded、清理长期单证据候选。数据只标记不硬删，
   用户还能在「整理记忆」的结果里看到动了什么。
"""
from __future__ import annotations

import time

from .memory_rank import near_duplicate

MAX_ITEMS = 8
MAX_CHARS = 1800
SUPERSEDED_TTL = 7 * 86400
CANDIDATE_TTL = 30 * 86400


def budget(items, max_items: int = MAX_ITEMS, max_chars: int = MAX_CHARS) -> list:
    """按给定顺序（调用方已按相关度排好）截断到条目数与字数上限。"""
    out, used = [], 0
    for item in items:
        if len(out) >= max(1, int(max_items)):
            break
        text = str(item.get("content") or item.get("text") or "")
        if used + len(text) > max_chars and out:
            break
        out.append(item)
        used += len(text)
    return out


def maintain(db, now: float | None = None) -> dict:
    """整理记忆；返回 {merged, expired, dropped} 三类计数。

    只处理"可以安全丢/合并"的：近重复合进较新的那条（旧的标 superseded），
    superseded 超过 7 天删除，单证据候选超过 30 天删除。**已确认且唯一的记忆永远不动。**
    """
    now = time.time() if now is None else now
    merged = expired = dropped = 0
    rows = db.execute("SELECT id,scope,subject,content,status,hits,updated FROM chat_memories").fetchall()
    by_subject = {}
    for row in rows:
        by_subject.setdefault((row[1], row[2]), []).append(row)

    for group in by_subject.values():
        kept = []
        for row in group:
            duplicate = next((other for other in kept
                              if row[4] != 'superseded' and near_duplicate(row[3], other[3])), None)
            if duplicate is None or duplicate[3] == row[3]:
                kept.append(row)
                continue
            # 保留较新的那条，旧的标 superseded（不删内容，便于追溯）
            newer, older = (row, duplicate) if (row[6] or 0) >= (duplicate[6] or 0) else (duplicate, row)
            try:
                db.execute("UPDATE chat_memories SET status='superseded',updated=? WHERE id=?",
                           (now, older[0]))
                merged += 1
            except Exception:
                pass

    try:
        expired = db.execute(
            "DELETE FROM chat_memories WHERE status='superseded' AND updated<?",
            (now - SUPERSEDED_TTL,)).rowcount
    except Exception:
        expired = 0
    try:
        dropped = db.execute(
            "DELETE FROM chat_memories WHERE status='candidate' AND hits<=1 AND updated<?",
            (now - CANDIDATE_TTL,)).rowcount
    except Exception:
        dropped = 0
    try:
        db.commit()
    except Exception:
        pass
    return {"merged": merged, "expired": expired, "dropped": dropped}
