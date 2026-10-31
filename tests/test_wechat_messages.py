"""`wechat_transports.message_from_raw` 的离线测试（微信消息字典）。

被测的是"微信原始字典 → 项目消息字典"这一层纯函数（契约第 1 节）。它决定了
后面所有插件看到的世界：谁说的、在哪个会话、@ 没 @ 机器人、正文里的媒体占位符。

全部离线：用一个 `backend='mock'` 的 WeChatBridge 当"后端信息源"，不连微信、
不碰数据库、不联网。
"""

import tempfile
import time
import unittest
from pathlib import Path

from src.wechat_bridge import GROUP_SUFFIX, WeChatBridge
from src.wechat_transports import classify_media, message_from_raw


def make_bridge(self_id="wxid_mock_bot", account_name="模拟微信"):
    folder = tempfile.TemporaryDirectory()
    bridge = WeChatBridge(data_dir=Path(folder.name), backend="mock")
    bridge.connect()
    bridge.self_id = self_id
    bridge.account_name = account_name
    return folder, bridge


def raw(**overrides):
    """一条"群里有人说话"的原始字典，按需覆盖字段。"""
    data = {
        "username": "12345@chatroom",
        "type": "文本",
        "sender_username": "wxid_a",
        "sender_id": "wxid_a",
        "content": "大家好啊",
        "local_id": 7,
        "shard": "message_0",
        "create_time": 1700000000.0,
        "attr": "friend",
    }
    data.update(overrides)
    return data


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp, self.bridge = make_bridge()
        self.addCleanup(self._tmp.cleanup)

    def convert(self, **overrides):
        return message_from_raw(raw(**overrides), self.bridge, ["鲸鱼娘"])


class IdentityTests(BridgeTestCase):
    def test_group_at_bot_is_flagged(self):
        message = self.convert(content="@鲸鱼娘 在吗")
        self.assertTrue(message["at"])
        self.assertEqual(message["kind"], "group")
        self.assertEqual(message["id"], "message_0:7")
        self.assertEqual(message["local_id"], 7)
        self.assertEqual(message["shard"], "message_0")

    def test_alias_in_aliases_list_is_detected_when_at_sign_present(self):
        # message_from_raw 的 at 只认"正文里的 @名字"（微信群里 @ 人会把昵称写进正文）。
        # 配置触发词（aliases）走同一条路：`@小深` 也算点名。
        message = message_from_raw(raw(content="@小深 帮我看下"), self.bridge, ["鲸鱼娘", "小深"])
        self.assertTrue(message["at"])

    def test_at_of_someone_else_is_not_bot_at(self):
        # 别人 @ 别人：既不是机器人昵称也不是触发词 → 不算点名
        message = message_from_raw(raw(content="@小蓝 帮我看下"), self.bridge, ["鲸鱼娘", "小深"])
        self.assertFalse(message["at"])

    def test_bare_alias_without_at_sign_is_left_to_the_runtime(self):
        # 只说"小深"没打 @：这一层刻意不猜（宁可漏判，交给运行时的触发词兜底），
        # 见 src/wechat_transports._detect_at 的说明。
        message = message_from_raw(raw(content="小深 帮我看下"), self.bridge, ["鲸鱼娘", "小深"])
        self.assertFalse(message["at"])
        self.assertIn("小深", message["text"])   # 正文必须原样保留，别把触发词吃掉

    def test_mock_at_flag_is_honoured(self):
        # 模拟后端没法把 @ 写进正文时用 mock_at 显式标记
        self.assertTrue(self.convert(content="你好", mock_at=True)["at"])

    def test_plain_group_chatter_is_not_at(self):
        message = self.convert(content="今天天气不错")
        self.assertFalse(message["at"])
        self.assertEqual(message["at_ids"], [])

    def test_group_kind_from_chatroom_suffix(self):
        self.assertEqual(self.convert()["kind"], "group")
        self.assertTrue(raw()["username"].endswith(GROUP_SUFFIX))

    def test_private_kind_when_username_is_not_chatroom(self):
        message = self.convert(username="wxid_friend")
        self.assertEqual(message["kind"], "private")
        self.assertEqual(message["target"], "wxid_friend")

    def test_stable_target_and_sender_fields(self):
        message = self.convert(content="你好")
        self.assertEqual(message["target"], "12345@chatroom")
        # 拿不到群名时 target_name 退回某个可用标识（绝不能是空串，白名单要靠它匹配）
        self.assertTrue(message["target_name"])
        self.assertEqual(message["sender_id"], "wxid_a")
        self.assertEqual(message["sender_name"], "wxid_a")
        self.assertIn(message["sender"], ("wxid_a", "12345@chatroom"))
        self.assertEqual(message["notice"], None)
        self.assertEqual(message["raw_type"], "文本")

    def test_explicit_target_name_wins(self):
        message = self.convert(target_name="测试群")
        self.assertEqual(message["target_name"], "测试群")


class DropRulesTests(BridgeTestCase):
    def test_self_message_by_sender_username_is_dropped(self):
        # 自己发的话不进管线，否则机器人会对着自己的话回复
        self.assertIsNone(self.convert(sender_username=self.bridge.self_id))

    def test_self_message_by_attr_is_dropped(self):
        self.assertIsNone(self.convert(attr="self"))

    def test_system_message_is_dropped(self):
        self.assertIsNone(self.convert(type="系统消息", content="你撤回了一条消息"))

    def test_message_without_username_is_dropped(self):
        self.assertIsNone(self.convert(username=""))

    def test_non_dict_is_dropped(self):
        for value in (None, "x", 3, []):
            with self.subTest(value=value):
                self.assertIsNone(message_from_raw(value, self.bridge, []))


class MediaPlaceholderTests(BridgeTestCase):
    def test_media_types_map_to_segments_and_placeholders(self):
        cases = {
            "图片": ("image", "[图片]"),
            "语音": ("voice", "[语音]"),
            "视频": ("video", "[视频]"),
            "动画表情": ("emotion", "[表情]"),
        }
        for raw_type, (kind, placeholder) in cases.items():
            with self.subTest(raw_type=raw_type):
                message = self.convert(type=raw_type, content="")
                self.assertEqual(message["media"][0]["type"], kind)
                self.assertEqual(message["text"], placeholder)
                self.assertIn(placeholder, message["text"])

    def test_media_segment_carries_download_keys(self):
        message = self.convert(type="图片", content="")
        segment = message["media"][0]
        self.assertEqual(segment["local_id"], 7)
        self.assertEqual(segment["shard"], "message_0")

    def test_text_message_has_no_media_segment(self):
        message = self.convert(content="你好")
        self.assertEqual(message["media"], [])

    def test_link_prefix_becomes_link_segment(self):
        for content in ("[链接] https://example.com", "链接:https://example.com"):
            with self.subTest(content=content):
                message = self.convert(type="文件/链接/卡片", content=content)
                self.assertEqual(message["media"][0]["type"], "link")
                self.assertEqual(message["text"], "[链接]")

    def test_file_prefix_becomes_file_segment_with_name(self):
        message = self.convert(type="文件/链接/卡片", content="[文件] 季度报表.xlsx")
        self.assertEqual(message["media"][0]["type"], "file")
        self.assertEqual(message["media"][0]["name"], "季度报表.xlsx")
        self.assertEqual(message["text"], "[文件:季度报表.xlsx]")

    def test_bare_file_prefix_is_file(self):
        message = self.convert(type="文件/链接/卡片", content="文件 说明.txt")
        self.assertEqual(message["media"][0]["type"], "file")
        self.assertIn("[文件", message["text"])

    def test_location_and_card_are_other(self):
        for content in ("[位置] xxx", "[个人名片] 小明"):
            with self.subTest(content=content):
                message = self.convert(type="文件/链接/卡片", content=content)
                self.assertEqual(message["media"][0]["type"], "other")

    def test_unrecognised_card_text_falls_back_to_file(self):
        message = self.convert(type="文件/链接/卡片", content="[未知卡片] 内容")
        self.assertEqual(message["media"][0]["type"], "file")
        self.assertEqual(message["text"], "[文件/链接]")


class ClassifyMediaTests(unittest.TestCase):
    def test_known_types(self):
        self.assertEqual(classify_media("图片", ""), ("image", "[图片]"))
        self.assertEqual(classify_media("语音", ""), ("voice", "[语音]"))

    def test_text_has_no_media(self):
        self.assertEqual(classify_media("文本", "你好"), (None, ""))

    def test_garbage_does_not_raise(self):
        self.assertEqual(classify_media(None, None), (None, ""))


class MessageIdTests(BridgeTestCase):
    def test_id_uses_shard_and_local_id(self):
        self.assertEqual(self.convert()["id"], "message_0:7")

    def test_id_falls_back_to_username_and_time_without_local_id(self):
        message = self.convert(local_id=None, create_time=1700000000.0)
        self.assertEqual(message["id"], "12345@chatroom:1700000000000")
        self.assertIsNone(message["local_id"])


if __name__ == "__main__":
    unittest.main()
