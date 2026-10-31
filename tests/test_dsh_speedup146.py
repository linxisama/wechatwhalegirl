"""1.3.46：DSH 预检缓存提速、默认超时、回合兜底与复读注入（离线验证，不接真实 DSH 或微信）。"""
import asyncio
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from src.config_manager import DEFAULT_CONFIG
from src.dsh_api import DshSettings
from src.dsh_bridge import DshError
from src.wechat_app import WeChatRuntime
from tests.test_wechat_app_flow import RuntimeTestCase, raw_message


def event(kind, **data):
    return {'type': kind, 'data': {'turn': 'turn-1', **data}}


class Socket:
    """最小事件流替身：先给开场快照，收到 prompt 后再吐出完整一轮答复。"""

    def __init__(self):
        self.frames = []
        self.stream = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def send(self, text):
        self.stream = json.loads(text)['streamId']
        self.frames = [{'type': 'item', 'streamId': self.stream,
                        'value': {'type': 'snapshot', 'records': []}}]

    async def recv(self):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        raise RuntimeError('断流')

    def answer(self):
        self.frames += [{'type': 'item', 'streamId': self.stream, 'value': {'type': 'event', 'event': e}}
                        for e in (event('turn/start'),
                                  event('assistant/message', message={'id': 'm', 'content': [
                                      {'type': 'text', 'text': 'DSH完整答复'}]}),
                                  event('turn/end', reason={'kind': 'completed'}))]


class DshCase(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.sockets = []

    async def make_service(self, **dsh):
        bot = await self.start_runtime()
        from src.agent_authority import authority
        authority(bot).bind({'platform': 'desktop', 'account': 'mock_machine', 'subject': 'owner'})
        bot.config._config['dsh'] = {'enabled': True, 'base_url': 'http://127.0.0.1:3080',
                                     'model': 'deepseek-flash', **dsh}
        return bot

    def connect(self, *args, **kwargs):
        socket = Socket()
        self.sockets.append(socket)
        return socket

    async def rpc(self, client, base, method, request=None):
        self.calls.append(method)
        if method == 'session/create':
            return {'sessionId': 'sid-1'}
        if method == 'session/prompt':
            self.sockets[-1].answer()
        return {}

    def stack(self, service, probe):
        stack = ExitStack()
        stack.enter_context(patch.object(service, 'probe', probe))
        stack.enter_context(patch.object(service, 'rpc', self.rpc))
        stack.enter_context(patch('src.dsh_bridge.cookie_for', return_value='opaque'))
        stack.enter_context(patch('websockets.asyncio.client.connect', self.connect))
        return stack

    async def generate(self, service):
        return await service.generate({'kind': 'group', 'target': 'room@chatroom'},
                                      [{'role': 'user', 'content': '现在的问题'}], '人设+当前记忆')

    def inbox_payload(self, bot):
        row = bot.db.execute('SELECT payload FROM inbox ORDER BY created LIMIT 1').fetchone()
        return json.loads(row[0])

    def notes(self, bot):
        return [item['text'] for item in bot.events]


class ProbeCacheTests(DshCase):
    async def test_twenty_rounds_only_probe_once(self):
        bot = await self.make_service()
        probe = AsyncMock(return_value={'preset_ready': True, 'message': ''})
        with self.stack(bot.dsh, probe):
            for _ in range(20):
                self.assertEqual(await self.generate(bot.dsh), 'DSH完整答复')
        self.assertEqual(probe.await_count, 1)
        self.assertEqual(sum('DSH 预检缓存命中' in text for text in self.notes(bot)), 19)

    async def test_failed_probe_is_not_cached(self):
        bot = await self.make_service()
        probe = AsyncMock(side_effect=[DshError('DSH不可用'), {'preset_ready': True, 'message': ''}])
        with self.stack(bot.dsh, probe):
            with self.assertRaises(DshError):
                await self.generate(bot.dsh)
            self.assertEqual(await self.generate(bot.dsh), 'DSH完整答复')
        self.assertEqual(probe.await_count, 2)

    async def test_cache_expires_after_sixty_seconds(self):
        bot = await self.make_service()
        now = [1000.0]
        bot.dsh.probe_clock = lambda: now[0]
        probe = AsyncMock(return_value={'preset_ready': True, 'message': ''})
        with self.stack(bot.dsh, probe):
            await self.generate(bot.dsh)
            now[0] += 59.9
            await self.generate(bot.dsh)
            self.assertEqual(probe.await_count, 1)
            now[0] += 0.2
            await self.generate(bot.dsh)
        self.assertEqual(probe.await_count, 2)

    async def test_changed_model_invalidates_cache(self):
        bot = await self.make_service()
        probe = AsyncMock(return_value={'preset_ready': True, 'message': ''})
        with self.stack(bot.dsh, probe):
            await self.generate(bot.dsh)
            bot.config._config['dsh']['model'] = 'deepseek-pro'
            await self.generate(bot.dsh)
        self.assertEqual(probe.await_count, 2)

    async def test_cache_hit_keeps_preset_verdict_and_message(self):
        bot = await self.make_service()
        probe = AsyncMock(return_value={'preset_ready': False,
                                        'message': 'DSH已连接，缺少专用无工具人设'})
        with self.stack(bot.dsh, probe):
            with self.assertRaisesRegex(DshError, '缺少专用无工具人设'):
                await self.generate(bot.dsh)
            with self.assertRaisesRegex(DshError, '缺少专用无工具人设'):
                await self.generate(bot.dsh)
        self.assertEqual(probe.await_count, 1)

    async def test_manual_probe_is_never_served_from_cache(self):
        bot = await self.make_service()
        probe = AsyncMock(return_value={'preset_ready': True, 'message': ''})
        with self.stack(bot.dsh, probe):
            await self.generate(bot.dsh)
            await bot.dsh.probe()
            await bot.dsh.probe()
        self.assertEqual(probe.await_count, 3)


class DshFallbackTests(DshCase):
    async def test_dsh_error_falls_back_to_local_reply_once(self):
        bot = await self.make_service()
        self.install_ai('本地兜底答复')
        with patch.object(bot.dsh, 'generate', AsyncMock(side_effect=DshError('DSH连接中断'))) as generate:
            bot.ingest(raw_message('鲸鱼娘 说句话'))
            await self.wait_for_send()
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(len(bot.bridge.sent_log), 1)
        self.assertEqual(bot.bridge.sent_log[0]['text'], '本地兜底答复')
        self.assertTrue(any('DSH 未响应，已本地兜底：DSH连接中断' in text for text in self.notes(bot)))
        self.assertNotIn('_dsh_generated', self.inbox_payload(bot))

    async def test_dsh_timeout_falls_back_to_local_reply(self):
        bot = await self.make_service()
        self.install_ai('超时兜底答复')
        with patch.object(bot.dsh, 'generate', AsyncMock(side_effect=asyncio.TimeoutError())):
            bot.ingest(raw_message('鲸鱼娘 说句话'))
            await self.wait_for_send()
        self.assertEqual(bot.bridge.sent_log[0]['text'], '超时兜底答复')
        self.assertTrue(any('已本地兜底' in text for text in self.notes(bot)))

    async def test_successful_dsh_turn_keeps_marker_and_engine_name(self):
        bot = await self.make_service()
        self.install_ai(boom=True)
        with patch.object(bot.dsh, 'generate', AsyncMock(return_value='DSH答复原样')):
            bot.ingest(raw_message('鲸鱼娘 说句话'))
            await self.wait_for_send()
        self.assertEqual(bot.bridge.sent_log[0]['text'], 'DSH答复原样')
        self.assertTrue(self.inbox_payload(bot).get('_dsh_generated'))
        self.assertTrue(any('模型 DSH/deepseek-flash' in text for text in self.notes(bot)))

    async def test_dsh_turn_injects_recent_sentences_without_extra_model_call(self):
        bot = await self.make_service()
        ai = self.install_ai('本地兜底答复')
        with patch.object(bot, '_recent_replies', lambda message: ['上次说过的句子']), \
             patch.object(bot.dsh, 'generate', AsyncMock(return_value='DSH答复')) as generate:
            bot.ingest(raw_message('鲸鱼娘 说句话'))
            await self.wait_for_send()
        prompt = generate.await_args.args[2]
        self.assertIn('上次说过的句子', prompt)
        self.assertIn('这次绝对不要使用或改写', prompt)
        self.assertEqual(ai.calls, [])

    async def test_disabled_dsh_never_calls_dsh(self):
        bot = await self.make_service()
        bot.config._config['dsh']['enabled'] = False
        ai = self.install_ai('本地答复')
        with patch.object(bot.dsh, 'generate', AsyncMock(return_value='不该出现')) as generate:
            bot.ingest(raw_message('鲸鱼娘 说句话'))
            await self.wait_for_send()
        self.assertEqual(generate.await_count, 0)
        self.assertEqual(bot.bridge.sent_log[0]['text'], '本地答复')
        self.assertTrue(ai.calls)


class DefaultTimeoutTests(unittest.TestCase):
    def test_config_default_timeout_dropped_to_thirty_seconds(self):
        self.assertEqual(DEFAULT_CONFIG['dsh']['timeout'], 30)

    def test_api_field_default_is_thirty_and_bounds_stay_five_to_one_twenty(self):
        self.assertEqual(DshSettings.model_fields['timeout'].default, 30)
        self.assertEqual(DshSettings().timeout, 30)
        self.assertEqual(DshSettings(timeout=5).timeout, 5)
        self.assertEqual(DshSettings(timeout=120).timeout, 120)
        for value in (4, 121):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                DshSettings(timeout=value)


class BannedHintTests(unittest.TestCase):
    def test_recent_replies_is_empty_without_enabled_plugin(self):
        self.assertEqual(WeChatRuntime._recent_replies(
            SimpleNamespace(plugins=SimpleNamespace(_plugins={})), {'id': 'm'}), [])
        disabled = SimpleNamespace(enabled=False, recent_replies=lambda message: ['不该取到'])
        self.assertEqual(WeChatRuntime._recent_replies(
            SimpleNamespace(plugins=SimpleNamespace(_plugins={'reply_dedupe': disabled})), {'id': 'm'}), [])

    def test_recent_replies_survives_broken_plugin(self):
        def boom(message):
            raise RuntimeError('插件坏了')
        broken = SimpleNamespace(enabled=True, recent_replies=boom)
        self.assertEqual(WeChatRuntime._recent_replies(
            SimpleNamespace(plugins=SimpleNamespace(_plugins={'reply_dedupe': broken})), {'id': 'm'}), [])

    def test_banned_hint_is_a_noop_without_history_and_appends_at_most_five(self):
        self.assertEqual(WeChatRuntime._banned_hint('提示词', []), '提示词')
        hinted = WeChatRuntime._banned_hint('提示词', ['A', 'B', 'C', 'D', 'E', 'F'])
        self.assertTrue(hinted.startswith('提示词'))
        self.assertIn('A；B；C；D；E', hinted)
        self.assertNotIn('F', hinted)


if __name__ == '__main__':
    unittest.main()
