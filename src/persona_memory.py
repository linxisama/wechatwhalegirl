"""人格记忆目录与日记周期（1.3.109）。

用户 2026-11-08 的原话：

  我是更偏向于只保留主人格的情况下，其他的人格只有在特定的聊天中才会出现，
  然后主人格为之为基本的，所有的日记都是以主人格为主，这样以日记的方式保留了记忆，
  而其他人格的记忆，会单独再另外的文件夹里面保存，每个人格只会读取相应文件夹的记忆，
  然后日记每存储一周会删除一次，只留取重要的内容或者关键词，这个记到每个月清理一次的
  月记中，同样出现频率最多的事情会一直保留。

拆成四条：
  1. 主人格为基：日记默认写到主人格目录
  2. 人格独立目录：data/personas/<persona>/memory/，只能读自己那份
  3. 日记周期：7 天提炼成周记 → 月记每月清理一次 → 高频条目永久保留
  4. （心情 4 小时重置在 src/plugins/emotion_state.py）

为什么单独一个模块：这三件事都需要「按人格寻址 + 路径安全 + 周期计算」，
散在 agent_diary / agent_workspace 里会各写一份路径校验，然后其中一份漏掉 .. 检查。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .persistence import atomic_text

TZ = timezone(timedelta(hours=8))

# 人格名只允许这些字符。中文人格名是常见用法（鲸鱼娘、小猫），必须放行；
# 但分隔符 / 盘符 / .. 一律拒绝 —— 人格名会成为目录名，一个 .. 就跳出根目录。
PERSONA_NAME_RE = re.compile(r"^[\w\-\u4e00-\u9fff][\w.\-\u4e00-\u9fff ]{0,63}$")

DAILY_DIR = "daily"
WEEKLY_DIR = "weekly"
MONTHLY_DIR = "monthly"

WEEK_SECONDS = 7 * 86400


class PersonaMemoryError(ValueError):
    """人格名或路径不合法。继承 ValueError，调用方既有的 except 也能接住。"""


def safe_persona(name: str) -> str:
    """校验人格名；不合法抛 PersonaMemoryError。

    不合法时**抛错而不是清洗**：清洗会把「鲸鱼娘/../别的」变成「鲸鱼娘别的」，
    用户以为记到了 A 人格，实际记到了别处，而且没有任何提示。
    """
    text = str(name or "").strip()
    if not text:
        raise PersonaMemoryError("人格名不能为空")
    if text in (".", ".."):
        raise PersonaMemoryError("人格名不能是 . 或 ..：" + repr(name))
    if "/" in text or "\\" in text:
        raise PersonaMemoryError("人格名不能含路径分隔符：" + repr(name))
    if text[1:2] == ":":
        raise PersonaMemoryError("人格名不能带盘符：" + repr(name))
    if not PERSONA_NAME_RE.match(text):
        raise PersonaMemoryError("人格名只能用文字、数字、下划线、点、空格和横线（2-64 字符）：" + repr(name))
    return text


def persona_memory_dir(root, persona: str, create: bool = True) -> Path:
    """data/personas/<persona>/memory/ —— 每个人格只读自己这一份。"""
    name = safe_persona(persona)
    path = Path(root) / "data" / "personas" / name / "memory"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _day_path(root, persona: str, day: str) -> Path:
    base = persona_memory_dir(root, persona) / DAILY_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / (str(day) + ".json")


def write_daily(root, persona: str, day: str, content: str,
                keywords: Optional[Iterable[str]] = None) -> Path:
    """写一天的日记。同一天重复写会覆盖（最后一份为准）。"""
    path = _day_path(root, persona, day)
    payload = {"day": str(day), "persona": safe_persona(persona),
               "content": str(content or ""),
               "keywords": [str(item).strip() for item in (keywords or ()) if str(item).strip()],
               "written": time.time()}
    atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2), backup=False)
    return path


def read_daily(root, persona: str, day: str) -> Optional[dict]:
    try:
        return json.loads(_day_path(root, persona, day).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def list_daily(root, persona: str) -> List[str]:
    """这个人格已有的日记日期，升序。"""
    base = persona_memory_dir(root, persona) / DAILY_DIR
    if not base.is_dir():
        return []
    return sorted(path.stem for path in base.glob("*.json"))


def _keyword_counts(entries: Iterable[dict]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for entry in entries:
        for word in entry.get("keywords") or ():
            text = str(word).strip()
            if text:
                counts[text] = counts.get(text, 0) + 1
    return counts


def rollup_week(root, persona: str, keep_days: int = 7,
                min_frequency: int = 2) -> dict:
    """把最老的 7 天日记压成一份周记，并删掉那 7 天的原始文件。

    「只留取重要的内容或者关键词」的落地方式：
      - 关键词按出现次数排序，出现 >= min_frequency 次的进周记；
      - 每条日记的正文取前 200 字进周记的摘要（不做模型总结，避免离线时不可用）；
      - 原始文件删掉 —— 这是「每存储一周会删除一次」的字面意思。

    不足 keep_days 天时什么都不做，返回 rolled=False。
    """
    days = list_daily(root, persona)
    if len(days) < keep_days:
        return {"rolled": False, "days": len(days), "kept": days}
    window = days[:keep_days]
    entries = [entry for entry in (read_daily(root, persona, day) for day in window) if entry]
    counts = _keyword_counts(entries)
    important = sorted((word for word, count in counts.items() if count >= min_frequency),
                       key=lambda word: (-counts[word], word))
    summary = []
    for entry in entries:
        body = str(entry.get("content") or "").strip().replace("\n", " ")[:200]
        if body:
            summary.append("- " + entry.get("day", "") + "：" + body)
    payload = {
        "persona": safe_persona(persona),
        "from_day": window[0],
        "to_day": window[-1],
        "days": window,
        "keywords": important,
        "keyword_counts": {word: counts[word] for word in important},
        "summary": summary,
        "rolled": time.time(),
    }
    base = persona_memory_dir(root, persona) / WEEKLY_DIR
    base.mkdir(parents=True, exist_ok=True)
    target = base / (window[0] + "_" + window[-1] + ".json")
    atomic_text(target, json.dumps(payload, ensure_ascii=False, indent=2), backup=False)
    removed = []
    for day in window:
        try:
            _day_path(root, persona, day).unlink()
            removed.append(day)
        except OSError:
            pass
    return {"rolled": True, "from_day": window[0], "to_day": window[-1],
            "keywords": important, "removed": removed, "path": str(target)}


def list_weekly(root, persona: str) -> List[str]:
    base = persona_memory_dir(root, persona) / WEEKLY_DIR
    if not base.is_dir():
        return []
    return sorted(path.stem for path in base.glob("*.json"))


def rollup_month(root, persona: str, min_frequency: int = 2) -> dict:
    """把这个月攒下的周记汇成月记；高频关键词永久保留。

    与周记的区别：月记**不删周记**（周记是「这一周发生了什么」的原始记录，
    删了就没法回溯；月记是「这个月反复出现什么」的结论）。
    """
    names = list_weekly(root, persona)
    if not names:
        return {"rolled": False, "weeks": 0}
    entries = []
    for name in names:
        path = persona_memory_dir(root, persona) / WEEKLY_DIR / (name + ".json")
        try:
            entries.append(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError):
            continue
    counts: Dict[str, int] = {}
    for entry in entries:
        for word, count in (entry.get("keyword_counts") or {}).items():
            counts[word] = counts.get(word, 0) + int(count)
    permanent = sorted((word for word, count in counts.items() if count >= min_frequency),
                       key=lambda word: (-counts[word], word))
    month = (names[0].split("_")[0] if names else "")[:7]
    payload = {
        "persona": safe_persona(persona),
        "month": month,
        "weeks": names,
        "keywords": permanent,
        "keyword_counts": {word: counts[word] for word in permanent},
        "rolled": time.time(),
    }
    base = persona_memory_dir(root, persona) / MONTHLY_DIR
    base.mkdir(parents=True, exist_ok=True)
    target = base / (month + ".json")
    atomic_text(target, json.dumps(payload, ensure_ascii=False, indent=2), backup=False)
    return {"rolled": True, "month": month, "keywords": permanent, "path": str(target)}


def monthly_keywords(root, persona: str, month: Optional[str] = None) -> List[str]:
    """读月记里的关键词；没写过返回空列表。给提示词注入用。"""
    base = persona_memory_dir(root, persona) / MONTHLY_DIR
    if not base.is_dir():
        return []
    if month:
        path = base / (str(month) + ".json")
    else:
        files = sorted(base.glob("*.json"))
        if not files:
            return []
        path = files[-1]
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return [str(word) for word in payload.get("keywords") or ()]