"""三个用户报的功能：拍一拍（戳一戳）识别、偷表情、关键词导入/导出。"""

import asyncio
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.gui.keyword_rules import parse_imported
from src.plugins.manager import PluginManager
from src.wechat_transports import message_from_raw

PAT_XML = ('<msg><appmsg appid="" sdkver="0"><title>"站高速看夕阳" 拍了拍 "怪怪." Ciallo～'
           '(∠・ω&lt;)⌒</title><des>abc</des></appmsg></msg>')


class _Bridge:
    self_id = 'wxid_bot'
    account_name = '鲸鱼娘'

    def display_name(self, *args):
        return '西莲寺紫'


def jpeg_bytes(size=(120, 120)):
    image = Image.new('RGB', size, (240, 200, 60))
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG')
    return buffer.getvalue()


class PatMessageTests(unittest.TestCase):
    def _raw(self, content, kind='文件/链接/卡片'):
        return {'username': 'wxid_group@chatroom', 'type': kind, 'sender_id': 'wxid_b',
                'sender_username': 'wxid_b', 'sender_name': '怪怪.',
                'create_time': time.time(), 'content': content, 'local_id': 9,
                'shard': 'message_0', 'attr': 'friend'}

    def test_pat_becomes_clean_text_instead_of_xml_card(self):
        message = message_from_raw(self._raw(PAT_XML), _Bridge(), [])
        self.assertEqual(message['text'], '[拍一拍] 站高速看夕阳 拍了拍 怪怪')
        self.assertEqual(message['media'], [])          # 不再当成链接卡片
        self.assertFalse(message['at'])

    def test_patting_the_bot_counts_as_addressing_it(self):
        message = message_from_raw(self._raw(PAT_XML.replace('"怪怪."', '"鲸鱼娘"')),
                                   _Bridge(), [])
        self.assertIn('鲸鱼娘', message['text'])
        self.assertTrue(message['at'])                  # 被拍的是机器人 → 允许回复

    def test_alias_pat_also_counts(self):
        message = message_from_raw(self._raw(PAT_XML.replace('"怪怪."', '"小鲸鱼"')),
                                   _Bridge(), ['小鲸鱼'])
        self.assertTrue(message['at'])

    def test_plain_card_is_untouched(self):
        card = '<msg><appmsg><title>某个分享</title></appmsg></msg>'
        message = message_from_raw(self._raw(card), _Bridge(), [])
        self.assertNotIn('拍了拍', message['text'])


class _Runtime:
    def __init__(self, store, call):
        from types import SimpleNamespace
        self.stickers = store
        self.transport = SimpleNamespace(call=call)
        self.notes = []

    def account(self):
        return 'acc'

    def note(self, text):
        self.notes.append(text)


class StealStickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.manager = PluginManager(Path(self.tmp.name) / 'state')
        self.plugin = self.manager._plugins['sticker_steal']
        self.stickers = SimpleNamespaceFactory()

    def _runtime(self):
        return _Runtime(self.stickers.store, AsyncMock(return_value={'ok': False, 'path': ''}))

    async def test_emotion_with_url_is_collected_through_the_cdn(self):
        runtime = self._runtime()
        message = {'kind': 'group', 'target': 'g@chatroom', 'sender_name': '小红',
                   'media': [{'type': 'emotion', 'local_id': 44, 'shard': 'm',
                              'url': 'http://vweixinf.tc.qq.com/x?m=ff'}]}
        with patch('src.sticker_library.fetch_image', AsyncMock(return_value=jpeg_bytes())):
            self.plugin.on_observed(message, runtime)
            added = await self.plugin.drain(runtime)
        self.assertEqual(added, 1)
        self.assertEqual(len(self.stickers.store.entries()), 1)

    async def test_emotion_without_url_is_skipped(self):
        runtime = self._runtime()
        message = {'kind': 'group', 'target': 'g@chatroom', 'sender_name': '小红',
                   'media': [{'type': 'emotion', 'local_id': 45, 'shard': 'm'}]}
        self.plugin.on_observed(message, runtime)
        self.assertEqual(self.plugin._queue, [])

    async def test_reply_path_image_still_goes_through_the_database(self):
        # 普通图片没有直链：仍走 download_media（1.2.20 的路径），而且只在"要回复这条"时收，
        # 群里没点名的普通图片不做后台下载（这是 1.2.2x 的既有决定，本版保持不变）。
        runtime = self._runtime()
        folder = Path(self.tmp.name)
        target = folder / 'pic.png'
        target.write_bytes(jpeg_bytes())
        runtime.transport.call = AsyncMock(return_value={'ok': True, 'path': str(target)})
        message = {'kind': 'group', 'target': 'g@chatroom', 'sender_name': '小红',
                   'media': [{'type': 'image', 'local_id': 7, 'shard': 'm'}]}
        self.plugin.on_observed(message, runtime)          # 只是观察到 → 不登记
        self.assertEqual(self.plugin._queue, [])
        self.plugin.collect_for_reply(message, runtime)    # 要回复这条 → 登记
        added = await self.plugin.drain(runtime)
        self.assertEqual(added, 1)
        runtime.transport.call.assert_awaited()


class SimpleNamespaceFactory:
    """给每个用例一个独立的图库目录。"""

    def __init__(self):
        from src.sticker_library import StickerLibrary
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = StickerLibrary(Path(self.tmp.name) / 'stickers')


class KeywordImportTests(unittest.TestCase):
    def test_text_and_json_formats_are_all_accepted(self):
        self.assertEqual(parse_imported('你好=你好呀\n晚安=早点睡'),
                         [('你好', '你好呀'), ('晚安', '早点睡')])
        self.assertEqual(parse_imported('你好|你好呀'), [('你好', '你好呀')])
        self.assertEqual(parse_imported('{"format":"keyword-rules-1","rules":'
                                        '[{"keyword":"你好","reply":"你好呀"}]}'),
                         [('你好', '你好呀')])
        self.assertEqual(parse_imported('[{"keyword":"甲","reply":"一"},["乙","二"]]'),
                         [('甲', '一'), ('乙', '二')])

    def test_bom_comments_and_blank_lines_are_tolerated(self):
        self.assertEqual(parse_imported('\ufeff# 注释\n\n你好=你好呀\n'), [('你好', '你好呀')])

    def test_bad_input_is_rejected_with_a_reason(self):
        for text in ('', '没有分隔符的一行', '{"rules": "不是数组"}', '[{"keyword":"a"}]'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_imported(text)

    def test_duplicate_keyword_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_imported('你好=甲\n你好=乙')


if __name__ == '__main__':
    unittest.main()
