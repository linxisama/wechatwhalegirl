"""大 JSONL 的流式读取（1.3.84）：按块读，绝不把整文件载入内存。

token-usage.jsonl 每轮一行、memory-guard.jsonl 每条警告一行，长期运行后会到几十万行。
以前统一用 Path.read_text().splitlines() 一次性读全量，控制台刷新面板时会瞬间占几十 MB。
这里改成逐块解码逐行产出，调用方决定保留多少行；解析规则与坏行跳过策略仍由调用方负责。
"""
from __future__ import annotations

from collections import deque

CHUNK_BYTES = 262144


def iter_lines(path, chunk_bytes=CHUNK_BYTES):
    """逐行产出文本（不含行尾）；文件不存在或读不动时安静结束，绝不抛。"""
    if not path:
        return
    try:
        handle = open(path, 'rb')
    except Exception:
        return
    try:
        pending = b''
        while True:
            try:
                block = handle.read(chunk_bytes)
            except Exception:
                return
            if not block:
                break
            pending += block
            parts = pending.split(b'\n')
            pending = parts.pop()
            for part in parts:
                yield part.decode('utf-8', 'replace').rstrip('\r')
        if pending:
            yield pending.decode('utf-8', 'replace').rstrip('\r')
    finally:
        try:
            handle.close()
        except Exception:
            pass


def tail_lines(path, limit):
    """只要最后 limit 行（保持原顺序）；内存只与 limit 有关。"""
    keep = max(0, int(limit))
    if not keep:
        return []
    rows = deque(maxlen=keep)
    for line in iter_lines(path):
        rows.append(line)
    return list(rows)
