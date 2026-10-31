"""交接指南 3.2：取图与视觉必须分开计时，不能笼统写"识图慢"。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.plugins.manager import PluginManager

PNG = ('data:image/png;base64,' + __import__('base64').b64encode(bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000d49444154789c63600000020001e221bc330000000049454e44ae426082')).decode())


class _Runtime:
    def __init__(self, root):
        self.root = root
        self.mode = 'mock'
        self.account = lambda: 'acc'
        self.notes = []
        self.ai = type('AI', (), {'api_url': 'https://example.invalid/v1/chat/completions',
                                  'model': 'chat-model', 'configured': True})()
        self.ai.describe_images = AsyncMock(return_value='一只红点')

    def note(self, text):
        self.notes.append(text)


class StageTimingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.plugin = self.manager._plugins['image_understanding']
        self.runtime = _Runtime(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_acquire_and_vision_are_timed_separately(self):
        message = {'kind': 'private', 'target': 'wxid_a', 'id': 'm1', 'text': '[图片]',
                   'media': [{'type': 'image', 'local_id': 1, 'shard': 's'}]}
        # 直接给"已经取到并落盘"的来源，跳过储藏区细节（那部分有自己的测试）
        with patch.object(self.plugin, '_local_source', AsyncMock(return_value=PNG)):
            await self.plugin.describe(message, self.runtime)
        joined = '\n'.join(self.runtime.notes)
        self.assertIn('取图/抽帧阶段耗时', joined)
        self.assertIn('图片识别成功', joined)
        self.assertIn('耗时', joined)

    async def test_timing_notes_are_not_emitted_for_skipped_group_images(self):
        message = {'kind': 'group', 'target': 'g@chatroom', 'id': 'm2', 'text': '[图片]',
                   '_picture_text_only': True,
                   'media': [{'type': 'image', 'local_id': 2, 'shard': 's'}]}
        await self.plugin.describe(message, self.runtime)
        self.assertFalse(any('耗时' in note for note in self.runtime.notes))
        self.runtime.ai.describe_images.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
