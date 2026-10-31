"""等完整处理完成再检查未点名图片，不依赖50ms调度窗口。"""
import asyncio
from unittest.mock import AsyncMock,Mock
from tests.test_wechat_app_flow import RuntimeTestCase,raw_message


class PictureGuard136Tests(RuntimeTestCase):
    async def test_text_only_picture_never_downloads_even_after_full_processing(self):
        bot=await self.start_runtime()
        ai=self.install_ai(boom=True)
        bot.transport.call=AsyncMock()
        bot.plugins.update_settings('sticker_steal',{'watchImages':True})
        bot.ingest(dict(raw_message('',at=False),type='图片'))
        await self.wait_for_inbox_done()
        bot.transport.call.assert_not_awaited();self.assertEqual(ai.calls,[])
        self.assertEqual(bot.plugins._plugins['sticker_steal']._queue,[])
        self.assertIn('收到图片消息',bot.bridge.sent_log[0]['text'])

    async def test_ocr_skips_text_only_marker_before_reader_or_download(self):
        bot=await self.start_runtime();plugin=bot.plugins._plugins['image_ocr']
        plugin._reader_for=Mock()
        bot.transport.call=AsyncMock()
        await plugin.on_prepare(dict(kind='group',_picture_text_only=True,media=[dict(type='image',local_id=1)]),bot)
        plugin._reader_for.assert_not_called()
        bot.transport.call.assert_not_awaited()
