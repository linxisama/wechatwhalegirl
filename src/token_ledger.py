"""每轮 token 账本（1.3.83）：本地估算为主，权威口径单独标注。

为什么要有这一层：以前只有"注入构成"的字数日志，谁也说不清一天烧了多少。DSH 侧的
projections.tokenUsage 才是权威口径，但只有接上 DSH 时才有；本地兜底回合只能用
字符数估算。两条口径都写进同一行，标 source，避免把估算值当账单。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

try:  # 包内正常运行走相对导入；脱离包结构（源码直跑、扁平单测）时退化为顶层模块
    from . import jsonl_tail
except ImportError:  # pragma: no cover - 仅在非包结构下走到
    import jsonl_tail

ASCII_PER_TOKEN = 4.0
CJK_PER_TOKEN = 1.0
RECENT_DAYS = 7
_FILE_NAME = 'token-usage.jsonl'


def estimate_tokens(text):
    """中文按 1 字 1 token、ASCII 按 4 字符 1 token 估算；纯估算，不是账单。"""
    if not text:
        return 0
    value = str(text)
    cjk = 0
    for ch in value:
        if '\u4e00' <= ch <= '\u9fff':
            cjk += 1
    ascii_count = len(value) - cjk
    return int(cjk * CJK_PER_TOKEN + ascii_count / ASCII_PER_TOKEN + 0.5)


def record(path, entry, now=None):
    if not path:
        return False
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(entry or {})
        payload.setdefault('ts', time.time() if now is None else now)
        with target.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + '\n')
        return True
    except Exception:
        return False


def summarize(path, days=RECENT_DAYS, now=None):
    out = {'rounds': 0, 'prompt_tokens': 0, 'reply_tokens': 0,
           'by_source': {}, 'days': int(days), 'last_ts': None}
    if not path:
        return out
    try:
        lines = jsonl_tail.iter_lines(path)
    except Exception:
        return out
    cutoff = (time.time() if now is None else now) - max(0, int(days)) * 86400
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        ts = float(row.get('ts') or 0)
        if ts and ts < cutoff:
            continue
        out['rounds'] += 1
        out['prompt_tokens'] += int(row.get('prompt_tokens') or 0)
        out['reply_tokens'] += int(row.get('reply_tokens') or 0)
        source = str(row.get('source') or 'local')
        out['by_source'][source] = out['by_source'].get(source, 0) + 1
        out['last_ts'] = ts or out['last_ts']
    return out


def path_for(root):
    if not root:
        return None
    return Path(root) / 'data' / 'wechat-only' / _FILE_NAME


def record_round(root, message, reply, prompt_text='', model='', now=None):
    """记录一轮回复的用量；任何异常都吞掉，账本绝不影响发送。"""
    try:
        message = message or {}
        source = 'dsh' if message.get('_dsh_generated') else 'local'
        entry = {
            'msg': str(message.get('id') or '')[:40],
            'kind': str(message.get('kind') or ''),
            'source': source,
            'model': str(model or '')[:60],
            'prompt_chars': len(prompt_text or ''),
            'reply_chars': len(str(reply or '')),
            'prompt_tokens': estimate_tokens(prompt_text),
            'reply_tokens': estimate_tokens(reply),
        }
        record(path_for(root), entry, now=now)
        return entry
    except Exception:
        return None
