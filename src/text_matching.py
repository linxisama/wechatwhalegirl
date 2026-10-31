"""触发词和固定回复共用的文字匹配；保留原来的包含匹配语义。"""
import re
import unicodedata


def normalized_text(value):
    return unicodedata.normalize("NFKC", str(value or "")).translate(
        {ord(c): None for c in "\u200b\ufeff\u2060"}).casefold()


def contains_keyword(text, keyword):
    word = normalized_text(keyword).strip()
    return bool(word) and word in normalized_text(text)


def configured_terms(value):
    # YAML 手填字符串不能逐字迭代，否则所有汉字都会被“至少两字”的闸门丢掉。
    if isinstance(value, str):
        return re.split(r"[,，\n]", value)
    return value if isinstance(value, (list, tuple)) else []
