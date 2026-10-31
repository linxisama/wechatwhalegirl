"""服务商兼容性回归测试：模型不接受 temperature、额度被思考吃光、账号限速。

这三条都是**在用户机器上用真实 API 撞出来的**，不是推想：

1. `kimi-k2.6` / `kimi-k2.7-code` / `kimi-k3` 这些思考模型**只接受 `temperature=1`**，
   发 0.9（人设里的值）直接 `HTTP 400 invalid temperature: only 1 is allowed for this model`，
   于是机器人一条都回不出来。
2. 思考模型的思维链**也算在 `max_tokens` 里**：人设给 100 时正文是空的
   （`finish_reason=length`），上层只会看到"模型未返回内容"。
3. 账号速率上限可能低到 **`max RPM: 3`**，撞到 429 后按服务端说的"等 1 秒"重试仍然超限
   （滚动一分钟的窗口），必须学会上限并在本地排队。
"""

import asyncio
import unittest
from unittest import mock

import httpx

from src.ai_engine import AIEngine


def _reply(content: str, finish: str = "stop", model: str = "m") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}, "finish_reason": finish}],
              "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        request=httpx.Request("POST", "https://api.test/v1/chat/completions"))


def _error(status: int, message: str, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"message": message, "type": "invalid_request_error"}},
        headers=headers or {},
        request=httpx.Request("POST", "https://api.test/v1/chat/completions"))


class _FakeClient:
    """按顺序吐出预置响应，并记下每次真正发出去的 payload。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads: list[dict] = []
        self.is_closed = False

    async def post(self, url, json=None):                 # noqa: A002 - 与 httpx 同名
        self.payloads.append(json)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, url):
        self.payloads.append({"__get__": url})
        return self.responses.pop(0)

    async def aclose(self):
        self.is_closed = True


def _engine(client, **kw):
    engine = AIEngine("https://api.moonshot.cn/v1", "fake", model="kimi-k2.6",
                      max_retries=2, **kw)
    engine._get_client = mock.AsyncMock(return_value=client)
    return engine


class TemperatureFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_detects_the_service_wording(self):
        self.assertTrue(AIEngine._is_temperature_rejection(
            "invalid temperature: only 1 is allowed for this model"))
        self.assertTrue(AIEngine._is_temperature_rejection("temperature is not supported"))
        self.assertFalse(AIEngine._is_temperature_rejection("invalid max_tokens"))
        self.assertFalse(AIEngine._is_temperature_rejection(""))

    async def test_payload_drops_temperature_once_the_model_rejected_it(self):
        engine = AIEngine("https://api.test/v1", "fake", model="kimi-k2.6", temperature=0.9)
        self.assertIn("temperature", engine._build_payload([], "", None))
        engine._no_temperature.add("kimi-k2.6")
        self.assertNotIn("temperature", engine._build_payload([], "", None))
        # 别的模型不受影响
        engine._no_temperature.add("kimi-k2.6")
        self.assertIn("temperature", engine._build_payload([], "", None, model="deepseek-chat"))

    async def test_chat_retries_without_temperature_and_still_answers(self):
        client = _FakeClient([
            _error(400, "invalid temperature: only 1 is allowed for this model"),
            _reply("在呢！说呗～"),
        ])
        engine = _engine(client)
        text = await engine.chat([{"role": "user", "content": "在吗"}],
                                 temperature=0.9, max_tokens=100)
        self.assertEqual(text, "在呢！说呗～")
        self.assertEqual(len(client.payloads), 2)
        self.assertIn("temperature", client.payloads[0])
        self.assertNotIn("temperature", client.payloads[1], "重试时不该再带上被拒的字段")
        self.assertIn("kimi-k2.6", engine._no_temperature)
        # 学会了之后，后续请求一次就成功（不再每条都先撞一次 400）
        client2 = _FakeClient([_reply("嗯呐")])
        engine._get_client = mock.AsyncMock(return_value=client2)
        self.assertEqual(await engine.chat([{"role": "user", "content": "在吗"}],
                                           temperature=0.9), "嗯呐")
        self.assertNotIn("temperature", client2.payloads[0])
        await engine.close()

    async def test_other_400_keeps_the_service_message(self):
        client = _FakeClient([_error(400, "max_tokens must be at least 1")])
        engine = _engine(client)
        self.assertEqual(await engine.chat([{"role": "user", "content": "hi"}]), "")
        self.assertIn("400", engine.last_error)
        self.assertIn("max_tokens must be at least 1", engine.last_error)
        await engine.close()


class ThinkingBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_reply_truncated_by_thinking_widens_the_budget(self):
        """正文被输出上限吃掉时放大额度重发，而不是报"模型未返回内容"。"""
        client = _FakeClient([_reply("", finish="length"), _reply("我在的呀～")])
        engine = _engine(client)
        text = await engine.chat([{"role": "user", "content": "在吗"}], max_tokens=100)
        self.assertEqual(text, "我在的呀～")
        self.assertEqual(len(client.payloads), 2)
        self.assertEqual(client.payloads[0]["max_tokens"], 100)
        self.assertGreater(client.payloads[1]["max_tokens"], 100)
        await engine.close()

    async def test_widening_happens_only_once(self):
        client = _FakeClient([_reply("", finish="length"), _reply("", finish="length")])
        engine = _engine(client)
        self.assertEqual(await engine.chat([{"role": "user", "content": "在吗"}],
                                           max_tokens=100), "")
        self.assertEqual(len(client.payloads), 2)
        self.assertEqual(engine.last_error, "模型返回了空内容")
        await engine.close()

    async def test_real_empty_answer_is_not_retried(self):
        client = _FakeClient([_reply("", finish="stop")])
        engine = _engine(client)
        self.assertEqual(await engine.chat([{"role": "user", "content": "在吗"}]), "")
        self.assertEqual(len(client.payloads), 1)
        await engine.close()


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_rpm_is_read_from_the_service_message(self):
        message = ("Your account org-xxx<ak-yyy> request reached organization "
                   "max RPM: 3, please try again after 1 seconds")
        self.assertEqual(AIEngine._rpm_from_message(message), 3)
        self.assertIsNone(AIEngine._rpm_from_message("Organization Rate limit exceeded"))
        self.assertIsNone(AIEngine._rpm_from_message(""))

    async def test_429_teaches_the_limit_and_uses_the_service_hint(self):
        client = _FakeClient([
            _error(429, "request reached organization max RPM: 3", headers={"Retry-After": "1"}),
            _reply("排队之后还是回上了"),
        ])
        engine = _engine(client)
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with mock.patch("src.ai_engine.asyncio.sleep", side_effect=fake_sleep):
            text = await engine.chat([{"role": "user", "content": "在吗"}])
        self.assertEqual(text, "排队之后还是回上了")
        self.assertEqual(engine._rpm_limit, 3)
        # 服务端说"等 1 秒"，但滚动一分钟的窗口下 1 秒不够 —— 至少要等得更久
        self.assertTrue(slept and slept[0] >= 3.0, slept)
        await engine.close()

    async def test_local_pacing_waits_when_the_window_is_full(self):
        engine = AIEngine("https://api.test/v1", "fake", model="m")
        engine._rpm_limit = 3
        engine._request_times = [0.0, 0.1, 0.2]
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with mock.patch("src.ai_engine.time.monotonic", return_value=10.0), \
                mock.patch("src.ai_engine.asyncio.sleep", side_effect=fake_sleep):
            await engine._wait_for_rate_slot()
        self.assertTrue(slept, "窗口已满时必须先排队")
        self.assertGreater(slept[0], 45.0)

    async def test_no_limit_learned_means_no_waiting(self):
        """还没从服务端学到上限时不排队。

        也刻意不记录请求时间：这份列表只在限速生效时需要，没人管的话它会随会话
        一直长下去（每条请求一个时间戳）。
        """
        engine = AIEngine("https://api.test/v1", "fake", model="m")
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with mock.patch("src.ai_engine.asyncio.sleep", side_effect=fake_sleep):
            await engine._wait_for_rate_slot()
        self.assertEqual(slept, [])
        self.assertEqual(engine._request_times, [])

    async def test_429_message_tells_the_user_the_actual_limit(self):
        client = _FakeClient([_error(429, "request reached organization max RPM: 3"),
                              _error(429, "request reached organization max RPM: 3")])
        engine = _engine(client)
        with mock.patch("src.ai_engine.asyncio.sleep", new=mock.AsyncMock()):
            self.assertEqual(await engine.chat([{"role": "user", "content": "在吗"}]), "")
        self.assertIn("429", engine.last_error)
        self.assertIn("每分钟 3 次", engine.last_error)
        self.assertIn("充值", engine.last_error)
        await engine.close()


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
