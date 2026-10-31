"""接管 DeepSeek 1.2.95 后的缺陷回归：完整发送链路和真实协议请求。"""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src.ai_engine import AIEngine
from src.wechat_app import _send_with_pacing
from src.wechat_transports import WeChatTransport
from src.voice_input import voice_needs_no_reply
from tests.test_image_understanding import PNG
from tests.test_wechat_app_flow import RuntimeTestCase, raw_message


class BubbleFlowTests(RuntimeTestCase):
    async def test_fast_and_normal_modes_keep_two_bubbles_and_plain_archive(self):
        for fast in (True, False):
            with self.subTest(fast=fast):
                runtime = await self.start_runtime()
                runtime.plugins.update_settings('reply_preferences', {'fastMode': fast})
                runtime.plugins.update_settings('chat_controls', {'replySegments': 2, 'segmentDelayMs': 200})
                self.install_ai('先检查接口地址。然后检查模型权限。')
                runtime.ingest(raw_message('帮我检查接口', chat='wxid_user', kind='private'))
                await self.wait_for_inbox_done()
                self.assertEqual([v['text'] for v in runtime.bridge.sent_log], ['先检查接口地址。', '然后检查模型权限。'])
                row = runtime.db.execute('SELECT state,reply,payload FROM inbox').fetchone()
                self.assertEqual(row[:2], ('sent', '先检查接口地址。然后检查模型权限。'))
                self.assertEqual(json.loads(row[2])['_reply_chunks_sent'], 2)
                self.assertNotIn('PACE', row[1])
                await runtime.close()

    async def test_second_bubble_failure_is_uncertain_and_not_retried(self):
        runtime = await self.start_runtime()
        # 1.3.57: two bubbles are opt-in now, so ask for two explicitly
        runtime.plugins.update_settings('chat_controls', {'replySegments': 2})
        self.install_ai('第一步检查地址。第二步检查密钥。')
        calls = []
        async def send(message, text, meta=None):
            calls.append(text)
            return (len(calls) == 1, '发送结果不确定')
        runtime.transport.send = send
        runtime.ingest(raw_message('测试两步', chat='wxid_user', kind='private'))
        async with asyncio.timeout(3):
            while runtime.db.execute('SELECT state FROM inbox').fetchone()[0] != 'uncertain':
                await asyncio.sleep(.02)
        await asyncio.sleep(.2)
        row = runtime.db.execute('SELECT error,payload FROM inbox').fetchone()
        self.assertEqual(len(calls), 2)
        self.assertIn('不重发', row[0])
        self.assertEqual(json.loads(row[1])['_reply_chunks_sent'], 1)

    async def test_untranscribed_voice_sends_nothing_and_never_calls_model(self):
        runtime = await self.start_runtime()
        runtime.plugins.update_settings('speech_to_text_wechat', {'enabled': False})
        ai = self.install_ai(boom=True)
        raw = raw_message('[语音]', chat='wxid_user', kind='private')
        raw['type'] = '语音'
        runtime.ingest(raw)
        await self.wait_for_inbox_done()
        self.assertEqual(ai.calls, [])
        self.assertEqual(runtime.bridge.sent_log, [])
        state, error = runtime.db.execute('SELECT state,error FROM inbox').fetchone()
        self.assertEqual(state, 'skipped')
        self.assertIn('语音未转写', error)


class SendGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_between_chunks_stops_next_and_media_is_sent_once(self):
        calls = []
        allowed = True
        async def send(msg, text, meta=None):
            nonlocal allowed
            calls.append((text, meta))
            allowed = False
            return True, ''
        msg = {}
        with self.assertRaisesRegex(RuntimeError, '不补发'):
            await _send_with_pacing(SimpleNamespace(send=send), msg,
                '第一句\x01PACE:0\x01第二句', {'media': ['only-once']}, guard=lambda: allowed)
        self.assertEqual(calls, [('第一句', {'media': ['only-once']})])
        self.assertEqual(msg['_reply_chunks_sent'], 1)


class DedicatedVisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_errors_keep_category_without_stale_result_or_usage(self):
        for failure in (400, 401, 403, 404, 429, 503, 'timeout', 'network'):
            with self.subTest(failure=failure):
                def handler(request):
                    if failure == 'timeout':
                        raise httpx.ReadTimeout('private endpoint details', request=request)
                    if failure == 'network':
                        raise httpx.ConnectError('private endpoint details', request=request)
                    return httpx.Response(failure, json={'error': {'message': 'rejected'}})
                real_client = httpx.AsyncClient
                def client(**kwargs):
                    return real_client(**kwargs, transport=httpx.MockTransport(handler))
                engine = AIEngine('https://main.test/v1', 'main-key')
                engine.last_error = 'stale'
                with patch('src.ai_engine.httpx.AsyncClient', side_effect=client):
                    with self.assertRaises(httpx.HTTPError):
                        await engine.describe_images([PNG], api_url='https://vision.test/v1', api_key='key')
                expected = ('超时' if failure == 'timeout' else 'ConnectError' if failure == 'network'
                            else f'HTTP {failure}')
                self.assertIn(expected, engine.last_error)
                self.assertNotIn('private endpoint', engine.last_error)
                self.assertEqual(engine.usage.request_count, 0)
                await engine.close()

    async def test_endpoint_controls_image_format_headers_and_usage(self):
        for main_style, url, target_style in (
            ('anthropic', 'https://vision.test/v1', 'openai'),
            ('openai', 'https://vision.test/v1/messages', 'anthropic'),
        ):
            with self.subTest(style=target_style):
                requests = []
                def handler(request):
                    requests.append(request)
                    if target_style == 'anthropic':
                        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '蓝色方块'}],
                            'usage': {'input_tokens': 12, 'output_tokens': 3}})
                    return httpx.Response(200, json={'choices': [{'message': {'content': [
                        {'type': 'reasoning', 'text': '不要泄露'}, {'type': 'text', 'text': '蓝色方块'}]}}],
                        'usage': {'prompt_tokens': 12, 'completion_tokens': 3}})
                real_client = httpx.AsyncClient
                def client(**kwargs):
                    return real_client(**kwargs, transport=httpx.MockTransport(handler))
                engine = AIEngine('https://chat.test/v1', 'chat-key', api_style=main_style)
                original = (engine.api_url, engine.api_style, engine.model)
                engine.last_error = 'HTTP 403 stale'
                with patch('src.ai_engine.httpx.AsyncClient', side_effect=client):
                    text = await engine.describe_images([PNG], api_url=url, api_key='vision-key')
                self.assertEqual(text, '蓝色方块')
                self.assertEqual(engine.last_error, '')
                self.assertEqual(engine.usage.total_tokens, 15)
                self.assertEqual(engine.usage.request_count, 1)
                self.assertEqual((engine.api_url, engine.api_style, engine.model), original)
                request = requests[0]
                payload = json.loads(request.content)
                part = payload['messages'][0]['content'][1]
                self.assertEqual(part['type'], 'image' if target_style == 'anthropic' else 'image_url')
                if target_style == 'anthropic':
                    self.assertEqual(request.headers['x-api-key'], 'vision-key')
                else:
                    self.assertEqual(request.headers['authorization'], 'Bearer vision-key')
                    self.assertEqual(request.url.path, '/v1/chat/completions')
                await engine.close()


class OCRLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_vision_does_not_wait_for_slow_ocr(self):
        from src.plugins.manager import PluginManager
        with tempfile.TemporaryDirectory() as folder:
            manager = PluginManager(Path(folder))
            plugin = manager._plugins['image_ocr']
            release = threading.Event()
            reader = SimpleNamespace(read=lambda path: (release.wait(5), 'late text')[1])
            message = {'vision_description': '蓝色方块'}
            start = time.monotonic()
            try:
                result = await plugin._read_without_holding_vision(reader, 'unused', message, SimpleNamespace(note=lambda v: None))
                self.assertEqual(result, '')
                self.assertLess(time.monotonic() - start, 1.7)
                self.assertNotIn('vision_text', message)
            finally:
                release.set()


class VoiceInputTests(unittest.TestCase):
    def test_caption_or_transcript_is_not_replaced(self):
        for msg in ({'text': '[语音] 请解释这个报错'}, {'text': '[语音]', 'voice_text': '你好'}):
            self.assertFalse(voice_needs_no_reply(dict(msg, media=[{'type': 'voice'}])))

    def test_untranscribed_voice_is_skipped_instead_of_noticing(self):
        self.assertTrue(voice_needs_no_reply({'media': [{'type': 'voice'}], 'text': '[语音]'}))

    def test_cache_cannot_cross_account_conversation_or_model_variant(self):
        from src.plugins.manager import PluginManager
        with tempfile.TemporaryDirectory() as folder:
            plugin = PluginManager(Path(folder))._plugins['sticker_memory']
            scope = 'wechat|account1|group:a'
            plugin.record_description(b'image', '蓝色方块', scope, 'model1|2048')
            self.assertEqual(plugin.cached(b'image', scope, 'model1|2048'), '蓝色方块')
            for other, variant in ((scope, 'model2|2048'), (scope, 'model1|4096'),
                                   ('wechat|account2|group:a', 'model1|2048'),
                                   ('wechat|account1|group:b', 'model1|2048')):
                self.assertEqual(plugin.cached(b'image', other, variant), '')

    def test_release_zip_uses_allowlist_and_default_configuration(self):
        from tools import make_release
        import zipfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'src').mkdir()
            (root / 'src' / 'safe.py').write_text('# source', encoding='utf-8')
            (root / 'data').mkdir()
            (root / 'data' / 'messages.json').write_text('PRIVATE_HISTORY')
            (root / 'mystery.tmpmarket').mkdir()
            (root / 'mystery.tmpmarket' / 'private.json').write_text('PRIVATE')
            (root / 'config.wechat.yaml').write_text('api_key: PRIVATE_KEY')
            out = root / 'source.zip'
            with patch.object(make_release, 'ROOT', root):
                make_release.write_source_zip(out)
            with zipfile.ZipFile(out) as z:
                self.assertEqual(set(z.namelist()), {'src/safe.py', 'config.wechat.yaml'})
                self.assertNotIn(b'PRIVATE', z.read('config.wechat.yaml'))


class MediaLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_account_cannot_receive_or_cache_old_download(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'download.png'
            image.write_bytes(b'test')
            release = threading.Event()
            bridge = SimpleNamespace(online=True, self_id='wxid_old', _db=object(),
                download_media=lambda *args: (release.wait(3), str(image))[1], disconnect=lambda: None)
            transport = WeChatTransport(bridge)
            task = asyncio.create_task(transport.download_media('chat', 1, 'image'))
            await asyncio.sleep(.05)
            bridge.self_id = 'wxid_new'
            bridge._db = object()
            release.set()
            self.assertIsNone(await task)
            self.assertEqual(transport._media_cache, {})
            await transport.close()

    async def test_close_discards_shared_task_without_cache_backfill(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'download.png'
            image.write_bytes(b'test')
            release = threading.Event()
            bridge = SimpleNamespace(online=True, self_id='wxid', _db=object(),
                download_media=lambda *args: (release.wait(3), str(image))[1], disconnect=lambda: None)
            transport = WeChatTransport(bridge)
            task = asyncio.create_task(transport.download_media('chat', 1, 'image'))
            await asyncio.sleep(.05)
            await transport.close()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(transport._media_cache, {})
            self.assertEqual(transport._media_tasks, {})
            self.assertIsNone(await transport.download_media('chat', 1, 'image'))
