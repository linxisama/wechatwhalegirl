"""微信消息字典与传输层：把微信原始数据转成项目统一消息结构，并负责发送。

分工（见 `instructions/WECHAT-CONTRACT.md`）：
- ``message_from_raw``：纯函数，把微信后端给的原始字典转成消息字典（可离线单测）。
- ``WeChatTransport``：运行时与插件使用的发送/媒体接口，内部把阻塞的自动化调用
  丢进线程池，绝不在事件循环里做键鼠操作。

时间与限流：所有"距上次发送多久"的判断都在这里做（`send_interval`），
插件层不需要关心。
"""
from __future__ import annotations

import asyncio
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .text_matching import contains_keyword, normalized_text
from .wechat_bridge import GROUP_SUFFIX, MEDIA_KINDS, SYSTEM_TYPES, WeChatBridge
from .send_outcome import SendOutcome, not_submitted

# 正文里出现这些前缀说明是链接/文件/位置/名片类消息（微信把它们都归在"文件/链接/卡片"）
_LINK_PREFIXES = ("[链接", "链接")
_FILE_PREFIXES = ("[文件", "文件")
_LOCATION_PREFIXES = ("[位置", "位置", "位置消息")
_CARD_PREFIXES = ("[个人名片", "[名片", "名片")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _conversation_kind(username: str) -> str:
    return "group" if str(username or "").endswith(GROUP_SUFFIX) else "private"


def _pat_title(content: str) -> str:
    """从 appmsg 卡片里抽出「A 拍了拍 B」的干净文本；不是拍一拍就返回空串。

    实测微信 4.1.15.13：「拍一拍」是一条 `文件/链接/卡片`，形如
    ``<msg><appmsg><title>"站高速看夕阳" 拍了拍 "怪怪." Ciallo～</title>…``。
    尾巴可能带表情后缀（"Ciallo～"），只保留到被拍的名字为止。
    """
    raw = str(content or "")
    if '拍了拍' not in raw or '<title>' not in raw:
        return ""
    match = re.search(r'<title>(.*?)</title>', raw, re.S)
    if not match:
        return ""
    title = html.unescape(re.sub(r'<[^>]+>', '', match.group(1))).strip()
    if '拍了拍' not in title:
        return ""
    head, _, tail = title.partition('拍了拍')
    tail = tail.strip()
    quoted = re.match(r'^["“](.+?)["”]', tail)
    if quoted:
        patted = quoted.group(1).strip()
    else:
        patted = re.split(r'[\s，,。]', tail)[0].strip()
    patted = patted.strip('"“”.')
    head = head.strip().strip('"“”')
    if not patted:
        return ""
    return f'{head} 拍了拍 {patted}'.strip()


def _refer_reply(content: str) -> dict:
    """解析微信"引用回复"（appmsg type=57 里的 `<refermsg>`）。

    真实场景（用户截图）：消息正文其实是 `<title>我再考考你 这个是谁</title>`，
    另外带一段 `<refermsg>`（被引用的图片/文字）。旧代码把它归成"文件/链接"，
    于是**正文和被引用的图全丢了**，模型只看到 `[文件/链接]`。
    这里抽出：正文 title、被引用内容的纯文字、被引用类型与发送者显示名。
    """
    raw = str(content or "")
    if "<refermsg>" not in raw:
        return {}
    title = ""
    match = re.search(r"<title>(.*?)</title>", raw, re.S)
    if match:
        title = re.sub(r"\s+", " ", _strip_tags(match.group(1))).strip()
    block = re.search(r"<refermsg>(.*?)</refermsg>", raw, re.S)
    if not block:
        return {"title": title} if title else {}
    # 被引用内容通常是**转义过的** XML，先还原再判断类型，否则会把 <type>3 当成正文。
    inner = html.unescape(block.group(1))
    # 被引用内容本身又可能是一段 XML（图片/文字/链接），先取出它的可读部分
    quoted_title = re.search(r"<title>(.*?)</title>", inner, re.S)
    content_part = re.search(r"<content>(.*?)</content>", inner, re.S)
    plain = re.sub(r"\s+", " ", _strip_tags(content_part.group(1))).strip() if content_part else ""
    kind = ""
    if "<img" in inner:
        kind = "image"
    elif "<videomsg" in inner or "<video" in inner:
        kind = "video"
    elif "<emoji" in inner:
        kind = "emotion"
    elif plain:
        kind = "text"
    quoted = ""
    if kind == "text":
        quoted = plain
    elif kind == "" and quoted_title:
        quoted = re.sub(r"\s+", " ", _strip_tags(quoted_title.group(1))).strip()
    display = ""
    name = re.search(r"<displayname>(.*?)</displayname>", inner, re.S)
    if name:
        display = _strip_tags(name.group(1)).strip()
    return {"title": title, "quoted": quoted[:300], "quoted_kind": kind, "quoted_from": display}


def _strip_tags(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or "")))


def classify_media(raw_type: str, content: str) -> Tuple[Optional[str], str]:
    """判断媒体类型并给出正文占位符。

    微信 4.x 把链接/文件/位置/名片统一报成"文件/链接/卡片"，只能按正文开头区分；
    区分不出来时按 `file` 处理（至少能把"这是一条非文本消息"表达清楚）。
    """
    raw_type = _as_text(raw_type)
    text = _as_text(content)
    if raw_type in MEDIA_KINDS:
        kind = MEDIA_KINDS[raw_type]
        placeholder = {"image": "[图片]", "voice": "[语音]",
                       "video": "[视频]", "emotion": "[表情]"}[kind]
        return kind, placeholder
    if raw_type == "文件/链接/卡片":
        if text.startswith(_LINK_PREFIXES):
            return "link", "[链接]"
        if text.startswith(_FILE_PREFIXES):
            name = text
            for prefix in _FILE_PREFIXES:
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
            name = name.strip("]】 ").split("\n")[0][:60]
            return "file", f"[文件:{name}]" if name else "[文件]"
        if text.startswith(_LOCATION_PREFIXES):
            return "other", "[位置]"
        if text.startswith(_CARD_PREFIXES):
            return "other", "[名片]"
        return "file", "[文件/链接]"
    return None, ""


def _detect_at(text: str, names: List[str]) -> bool:
    """正文里是否 @ 了机器人。

    微信在群里 @ 人会把昵称写进正文（形如 `@鲸鱼娘 `）。昵称拿不到时不做猜测 ——
    宁可漏判（交给关键词触发兜底），也不要把"别人 @ 别人"当成 @ 机器人。
    """
    for name in names:
        if name and re.search(r'@\s*' + re.escape(normalized_text(str(name))) +
                              r'(?=[\s:：,，。!?！？;；]|$)', normalized_text(text)):
            return True
    return False


def message_from_raw(raw: Dict[str, Any], bridge: WeChatBridge,
                     aliases: Optional[List[str]] = None) -> Optional[dict]:
    """微信原始字典 → 项目消息字典；不该处理的消息返回 None。

    会被丢掉的情况：系统消息、自己发的消息、拿不到会话标识的消息。
    """
    if not isinstance(raw, dict):
        return None
    raw_type = _as_text(raw.get("type"))
    if raw_type in SYSTEM_TYPES:
        return None

    username = _as_text(raw.get("username"))
    if not username:
        return None

    sender_username = _as_text(raw.get("sender_username"))
    sender_id = _as_text(raw.get("sender_id"))
    # 自己发的消息不入管线（否则机器人会把自己的话当成用户在说话）
    if bridge.self_id and sender_username and sender_username == bridge.self_id:
        return None
    if _as_text(raw.get("attr")) == "self":
        return None

    content = _as_text(raw.get("content"))
    kind = _conversation_kind(username)
    # 「拍一拍」（戳一戳）在微信 4.x 里是一条 `文件/链接/卡片` 消息，正文是
    # `<msg><appmsg><title>"A" 拍了拍 "B"</title>…`。以前被当成链接卡片，模型只看到一段
    # XML 残片；这里抽成干净的 `[拍一拍] A 拍了拍 B`，并按"有没有点到机器人名字"决定算不算点名。
    pat = _pat_title(content)
    refer = _refer_reply(content) if not pat else {}
    media_kind, placeholder = (None, "") if pat else classify_media(raw_type, content)
    if refer:
        # 引用回复：正文是 title，不是"[文件/链接]"；被引用的内容单独挂到 message 上，
        # 供后续（识图/引用展示）使用，不混进正文干扰模型。
        media_kind, placeholder = None, ""
        if not pat:
            text_head = refer.get("title") or ""
            if text_head:
                text = text_head
    media_xml = bool(media_kind and re.search(r'<(?:\?xml|msg\b|img\b|emoji\b)', content, re.I))

    # 引用回复的正文取 title（上面已解析），否则按原文
    text = ('[拍一拍] ' + pat) if pat else (refer.get("title") or content)
    media: List[dict] = []
    if media_kind:
        item = {
            "type": media_kind,
            "local_id": raw.get("local_id"),
            "shard": _as_text(raw.get("shard")),
            "name": Path(placeholder.strip("[]").replace("文件:", "")).name if media_kind == "file" else "",
            "text": content[:200] if media_kind in {"link", "other"} else "",
        }
        # 表情/图片的 XML 里带 CDN 直链（`cdnurl`），这是**唯一**能绕过
        # "微信 4.x 本地表情文件加密"的办法：不用解密、不用截图，直接下原图。
        # 实测 4.1.15.13：cdnurl 返回 200 + JPEG，字节数与 XML 里的 len 一致。
        if media_kind in {"emotion", "image"} and media_xml:
            fields = {name.casefold():value for name,quote,value in re.findall(r'''(\w+)\s*=\s*(["'])(.*?)\2''',content,re.S)}
            # 图片/表情的 XML 字段名不统一：表情常是 `cdnurl`，聊天图片是
            # `cdnbigimgurl`/`cdnmidimgurl`/`cdnthumburl`。以前只认前两个，
            # 图片就退化成本地缩略图（实测存下来只有 180×149、6KB），
            # 再怎么调"送模型的最长边"也没用 —— 源头就已经是缩略图了。
            link = ""
            for key in ("cdnbigimgurl", "cdnmidimgurl", "cdnurl", "cdnthumburl", "thumburl"):
                candidate = html.unescape(str(fields.get(key) or "").strip())
                if candidate.startswith(("http://", "https://")):
                    link = candidate
                    break
            if link:
                item["url"] = link
            if fields.get("md5"):
                item["md5"] = fields["md5"]
            for size_key in ("hdlength", "length", "len"):
                try:
                    value = int(fields.get(size_key) or 0)
                except (TypeError, ValueError):
                    continue
                if value:
                    item["size"] = value
                    break
        media.append(item)
        # XML 是媒体描述而非用户正文；纯文字说明与其中的点名不能被占位符覆盖。
        caption = content.strip()
        text = (caption + '\n' + placeholder if caption and not media_xml and not caption.startswith('<')
                and caption != placeholder and media_kind in {'image', 'emotion'} else placeholder)

    # 最后一位发言者是成员，不是会话名称；不能把私聊管理/群回复路由到成员昵称。
    target_name = _as_text(raw.get("target_name")) or bridge.display_name(username)
    sender_name = sender_username or _as_text(raw.get("sender"))
    display = _as_text(raw.get("sender_name")) or (bridge.display_name(sender_username, sender_name) if sender_username else sender_name)

    names = [bridge.account_name] + [str(a) for a in (aliases or [])]
    names = [n for n in names if n]
    supplied_ids = raw.get('at_ids') or raw.get('atuserlist') or []
    if isinstance(supplied_ids, str):
        supplied_ids = re.split(r'[,，;\s]+', supplied_ids)
    at_ids = [value for value in supplied_ids if isinstance(value, str) and value] if isinstance(supplied_ids, (list, tuple)) else []
    at = (bool(raw.get("mock_at")) or bool(bridge.self_id and bridge.self_id in at_ids)
          or (not media_xml and _detect_at(content, names)))
    if pat:
        # 拍一拍没有 @：只有"被拍的正是机器人（或它的别名）"才算点名，
        # 否则别人互拍不该触发机器人插话。
        patted = pat.rsplit('拍了拍', 1)[-1].strip()
        if any(name and name in patted for name in names):
            at = True

    local_id = raw.get("local_id")
    shard = _as_text(raw.get("shard"))
    message_id = f"{shard}:{local_id}" if local_id is not None else \
        f"{username}:{int(float(raw.get('create_time') or time.time()) * 1000)}"

    return {
        "id": message_id,
        # 引用回复（appmsg type=57）：正文取自 <title>，被引用内容挂在 refer 上，
        # 供后续识图/展示使用，不混进正文。（见 _refer_reply 的说明）
        **({"refer": refer} if refer else {}),
        # sort_seq 是数据库提供的同一事件标识，分片迁移后 local_id 可能变化。
        "source_event": str(raw.get('server_id') or raw.get('sort_seq') or ''),
        "local_id": local_id,
        "shard": shard,
        "kind": _as_text(raw.get("mock_kind")) or kind,
        "target": username,
        "target_name": target_name,
        "text": text,
        "media": media,
        "sender": display or "微信用户",
        # 昵称可能重名，不能拿它作为跨会话个人档案或管理员身份。
        "sender_id": sender_username or sender_id or (username if kind == 'private' else ''),
        "sender_name": display or sender_name,
        "at": at,
        "at_ids": at_ids,
        "raw_type": raw_type or "文本",
        "notice": None,
        "created": float(raw.get("create_time") or time.time()),
    }


class WeChatTransport:
    """运行时眼里的"微信通道"：发送、下载媒体、查会话。

    发送为什么要**专用单线程**并且带超时：微信的发送是键鼠自动化，实测会偶发卡住
    （旧版项目最大的痛点就是"机器人活着但不回消息"）。所以：

    * 所有发送都在**同一个只含一个线程**的执行器里排队 —— 任何时刻只有一次界面操作，
      绝不会出现两次点击互相打断；
    * 每次发送都套 25 秒超时。超时后**不重发**（结果不确定，重发会重复发消息），
      记为一次发送失败；连续失败会触发运行时的连接守护重连；
    * 重连时用 :meth:`reset_sender` 换一个新执行器，把卡死的那个线程彻底丢掉
      （Python 杀不掉线程，只能不再等它）。
    """

    mode = "wechat"

    def __init__(self, bridge: WeChatBridge, send_interval: float = 1.5,
                 note: Optional[Any] = None):
        self.bridge = bridge
        self.send_interval = max(0.0, float(send_interval or 0.0))
        self._note = note or (lambda text: None)
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self._timeout = float(WeChatBridge.SEND_TIMEOUT)
        self._sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wechat-send")
        self._media_tasks = {}
        self._media_cache = {}
        self._media_closed = False
        self._accept_sends = True
        self._send_epoch = 0
        self._send_closed = False

    def halt_sends(self):
        self._accept_sends = False
        self._send_epoch += 1
        # 只读媒体桥也可复用传输层；关闭仍先失效宿主任务，真实发送桥再取消子进程。
        cancel = getattr(self.bridge, 'cancel_pending_sends', None)
        if callable(cancel):
            cancel()

    def resume_sends(self):
        if self._send_closed:
            return
        self.bridge.resume_sends()
        self._accept_sends = True

    # ── 只读属性 ────────────────────────────────────────────────────────────
    @property
    def online(self) -> bool:
        return bool(self.bridge.online)

    @property
    def self_id(self) -> str:
        return self.bridge.self_id

    @property
    def account_name(self) -> str:
        return self.bridge.account_name

    @property
    def backend(self) -> str:
        return self.bridge.backend

    def status(self) -> dict:
        return self.bridge.status()

    # ── 发送 ────────────────────────────────────────────────────────────────
    async def _wait_turn(self) -> None:
        """发送间隔闸门：两次发送之间至少隔 `send_interval` 秒。

        这是给微信风控留的余量，也是给 UIA 留的喘息时间（连着发容易撞上"发送超时"）。
        """
        if self.send_interval <= 0:
            return
        gap = time.time() - self._last_send
        if gap < self.send_interval:
            await asyncio.sleep(self.send_interval - gap)

    @staticmethod
    def _split_meta(meta: Optional[dict]) -> Tuple[List[str], List[dict]]:
        meta = meta if isinstance(meta, dict) else {}
        names: List[str] = []
        for item in meta.get("mentions") or []:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("nickname") or item.get("userId") or "").strip()
            else:
                name = str(item or "").strip()
            if name and name not in names:
                names.append(name)
        files: List[dict] = []
        for item in meta.get("media") or []:
            if isinstance(item, dict) and isinstance(item.get("data"), dict):
                files.append(item)
        return names[: WeChatBridge.MAX_AT], files

    def _send_current(self, epoch):
        return self._accept_sends and not self._send_closed and epoch == self._send_epoch

    async def _run_send(self, func, *args, epoch=None) -> Tuple[bool, str]:
        """在专用单线程里执行一次发送，并套上超时。

        超时后**不重发**：结果不确定（消息可能已经发出去了），重发会重复发。
        卡死的那个线程不再等待，由运行时的连接守护负责重连与换执行器。
        """
        epoch = self._send_epoch if epoch is None else epoch
        if not self._send_current(epoch):
            return not_submitted('机器人已停止，本条不再发送')
        loop = asyncio.get_running_loop()
        def submit():
            # 线程池中的排队也可能跨越停止/恢复，真正调用桥前再检查。
            if not self._send_current(epoch):
                return not_submitted('此发送任务已失效，本条不再发送')
            return func(*args)
        future = loop.run_in_executor(self._sender, submit)
        try:
            return await asyncio.wait_for(future, timeout=self._timeout)
        except asyncio.TimeoutError:
            if epoch == self._send_epoch:
                self.bridge.cancel_pending_sends()
                mark = getattr(self.bridge, "mark_send_timeout", None)
                if callable(mark):
                    mark(f"发送超过 {self._timeout:g} 秒没有返回")
            return SendOutcome(False, f"发送超时（{self._timeout:g} 秒）：结果不确定，本次不重发",
                               {'submission_state': 'uncertain'})
        except asyncio.CancelledError:
            if epoch == self._send_epoch:
                self.bridge.cancel_pending_sends()
            raise

    def reset_sender(self) -> None:
        """丢掉卡死的发送线程，换一个新的单线程执行器（重连后调用）。"""
        if self._send_closed:
            return
        self._send_epoch += 1
        old, self._sender = self._sender, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="wechat-send")
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except TypeError:      # Python < 3.9 没有 cancel_futures
            old.shutdown(wait=False)

    async def send(self, message: dict, text: str, meta: Optional[dict] = None) -> Tuple[bool, str]:
        """回复一条入站消息：先发正文（可 @），再发附加图片/文件。"""
        epoch = self._send_epoch
        text = "" if text is None else str(text)
        names, files = self._split_meta(meta)
        target = str(message.get("target") or "")
        target_name = str(message.get("target_name") or "")
        # 群聊里回复时默认 @ 提问者：微信里不 @ 对方很容易被忽略（这也是旧版的做法）
        if message.get('_suppress_mentions') is True:
            names = []
        elif message.get("kind") == "group" and not names and message.get("sender_name"):
            names = [str(message["sender_name"])][:1]

        async with self._send_lock:
            if not self._send_current(epoch):
                return not_submitted('此发送任务已失效，本条不再发送')
            ok, detail = True, "已发送"
            outcome = SendOutcome(ok, detail)
            text_submitted = False
            if text.strip():
                await self._wait_turn()
                outcome = await self._run_send(
                    self.bridge.send_text, target, text, target_name, names or None, epoch=epoch)
                ok, detail = outcome
                self._last_send = time.time()
                if not ok:
                    return outcome
                text_submitted = True
            if files and (getattr(self.bridge, 'background_send', False) is True
                          or getattr(self.bridge, 'text_only', False) is True):
                self.bridge.note('当前后台通道仅发送文字，跳过附加表情图片')
                return outcome if text.strip() else not_submitted('严格后台模式跳过图片，本轮没有文字，未发送任何内容')
            if files:
                paths = [str(item["data"].get("file") or "") for item in files]
                paths = [p for p in paths if p]
                if paths:
                    await self._wait_turn()
                    image = all(item.get("type") == "image" for item in files if item.get("data"))
                    outcome = await self._run_send(
                        self.bridge.send_files, target, paths, target_name, image, epoch=epoch)
                    ok, detail = outcome
                    if not ok and text_submitted:
                        outcome = SendOutcome(False, detail, dict(getattr(outcome, 'diagnostic', {}),
                                              partial_content=True))
                    self._last_send = time.time()
            return outcome

    async def send_to(self, target: str, text: str, kind: str = "group",
                      target_name: str = "") -> Tuple[bool, str]:
        """主动发给某个会话（按 username 或显示名）。"""
        epoch = self._send_epoch
        if not self._send_current(epoch):
            return not_submitted('机器人已停止，本条不再发送')
        if not str(text or "").strip():
            return not_submitted("内容为空")
        if not self.online:
            return not_submitted("微信未连接")
        resolved = await asyncio.to_thread(self.bridge.resolve_session, target)
        name = target_name or (target if resolved != target else "")
        async with self._send_lock:
            await self._wait_turn()
            outcome = await self._run_send(
                self.bridge.send_text, resolved, str(text), name, None, epoch=epoch)
            self._last_send = time.time()
            return outcome

    async def send_files(self, target: str, paths: List[str], kind: str = "group",
                         target_name: str = "", as_image: bool = True) -> Tuple[bool, str]:
        epoch = self._send_epoch
        if not self._send_current(epoch):
            return not_submitted('机器人已停止，本条不再发送')
        if not paths:
            return not_submitted("没有要发送的文件")
        if not self.online:
            return not_submitted("微信未连接")
        resolved = await asyncio.to_thread(self.bridge.resolve_session, target)
        name = target_name or (target if resolved != target else "")
        async with self._send_lock:
            await self._wait_turn()
            outcome = await self._run_send(
                self.bridge.send_files, resolved, list(paths), name, as_image, epoch=epoch)
            self._last_send = time.time()
            return outcome

    async def download_media(self, chat: str, local_id: Any, msg_type: str,
                             shard: str = "") -> Optional[str]:
        """下载收到的媒体（本地数据库 + 解密，通常很快）。

        仍然给一个超时：极端情况下密钥/文件缺失会让库一直重试，
        不能因此把整条回复链路拖住（拿不到图就只回文字）。
        """
        if not self.online or self._media_closed:
            return None
        key = (id(getattr(self.bridge, '_db', None)), self.self_id, str(chat),
               str(local_id), str(msg_type), str(shard))
        cached = self._media_cache.get(key)
        if cached and time.monotonic() - cached[0] < 30 and Path(cached[1]).is_file():
            return cached[1]
        task = self._media_tasks.get(key)
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(
                self.bridge.download_media, chat, local_id, msg_type, shard))
            self._media_tasks[key] = task
            # OCR、自动收集和视觉共用同一任务；一位调用者超时不能取消其他调用者。
            def finished(done):
                if self._media_tasks.get(key) is done:
                    self._media_tasks.pop(key, None)
                if done.cancelled():
                    return
                try:
                    path = done.result()
                    active = (id(getattr(self.bridge, '_db', None)), self.self_id)
                    if not self._media_closed and self.online and key[:2] == active and path and Path(path).is_file():
                        self._media_cache[key] = (time.monotonic(), path)
                        while len(self._media_cache) > 64:
                            self._media_cache.pop(next(iter(self._media_cache)))
                except Exception:
                    pass
            task.add_done_callback(finished)
        try:
            path = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=60.0)
            if self._media_closed or not self.online or key[:2] != (id(getattr(self.bridge, '_db', None)), self.self_id):
                return None
            return path
        except asyncio.TimeoutError:
            self._note_with(f"下载媒体超时（60 秒，{msg_type} #{local_id}），本轮不带这张图")
            return None

    def _note_with(self, text: str) -> None:
        try:
            self._note(text)
        except Exception:
            pass

    # ── 插件可用的受控接口 ──────────────────────────────────────────────────
    async def call(self, action: str, params: Optional[dict] = None) -> dict:
        """插件唯一被允许的"通道调用"入口（见契约第 5 节）。"""
        params = params or {}
        try:
            if action == "send_to":
                ok, detail = await self.send_to(
                    str(params.get("target") or ""), str(params.get("text") or ""),
                    str(params.get("kind") or "group"), str(params.get("target_name") or ""))
                return {"ok": ok, "message": detail}
            if action == "download_media":
                path = await self.download_media(
                    str(params.get("chat") or ""), params.get("local_id"),
                    str(params.get("msg_type") or "image"), str(params.get("shard") or ""))
                return {"ok": bool(path), "path": path or ""}
            if action == "video_frames":
                fn = getattr(self.bridge, "video_frames", None)
                if fn is None:
                    return {"ok": False, "message": "当前通道不支持视频抽帧", "frames": []}
                return await asyncio.to_thread(fn, str(params.get("chat") or ""),
                                               params.get("local_id"), str(params.get("shard") or ""),
                                               int(params.get("want") or 3),
                                               int(params.get("max_side") or 768))
            if action == "get_member_names":
                names = await asyncio.to_thread(
                    self.bridge.member_names, str(params.get("chat") or ""))
                return {"ok": True, "names": names}
            if action == "get_self_info":
                return {"ok": True, "wxid": self.bridge.self_id,
                        "name": self.bridge.account_name}
            return {"ok": False, "message": f"不支持的通道动作：{action}"}
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    async def close(self) -> None:
        # 关闭不创建新执行器，否则每次退出都会留一个没有用途的线程池。
        if self._send_closed:
            return
        self._send_closed = True
        self.halt_sends()
        self._media_closed = True
        tasks = list(self._media_tasks.values())
        self._media_tasks.clear()
        self._media_cache.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._sender.shutdown(wait=False, cancel_futures=True)
        await asyncio.to_thread(self.bridge.disconnect)
