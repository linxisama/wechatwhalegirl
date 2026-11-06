"""记忆召回排序、冲突覆盖与维护（1.3.0 P1-3）。

按计划的口径：`0.62*相似度 + 0.20*新鲜度 + 0.18*复证次数`。
为什么不能只按"最近更新"排：用户问"我住哪"时，一条三天前、只说过一次但高度相关的记忆，
应该排在昨天随口说的无关内容前面；反过来，问"最近在忙什么"时新鲜度就该占上风。

纯函数模块：不碰数据库、不依赖运行时，方便测试与离线校验。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# 概念归一：不同说法指向同一件事，召回时按同一组算
CONCEPTS = {
    "住处": ("住", "搬到", "搬家", "定居", "租房"),
    "称呼": ("叫", "昵称", "名字", "称呼"),
    "工作": ("上班", "工作", "公司", "实习", "打工", "学校", "上学"),
    "喜好": ("喜欢", "偏爱", "讨厌", "不喜欢", "爱吃", "不吃"),
}

# 变化词：出现这些说明"旧事实被替代"，对应记忆要标 superseded
CHANGE_WORDS = {
    "住处": ("搬家", "搬到", "搬去", "换了住处", "现在住"),
    "称呼": ("改名", "改叫", "现在叫", "换成叫"),
    "工作": ("换工作", "跳槽", "离职", "入职", "转学", "毕业"),
}

# 敏感信息：命中就不入库（safe_fact 已有密码/密钥，这里补证件与验证码）
SENSITIVE = (
    r"\d{17}[\dxX]",                      # 身份证
    r"(?:银行卡|卡号)\D{0,4}\d{12,}",      # 银行卡
    r"(?:验证码|校验码)\D{0,4}\d{4,8}",    # 验证码
    r"\b\d{16,19}\b",                      # 长串数字（疑似卡号）
)


def bigrams(text: str) -> set:
    """中文二元组 + 英文整词：够用的粗相似度，不需要分词器。"""
    raw = str(text or "").lower()
    words = set(re.findall(r"[a-z0-9]{2,}", raw))
    grams = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", raw):
        grams.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return words | grams


def similarity(query: str, content: str) -> float:
    """0~1：二元组重合（主）+ 概念归一（补）+ 字面包含（兜底）。"""
    if not query:
        return 0.0
    left, right = bigrams(query), bigrams(content)
    if not left or not right:
        return 0.0
    overlap = len(left & right) / len(left)
    bonus = 0.0
    for words in CONCEPTS.values():
        if any(word in query for word in words) and any(word in content for word in words):
            bonus = 0.25
            break
    if query in content:
        bonus = max(bonus, 0.5)
    return min(1.0, overlap + bonus)


def freshness(updated: float, now: float, half_life_days: float = 14.0) -> float:
    """半衰期衰减：默认 14 天折半，越旧越接近 0（不直接为 0，避免老事实彻底看不见）。"""
    if not updated:
        return 0.0
    age_days = max(0.0, (now - float(updated)) / 86400.0)
    return 0.5 ** (age_days / max(0.5, half_life_days))


def evidence_score(hits) -> float:
    """复证次数：1 次算 0.2，3 次 0.6，5 次及以上满分。"""
    try:
        count = int(hits or 0)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0, count) / 5.0)


def score(query: str, content: str, updated: float, hits, now: float,
          half_life_days: float = 14.0) -> float:
    """计划里的权重：相似度 0.62 / 新鲜度 0.20 / 复证 0.18。"""
    return (0.62 * similarity(query, content) +
            0.20 * freshness(updated, now, half_life_days) +
            0.18 * evidence_score(hits))


def is_sensitive(text: str) -> bool:
    return any(re.search(pattern, str(text or ""), re.I) for pattern in SENSITIVE)


def supersedes(old_content: str, new_content: str) -> bool:
    """新记忆是否把旧记忆"顶掉"（搬家/改名/换工作这类变化）。

    只在**同一概念组**里判断：说"我喜欢猫"不该把"我喜欢狗"标成过期。
    """
    for concept, words in CHANGE_WORDS.items():
        if not any(word in new_content for word in words):
            continue
        group = CONCEPTS.get(concept, ())
        if any(word in old_content for word in group) and not any(
                word in new_content for word in ("不", "没")):
            return True
    return False


def near_duplicate(left: str, right: str, threshold: float = 0.92) -> bool:
    """近重复判断（维护时合并用）。"""
    return SequenceMatcher(None, str(left), str(right)).ratio() >= threshold


# 召回时的默认近重复阈值：比维护（0.92）松一点，因为召回要防的是"同一件事换个说法
# 占掉两个名额"，而维护要防的是误合并两条不同事实。
RECALL_DUP_THRESHOLD = 0.82


def content_of(item) -> str:
    """兼容 dict / sqlite Row / 纯字符串三种形态。"""
    if isinstance(item, str):
        return item
    try:
        return str(item["content"] or "")
    except Exception:
        try:
            return str(item[3] or "")
        except Exception:
            return ""


def field_of(item, name, index, default=0):
    """按名字取值，取不到就按列号取；两种都失败返回默认值。"""
    if isinstance(item, dict):
        return item.get(name, default)
    try:
        return item[name]
    except Exception:
        try:
            return item[index]
        except Exception:
            return default


def dedupe(items, threshold: float = RECALL_DUP_THRESHOLD, key=None):
    """去掉近重复条目，保留**先出现的**那条。

    调用方先按相关度排好序，所以"先出现"就是"更该被召回"的那条。为什么必须去重：
    用户同一句话往往被反复记成多条（原话、加标点、加主语），全都进上下文既贵又
    把新信息挤掉——这就是"同一句话反复被召回"的真实成因。
    返回 (保留列表, 去掉的条数)。
    """
    kept, dropped = [], 0
    seen_text = []
    for item in items or ():
        text = key(item) if callable(key) else content_of(item)
        text = str(text or "")
        if any(near_duplicate(text, other, threshold) for other in seen_text):
            dropped += 1
            continue
        seen_text.append(text)
        kept.append(item)
    return kept, dropped


def rank(items, query: str, now: float, limit: int = 5, half_life_days: float = 14.0,
         min_score: float = 0.10, dup_threshold: float = RECALL_DUP_THRESHOLD):
    """按 score 排序 + 去重 + 截断，返回 (结果列表, 统计)。

    统计里 kept/dropped/duplicates 是给日志用的：召回到底压掉了多少重复、
    有多少条因为相关度不足被丢弃，用户第一次能看见。
    """
    scored = []
    for item in items or ():
        text = content_of(item)
        if not text:
            continue
        updated = field_of(item, "updated", 7, 0)
        hits = field_of(item, "hits", 5, 0)
        try:
            value = score(query, text, float(updated or 0), hits, now, half_life_days)
        except (TypeError, ValueError):
            value = 0.0
        scored.append((value, item))
    # 稳定排序：同分时保持调用方给的顺序（通常是 updated DESC），行为可预测
    scored.sort(key=lambda pair: pair[0], reverse=True)
    threshold = float(min_score or 0)
    filtered = [item for value, item in scored if not query or value >= threshold]
    kept, dropped = dedupe(filtered, dup_threshold)
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 5
    return kept[:cap], {"scored": len(scored), "kept": min(cap, len(kept)),
                        "duplicates": dropped,
                        "below_threshold": len(scored) - len(filtered)}
