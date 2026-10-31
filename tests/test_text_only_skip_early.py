"""交接指南 3.3：未点名的群图必须在**下载/保存之前**就跳过。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.plugins.manager import PluginManager


class _Runtime:
    def __init__(self, root):
        self.root = root
        self.mode = 'mock'
        self.account = lambda: 'acc'
        self.notes = []
        self.transport = type('T', (), {'call': AsyncMock()})()

    def note(self, text):
        self.notes.append(text)


class EarlySkipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.manager = PluginManager(root / 'plugins')
        self.plugin = self.manager._plugins['image_understanding']
        self.runtime = _Runtime(root)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_no_download_and_no_vision_for_unmentioned_group_image(self):
        message = {'kind': 'group', 'target': 'g@chatroom', 'id': 'm1', 'text': '[图片]',
                   '_picture_text_only': True,
                   'media': [{'type': 'image', 'local_id': 1, 'shard': 's'}]}
        with patch.object(self.plugin, '_acquire_source', AsyncMock()) as acquire:
            await self.plugin.describe(message, self.runtime)
        acquire.assert_not_awaited()                      # 一次都没取图
        self.runtime.transport.call.assert_not_awaited()   # 也没走 download_media
        self.assertIn('略过', message['vision_status'])
        self.assertFalse((Path(self.runtime.root) / 'data' / 'received_images').exists())

    async def test_mentioned_image_still_processed(self):
        message = {'kind': 'group', 'target': 'g@chatroom', 'id': 'm2', 'text': '[图片]',
                   'media': [{'type': 'image', 'local_id': 2, 'shard': 's'}]}
        with patch.object(self.plugin, '_acquire_source', AsyncMock(return_value='')) as acquire:
            await self.plugin.describe(message, self.runtime)
        acquire.assert_awaited()


if __name__ == '__main__':
    unittest.main()
