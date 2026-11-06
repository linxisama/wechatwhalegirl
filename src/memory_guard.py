"""记忆写入守卫：只报告、不删除、不拦截（1.3.83）。

DECISION（用户 m04654）：默认 report —— 命中可疑特征时只写日志与计数，记忆照旧落库；
任何"删除/合并"都留给用户在界面里确认。自动判定一定有误杀，而记忆是长期资产，
误删比留脏数据更难挽回。因此本模块**没有**任何写库能力，只做纯函数判定 + 追加日志。
"""
from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path

try:  # 包内正常运行走相对导入；脱离包结构（源码直跑、扁平单测）时退化为顶层模块
    from . import jsonl_tail
except ImportError:  # pragma: no cover - 仅在非包结构下走到
    import jsonl_tail

MODES = ('report', 'off')
DUP_THRESHOLD = 0.95
DIARY_MIN_CHARS = 40
MIN_CHARS = 6

# 阈值可配置（1.3.92）：旧版把去重阈值/模糊下限写死，不同人设的记忆风格差很远——
# 话痨人设的"好的/嗯嗯"很多，短句下限该松；记录型人设反而要严。上限/下限是护栏，
# 防止把 0.95 配成 0.3 之后所有记忆都被判重复。
LIMIT_BOUNDS = {
    'dupThreshold': (0.60, 0.999),
    'minChars': (2, 40),
    'diaryMinChars': (20, 400),
}


def _bounded(raw, low, high):
    """只接受真正的数值且落在护栏内；其余（None/字符串/越界）一律退回默认。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value != value or value < low or value > high:   # NaN 也在这里被挡掉
        return None
    return value


def limits(config=None):
    """读 memory_guard.limits.* 配置；任何非法值都退回模块默认，绝不抛。"""
    cfg = config if isinstance(config, dict) else {}
    nested = cfg.get('limits')
    nested = nested if isinstance(nested, dict) else {}

    def pick(name, default, as_int=False):
        raw = nested.get(name, cfg.get(name))
        low, high = LIMIT_BOUNDS[name]
        value = _bounded(raw, low, high)
        if value is None:
            return default
        return int(value) if as_int else value

    return {
        'dupThreshold': pick('dupThreshold', DUP_THRESHOLD),
        'minChars': pick('minChars', MIN_CHARS, as_int=True),
        'diaryMinChars': pick('diaryMinChars', DIARY_MIN_CHARS, as_int=True),
    }

_SECRET = re.compile(
    r'(密码|口令|密钥|私钥|api[ _-]?key|apikey|access[ _-]?token|secret|sk-[A-Za-z0-9_-]{8,}'
    r'|BEGIN [A-Z ]*PRIVATE KEY|身份证号?|银行卡号?|信用卡|验证码|支付密码)', re.I)
_RELATIVE = re.compile(
    r'(今天|明天|昨天|后天|前天|刚才|刚刚|现在|本周|上周|下周|这周|本月|上月|下月|这个月|上个月'
    r'|今年|去年|明年|一会儿|等会|待会|最近|目前)')
_DIARY = re.compile(r'(我今天|我刚|我觉得|我感觉|心情|难过|好累|开心|晚安|早安|睡不着)')
_SPLIT = re.compile('[' + re.escape(' \t\r\n,，。.!！?？;；:：、（）()【】[]"“”‘’/|+*=~') + ']+')
_STOP = {'的', '了', '是', '在', '我', '你', '他', '她', '它', '和', '就', '都', '也', '很', '有', '不'}


def mask_secret(text):
    """把像密钥/密码的片段打码，日志里绝不出现原文。"""
    value = str(text or '')
    return _SECRET.sub('***', value)


def keywords(text):
    """粗粒度关键词：按标点与空白切开，去掉单字停用词与纯数字。"""
    parts = [p for p in _SPLIT.split(str(text or '')) if p]
    return [p for p in parts if len(p) > 1 and p not in _STOP and not p.isdigit()]


def similarity(left, right):
    return difflib.SequenceMatcher(None, str(left or ''), str(right or '')).ratio()


def _existing_pairs(existing):
    """existing 里可能混着 sqlite Row、dict、str，统一成 (id, content)。"""
    for item in existing or ():
        if isinstance(item, str):
            yield '', item
            continue
        content, ident = '', ''
        try:
            content = item['content']
        except Exception:
            try:
                content = item[1]
            except Exception:
                content = ''
        try:
            ident = item['id']
        except Exception:
            try:
                ident = item[0]
            except Exception:
                ident = ''
        yield str(ident or ''), str(content or '')


def inspect(text, existing=(), config=None):
    """判定一条待写入记忆；返回纯数据，绝不改库、绝不抛异常。

    阈值从 config 读（见 limits()），非法值退回默认——记忆是长期资产，配置写错
    不应该让守卫静默失效，也不应该让它把一切都判成重复。
    """
    cfg = config if isinstance(config, dict) else {}
    mode = str(cfg.get('mode') or 'report').lower()
    caps = limits(cfg)
    value = str(text or '').strip()
    result = {'verdict': 'ok', 'codes': [], 'issues': [], 'mode': mode,
              'similarity': 0.0, 'duplicate_of': '', 'keyword_count': 0,
              'masked': mask_secret(value)[:200]}
    if mode == 'off' or not value:
        return result
    codes, issues = result['codes'], result['issues']

    if _SECRET.search(value):
        codes.append('SECRET')
        issues.append({'code': 'SECRET', 'detail': '含疑似密码/密钥/证件号'})
    if _RELATIVE.search(value):
        codes.append('RELATIVE_TIME')
        issues.append({'code': 'RELATIVE_TIME', 'detail': '含"今天/上周"这类相对时间，放久了会失真'})
    if len(value) >= caps['diaryMinChars'] and value.startswith('我') and _DIARY.search(value):
        codes.append('DIARY_SMELL')
        issues.append({'code': 'DIARY_SMELL', 'detail': '像日记/心情摘抄，通常不该当长期事实'})

    best, best_id = 0.0, ''
    for ident, content in _existing_pairs(existing):
        score = similarity(value, content)
        if score > best:
            best, best_id = score, ident
    result['similarity'] = round(best, 4)
    if best >= caps['dupThreshold']:
        codes.append('DUP')
        result['duplicate_of'] = best_id
        issues.append({'code': 'DUP', 'detail': '与已有记忆过于相似'})

    count = len(keywords(value))
    result['keyword_count'] = count
    if len(value) < caps['minChars']:
        codes.append('TOO_VAGUE')
        issues.append({'code': 'TOO_VAGUE', 'detail': '关键词太少，太模糊'})

    result['verdict'] = 'warn' if codes else 'ok'
    return result


def note(path, entry, now=None):
    """追加一行 JSONL；任何 IO 失败都只返回 False，绝不影响记忆写入。"""
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


def summary(path, limit=200):
    """读最近 limit 行做计数；文件不存在或坏行一律跳过。"""
    out = {'total': 0, 'warn': 0, 'codes': {}, 'last_ts': None, 'mode': 'report'}
    if not path:
        return out
    try:
        lines = jsonl_tail.tail_lines(path, limit)
    except Exception:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        out['total'] += 1
        codes = row.get('codes') or []
        if codes:
            out['warn'] += 1
        for code in codes:
            out['codes'][code] = out['codes'].get(code, 0) + 1
        out['last_ts'] = row.get('ts') or out['last_ts']
    return out

def recent(path, limit=20):
    """最近 limit 条报告（新到旧）；条数有顶，绝不回全量。"""
    if not path:
        return []
    try:
        keep = max(1, min(200, int(limit)))
        lines = jsonl_tail.tail_lines(path, keep)
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return list(reversed(out))
