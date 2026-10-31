"""DSH 本轮记忆注入的显式清单：只读 message['_memory_items']，纯标准库。

为什么要有这一层：长期记忆过去只随 system_prompt 的 plugins 块进入 DSH，预算一紧
整块被丢，事后也没有任何地方说得清“这一轮到底注入了哪几条”。这里把插件真正插进
conversation 的原文收成不可变清单：render() 交给 DSH 做人设尾部块，log_line() 与
recent() 只留统计（条数、字数），**绝不保留任何记忆正文**。

约定：数据来源只有 message['_memory_items']（由 conversation_memory 插件挂上）。
本模块不打开数据库、不 import 插件、不联网，因此任何调用点都能安全使用。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType

MAX_ITEMS = 12
MAX_CHARS = 1200
ITEM_CHARS = 200
RECENT_LIMIT = 20

SOURCES = ('manual', 'notes', 'learned')
AVENUES = SOURCES + ('archive',)
LABELS = {'manual': '手动记忆', 'notes': '长期记忆', 'learned': '学习结果'}


@dataclass(frozen=True)
class Item:
    """一条要注入的记忆：source 决定排序与标签，text 是原文（不截断）。"""
    source: str
    text: str


def _select(items):
    """按顺序保前丢后：每条封顶 ITEM_CHARS，最多 MAX_ITEMS 条 / MAX_CHARS 字。"""
    shown, used = [], 0
    for item in items:
        text = item.text
        if len(text) > ITEM_CHARS:
            text = text[:ITEM_CHARS - 1] + '…'
        if len(shown) >= MAX_ITEMS or (shown and used + len(text) > MAX_CHARS):
            break
        shown.append((item, text))
        used += len(text)
    return tuple(shown), len(items) - len(shown), used


@dataclass(frozen=True)
class Brief:
    """一轮记忆注入的不可变快照。counts 是只读映射，items 是有序元组。"""
    items: tuple = ()
    counts: object = field(default_factory=lambda: MappingProxyType({key: 0 for key in AVENUES}))
    chars: int = 0
    is_empty: bool = True

    def render(self):
        """中文紧凑块：标题 + 逐条编号；空清单返回空字符串（不占 persona）。"""
        shown, _hidden, _chars = _select(self.items)
        if not shown:
            return ''
        counts = self.counts
        lines = ['本轮长期记忆清单（manual=%d notes=%d learned=%d archive=%d）：' % (
            counts['manual'], counts['notes'], counts['learned'], counts['archive'])]
        for index, (item, text) in enumerate(shown, 1):
            lines.append('%d. 【%s】%s' % (index, LABELS.get(item.source, item.source), text))
        hidden = len(self.items) - len(shown)
        if hidden > 0:
            lines.append('（还有 %d 条未显示）' % hidden)
        return '\n'.join(lines)

    def log_line(self):
        """一行统计，供运行日志与 /api/dsh/memory-injections 核对。"""
        counts = self.counts
        return '记忆注入 manual=%d notes=%d learned=%d archive=%d chars=%d empty=%d' % (
            counts['manual'], counts['notes'], counts['learned'], counts['archive'],
            self.chars, 1 if self.is_empty else 0)


def _empty():
    return Brief()


def _rows(value, keys):
    """收一栏原文；整栏类型不对或混进非预期元素就整栏作废（降级为空清单）。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        return None
    rows = []
    for row in value:
        if isinstance(row, str):
            text = row.strip()
        elif isinstance(row, dict):
            text = ''
            for key in keys:
                if isinstance(row.get(key), str):
                    text = row[key].strip()
                    break
        else:
            return None
        if text:
            rows.append(text)
    return rows


def _number(value):
    """非数字一律当 0：统计宁可少记，也不能让整轮提问失败。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return 0


def _valid_counts(value):
    """可选的 counts 字段：只要有一个不是数字就整轮降级为空清单。"""
    if not hasattr(value, 'get'):
        return False
    for key in AVENUES:
        item = value.get(key, 0)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0:
            return False
    return True


def brief(message):
    """从 message['_memory_items'] 生成本轮清单；任何畸形输入都退化成空清单。"""
    try:
        raw = message.get('_memory_items') if isinstance(message, dict) else None
        if not isinstance(raw, dict):
            return _empty()
        if 'counts' in raw and not _valid_counts(raw['counts']):
            return _empty()
        archive = raw.get('archive_recalled', 0)
        archive = 0 if archive is None else archive
        if isinstance(archive, bool) or not isinstance(archive, (int, float)) or archive < 0:
            return _empty()
        groups = []
        for source, keys in (('manual', ()), ('notes', ('content', 'text')),
                             ('learned', ('text', 'content'))):
            rows = _rows(raw.get(source), keys)
            if rows is None:
                return _empty()
            groups.append((source, rows))
        items = tuple(Item(source, text) for source, rows in groups for text in rows)
        counts = {source: len(rows) for source, rows in groups}
        counts['archive'] = int(archive)
        _shown, _hidden, chars = _select(items)
        return Brief(items, MappingProxyType(counts), chars, not items)
    except Exception:
        # 记忆清单只是旁路核对，绝不允许它把正常提问打挂。
        return _empty()


_RECENT = deque(maxlen=RECENT_LIMIT)
_LOCK = threading.Lock()


def record(counts, chars=0, empty=None):
    """记一轮统计：时间戳 + 四个计数 + 字数 + 空标记，只存数字，不存正文。"""
    values = {key: 0 for key in AVENUES}
    try:
        if hasattr(counts, 'get'):
            values = {key: _number(counts.get(key, 0)) for key in AVENUES}
    except Exception:
        values = {key: 0 for key in AVENUES}
    entry = {'at': round(time.time(), 3), **values,
             'chars': _number(chars), 'empty': 1 if empty else 0}
    with _LOCK:
        _RECENT.append(entry)
    return dict(entry)


def recent():
    """最近若干轮统计的只读副本（旧的在前、新的在后），不含任何记忆正文。"""
    with _LOCK:
        return tuple(dict(row) for row in _RECENT)
