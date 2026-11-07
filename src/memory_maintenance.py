"""记忆维护与预算（1.3.0 P1-3 收尾；1.3.92 加去重/配置/淘汰）。

四件事：
1. **预算**：手动记忆按相关度最多 8 条 / 1800 字，别把上百条全塞进提示词（既贵又乱）。
2. **去重**：同一句话常被记成多条（原话、加标点、加主语），预算里必须先去重再截断，
   否则一条记忆占掉两个名额，把新信息挤出去。
3. **整理**：合并近重复、清理过期 superseded、清理长期单证据候选。数据只标记不硬删，
   用户还能在「整理记忆」的结果里看到动了什么。
4. **淘汰**：容量上限从模块常量改成可配置；淘汰按"相关度 + 新鲜度 + 命中"算分，
   而不是按 updated 的 FIFO——老但常被复证的事实不该先死。

所有数值都是"字"不是 token；估算口径见 token_ledger.estimate_tokens。
"""
from __future__ import annotations

import time

from .memory_rank import content_of, near_duplicate

MAX_ITEMS = 8
MAX_CHARS = 1800
SUPERSEDED_TTL = 7 * 86400
CANDIDATE_TTL = 30 * 86400
# 容量上限（条）：超过就淘汰得分最低的。旧版把它写死在 chat_state.remember() 的
# LIMIT max(10,min(5000,maximum)) 里，调用方无法按人格/场景收紧，这里给一份默认值。
MAX_TOTAL_ITEMS = 3000
# 淘汰保护：最近这么多秒内动过的记忆一律不动，避免刚记住就被自己的淘汰规则清掉
EVICT_GRACE = 3600.0
# 淘汰打分权重：命中次数占大头（复证＝真实价值），其次相关度/新鲜度
EVICT_W_HITS = 0.5
EVICT_W_FRESH = 0.3
EVICT_W_SELF = 0.2


def budget(items, max_items: int = MAX_ITEMS, max_chars: int = MAX_CHARS,
           dedupe: bool = True, threshold: float = 0.82) -> list:
    """先按近重复去重，再按给定顺序截断到条目数与字数上限。

    调用方已经按相关度排好序；这里只做"同一件事只留一条 + 砍到预算内"。
    首条永远保留：单条超长时也要给调用方一条可用记忆，否则预算就把记忆清零了。
    """
    out, used = [], 0
    seen = []
    for item in items or ():
        if len(out) >= max(1, int(max_items)):
            break
        text = str(item.get("content") or item.get("text") or "") if isinstance(item, dict) \
            else content_of(item)
        if dedupe and text:
            if any(near_duplicate(text, other, threshold) for other in seen):
                continue
            seen.append(text)
        if used + len(text) > max_chars and out:
            break
        out.append(item)
        used += len(text)
    return out


def evict_score(row, now: float) -> float:
    """淘汰打分：命中越多、越新、内容越具体（越长）越该留。分数低者先淘汰。"""
    hits = float(row[5] or 0)
    updated = float(row[6] or 0)
    text = str(row[3] or "")
    age_days = max(0.0, (now - updated) / 86400.0) if updated else 365.0
    fresh = 0.5 ** (age_days / 14.0)
    # 命中次数归一：5 次封顶，和 memory_rank.evidence_score 同口径
    evidence = min(1.0, max(0.0, hits) / 5.0)
    detail = min(1.0, len(text) / 40.0)
    return EVICT_W_HITS * evidence + EVICT_W_FRESH * fresh + EVICT_W_SELF * detail


def _commit(db) -> bool:
    """提交事务；失败就回滚并返回 False —— 不能把没落盘的改动当成已生效。"""
    try:
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def evict(db, maximum: int = MAX_TOTAL_ITEMS, now: float | None = None,
          grace: float = EVICT_GRACE, commit: bool = True) -> int:
    """超出容量上限时淘汰得分最低的记忆，返回淘汰条数。

    为什么不是 FIFO：按 updated 排序删最旧的，会优先删掉"很久没提但被复证过"的
    稳定事实（口味、住址），而这些恰恰是最该长期保留的；反而留下刚被误记的一条。
    淘汰只动 confirmed/candidate 里得分最低的，superseded 交给 maintain 的 TTL 清。

    commit=False 时本函数不提交也不回滚，事务交给 caller：ChatState.remember 的
    INSERT 还没提交，这里一 rollback 就会把用户刚说的那条记忆一起丢掉。
    """
    now = time.time() if now is None else now
    try:
        cap = max(1, int(maximum))
    except (TypeError, ValueError):
        cap = MAX_TOTAL_ITEMS
    try:
        rows = db.execute(
            "SELECT id,scope,subject,content,status,hits,updated FROM chat_memories").fetchall()
    except Exception:
        return 0
    if len(rows) <= cap:
        return 0
    removable = [row for row in rows
                 if str(row[4] or '') != 'superseded'
                 and (not row[6] or now - float(row[6]) >= grace)]
    excess = len(rows) - cap
    if not removable or excess <= 0:
        return 0
    removable.sort(key=lambda row: (evict_score(row, now), float(row[6] or 0)))
    removed = 0
    for row in removable[:excess]:
        try:
            db.execute("DELETE FROM chat_memories WHERE id=?", (row[0],))
            removed += 1
        except Exception:
            continue
    if not commit:
        # caller 自己持有事务（ChatState.remember 要在同一次提交里连 INSERT 一起落盘），
        # 这里既不提交也不回滚，免得把 caller 还没提交的写入一起丢掉。
        return removed
    if not _commit(db):
        # 没提交成功等于一条都没删：回滚后按 0 条报告，别让调用方以为清理过了。
        return 0
    return removed


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
    if not _commit(db):
        # 同上：回滚之后 merged/expired/dropped 一条都没留下，不能报非零。
        return {"merged": 0, "expired": 0, "dropped": 0, "evicted": 0,
                "persisted": False}
    # 整理完顺手按容量上限淘汰一次：maintain 是"用户点整理"的入口，淘汰放这里
    # 而不是每条消息都算，避免在最热的写入路径上做全表打分。
    evicted = evict(db, now=now)
    return {"merged": merged, "expired": expired, "dropped": dropped,
            "evicted": evicted, "persisted": True}
