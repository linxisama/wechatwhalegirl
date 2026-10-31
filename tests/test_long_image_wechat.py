import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.long_image import plan_tiles, split_long_image
from src.plugins.image_understanding import ImageUnderstanding


def picture(width=400, height=6000):
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtGui import QImage
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0xff336699)
    output = QBuffer()
    output.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(output, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(bytes(output.data())).decode()


class LongImageTests(unittest.TestCase):
    def test_overlap_and_unread_tail(self):
        plan, partial = plan_tiles(400, 6000)
        self.assertTrue(partial)
        self.assertEqual(len(plan), 3)
        self.assertLess(plan[1][0], sum(plan[0]))
        self.assertFalse(plan_tiles(400, 3000)[1])

    def test_decode_real_tiles(self):
        from PyQt6.QtGui import QImage
        parts, partial = split_long_image(picture())
        self.assertTrue(partial)
        self.assertEqual(len(parts), 3)
        for part in parts:
            image = QImage.fromData(base64.b64decode(part.split(',', 1)[1]))
            self.assertEqual((image.width(), image.height()), (400, 1800))
        self.assertIsNone(split_long_image(picture(400, 400)))

    def test_reject_invalid_dimensions(self):
        for dimensions in [(0, 5), (-1, 2), (10000, 10000)]:
            with self.assertRaises(ValueError):
                plan_tiles(*dimensions)


class LongImagePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_never_uses_or_populates_whole_image_cache(self):
        memory = Mock(enabled=True)
        manager = SimpleNamespace(_plugins={'sticker_memory': memory},
            get_settings=lambda _: dict(ImageUnderstanding.META.default_settings, longImage=True))
        plugin = ImageUnderstanding(manager)
        plugin._local_source = AsyncMock(return_value=picture())
        runtime = SimpleNamespace(mode='wechat', account=lambda: 'bot', note=Mock(),
            ai=SimpleNamespace(api_url='https://example.com', model='vision',
                               describe_images=AsyncMock(return_value='截图文字')))
        message = {'target': 'room', 'kind': 'group', 'media': [{'type': 'image'}]}
        await plugin.describe(message, runtime)
        self.assertIn('部分缺失', message['vision_status'])
        self.assertIn('同一张长截图', message['vision_description'])
        self.assertEqual(len(runtime.ai.describe_images.call_args.args[0]), 3)
        runtime.ai.describe_images.assert_awaited_once()
        memory.cached.assert_not_called()
        memory.record_description.assert_not_called()

    async def test_disabled_does_not_download(self):
        manager = SimpleNamespace(_plugins={}, get_settings=lambda _: {'enabled': False, 'longImage': True})
        plugin = ImageUnderstanding(manager)
        plugin._local_source = AsyncMock()
        message = {'media': [{'type': 'image'}], 'vision_description': '旧内容'}
        await plugin.describe(message, SimpleNamespace())
        plugin._local_source.assert_not_awaited()
        self.assertNotIn('vision_description', message)

    async def test_corrupt_image_has_no_model_call(self):
        manager = SimpleNamespace(_plugins={}, get_settings=lambda _: {'enabled': True, 'longImage': True})
        plugin = ImageUnderstanding(manager)
        plugin._local_source = AsyncMock(return_value='data:image/png;base64,eA==')
        runtime = SimpleNamespace(mode='wechat', account=lambda: '', note=Mock(), ai=AsyncMock())
        message = {'media': [{'type': 'image'}]}
        await plugin.describe(message, runtime)
        self.assertIn('失败', message['vision_status'])
        runtime.ai.describe_images.assert_not_called()
