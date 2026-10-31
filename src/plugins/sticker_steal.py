"""sticker_steal.py - 自动收集别人发来的图片与表情，攒成机器人本地图库。

对照 QQ 版 `sticker_steal.py`，微信侧有三处必须不同的地方：

1. **触发时机**：精简版只在已进入回复链路的 `on_prepare` 登记并下载图片。
   未触发回复的群聊不下载、不后台轮询收集，减少不必要的媒体操作。
2. **能不能拿到图**：微信 4.x 的"动画表情"在本地库里是**加密**的，第三方库也没有表情下载接口
   （`wechatauto.media.MediaDownloader` 只有图片/语音/视频/文件），所以
   **`emotion` 类型的表情收不到字节**——这一点在界面提示里如实写明，不假装支持。
   好消息是好友发的大部分"表情包"其实是以 `图片` 类型到达的，那些照收不误。
3. **`stickerOnly` 只能在下载之后判断**：微信的消息段里没有尺寸信息，
   要拿到图才知道大小。所以"只收表情/小图"是先下载再决定存不存。

默认设置刻意与 QQ 版不同：**默认开启**（用户明确要"保存别人发的图片"这个功能），
但**默认不收私聊**（群里的表情才是共享素材），随时可以在插件里改。
"""
from __future__ import annotations

from typing import Optional
import time

from . import PluginBase, PluginMeta, FieldSchema

# 一次 drain 最多下载几张：够快，又不会因为攒了几百张图把 CPU/磁盘打满
DRAIN_LIMIT = 5
# 登记队列上限。超出就丢最老的 —— 丢的是"收集"不是"回复"，宁可漏收也不占内存。
QUEUE_LIMIT = 200
# 最近处理过的媒体指纹，避免同一条消息被反复尝试下载（失败的不再重试）
SEEN_LIMIT = 2000


class StickerSteal(PluginBase):
    META = PluginMeta(
        id="sticker_steal",
        name="回复时收集表情图",
        description="只在图片已触发回复时收进本地图库；不下载闲聊图片，不后台轮询。可在表情包管理上传图片、改标签与识别。收集本身不发送。",
        category="media",
        enabled_by_default=True,
        config_schema={
            "enabled":        FieldSchema(type="bool", label="启用自动收集"),
            "watchImages":    FieldSchema(type="bool", label="旧版图片收集参数（保留，仍只收已触发回复的图）",
                                      description="保留旧配置兼容；不会因此下载未触发回复的群图。"),
        "stickerOnly":    FieldSchema(type="bool", label="只收表情包和小图（正常大小的照片不收）"),
            "allowPrivate":   FieldSchema(type="bool", label="私聊发来的也收"),
            "blockedSenders": FieldSchema(type="string", label="不收这些人的（逗号分隔昵称或 wxid）"),
            "maxLibrarySize": FieldSchema(type="int", label="最多收藏张数", min=10, max=2000),
            "maxLibraryMb":   FieldSchema(type="int", label="图库容量上限（MB）", min=6, max=512),
        },
        default_settings={"enabled": True, "watchImages": False,
                          "stickerOnly": True, "allowPrivate": False,
                          "blockedSenders": "", "maxLibrarySize": 800, "maxLibraryMb": 96},
    )

    def __init__(self, manager):
        super().__init__(manager)
        self._queue: list = []
        self._seen: list = []
        self._seen_set: set = set()

    # ── 同步钩子：只登记，不下载 ────────────────────────────────────────────
    def on_observed(self, message: dict, runtime) -> None:
        # 宿主在策略判定前显式标记只观察；不能留下稍后下载的任务。
        # 保留旧调用方已授权登记CDN表情的接口，宿主普通观察不走此兼容路径。
        if message.get('_observation_only'):
            return
        self.collect_for_reply(message, runtime, kinds=('emotion',))

    def collect_for_reply(self, message: dict, runtime, kinds=("image", "emotion")):
        if message.get('_picture_text_only'):
            return
        if not self.get("enabled"):
            return
        if message.get("kind") == "private" and not self.get("allowPrivate", False):
            return
        if self._blocked(message):
            return
        if not getattr(runtime, "stickers", None):
            return
        media = message.get("media") or []
        for item in media[:3]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind not in kinds:
                continue
            link = str(item.get("url") or "")
            if kind == "emotion":
                if not link:
                    # 没给直链的表情没法取（本地文件是加密的），跳过
                    continue
            elif kind == "image":
                if item.get("local_id") in (None, ""):
                    continue
            else:
                continue
            local_id = item.get("local_id")
            account = str(getattr(runtime, 'account', lambda: '')())
            key = f"{account}:{message.get('target')}:{item.get('shard')}:{local_id or link}"
            if key in self._seen_set:
                continue
            self._queue.append({"key": key, "account": account,
                                "chat": str(message.get("target") or ""),
                                "local_id": local_id, "shard": str(item.get("shard") or ""),
                                "url": link, "kind": kind,
                                "sender": str(message.get("sender_name") or message.get("sender") or "")})
            self._mark(key)
        while len(self._queue) > QUEUE_LIMIT:
            self._queue.pop(0)

    def _blocked(self, message: dict) -> bool:
        raw = str(self.get("blockedSenders", "") or "").replace("，", ",")
        names = {part.strip() for part in raw.split(",") if part.strip()}
        if not names:
            return False
        return bool(names & {str(message.get("sender") or ""), str(message.get("sender_name") or "")})

    def _mark(self, key: str) -> None:
        self._seen.append(key)
        self._seen_set.add(key)
        while len(self._seen) > SEEN_LIMIT:
            self._seen_set.discard(self._seen.pop(0))

    # ── 异步钩子：分批下载 ──────────────────────────────────────────────────
    async def drain(self, runtime) -> int:
        """把登记过的图片下载入本地图库，返回新增张数。"""
        store = getattr(runtime, "stickers", None)
        if not store or not self.get("enabled"):
            return 0
        added = 0
        for _ in range(min(DRAIN_LIMIT, len(self._queue))):
            if not self._queue:
                break
            item = self._queue.pop(0)
            if item.get('account') != str(getattr(runtime, 'account', lambda: '')()):
                continue
            if item.get('retry_at', 0) > time.monotonic():
                self._queue.append(item)
                continue
            data = b""
            if item.get("url"):
                # 表情（动画表情）走 CDN 直链：本地文件是加密的，但直链就是原图
                from ..sticker_library import fetch_image
                try:
                    data = await fetch_image(item["url"])
                except Exception:
                    data = b""
                if not data:
                    self._retry(item)
                    continue
            else:
                try:
                    answer = await runtime.transport.call("download_media", {
                        "chat": item["chat"], "local_id": item["local_id"],
                        "msg_type": "image", "shard": item["shard"]})
                except Exception:
                    self._retry(item)
                    continue
                if not isinstance(answer, dict):
                    self._retry(item)
                    continue
                path = str(answer.get("path") or "")
                if not answer.get("ok") or not path:
                    self._retry(item)
                    continue
                try:
                    with open(path, "rb") as handle:
                        data = handle.read()
                except OSError:
                    self._retry(item)
                    continue
            if not data:
                continue
            try:
                # 表情（emotion）本身就是表情包，不按"小图"再筛一遍
                if (item.get("kind") != "emotion" and self.get("stickerOnly", True)
                        and not _looks_like_sticker(data)):
                    continue
                entry, is_new = store.put(data, "", max_items=int(self.get("maxLibrarySize", 800)),
                                          max_mb=int(self.get("maxLibraryMb", 96)))
                store.bind({"shard": item["shard"], "local_id": item.get("local_id"),
                            "url": item.get("url", "")}, entry["id"])
                if is_new:
                    added += 1
                    hub = getattr(runtime, 'image_chat', None)
                    if hub and hub.automatic():
                        hub.enqueue([entry['id']], origin='auto')
            except (OSError, ValueError):
                continue
        if added:
            runtime.note(f"已收藏 {added} 张表情图片，可在「表情包管理」里改标签和说明")
        return added

    def _retry(self, item):
        # 数据库事件可能先于媒体落盘；只重试只读下载，不重发微信消息。
        item['attempts'] = item.get('attempts', 0) + 1
        if item['attempts'] < 3:
            item['retry_at'] = time.monotonic() + 1
            self._queue.append(item)

    async def tick(self, runtime) -> None:
        return

    async def on_prepare(self, message: dict, runtime) -> None:
        if message.get('_picture_text_only'):
            return
        # 机器人本来就要干活了，顺手把排队中的图收掉，不用等下一次 tick（60 秒）
        self.collect_for_reply(message, runtime)
        await self.drain(runtime)

    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        if str(message.get("text", "")).strip() != "/表情库":
            return None
        store = getattr(runtime, "stickers", None)
        if not store:
            return "本地表情库暂时读不出来，请检查数据目录或从备份恢复。"
        total = len(store.entries())
        state = "开" if self.get("enabled") else "关"
        return (f"机器人本地图库共 {total} 张（自动收集：{state}）。"
                "可在「插件中心 → 自动收集表情图」调整，或在「表情包管理」里改标签、识别和删除。")


def _looks_like_sticker(data: bytes) -> bool:
    """小图或动图才算表情包；正常大小的照片不收。

    阈值与 QQ 版一致（400px / 512KB）：微信侧拿不到"动画表情"标记，
    只能靠尺寸与体积判断，所以这里刻意保守 —— 宁可漏收一张，也别把整个图库塞满照片。
    """
    from ..sticker_library import inspect_image
    try:
        info = inspect_image(data)
    except ValueError:
        return False
    if info.get("animated"):
        return True
    return (int(info.get("width") or 0) <= 400 and int(info.get("height") or 0) <= 400
            and len(data) <= 512 * 1024)
