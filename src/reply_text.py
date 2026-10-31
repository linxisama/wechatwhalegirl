"""回复文本的收尾整理：去多余空格、去句内复读、判断"和最近说过的是不是同一句"。

为什么要单独一层：实测模型会吐出 `"你好 呀，今天  天气不错  "` 这种带多余空格的文本，
而发送链**故意不做 strip**（有测试钉住"分段标记分支才 strip，普通分支原样发"），
所以必须在"模型返回值 → 插件收尾"这一段统一整理，而不是去改发送层。

另外两件事也放这里，因为都是"文本归一化"的同一套口径：
* **句内复读**：一条回复里同一句出现两次（用户报的"一次回答两次语句"）；
* **跨条复读**：这条回复和最近回过的高度相似（用户报的"回答重复度很高"），
  由运行时决定要不要重生成。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# 汉字/日文假名之间的半角空格：只在这种位置删，绝不动英文与代码里的空格。
_CJK = r'\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af'
_CJK_GAP = re.compile(rf'(?<=[{_CJK}])[ \t]+(?=[{_CJK}])')
_MENTION_GAP = re.compile(r'(?:^|(?<=[。！？!?\n]))[\s\u200b\ufeff]*(?:\*\*|__)?[@＠][^\s,，:：]{1,80}(?P<gap>[ \t]+)')
# 归一化时丢掉的字符：空白与常见中英标点。用于"是不是同一句"的比较。
_NOISE = re.compile(r'[\s，。！？!?、；;：:,.~～…—\-—()（）【】\[\]「」“”"\'’‘]+')
_SENTENCE_END = re.compile(r'(?<=[。！？!?；;…\n])')
# 短于这个长度的重复句子不动（"哈哈""嗯嗯"这种重复是语气，不是复读）
_MIN_REPEAT = 6

# 模型偶尔会把会话协议标记（DeepSeek Harness 的 `<acp ...>...</acp>`）当成正文吐出来，
# 实测真机就出现过 reply 以 `<acp tokens="35" type="text">m00006</acp>主人这是…` 开头。
# 成对整段优先删；落单标签再删；其它尖括号文本原样保留。
_MODEL_TAG_PAIR = re.compile(r'<acp\b[^>]*>.*?</acp\s*>', re.S | re.I)
_MODEL_TAG_LOOSE = re.compile(r'</?acp\b[^>]*>', re.I)


def normalize_key(text: str) -> str:
    """比较用的归一化键：去掉空白与标点，只留下实义字符。"""
    return _NOISE.sub('', str(text or '')).lower()


def similar(left: str, right: str, threshold: float = 0.9) -> bool:
    """两条回复是不是"几乎同一句"。"""
    a, b = normalize_key(left), normalize_key(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 6:        # 太短的句子不比相似度，避免"在吗/在的"被误判
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def dedupe_sentences(text: str) -> str:
    """同一条回复里重复出现的句子只保留第一处。

    只处理**够长的**重复句（≥6 个实义字符），所以"哈哈""嗯嗯""～"这类语气重复不受影响；
    判重前会去掉标点与空白，因此"你今天真好看。"与"你今天真好看！"算同一句。
    """
    parts = _SENTENCE_END.split(str(text or ''))
    seen: set[str] = set()
    kept: list[str] = []
    for part in parts:
        key = normalize_key(part)
        if key and len(key) >= _MIN_REPEAT:
            if key in seen:
                continue
            seen.add(key)
        kept.append(part)
    return ''.join(kept)


def join_lines(text: str) -> str:
    """把多行合并成一段：微信气泡里不需要换行。

    为什么这么做：模型习惯用"空一行"分段，发到微信里就是气泡中间空一截，
    用户看到的就是"多余的空格"。中文直接连起来（"了。要是…"读得通），
    只有在英文/数字边界之间补一个空格，避免把 "hello" 和 "world" 粘成一个词。
    """
    out = ''
    for line in str(text or '').split('\n'):
        line = line.strip()
        if not line:
            continue
        if (out and out[-1].isascii() and out[-1].isalnum()
                and line[0].isascii() and line[0].isalnum()):
            out += ' '
        out += line
    return out


def strip_model_markers(text: str) -> str:
    """去掉模型误吐的会话协议标记（`<acp ...>m00012</acp>` 之类）。

    只处理 acp 这一族标记：先删成对的整段，再删落单的开/闭标签；
    其它尖括号文本（颜文字、比较符、代码片段）原样保留。
    """
    value = _MODEL_TAG_PAIR.sub('', str(text or ''))
    return _MODEL_TAG_LOOSE.sub('', value)


def tidy_reply(text: str) -> str:
    """发送前的统一整理：压掉中文间空格 → 去句内复读 → 合并换行 → 去首尾空白。

    刻意**不**做全局删空格：英文单词之间、代码缩进、颜文字里的空格都得留着。
    """
    value = str(text or '')
    # 保留点名与正文之间的边界，否则“@昵称 哼，正文”会变成“@昵称哼，正文”，
    # 发送前去称呼时连正文第一个词也被误删。
    mention_gaps = {match.start('gap') for match in _MENTION_GAP.finditer(value)}
    value = _CJK_GAP.sub(lambda match: match.group() if match.start() in mention_gaps else '', value)
    value = dedupe_sentences(value)
    return join_lines(value).strip()
