"""1.2.20 基础上的媒体回归：真图片 + 模拟下载/模型，不连接微信。"""
import asyncio
import base64
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from src.image_input import vision_failure
from src.plugins.manager import PluginManager
from src.sticker_library import StickerLibrary, frames_for_vision, inspect_image
from src.wechat_bridge import WeChatBridge
from src.wechat_transports import WeChatTransport


def animated():
    frames = [Image.new('RGB', (32, 32), c) for c in ('red', 'green', 'blue')]
    out = io.BytesIO()
    frames[0].save(out, 'GIF', save_all=True, append_images=frames[1:], duration=100)
    return out.getvalue()


class RecognitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root / 'plugins')
        self.path = self.root / 'animation.gif'
        self.path.write_bytes(animated())
        self.download = AsyncMock(return_value={'ok': True, 'path': str(self.path)})
        self.ai = SimpleNamespace(api_url='https://example.test/v1', model='my vision',
                                  describe_images=AsyncMock(return_value='角色挥手'))
        self.store = StickerLibrary(self.root / 'stickers')
        self.runtime = SimpleNamespace(mode='wechat', account=lambda: 'bot', note=Mock(),
            transport=SimpleNamespace(call=self.download), ai=self.ai, stickers=self.store)

    def message(self, *kinds):
        return {'kind': 'group', 'target': 'room', 'text': '[图片]',
                'media': [{'type': kind, 'local_id': i, 'shard': 's'} for i, kind in enumerate(kinds)]}

    async def test_chat_extracts_animation_and_updates_collected_description(self):
        entry, _ = self.store.put(animated(), '挥手')
        msg = self.message('image')
        await self.manager.describe_images(msg, self.runtime)
        sources = self.ai.describe_images.await_args.args[0]
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(s.startswith('data:image/png;') for s in sources))
        colors = [Image.open(io.BytesIO(base64.b64decode(s.split(',')[1]))).convert('RGB').getpixel((0, 0)) for s in sources]
        self.assertEqual(colors, [(255, 0, 0), (0, 0, 255)])
        self.assertEqual(msg['vision_status'], 'ok')
        self.assertIn('动图抽取', msg['vision_description'])
        self.assertIn('角色挥手', self.store.get(entry['id'])['description'])

    async def test_animation_cache_uses_original_bytes_after_restart(self):
        await self.manager.describe_images(self.message('image'), self.runtime)
        restarted = PluginManager(self.root / 'plugins')
        msg = self.message('image')
        await restarted.describe_images(msg, self.runtime)
        self.ai.describe_images.assert_awaited_once()
        self.assertTrue(msg['vision_cache_hit'])

    async def test_three_images_are_not_displaced_by_animation_frames(self):
        msg = self.message('image', 'image', 'image')
        await self.manager.describe_images(msg, self.runtime)
        self.assertEqual(len(self.ai.describe_images.await_args.args[0]), 3)
        self.assertIn('第 1、2、3 张', msg['vision_description'])
        self.assertEqual(msg['vision_status'], 'ok')

    async def test_webp_is_normalized_before_sending_to_model(self):
        out = io.BytesIO()
        Image.new('RGB', (32, 32), 'blue').save(out, 'WEBP')
        self.path.write_bytes(out.getvalue())
        msg = self.message('image')
        await self.manager.describe_images(msg, self.runtime)
        self.assertEqual(msg['vision_status'], 'ok')
        self.assertTrue(self.ai.describe_images.await_args.args[0][0].startswith('data:image/png;'))

    async def test_emotion_does_not_call_file_download_or_model(self):
        msg = self.message('emotion')
        msg['vision_description'] = '过期描述'
        await self.manager.describe_images(msg, self.runtime)
        self.download.assert_not_awaited()
        self.ai.describe_images.assert_not_awaited()
        self.assertNotIn('vision_description', msg)
        self.assertIn('加密', msg['vision_status'])

    async def test_mixed_emotion_is_not_reported_as_fully_seen_or_cached(self):
        msg = self.message('image', 'emotion')
        await self.manager.describe_images(msg, self.runtime)
        self.assertIn('部分缺失', msg['vision_status'])
        self.assertEqual(len(self.manager._plugins['sticker_memory'].cache()), 0)

    async def test_failed_vision_distinguishes_permission_without_leaking_credentials(self):
        self.ai.last_error = 'HTTP 403: secret-key https://example.test/?token=secret'
        self.ai.describe_images.return_value = ''
        msg = self.message('image')
        await self.manager.describe_images(msg, self.runtime)
        self.assertIn('权限', msg['vision_status'])
        self.assertNotIn('secret', str(msg))
        self.assertFalse(self.store.entries())

    async def test_thumbnail_is_partial_and_not_cached_as_full_image(self):
        thumb = self.root / '7_thumb.gif'
        thumb.write_bytes(animated())
        self.download.return_value['path'] = str(thumb)
        msg = self.message('image')
        await self.manager.describe_images(msg, self.runtime)
        self.assertIn('缩略图', msg['vision_status'])
        self.assertEqual(len(self.manager._plugins['sticker_memory'].cache()), 0)

    async def test_unknown_observation_hook_is_a_noop_for_all_default_plugins(self):
        with patch('builtins.print') as output:
            self.manager.run_on_observed({'text': '普通消息', 'media': []}, self.runtime)
        output.assert_not_called()

    async def test_collection_retries_when_database_event_precedes_image_file(self):
        plugin = self.manager._plugins['sticker_steal']
        msg = self.message('image')
        plugin.collect_for_reply(msg, self.runtime)
        self.download.side_effect = [{'ok': False, 'path': ''}, {'ok': True, 'path': str(self.path)}]
        self.assertEqual(await plugin.drain(self.runtime), 0)
        self.assertEqual(len(plugin._queue), 1)
        plugin._queue[0]['retry_at'] = 0
        self.assertEqual(await plugin.drain(self.runtime), 1)
        self.assertFalse(plugin._queue)

    async def test_pending_collection_is_discarded_after_switching_account(self):
        plugin = self.manager._plugins['sticker_steal']
        plugin.collect_for_reply(self.message('image'), self.runtime)
        self.runtime.account = lambda: 'another-account'
        self.assertEqual(await plugin.drain(self.runtime), 0)
        self.download.assert_not_awaited()


class MediaTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bridge = WeChatBridge(self.root, backend='mock')
        self.bridge.connect()
        self.transport = WeChatTransport(self.bridge)
        self.path = self.root / 'image.gif'
        self.path.write_bytes(animated())

    async def asyncTearDown(self):
        await self.transport.close()
        self.tmp.cleanup()

    async def test_ocr_collection_and_vision_share_one_download(self):
        self.bridge.download_media = Mock(return_value=str(self.path))
        results = await asyncio.gather(*(self.transport.download_media('g', 7, 'image', 's') for _ in range(3)))
        self.assertEqual(results, [str(self.path)] * 3)
        self.assertEqual(await self.transport.download_media('g', 7, 'image', 's'), str(self.path))
        self.bridge.download_media.assert_called_once()

    async def test_failed_download_is_not_cached(self):
        self.bridge.download_media = Mock(side_effect=[None, str(self.path)])
        self.assertIsNone(await self.transport.download_media('g', 7, 'image', 's'))
        self.assertEqual(await self.transport.download_media('g', 7, 'image', 's'), str(self.path))
        self.assertEqual(self.bridge.download_media.call_count, 2)

    async def test_connection_chat_shard_and_account_are_part_of_cache_key(self):
        self.bridge.download_media = Mock(return_value=str(self.path))
        await self.transport.download_media('g', 7, 'image', 's')
        await self.transport.download_media('other', 7, 'image', 's')
        await self.transport.download_media('g', 7, 'image', 'other')
        self.bridge.self_id = 'different'
        await self.transport.download_media('g', 7, 'image', 's')
        self.bridge._db = object()
        await self.transport.download_media('g', 7, 'image', 's')
        self.assertEqual(self.bridge.download_media.call_count, 5)

    async def test_cancelling_one_consumer_does_not_cancel_shared_download(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(*args):
            entered.set()
            release.wait(3)
            return str(self.path)
        self.bridge.download_media = Mock(side_effect=fetch)
        one = asyncio.create_task(self.transport.download_media('g', 7, 'image', 's'))
        await asyncio.to_thread(entered.wait, 2)
        two = asyncio.create_task(self.transport.download_media('g', 7, 'image', 's'))
        await asyncio.sleep(0)
        one.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await one
        release.set()
        self.assertEqual(await two, str(self.path))
        self.bridge.download_media.assert_called_once()


class MediaBridgeTests(unittest.TestCase):
    def test_local_best_image_reuses_downloader_and_isolates_shards(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = WeChatBridge(Path(folder), backend='mock')
            bridge.backend = 'wechatauto'
            bridge._db = object()
            calls, instances = [], []
            class Downloader:
                def __init__(self, db, save_dir):
                    self.db = db
                    instances.append(self)
                def download_image(self, chat, local_id, **kwargs):
                    calls.append(kwargs)
                    target = Path(kwargs['save_dir']) / f'{local_id}.gif'
                    target.write_bytes(animated())
                    return str(target)
            with patch.dict(sys.modules, {'wechatauto.media': SimpleNamespace(MediaDownloader=Downloader)}):
                first = bridge.download_media('g', 7, 'image', 's1')
                second = bridge.download_media('g', 7, 'image', 's2')
                self.assertNotEqual(first, second)
                self.assertEqual(len(instances), 1)
                self.assertEqual([c['tier'] for c in calls], ['best', 'best'])
                self.assertIsNone(bridge.download_media('g', 7, 'emotion', 's2'))
                self.assertEqual(len(calls), 2)
                bridge._db = object()
                bridge.download_media('g', 8, 'image', 's1')
                self.assertEqual(len(instances), 2)

    def test_oversized_pixels_are_rejected_before_decode(self):
        image = Mock(width=5000, height=5000)
        with patch('src.sticker_library.Image.open', return_value=image):
            with self.assertRaises(ValueError):
                inspect_image(b'image')
        image.load.assert_not_called()

    def test_failure_categories_do_not_echo_provider_body(self):
        for error, hint in [('HTTP 400', '图片输入'), ('HTTP 401', '权限'),
                            ('HTTP 404', '模型'), ('HTTP 429', '额度'), ('请求超时', '超时')]:
            with self.subTest(error=error):
                result = vision_failure(SimpleNamespace(last_error=error + ' secret'))
                self.assertIn(hint, result)
                self.assertNotIn('secret', result)
