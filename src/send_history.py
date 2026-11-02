# -*- coding: utf-8 -*-
"""发送记录只读解析：从后台日志尾部提取 phase / failed / timing 三类行。

设计约束（有意为之）：
- 只读日志**尾部**（走 jsonl_tail.tail_lines），绝不整文件读；日志再大也只读最后几千行；
- 只解析、不判断成败、不落盘、不碰微信；解析不出来就跳过，绝不抛；
- 日志里会话名已被 mask_chat_name 打码、正文只有 len=，这里不做任何还原。
"""
from __future__ import annotations

import re

try:  # 包内导入（from . import send_history）
    from . import jsonl_tail
except ImportError:  # 源码直跑
    import jsonl_tail  # type: ignore

TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
KINDS = ('phase', 'failed', 'timing')

# 失败原因码 -> 控制台可直接显示的中文（与 src/wechat_guia_send.py 的 REASON_TEXT 同口径，
# 这里另存一份是为了不让日志解析模块拖进 OCR / 图像依赖）。
CODE_TEXT = {
    'chat_not_open': '已核验的当前会话不是目标',
    'title_unverified': '标题识别未通过，本次不继续输入或提交',
    'guia_unavailable': '「坐标+OCR」发送通道不可用',
    'minimized': '微信窗口最小化且自动恢复失败，无法点击发送',
    'target_not_open': '当前会话不是目标会话，本次未输入、未发送',
    'invalid_target': '目标会话名不合法，本次未操作微信',
    'draft_not_empty': '当前输入框已有草稿，发送尚未就绪',
    'send_failed': '「坐标+OCR」发送失败',
    'blocked_by_window': '微信窗口被其他程序遮挡',
    'foreground_lost': '微信失去前台焦点，为避免输入到别的窗口，本次未输入',
    'search_unavailable': '搜索框不可用，未能自动打开会话',
    'auto_open_disabled': '自动打开会话已停用，需要手动点开会话',
    'ocr_budget': '标题识别超出时间预算',
    'ocr_empty': '标题识别读不到文字',
    'timeout': '超过发送时限',
    'no_caret': '找不到输入光标',
    'window_not_found': '找不到微信窗口',
}

CODE_HINT = {
    'target_not_open': '请先手动点开目标会话（程序不会替你切会话）',
    'search_unavailable': '请手动点开会话；程序已停止自动打开，微信升级后会自动恢复',
    'blocked_by_window': '请关闭或最小化遮挡微信的窗口后重试',
    'foreground_lost': '请手动点击微信并保持前台；若已粘贴请先核对草稿',
    'title_unverified': '请保留标题识别诊断；程序不会放宽目标核验或重发',
    'guia_unavailable': '请确认微信已登录、窗口未最小化、系统 OCR 可用后重试',
    'send_failed': '请核对聊天记录；输入框里的残留草稿需要手动清空',
}


def describe(reason):
    """原因码 -> 中文；未知码原样返回，空值返回空串。"""
    if not reason:
        return ''
    return CODE_TEXT.get(str(reason), str(reason))


def hint_for(reason):
    return CODE_HINT.get(str(reason or ''), '')


def _fields(text):
    """把 "k=v k2=v2" 拆成 dict；没有 = 的词记成 k=''（如 timing 行的 guest/worker）。"""
    out = {}
    for token in text.split():
        if '=' in token:
            key, _, value = token.partition('=')
            if key:
                out[key] = value
        else:
            out[token] = ''
    return out


def _kind_at(line):
    for kind in KINDS:
        marker = ' send %s' % kind
        pos = line.find(marker)
        if pos >= 0 and (pos + len(marker) >= len(line) or line[pos + len(marker)] in ' ='):
            return kind, pos + len(marker)
    return None, -1


def parse_line(line):
    """一行日志 -> 记录 dict；不是发送记录就返回 None。"""
    if not isinstance(line, str):
        return None
    text = line.rstrip('\r\n')
    kind, cut = _kind_at(text)
    if kind is None:
        return None
    match = TS_RE.match(text)
    ts = match.group(1) if match else ''
    rest = text[cut:].lstrip()
    if kind == 'phase' and rest.startswith('='):
        first, _, rest = rest[1:].partition(' ')
        fields = {'phase': first}
        fields.update(_fields(rest))
    else:
        fields = _fields(rest)
    record = {'ts': ts, 'kind': kind}
    if kind == 'phase':
        record.update({
            'phase': fields.get('phase', ''),
            'who': fields.get('who', ''),
            'len': _int(fields.get('len')),
            'reason': fields.get('reason', ''),
            'channel': fields.get('channel', ''),
            'pid': _int(fields.get('pid')),
        })
    elif kind == 'failed':
        record.update({
            'reason': fields.get('reason', ''),
            'who': fields.get('who', ''),
            'len': _int(fields.get('len')),
            'channel': fields.get('channel', ''),
            'title': fields.get('title', ''),
            'hint': fields.get('hint', ''),
            'foreground': fields.get('foreground', ''),
            'pid': _int(fields.get('pid')),
            'text': describe(fields.get('reason', '')),
            'advice': hint_for(fields.get('reason', '')),
        })
    else:
        scope = ''
        metrics = {}
        for key, value in fields.items():
            if value == '' and key in ('guest', 'worker'):
                scope = key
            elif key.endswith('_ms'):
                metrics[key] = _int(value)
        record['scope'] = scope
        record['metrics'] = metrics
        if metrics:
            record['total_ms'] = next((metrics[k] for k in ('total_ms', 'guest_total_ms') if k in metrics), sum(metrics.values()))
    return record


def _int(value):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def parse_lines(lines):
    out = []
    for line in lines or ():
        record = parse_line(line)
        if record is not None:
            out.append(record)
    return out


def recent(path, limit=50):
    """日志尾部最近 limit 条发送记录（按时间升序）。文件不存在/读失败返回 []。"""
    try:
        wanted = int(limit)
    except (TypeError, ValueError):
        wanted = 50
    keep = wanted if 1 <= wanted <= 200 else 50
    # 一条真实发送会产生 10~20 行阶段记录，尾部取 30 倍足够覆盖 limit 条。
    lines = jsonl_tail.tail_lines(path, max(400, keep * 30))
    return parse_lines(lines)[-keep:]


def summarize(items):
    """给面板用的聚合：各类条数、失败码分布、最近一次失败与最近阶段。"""
    items = list(items or ())
    phases = [r for r in items if r.get('kind') == 'phase']
    failures = [r for r in items if r.get('kind') == 'failed']
    timings = [r for r in items if r.get('kind') == 'timing']
    codes = {}
    for row in failures:
        code = row.get('reason') or 'unknown'
        codes[code] = codes.get(code, 0) + 1
    return {
        'counts': {'phases': len(phases), 'failures': len(failures), 'timings': len(timings)},
        'failure_codes': codes,
        'last_failure': failures[-1] if failures else None,
        'last_phase': phases[-1] if phases else None,
        'last_timing': timings[-1] if timings else None,
    }
