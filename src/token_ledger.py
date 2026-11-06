"""每轮 token 账本 + 上下文预算报告（1.3.92）。

为什么要有这一层：以前只有"注入构成"的字数日志，谁也说不清一天烧了多少。DSH 侧的
projections.tokenUsage 才是权威口径，但只有接上 DSH 时才有；本地兜底回合只能用
字符数估算。两条口径都写进同一行，标 source，避免把估算值当账单。

1.3.92 的两处改动都来自真实样本（本机 16 轮已发送回复的 completion_tokens 对照）：

1. **估算系数校准**。旧口径"CJK 1 字 = 1 token"实测高估 22.6%（偏差 +12.3%）：
   汉字在 BPE 里常常 1~2 字才切一个 token，而全角标点几乎必占一个 token，两者
   混算成同一系数必然偏高。改成汉字 0.75、全角标点 1.2、其余 1/4 字符后，同一批
   样本的平均绝对误差降到 10.8%、偏差 -0.7%。历史 JSONL 不重算，新旧口径会混在
   同一份账里，因此每行带 estimator 版本号。
2. **分块预算报告**。光有一行总字数回答不了"这轮 6000 字预算里人设占多少、插件占
   多少、被砍了多少"。record_round 现在只读消费 PromptPlan.sizes（不 import
   prompt_budget，避免循环依赖），把 persona/continuity/plugins/rules/image 连同
   历史/记忆/学习三块一起记账，report() 再汇总成占比。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

try:  # 包内正常运行走相对导入；脱离包结构（源码直跑、扁平单测）时退化为顶层模块
    from . import jsonl_tail
except ImportError:  # pragma: no cover - 仅在非包结构下走到
    import jsonl_tail

ASCII_PER_TOKEN = 4.0
CJK_PER_TOKEN = 0.75
FULLWIDTH_PER_TOKEN = 1.2
RECENT_DAYS = 7
ESTIMATOR = 'v2'
_FILE_NAME = 'token-usage.jsonl'
# 分块账本的固定顺序：前五块来自 PromptPlan.sizes，后三块由调用方补（历史/记忆/学习）
BLOCKS = ('persona', 'continuity', 'plugins', 'rules', 'image',
          'history', 'memory', 'learning')
# 同进程多线程同时记账时串行写入：一行 JSON 在 Windows 上不保证原子追加
_WRITE_LOCK = threading.Lock()


def _count(text):
    """把一段文字拆成（汉字, 全角标点, 其他）三类字数。"""
    han = full = 0
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:
            han += 1
        elif 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF:
            full += 1
    return han, full, len(text) - han - full


def estimate_tokens(text):
    """汉字 0.75、全角标点 1.2、其余按 1/4 字符折算；纯估算，不是账单。"""
    if not text:
        return 0
    han, full, other = _count(str(text))
    return int(han * CJK_PER_TOKEN + full * FULLWIDTH_PER_TOKEN
               + other / ASCII_PER_TOKEN + 0.5)


def estimate_chars(chars):
    """只有字数、没有原文时的兜底折算（按汉字密度给系数）。"""
    try:
        count = max(0, int(chars or 0))
    except (TypeError, ValueError):
        return 0
    return int(count * CJK_PER_TOKEN + 0.5)


def record(path, entry, now=None):
    """追加一行账目；写入串行化，避免多线程同时追加时把一行切成两半。"""
    if not path:
        return False
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(entry or {})
        payload.setdefault('ts', time.time() if now is None else now)
        line = json.dumps(payload, ensure_ascii=False) + '\n'
        with _WRITE_LOCK:
            with target.open('a', encoding='utf-8') as handle:
                handle.write(line)
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


def _sizes_of(plan):
    """从 PromptPlan（或任何带 sizes 的对象）只读取出分块字数。

    刻意不 import prompt_budget：账本是叶子模块，被 wechat_app 在最热的发送路径上
    调用，反向依赖会把提示词预算模块一起拖进来。鸭子类型足够——拿不到就返回空表。
    """
    if plan is None:
        return {}
    sizes = None
    getter = getattr(plan, 'sizes', None)
    if isinstance(getter, dict):
        sizes = getter
    elif callable(getter):
        try:
            sizes = getter()
        except Exception:
            sizes = None
    if not isinstance(sizes, dict):
        return {}
    out = {}
    for name in BLOCKS:
        try:
            value = int(sizes.get(name) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            out[name] = value
    return out


def _budget_of(plan, blocks):
    """把 PromptPlan 的 sizes/dropped/上限抄成一份可比较的预算快照。"""
    limit = 0
    dropped = []
    if plan is not None:
        try:
            limit = int(getattr(plan, '_limit', 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        if not limit:
            try:
                limit = int(getattr(plan, 'limit', 0) or 0)
            except (TypeError, ValueError):
                limit = 0
        raw = getattr(plan, 'dropped', ()) or ()
        try:
            dropped = [str(name) for name in raw]
        except TypeError:
            dropped = []
    used = sum(int(value) for value in (blocks or {}).values())
    # 被砍掉多少字＝各块上限之和与实际注入之差的下界；上限不可知时退回 0 而不是瞎猜
    return {'limit': limit, 'used': used, 'dropped': dropped,
            'over': max(0, used - limit) if limit else 0}


def record_round(root, message, reply, prompt_text='', model='', now=None, plan=None,
                 blocks=None, usage=None):
    """记录一轮回复的用量；任何异常都吞掉，账本绝不影响发送。

    plan   : PromptPlan（只读 .sizes/.dropped），用来写分块字数；
    blocks : 覆盖/补充分块，例如 {'history': 2400, 'memory': 320, 'learning': 96}；
    usage  : 服务商真实 usage（{'prompt_tokens':..,'completion_tokens':..}），有就存，
             与估算值并存，绝不覆盖——误差要能被算出来才有意义。
    """
    try:
        message = message or {}
        source = 'dsh' if message.get('_dsh_generated') else 'local'
        sizes = _sizes_of(plan)
        if isinstance(blocks, dict):
            for name, value in blocks.items():
                try:
                    count = int(value or 0)
                except (TypeError, ValueError):
                    continue
                if count > 0:
                    sizes[str(name)] = count
                elif str(name) in sizes:
                    sizes.pop(str(name), None)
        budget = _budget_of(plan, sizes)
        entry = {
            'msg': str(message.get('id') or '')[:40],
            'kind': str(message.get('kind') or ''),
            'source': source,
            'model': str(model or '')[:60],
            'prompt_chars': len(prompt_text or ''),
            'reply_chars': len(str(reply or '')),
            'prompt_tokens': estimate_tokens(prompt_text),
            'reply_tokens': estimate_tokens(reply),
            'estimator': ESTIMATOR,
        }
        if sizes:
            entry['sizes'] = sizes
            entry['block_tokens'] = {name: estimate_chars(count)
                                     for name, count in sizes.items()}
            entry['budget'] = budget
        if isinstance(usage, dict):
            try:
                real_prompt = int(usage.get('prompt_tokens') or 0)
            except (TypeError, ValueError):
                real_prompt = 0
            try:
                real_reply = int(usage.get('completion_tokens') or 0)
            except (TypeError, ValueError):
                real_reply = 0
            if real_prompt or real_reply:
                entry['real_prompt_tokens'] = real_prompt
                entry['real_reply_tokens'] = real_reply
        record(path_for(root), entry, now=now)
        return entry
    except Exception:
        return None


def _ratio(part, whole):
    try:
        part, whole = float(part), float(whole)
    except (TypeError, ValueError):
        return 0.0
    if whole <= 0:
        return 0.0
    return round(part / whole, 4)


def report(path, rounds=20, now=None, days=RECENT_DAYS):
    """最近 N 轮的分块预算汇总：每块平均占多少字、占比多少、被砍了多少。

    回答的就是用户那句"这轮 6000 字预算里，人设占多少、插件占多少、被砍了多少"。
    只用最近 rounds 行（jsonl_tail 有界读取），绝不全量加载。
    """
    keep = max(1, min(500, int(rounds or 20)))
    out = {'rounds': 0, 'days': int(days), 'blocks': {}, 'avg_chars': {},
           'share': {}, 'limit': 0, 'used': 0, 'over': 0, 'dropped': {},
           'prompt_tokens': 0, 'reply_tokens': 0,
           'real_prompt_tokens': 0, 'real_reply_tokens': 0,
           'error_pct': None, 'last_ts': None}
    if not path:
        return out
    try:
        lines = jsonl_tail.tail_lines(path, keep)
    except Exception:
        return out
    totals, dropped, limits = {}, {}, []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        out['rounds'] += 1
        out['prompt_tokens'] += int(row.get('prompt_tokens') or 0)
        out['reply_tokens'] += int(row.get('reply_tokens') or 0)
        out['real_prompt_tokens'] += int(row.get('real_prompt_tokens') or 0)
        out['real_reply_tokens'] += int(row.get('real_reply_tokens') or 0)
        sizes = row.get('sizes')
        if isinstance(sizes, dict):
            for name, value in sizes.items():
                try:
                    totals[str(name)] = totals.get(str(name), 0) + int(value or 0)
                except (TypeError, ValueError):
                    continue
        budget = row.get('budget')
        if isinstance(budget, dict):
            try:
                if int(budget.get('limit') or 0):
                    limits.append(int(budget.get('limit')))
            except (TypeError, ValueError):
                pass
            for name in budget.get('dropped') or ():
                dropped[str(name)] = dropped.get(str(name), 0) + 1
        out['last_ts'] = row.get('ts') or out['last_ts']
    if not out['rounds']:
        return out
    out['blocks'] = totals
    out['avg_chars'] = {name: round(value / out['rounds'], 1)
                        for name, value in totals.items()}
    used = sum(totals.values())
    out['used'] = used
    out['share'] = {name: _ratio(value, used) for name, value in totals.items()}
    out['limit'] = int(sum(limits) / len(limits)) if limits else 0
    # 用"各轮上限之和"而不是"平均上限×轮数"：不同轮的上限可能不同（人格/配置切换过）
    out['over'] = max(0, used - sum(limits)) if limits else 0
    out['dropped'] = dropped
    # 有真实 usage 时才给误差：估算值减真实值再除以真实值
    if out['real_prompt_tokens']:
        out['error_pct'] = round(
            100.0 * (out['prompt_tokens'] - out['real_prompt_tokens'])
            / out['real_prompt_tokens'], 2)
    return out
