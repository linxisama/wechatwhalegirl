"""消息段组装与插件元数据链路的离线回归测试（微信口径）。

背景：插件在 on_post_reply 里返回 (文本, 元数据) 时，元数据必须真的变成
回复段（@ 提醒、图片/文件），而不是被静默丢弃。这条链路跨越
manager（收集元数据）→ wechat_app（_send_with_pacing 透传）→ transport（发送），
任何一环断掉都会表现为"插件看起来生效了、对方却收不到提醒"。

微信与 QQ 的关键差异（见 instructions/WECHAT-CONTRACT.md 第 3 节）：
- `at` 段按**群昵称** @，数据是 `{"name": "小明"}`，没有数字 QQ 号；
- `ALLOWED_TYPES = {text, at, image, file}`，没有 face/video；
- 图片/文件都是本机绝对路径（`{"type": "image", "data": {"file": ...}}`）。
"""

import asyncio
import json
import unittest

from src.plugin_segments import (
    ALLOWED_TYPES,
    build_reply_segments,
    normalize_mentions,
    strip_mentions_from_text,
)
from src.wechat_app import _send_with_pacing


class ReplySegmentsTests(unittest.TestCase):
    def test_text_only_keeps_single_text_segment(self):
        self.assertEqual(
            build_reply_segments("你好", None),
            [{"type": "text", "data": {"text": "你好"}}],
        )
        self.assertEqual(
            build_reply_segments("你好", {}),
            [{"type": "text", "data": {"text": "你好"}}],
        )

    def test_mentions_go_before_text_by_default(self):
        segments = build_reply_segments("你说得对", {"mentions": ["小明"]})
        self.assertEqual(segments[0], {"type": "at", "data": {"name": "小明"}})
        self.assertEqual(segments[1], {"type": "text", "data": {"text": "你说得对"}})

    def test_append_mentions_puts_at_last(self):
        segments = build_reply_segments("在的", {"mentions": ["小明"], "append_mentions": True})
        self.assertEqual(segments[0]["type"], "text")
        self.assertEqual(segments[1], {"type": "at", "data": {"name": "小明"}})

    def test_mention_forms_are_normalized_and_deduplicated(self):
        # 微信按名称 @：dict 形式只认 name；QQ 版的 qq/user_id 故意不认（认了会 @ 错人）
        mentions = normalize_mentions(["小明", {"name": "小明"}, {"name": "小红"},
                                       {"qq": "42"}, {"user_id": "7"}, None, ""])
        self.assertEqual([m["data"]["name"] for m in mentions], ["小明", "小红"])

    def test_qq_style_dict_forms_are_not_accepted(self):
        # QQ 版写法（qq / user_id 字段）在微信版没有意义：微信按名称 @，
        # 这些字段里的数字 ID 既不是 wxid 也不是昵称，认了只会 @ 错人。
        self.assertEqual(normalize_mentions([{"qq": "42"}, {"user_id": "7"}]), [])

    def test_scalar_value_is_used_as_a_name(self):
        # 契约允许元素直接是名字；标量会被 str() 成名字（"42" 就是一个合法群昵称）
        self.assertEqual(normalize_mentions([42])[0]["data"]["name"], "42")

    def test_media_segments_are_appended_in_order(self):
        meta = {"media": [
            {"type": "image", "data": {"file": "C:/tmp/a.png"}},
            {"type": "file", "data": {"file": "C:/tmp/b.zip"}},
        ]}
        segments = build_reply_segments("看图", meta)
        self.assertEqual([s["type"] for s in segments], ["text", "image", "file"])

    def test_disallowed_media_types_are_ignored(self):
        meta = {"media": [
            {"type": "video", "data": {"file": "a.mp4"}},
            {"type": "image", "data": "not-a-dict"},
            "garbage",
        ]}
        segments = build_reply_segments("x", meta)
        self.assertEqual([s["type"] for s in segments], ["text"])
        self.assertNotIn("video", ALLOWED_TYPES)
        self.assertNotIn("face", ALLOWED_TYPES)

    def test_allowed_types_are_wechat_only(self):
        self.assertEqual(ALLOWED_TYPES, frozenset({"text", "at", "image", "file"}))

    def test_empty_text_with_mentions_only(self):
        segments = build_reply_segments("", {"mentions": ["小明"]})
        self.assertEqual(segments, [{"type": "at", "data": {"name": "小明"}}])

    def test_empty_text_and_no_meta_returns_no_segments(self):
        self.assertEqual(build_reply_segments("", None), [])

    def test_text_override_replaces_reply(self):
        segments = build_reply_segments("原文本", {"text": "新文本"})
        self.assertEqual(segments[0]["data"]["text"], "新文本")


class StripMentionsTests(unittest.TestCase):
    def test_strips_claimed_mention(self):
        self.assertEqual(strip_mentions_from_text("@泡影之梦 你说得对", ["泡影之梦"]), " 你说得对")

    def test_does_not_touch_other_mentions(self):
        self.assertEqual(strip_mentions_from_text("@a 和 @b", ["a"]), " 和 @b")

    def test_does_not_strip_inside_email_like_text(self):
        self.assertEqual(strip_mentions_from_text("foo@bar 你好", ["bar"]), "foo@bar 你好")

    def test_longest_name_first(self):
        text = "@小鲸鱼娘 你好"
        self.assertEqual(strip_mentions_from_text(text, ["鲸鱼娘", "小鲸鱼娘"]), " 你好")


class FakeTransportMeta:
    """同时记录是否收到 meta 的假 transport（支持第三参数）。"""

    def __init__(self):
        self.calls = []

    async def send(self, message, text, meta=None):
        self.calls.append((text, meta))


class FakeTransportNoMeta:
    """只接受两个参数的旧式 transport，用于验证向后兼容。"""

    def __init__(self):
        self.calls = []

    async def send(self, message, text):
        self.calls.append(text)


class MetaPassthroughTests(unittest.IsolatedAsyncioTestCase):
    async def test_meta_is_passed_when_supported(self):
        transport = FakeTransportMeta()
        meta = {"mentions": ["小明"]}
        await _send_with_pacing(transport, {"id": "m"}, "你好", meta)
        self.assertEqual(transport.calls, [("你好", meta)])

    # 兼容性：只接受 (message, text) 的旧式 transport 不能被强行塞第三个参数。
    # 修法（已实施）：wechat_app 用 `_accepts_meta(transport.send)` 检查签名，
    # 只有在签名允许时才传元数据。
    async def test_meta_ignored_gracefully_when_unsupported(self):
        transport = FakeTransportNoMeta()
        await _send_with_pacing(transport, {"id": "m"}, "你好", {"mentions": ["小明"]})
        self.assertEqual(transport.calls, ["你好"])

    async def test_no_meta_keeps_two_argument_call(self):
        transport = FakeTransportNoMeta()
        await _send_with_pacing(transport, {"id": "m"}, "你好", None)
        self.assertEqual(transport.calls, ["你好"])

    async def test_meta_only_on_first_chunk_when_split(self):
        transport = FakeTransportMeta()
        marker = "\x01PACE:10\x01"
        from unittest.mock import patch
        with patch("src.wechat_app.asyncio.sleep", new=_noop_sleep):
            await _send_with_pacing(transport, {"id": "m"}, f"第一句{marker}第二句",
                                    {"mentions": ["小明"]})
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][0], "第一句")
        self.assertIsNotNone(transport.calls[0][1])
        self.assertEqual(transport.calls[1][0], "第二句")
        self.assertIsNone(transport.calls[1][1])


async def _noop_sleep(_seconds):
    return None


class WeChatTransportSegmentTests(unittest.IsolatedAsyncioTestCase):
    """真实传输层：确认 @ 名单与媒体段真的进了 WeChatBridge 的调用参数。"""

    def _make(self):
        import tempfile
        from pathlib import Path

        from src.wechat_bridge import WeChatBridge
        from src.wechat_transports import WeChatTransport

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        bridge = WeChatBridge(data_dir=root, backend="mock")
        bridge.connect()
        transport = WeChatTransport(bridge, send_interval=0)
        return bridge, transport

    async def test_group_reply_mentions_sender_by_name(self):
        bridge, transport = self._make()
        message = {"kind": "group", "target": "12345@chatroom", "target_name": "测试群",
                   "id": "m1", "sender_name": "阿明"}
        ok, detail = await transport.send(message, "你说得对", {"mentions": ["小明"]})
        self.assertTrue(ok, detail)
        self.assertEqual(bridge.sent_log[-1]["at"], ["小明"])

    async def test_group_reply_defaults_to_sender_when_no_mentions(self):
        # 微信群里不 @ 对方很容易被忽略，所以回复群消息默认 @ 发送者
        bridge, transport = self._make()
        message = {"kind": "group", "target": "12345@chatroom", "target_name": "测试群",
                   "id": "m1", "sender_name": "阿明"}
        await transport.send(message, "在的", None)
        self.assertEqual(bridge.sent_log[-1]["at"], ["阿明"])

    async def test_private_reply_does_not_mention(self):
        bridge, transport = self._make()
        message = {"kind": "private", "target": "wxid_a", "target_name": "阿明",
                   "id": "m1", "sender_name": "阿明"}
        await transport.send(message, "在的", None)
        self.assertEqual(bridge.sent_log[-1]["at"], [])

    async def test_image_media_is_sent_as_image(self):
        import tempfile
        from pathlib import Path

        bridge, transport = self._make()
        with tempfile.TemporaryDirectory() as folder:
            picture = Path(folder) / "a.png"
            picture.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            message = {"kind": "private", "target": "wxid_a", "target_name": "阿明", "id": "m1"}
            ok, detail = await transport.send(
                message, "看图", {"media": [{"type": "image", "data": {"file": str(picture)}}]})
            self.assertTrue(ok, detail)
        # 模拟后端把"发文件"也记进发送日志（带 [文件] 前缀）
        self.assertTrue(any("[文件]a.png" in entry["text"] for entry in bridge.sent_log))


if __name__ == "__main__":
    unittest.main()
