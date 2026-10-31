"""把插件的回复元数据组装成微信回复段。

背景（微信版）：真 @ 在微信里是按**群昵称**选的，正文里写 ``@某人`` 只是普通字符，
**对方收不到提醒**。所以 ``at_mention`` 这类插件不能只改正文，必须把"要 @ 谁"作为
元数据交给发送链，由发送链在发送时把昵称对应到群成员。

本模块只做"元数据 → 段"的纯组装，不发送、不联网、不读配置，便于离线测试。
"""

from typing import Any, Dict, Iterable, List, Optional

# 允许出现在回复里的消息段类型（白名单，避免插件塞进任意 action）。
# 微信发送走本机文件，因此图片/文件都是 ``{"type": ..., "data": {"file": <绝对路径>}}``。
ALLOWED_TYPES = frozenset({"text", "at", "image", "file"})


def _text_segment(text: str) -> Dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def _mention_name(item: Any) -> str:
    """从提及项里取出名字。

    契约第 3 节允许两种形式：``"小明"`` 与 ``{"name": "小明"}``。
    ``qq``/``user_id`` 是 QQ 版的旧写法，这里刻意不认：微信按名称 @，
    数字 ID 既不是 wxid 也不是昵称，认了只会 @ 错人。
    """
    if isinstance(item, dict):
        return str(item.get("name") or "").strip()
    if item is None:
        return ""
    return str(item).strip()


def normalize_mentions(mentions: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """规整提及列表，按名字去重并保持先后顺序。"""
    result: List[Dict[str, Any]] = []
    seen = set()
    for item in mentions or []:
        name = _mention_name(item)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({"type": "at", "data": {"name": name}})
    return result


def build_reply_segments(reply: str, meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """把回复文本与插件元数据组装成段列表。

    meta 支持的键（全部可选）：
      - ``mentions``：要 @ 的群成员**名称**列表，元素可以是 ``"小明"`` 或
        ``{"name": "小明"}``。@ 段会被放在文本之前。
      - ``append_mentions``：True 时把 @ 段放在文本之后（默认放在之前）。
      - ``media``：附加媒体段列表，元素形如 ``{"type": "image", "data": {"file": ...}}``；
        只接受 ``ALLOWED_TYPES`` 内的类型，其余忽略。
      - ``text``：覆盖 reply 文本（插件改写正文时用）。

    返回的列表至少含一个段；文本为空且没有其它段时返回空列表，由调用方决定
    是否跳过发送（例如"只 @ 不说话了"的情况）。
    """
    meta = meta or {}
    text = meta.get("text", reply)
    text = "" if text is None else str(text)

    segments: List[Dict[str, Any]] = []
    mentions = normalize_mentions(meta.get("mentions"))
    if mentions and not meta.get("append_mentions"):
        segments.extend(mentions)
    if text:
        segments.append(_text_segment(text))
    if mentions and meta.get("append_mentions"):
        segments.extend(mentions)

    for item in meta.get("media") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        data = item.get("data")
        if kind in ALLOWED_TYPES and isinstance(data, dict):
            segments.append({"type": kind, "data": dict(data)})
    return segments


def strip_mentions_from_text(text: str, names: Iterable[str]) -> str:
    """把正文里已被认领的 ``@名字`` 去掉，避免和真 @ 重复显示。

    只处理形如 ``@名字`` 的整词（后面跟空白、标点或结尾），并且要求 @ 前面不是
    字母/数字/下划线——否则 ``foo@bar`` 这类邮箱式文本会被误删。
    """
    if not text:
        return text
    result = text
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        for candidate in (f"@{name}", f"@ {name}"):
            index = 0
            while True:
                found = result.find(candidate, index)
                if found == -1:
                    break
                end = found + len(candidate)
                following = result[end:end + 1]
                preceding = result[found - 1:found] if found else ""
                if following and (following.isalnum() or following == "_"):
                    index = end
                    continue
                if preceding and (preceding.isalnum() or preceding == "_"):
                    index = end
                    continue
                result = result[:found] + result[end:]
                index = found
    return result
