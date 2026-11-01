"""1.3.71：发送链路自检（send_selftest）与 /api/wechat/selftest-send。

全部离线：不启真实微信、不发任何消息、不监听端口。通道对象一律用替身，
路由测试用 fastapi.TestClient + 只带 transport 的假 bot。
"""
import asyncio
import inspect
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import send_selftest as selftest


class FakeClock:
    """可推进的单调钟，用来验证冷却窗口。"""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeTransport:
    """只实现自检用到的两个属性：online 与 send_to。"""

    def __init__(self, ok=True, message="发送成功", online=True, error=None):
        self.online = online
        self.ok = ok
        self.message = message
        self.error = error
        self.calls = []

    async def send_to(self, target, text, kind="", target_name=""):
        self.calls.append({"target": target, "text": text, "kind": kind, "target_name": target_name})
        if self.error is not None:
            raise self.error
        return self.ok, self.message


class TextTests(unittest.TestCase):
    def test_text_has_prefix_version_and_time(self):
        text = selftest.build_text("1.3.71", now=time.mktime((2026, 1, 2, 3, 4, 5, 0, 0, -1)))
        self.assertTrue(text.startswith(selftest.SELFTEST_PREFIX))
        self.assertIn("WhaleGirlWeChat 1.3.71", text)
        self.assertIn("03:04:05", text)

    def test_text_never_contains_account_or_chat_name(self):
        text = selftest.build_text("1.3.71")
        self.assertNotIn("wxid", text)
        self.assertNotIn("@chatroom", text)

    def test_text_is_short_enough_for_a_single_bubble(self):
        text = selftest.build_text("1.3.71" * 40)
        self.assertLessEqual(len(text), selftest.SELFTEST_MAX_CHARS)

    def test_empty_version_does_not_leave_double_space(self):
        text = selftest.build_text("")
        self.assertNotIn("  ", text)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def make(self, transport):
        return selftest.SendSelfTest(transport, version="1.3.71", clock=self.clock)

    def test_without_confirm_nothing_is_sent(self):
        transport = FakeTransport()
        result = asyncio.run(self.make(transport).run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_confirmed")
        self.assertEqual(transport.calls, [])

    def test_confirm_sends_exactly_one_message_to_file_helper(self):
        transport = FakeTransport()
        result = asyncio.run(self.make(transport).run(confirm=True))
        self.assertTrue(result["ok"])
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["target"], "文件传输助手")
        self.assertEqual(call["target_name"], "文件传输助手")
        self.assertEqual(call["kind"], "private")
        self.assertIn(selftest.SELFTEST_PREFIX, call["text"])
        self.assertGreaterEqual(result["elapsed"], 0)

    def test_target_cannot_be_chosen_by_caller(self):
        params = inspect.signature(selftest.SendSelfTest.run).parameters
        self.assertNotIn("target", params)
        self.assertEqual(list(params), ["self", "confirm"])

    def test_offline_transport_is_reported_without_sending(self):
        transport = FakeTransport(online=False)
        result = asyncio.run(self.make(transport).run(confirm=True))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "offline")
        self.assertIn("启动机器人", result["message"])
        self.assertEqual(transport.calls, [])

    def test_missing_transport_is_reported(self):
        result = asyncio.run(self.make(None).run(confirm=True))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unavailable")

    def test_failure_detail_is_passed_through(self):
        transport = FakeTransport(ok=False, message="输入框已有草稿，本次未发送")
        result = asyncio.run(self.make(transport).run(confirm=True))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "send_failed")
        self.assertIn("输入框已有草稿", result["message"])

    def test_exception_becomes_result_not_traceback(self):
        transport = FakeTransport(error=RuntimeError("窗口句柄失效"))
        result = asyncio.run(self.make(transport).run(confirm=True))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "error")
        self.assertIn("RuntimeError", result["message"])

    def test_second_click_within_cooldown_is_refused(self):
        transport = FakeTransport()
        service = self.make(transport)
        asyncio.run(service.run(confirm=True))
        self.clock.advance(selftest.SELFTEST_COOLDOWN - 1)
        again = asyncio.run(service.run(confirm=True))
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "too_soon")
        self.assertEqual(len(transport.calls), 1)

    def test_after_cooldown_a_second_self_test_is_allowed(self):
        transport = FakeTransport()
        service = self.make(transport)
        asyncio.run(service.run(confirm=True))
        self.clock.advance(selftest.SELFTEST_COOLDOWN + 1)
        again = asyncio.run(service.run(confirm=True))
        self.assertTrue(again["ok"])
        self.assertEqual(len(transport.calls), 2)

    def test_failed_attempt_also_starts_cooldown(self):
        transport = FakeTransport(ok=False, message="超时")
        service = self.make(transport)
        asyncio.run(service.run(confirm=True))
        self.clock.advance(1)
        again = asyncio.run(service.run(confirm=True))
        self.assertEqual(again["reason"], "too_soon")
        self.assertEqual(len(transport.calls), 1)

    def test_cooldown_left_counts_down(self):
        transport = FakeTransport()
        service = self.make(transport)
        self.assertEqual(service.cooldown_left(), 0.0)
        asyncio.run(service.run(confirm=True))
        self.assertAlmostEqual(service.cooldown_left(), selftest.SELFTEST_COOLDOWN, places=1)
        self.clock.advance(selftest.SELFTEST_COOLDOWN)
        self.assertEqual(service.cooldown_left(), 0.0)

    def test_result_always_carries_target_and_text(self):
        transport = FakeTransport()
        result = asyncio.run(self.make(transport).run(confirm=True))
        self.assertEqual(result["target"], "文件传输助手")
        self.assertTrue(result["text"].startswith(selftest.SELFTEST_PREFIX))


class RouteTests(unittest.TestCase):
    def setUp(self):
        from src.wechat_app import create_app
        self.app = create_app()
        self.bot = type("Bot", (), {})()
        self.bot.transport = FakeTransport()
        self.app.state.bot = self.bot
        self.client = TestClient(self.app)

    def test_route_requires_explicit_confirm(self):
        response = self.client.post("/api/wechat/selftest-send", json={})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["reason"], "not_confirmed")
        self.assertEqual(self.bot.transport.calls, [])

    def test_route_sends_once_and_reports_success(self):
        response = self.client.post("/api/wechat/selftest-send", json={"confirm": True})
        body = response.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.bot.transport.calls), 1)
        self.assertEqual(body["target"], "文件传输助手")

    def test_route_reuses_one_service_so_cooldown_holds_across_calls(self):
        self.client.post("/api/wechat/selftest-send", json={"confirm": True})
        second = self.client.post("/api/wechat/selftest-send", json={"confirm": True}).json()
        self.assertEqual(second["reason"], "too_soon")
        self.assertEqual(len(self.bot.transport.calls), 1)

    def test_broken_body_is_treated_as_not_confirmed(self):
        response = self.client.post("/api/wechat/selftest-send", content=b"not json",
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reason"], "not_confirmed")


if __name__ == "__main__":
    unittest.main()
