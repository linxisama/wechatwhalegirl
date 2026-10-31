"""真实图片字节、隔离配置和模拟 UIA 的回归，不操作真实微信。"""
import asyncio
import base64
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image

from src.ai_engine import AIEngine
from src.image_input import prepare_vision_source, unread_image_reply, vision_failure
from src.plugins.manager import PluginManager


def png(mode='RGB', color='red', size=(40, 60)):
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, 'PNG')
    return out.getvalue()


class ImagePreparationTests(unittest.TestCase):
    def test_transparent_background_is_white_and_foreground_survives(self):
        image = Image.new('RGBA', (40, 40), (0, 0, 0, 0))
        image.paste((200, 20, 20, 255), (10, 10, 30, 30))
        stream = io.BytesIO()
        image.save(stream, 'PNG')
        source = 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode()
        result = prepare_vision_source(source)
        with Image.open(io.BytesIO(base64.b64decode(result.split(',')[1]))) as output:
            self.assertEqual(output.mode, 'RGB')
            self.assertGreater(min(output.getpixel((0, 0))), 240)
            red, green, _ = output.getpixel((20, 20))
            self.assertGreater(red, green + 100)

    def test_large_image_keeps_aspect_and_bounded_payload(self):
        source = 'data:image/png;base64,' + base64.b64encode(png(size=(2400, 1200))).decode()
        result = prepare_vision_source(source)
        with Image.open(io.BytesIO(base64.b64decode(result.split(',')[1]))) as output:
            self.assertEqual(output.size, (1536, 768))
        self.assertLess(len(result), 100000)

    def test_exif_orientation_is_applied(self):
        image = Image.new('RGB', (20, 40), 'blue')
        metadata = image.getexif()
        metadata[274] = 6
        stream = io.BytesIO()
        image.save(stream, 'JPEG', exif=metadata)
        result = prepare_vision_source('data:image/jpeg;base64,' + base64.b64encode(stream.getvalue()).decode())
        with Image.open(io.BytesIO(base64.b64decode(result.split(',')[1]))) as output:
            self.assertEqual(output.size, (40, 20))

    def test_timeout_wins_over_error_left_by_previous_request(self):
        result = vision_failure(SimpleNamespace(last_error='HTTP 403 secret'), TimeoutError())
        self.assertIn('超时', result)
        self.assertNotIn('权限', result)
        self.assertNotIn('secret', result)

    def test_unread_photo_uses_actual_failure_instead_of_invented_visual_content(self):
        reply = unread_image_reply({'text': '[图片]', 'vision_status': '失败：识图请求超时'})
        self.assertIn('超时', reply)
        self.assertNotIn('黑', reply)
        self.assertNotIn('信号', reply)

    def test_text_question_ocr_and_partial_success_keep_original_reply_flow(self):
        for message in (
            {'text': '这张图什么意思？', 'vision_status': '失败：请求超时'},
            {'text': '[图片]\n[图片文字] 菜单', 'vision_status': '失败：请求超时'},
            {'text': '[图片]', 'vision_status': '部分缺失', 'vision_description': '红色'},
            {'text': '[图片]', 'vision_status': 'ok', 'vision_description': '红色'},
        ):
            self.assertEqual(unread_image_reply(message), '')


class VisionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'picture.png'
        self.path.write_bytes(png())
        self.manager = PluginManager(self.root / 'plugins')
        self.download = AsyncMock(return_value={'ok': True, 'path': str(self.path)})
        self.ai = SimpleNamespace(api_url='https://provider.test/v1', model='vision',
                                  last_error='', describe_images=AsyncMock(return_value='红色画面'))
        self.runtime = SimpleNamespace(mode='wechat', account=lambda: 'bot', note=Mock(),
            ai=self.ai, transport=SimpleNamespace(call=self.download),
            speed_plan=lambda: {'vision_timeout': 6})
        self.message = {'kind': 'private', 'target': 'friend', 'text': '[图片]',
                        'media': [{'type': 'image', 'local_id': 1, 'shard': 's'}]}

    async def test_turbo_cannot_cancel_vision_before_configured_timeout(self):
        real_wait = asyncio.wait_for
        seen = []
        async def capture(awaitable, timeout):
            seen.append(timeout)
            return await real_wait(awaitable, timeout)
        with patch('src.plugins.image_understanding.asyncio.wait_for', side_effect=capture):
            await self.manager.describe_images(self.message, self.runtime)
        # 断言的是"用插件配置的值"，不是写死的 15：默认值从 15 调到 25 时这条不该红。
        configured = float(self.manager._plugins['image_understanding'].get('visionTimeout'))
        self.assertEqual(seen, [configured])
        self.assertEqual(self.message['vision_status'], 'ok')

    async def test_file_becomes_available_after_incoming_database_event(self):
        self.download.side_effect = [{'ok': False}, {'ok': True, 'path': str(self.path)}]
        await self.manager.describe_images(self.message, self.runtime)
        self.assertEqual(self.download.await_count, 2)
        self.ai.describe_images.assert_awaited_once()
        self.assertEqual(self.message['vision_status'], 'ok')

    async def test_timeout_is_specific_and_never_saved_as_success(self):
        self.ai.last_error = 'HTTP 403 from previous request'
        self.ai.describe_images.side_effect = asyncio.TimeoutError()
        await self.manager.describe_images(self.message, self.runtime)
        self.assertIn('超时', self.message['vision_status'])
        self.assertNotIn('vision_description', self.message)
        self.assertFalse(self.manager._plugins['sticker_memory'].cache())

    async def test_provider_receives_real_image_blocks_and_pixels(self):
        engine = AIEngine('https://provider.test/v1', 'fake', model='chat')
        post = AsyncMock(return_value=httpx.Response(200, json={
            'choices': [{'message': {'content': '红色'}, 'finish_reason': 'stop'}]},
            request=httpx.Request('POST', engine.api_url)))
        engine._get_client = AsyncMock(return_value=SimpleNamespace(post=post))
        result = await engine.describe_images(['data:image/png;base64,' +
                       base64.b64encode(self.path.read_bytes()).decode()], model='vision')
        self.assertEqual(result, '红色')
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['model'], 'vision')
        self.assertEqual(engine.model, 'chat')
        source = payload['messages'][0]['content'][1]['image_url']['url']
        with Image.open(io.BytesIO(base64.b64decode(source.split(',')[1]))) as image:
            self.assertGreater(image.getpixel((0, 0))[0], 240)
        post.assert_awaited_once()


class StrictBackgroundTests(unittest.TestCase):
    def test_explicit_background_choice_roundtrips_and_survives_restart(self):
        from fastapi.testclient import TestClient
        from src.wechat_app import WeChatRuntime, create_app
        from tests.test_wechat_recovery import _write_config
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            config = _write_config(root)
            bot = WeChatRuntime(root=root, config_path=config, db_path=root / 'messages.db')
            try:
                with TestClient(create_app(bot)) as client:
                    draft = client.get('/api/settings').json()
                    draft.update(backend='mock', send_mode='background', background_send=True,
                                 send_compat=True, auto_open_chat=True)
                    response = client.post('/api/settings', json=draft)
                    self.assertTrue(response.json()['success'], response.text)
                    saved = client.get('/api/settings').json()
                    self.assertTrue(saved['background_send'])
                    self.assertFalse(saved['send_compat'])
                    self.assertTrue(bot.bridge.background_send)
            finally:
                bot.db.close()
            restarted = WeChatRuntime(root=root, config_path=config, db_path=root / 'messages.db')
            try:
                self.assertTrue(restarted.bridge.background_send)
                self.assertFalse(restarted.bridge.send_compat)
            finally:
                restarted.db.close()

    def test_strict_worker_never_falls_back_when_controls_are_unavailable(self):
        from src.wechat_send_worker import execute
        with patch('src.wechat_background.send_background', return_value=(False, '等待手动打开')) as quiet, \
             patch('src.wechat_send_worker.send_text', side_effect=AssertionError('foreground')), \
             patch('src.wechat_send_worker.enable_send_compat', side_effect=AssertionError('wake')), \
             patch('src.wechat_send_worker._restore_foreground', side_effect=AssertionError('restore')):
            result = execute({'action': 'text', 'who': 'friend', 'text': 'hello',
                              'background_send': True, 'send_compat': True, 'auto_open_chat': True})
        self.assertFalse(result[0])
        quiet.assert_called_once()


class SettingsGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_selecting_strict_overrides_existing_compat_and_is_submitted(self):
        import wechat_gui as gui
        tab = gui.SettingsTab()
        try:
            tab.send_compat.setChecked(True)
            tab.background_send.setChecked(True)
            self.assertTrue(tab.background_send.isChecked())
            self.assertFalse(tab.send_compat.isChecked())
            self.assertFalse(tab.auto_open_chat.isEnabled())
            self.assertIn('严格后台', tab.send_compat_hint.text())
            self.assertEqual(tab.collect()['send_mode'], 'background')
        finally:
            tab.close()

    def test_loading_conflicting_old_values_preserves_background_intent(self):
        import wechat_gui as gui
        tab = gui.SettingsTab()
        try:
            tab.populate({'send_compat': True, 'background_send': True})
            self.assertTrue(tab.background_send.isChecked())
            self.assertFalse(tab.send_compat.isChecked())
            self.assertEqual(tab.collect()['send_mode'], 'background')
        finally:
            tab.close()
