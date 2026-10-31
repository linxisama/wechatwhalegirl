"""`WeChatTransport` 的离线测试（发送、@ 名单、媒体、通道调用）。

用 `WeChatBridge(backend='mock')` 做后端：它不会碰微信，只把"要发什么"记进
`sent_log`，因此可以逐条断言传输层到底交给了桥什么（契约第 5 节）。

发送间隔（`send_interval`）是给微信风控留的余量，测试里用 0.05 秒验证它真的生效。
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.wechat_bridge import WeChatBridge
from src.wechat_transports import WeChatTransport


def make_transport(send_interval=0.0):
    folder = tempfile.TemporaryDirectory()
    bridge = WeChatBridge(data_dir=Path(folder.name), backend="mock")
    bridge.connect()
    transport = WeChatTransport(bridge, send_interval=send_interval)
    return folder, bridge, transport


def group_message(**overrides):
    message = {"id": "m1", "kind": "group", "target": "12345@chatroom",
               "target_name": "测试群", "sender_name": "阿明"}
    message.update(overrides)
    return message


class TransportTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp, self.bridge, self.transport = make_transport()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    @property
    def sent(self):
        return list(self.bridge.sent_log)


class SendTests(TransportTestCase):
    async def test_send_text_reaches_bridge(self):
        ok, detail = await self.transport.send(group_message(), "在的，怎么啦")
        self.assertTrue(ok, detail)
        self.assertEqual(self.sent[-1]["text"], "在的，怎么啦")
        self.assertEqual(self.sent[-1]["target"], "12345@chatroom")
        self.assertEqual(self.sent[-1]["target_name"], "测试群")

    async def test_group_reply_defaults_to_at_the_sender(self):
        await self.transport.send(group_message(sender_name="阿明"), "收到")
        self.assertEqual(self.sent[-1]["at"], ["阿明"])

    async def test_meta_mentions_override_the_default(self):
        await self.transport.send(group_message(), "收到", {"mentions": ["小明", "小红"]})
        self.assertEqual(self.sent[-1]["at"], ["小明", "小红"])

    async def test_suppression_blocks_both_explicit_metadata_and_default_sender(self):
        for meta in (None, {'mentions': ['小明', {'name': '小红'}]}):
            await self.transport.send(group_message(_suppress_mentions=True), '收到', meta)
            self.assertEqual(self.sent[-1]['at'], [])

    async def test_meta_mentions_accept_dict_form(self):
        await self.transport.send(group_message(), "收到", {"mentions": [{"name": "小明"}]})
        self.assertEqual(self.sent[-1]["at"], ["小明"])

    async def test_private_reply_never_at(self):
        message = group_message(kind="private", target="wxid_a")
        await self.transport.send(message, "在的")
        self.assertEqual(self.sent[-1]["at"], [])

    async def test_mention_count_is_capped(self):
        names = [f"群友{i}" for i in range(12)]
        await self.transport.send(group_message(), "集合", {"mentions": names})
        self.assertEqual(len(self.sent[-1]["at"]), WeChatBridge.MAX_AT)

    async def test_empty_text_sends_nothing(self):
        ok, detail = await self.transport.send(group_message(), "   ")
        self.assertTrue(ok)
        self.assertEqual(self.sent, [])

    async def test_offline_send_returns_failure_without_raising(self):
        self.bridge.online = False
        ok, detail = await self.transport.send(group_message(), "在的")
        self.assertFalse(ok)
        self.assertIn("未连接", detail)
        self.assertEqual(self.sent, [])

    async def test_offline_send_to_returns_failure_without_raising(self):
        self.bridge.online = False
        ok, detail = await self.transport.send_to("12345@chatroom", "在的")
        self.assertFalse(ok)
        self.assertIn("未连接", detail)


class MediaTests(TransportTestCase):
    async def _capture_files(self):
        """包一层 bridge.send_files，记录 as_image 与路径，同时保留原行为。"""
        calls = []
        original = self.bridge.send_files

        def wrapper(target, paths, target_name="", as_image=False):
            calls.append({"target": target, "paths": list(paths),
                          "target_name": target_name, "as_image": as_image})
            return original(target, paths, target_name, as_image)

        self.bridge.send_files = wrapper
        return calls

    def _png(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "a.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return path

    async def test_image_media_goes_through_send_files_as_image(self):
        calls = await self._capture_files()
        picture = self._png()
        ok, detail = await self.transport.send(
            group_message(), "", {"media": [{"type": "image", "data": {"file": str(picture)}}]})
        self.assertTrue(ok, detail)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["as_image"])
        self.assertEqual(calls[0]["paths"], [str(picture)])

    async def test_file_media_is_not_sent_as_image(self):
        calls = await self._capture_files()
        document = Path(self._png().parent) / "报表.xlsx"
        document.write_bytes(b"xlsx")
        ok, detail = await self.transport.send(
            group_message(), "", {"media": [{"type": "file", "data": {"file": str(document)}}]})
        self.assertTrue(ok, detail)
        self.assertFalse(calls[0]["as_image"])

    async def test_mixed_media_falls_back_to_file_channel(self):
        calls = await self._capture_files()
        folder = Path(self._png().parent)
        document = folder / "报表.xlsx"
        document.write_bytes(b"xlsx")
        await self.transport.send(group_message(), "", {"media": [
            {"type": "image", "data": {"file": str(folder / "a.png")}},
            {"type": "file", "data": {"file": str(document)}},
        ]})
        self.assertFalse(calls[0]["as_image"])

    async def test_text_and_media_are_both_sent(self):
        await self.transport.send(
            group_message(), "看图",
            {"media": [{"type": "image", "data": {"file": str(self._png())}}]})
        self.assertEqual(self.sent[0]["text"], "看图")
        self.assertTrue(any("[文件]" in entry["text"] for entry in self.sent))

    async def test_media_without_file_key_is_ignored(self):
        calls = await self._capture_files()
        await self.transport.send(group_message(), "看图", {"media": [{"type": "image", "data": {}}]})
        self.assertEqual(calls, [])

    async def test_send_files_returns_failure_when_offline(self):
        self.bridge.online = False
        ok, detail = await self.transport.send_files("12345@chatroom", [str(self._png())])
        self.assertFalse(ok)
        self.assertIn("未连接", detail)

    async def test_send_files_requires_paths(self):
        ok, detail = await self.transport.send_files("12345@chatroom", [])
        self.assertFalse(ok)
        self.assertIn("没有", detail)


class SendToTests(TransportTestCase):
    async def test_send_to_resolves_session_first(self):
        resolved = []
        self.bridge.resolve_session = lambda text: resolved.append(text) or "12345@chatroom"
        ok, detail = await self.transport.send_to("测试群", "大家好")
        self.assertTrue(ok, detail)
        self.assertEqual(resolved, ["测试群"])
        self.assertEqual(self.sent[-1]["target"], "12345@chatroom")

    async def test_send_to_empty_text_is_refused(self):
        ok, detail = await self.transport.send_to("12345@chatroom", "   ")
        self.assertFalse(ok)
        self.assertIn("空", detail)

    async def test_send_to_name_is_used_as_display_name_when_resolved(self):
        # 解析成功（群名 → username）时，原名作为会话显示名带下去（微信按显示名搜索会话）
        self.bridge.resolve_session = lambda text: "12345@chatroom"
        await self.transport.send_to("测试群", "大家好")
        self.assertEqual(self.sent[-1]["target"], "12345@chatroom")
        self.assertEqual(self.sent[-1]["target_name"], "测试群")

    async def test_send_to_unresolved_target_has_no_display_name(self):
        # resolve_session 原样返回时，target 本身就是发送用的名字，不再重复当显示名
        self.bridge.resolve_session = lambda text: text
        await self.transport.send_to("测试群", "大家好")
        self.assertEqual(self.sent[-1]["target"], "测试群")
        self.assertEqual(self.sent[-1]["target_name"], "")

    async def test_send_to_keeps_explicit_target_name(self):
        self.bridge.resolve_session = lambda text: text
        await self.transport.send_to("测试群", "大家好", target_name="备注名")
        self.assertEqual(self.sent[-1]["target_name"], "备注名")


class SendIntervalTests(TransportTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._tmp2, self.bridge, self.transport = make_transport(send_interval=0.05)
        self.addCleanup(self._tmp2.cleanup)

    async def test_two_sends_are_spaced_by_send_interval(self):
        await self.transport.send(group_message(), "第一句")
        started = time.monotonic()
        await self.transport.send(group_message(), "第二句")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.045, f"两次发送间隔过短：{elapsed}")

    async def test_interval_is_skipped_when_zero(self):
        self.transport.send_interval = 0
        await self.transport.send(group_message(), "第一句")
        started = time.monotonic()
        await self.transport.send(group_message(), "第二句")
        self.assertLess(time.monotonic() - started, 0.05)


class CallActionTests(TransportTestCase):
    async def test_get_self_info(self):
        result = await self.transport.call("get_self_info")
        self.assertTrue(result["ok"])
        self.assertEqual(result["wxid"], self.bridge.self_id)
        self.assertEqual(result["name"], self.bridge.account_name)
        self.assertTrue(result["name"])

    async def test_get_member_names_returns_list(self):
        result = await self.transport.call("get_member_names", {"chat": "12345@chatroom"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["names"], [])

    async def test_get_member_names_can_be_stubbed(self):
        self.bridge.member_names = lambda chat: ["小明", "小红"]
        result = await self.transport.call("get_member_names", {"chat": "12345@chatroom"})
        self.assertEqual(result["names"], ["小明", "小红"])

    async def test_download_media_reports_failure_when_unavailable(self):
        # 模拟后端拿不到媒体文件：必须如实报 ok=False，不能编一个路径出来
        result = await self.transport.call("download_media", {
            "chat": "12345@chatroom", "local_id": 7, "msg_type": "image", "shard": "message_0"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["path"], "")

    async def test_download_media_returns_path_when_available(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        picture = Path(folder.name) / "a.png"
        picture.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.bridge.download_media = lambda chat, local_id, msg_type, shard="": str(picture)
        result = await self.transport.call("download_media", {
            "chat": "12345@chatroom", "local_id": 7, "msg_type": "image"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], str(picture))

    async def test_send_to_action_reports_result(self):
        result = await self.transport.call("send_to", {
            "target": "12345@chatroom", "text": "大家好", "kind": "group"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.sent[-1]["text"], "大家好")

    async def test_unknown_action_is_refused(self):
        result = await self.transport.call("send_to_label", {"target": "x"})
        self.assertFalse(result["ok"])
        self.assertIn("不支持", result["message"])

    async def test_exception_is_returned_not_raised(self):
        def boom(*args, **kwargs):
            raise RuntimeError("桥炸了")

        self.bridge.member_names = boom
        result = await self.transport.call("get_member_names", {"chat": "12345@chatroom"})
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["message"])


class InterfaceTests(TransportTestCase):
    async def test_readonly_properties_mirror_the_bridge(self):
        self.assertEqual(self.transport.mode, "wechat")
        self.assertEqual(self.transport.backend, "mock")
        self.assertTrue(self.transport.online)
        self.assertEqual(self.transport.self_id, self.bridge.self_id)
        self.assertEqual(self.transport.account_name, self.bridge.account_name)

    async def test_close_disconnects_without_raising(self):
        await self.transport.close()
        self.assertFalse(self.bridge.online)

    async def test_blocking_bridge_calls_run_off_the_loop(self):
        # 键鼠/数据库调用绝不能跑在事件循环线程上（会把所有插件 tick 一起卡住）
        threads = []
        original = self.bridge.send_text

        def spy(target, text, target_name="", at=None):
            threads.append(threading.current_thread())
            return original(target, text, target_name, at)

        self.bridge.send_text = spy
        await self.transport.send(group_message(), "在的")
        self.assertTrue(threads)
        self.assertIsNot(threads[0], threading.main_thread())


if __name__ == "__main__":
    unittest.main()
