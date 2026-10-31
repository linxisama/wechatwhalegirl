"""send_pacing 的 \x01PACE:N\x01 标记解析测试（微信口径）。

标记会把一条回复拆成多条发送，两次发送之间 sleep N 毫秒。
`wechat_app._send_with_pacing` 的签名是 `(transport, message, reply, meta=None)`：
微信版的 transport 都是三参数的 `send(message, text, meta)`。
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.wechat_app import _send_with_pacing


class FakeTransport:
    """记录每次发送的 (message, text, meta)。"""

    def __init__(self):
        self.calls = []

    async def send(self, message, text, meta=None):
        self.calls.append((message, text, meta))

    @property
    def texts(self):
        return [text for _, text, _ in self.calls]


class PacingMarkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = {"id": "msg-1", "text": "hello"}

    async def _run(self, reply, fake, sleep_mock, meta=None):
        with patch("src.wechat_app.asyncio.sleep", sleep_mock):
            await _send_with_pacing(fake, self.message, reply, meta)

    async def test_no_marker_sends_original_once_without_strip(self):
        # 无标记路径按原实现直接发送 reply 原文，不做 strip。
        fake = FakeTransport()
        sleep_mock = SimpleNamespace(call_args_list=[])
        await self._run("  hello world ", fake, sleep_mock)
        self.assertEqual(fake.texts, ["  hello world "])
        self.assertEqual(sleep_mock.call_args_list, [])

    async def test_single_marker_splits_segments_and_skips_empty(self):
        fake = FakeTransport()
        sleep_calls = []
        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        reply = chr(1) + "PACE:500" + chr(1) + " first " + chr(1) + "PACE:700" + chr(1) + "   " + chr(1) + "PACE:900" + chr(1) + "last"
        await self._run(reply, fake, fake_sleep)
        self.assertEqual(fake.texts, ["first", "last"])
        self.assertEqual(sleep_calls, [0.5, 0.7, 0.9])

    async def test_multiple_markers_accumulate_in_order(self):
        fake = FakeTransport()
        sleep_calls = []
        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        reply = chr(1) + "PACE:10" + chr(1) + "a" + chr(1) + "PACE:20" + chr(1) + "b" + chr(1) + "PACE:30" + chr(1) + "c"
        await self._run(reply, fake, fake_sleep)
        self.assertEqual(fake.texts, ["a", "b", "c"])
        self.assertEqual(sleep_calls, [0.01, 0.02, 0.03])

    async def test_sleep_uses_integer_milliseconds_over_1000(self):
        fake = FakeTransport()
        calls = []
        async def fake_sleep(seconds):
            calls.append(seconds)

        reply = chr(1) + "PACE:25" + chr(1) + "text"
        await self._run(reply, fake, fake_sleep)
        self.assertEqual(calls, [0.025])

    async def test_malformed_markers_are_not_treated_as_markers(self):
        malformed = [
            chr(1) + "PACE:" + chr(1),
            chr(1) + "pace:10" + chr(1),
            chr(1) + "PACE:10",
            chr(1) + "PACE:x" + chr(1),
            chr(1) + "PACE:10" + chr(2),
        ]
        for reply in malformed:
            fake = FakeTransport()
            sleep_calls = []
            async def fake_sleep(seconds):
                sleep_calls.append(seconds)

            await self._run(reply, fake, fake_sleep)
            self.assertEqual(fake.texts, [reply.strip()])
            self.assertEqual(sleep_calls, [])

    async def test_marker_only_content_does_not_send_empty_message(self):
        fake = FakeTransport()
        sleep_calls = []
        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        reply = chr(1) + "PACE:500" + chr(1) + chr(1) + "PACE:600" + chr(1)
        await self._run(reply, fake, fake_sleep)
        self.assertEqual(fake.calls, [])
        self.assertEqual(sleep_calls, [0.5, 0.6])

    async def test_consecutive_markers_with_empty_text_do_not_send_blank(self):
        fake = FakeTransport()
        sleep_calls = []
        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        reply = chr(1) + "PACE:100" + chr(1) + "  " + chr(1) + "PACE:200" + chr(1) + "word"
        await self._run(reply, fake, fake_sleep)
        self.assertEqual(fake.texts, ["word"])
        self.assertEqual(sleep_calls, [0.1, 0.2])

    async def test_meta_only_rides_on_first_chunk(self):
        # 微信里 @ 与图片只应挂在第一条上，否则每句都会 @ 一遍 / 重发一次图
        fake = FakeTransport()
        sleep_calls = []
        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        reply = chr(1) + "PACE:10" + chr(1) + "第一句" + chr(1) + "PACE:10" + chr(1) + "第二句"
        await self._run(reply, fake, fake_sleep, meta={"mentions": ["小明"]})
        self.assertEqual(fake.texts, ["第一句", "第二句"])
        self.assertEqual(fake.calls[0][2], {"mentions": ["小明"]})
        self.assertIsNone(fake.calls[1][2])


if __name__ == "__main__":
    unittest.main()
