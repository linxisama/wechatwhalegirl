"""「动画表情」识别：微信 4.x 的本地表情文件是加密的，但消息 XML 里有 CDN 直链。

用户实测：连续收到几张表情，机器人一直回"读不懂/打不开/密信"。根因是识图链只处理
`type='image'`，`emotion` 直接被跳过（本地文件解密不出来，`download_media` 只会白等）。

现在两条改动：
1. `message_from_raw` 从表情 XML 里解析 `cdnurl` / `md5` / `len`（实测 cdnurl 返回 200 + JPEG）；
2. 识图插件把"带直链的表情"当图片处理，先用直链取图，取不到才如实说没法读。
"""

import asyncio
import base64
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.plugins.manager import PluginManager
from src.wechat_transports import message_from_raw

EMOJI_XML = (
    '<msg><emoji fromusername="wxid_b" tousername="wxid_bot" type="2" idbuffer="media:0_0" '
    'md5="e4a520c42c201bc2019f2a81fb5ceb5d" len="41510" cdnurl="http://vweixinf.tc.qq.com/110/20401/'
    'stodownload?m=e4a520c42c201bc2019f2a81fb5ceb5d&amp;filekey=abc&amp;hy=SH" thumburl="" '
    'encrypturl="http://vweixinf.tc.qq.com/110/20402/stodownload?m=3589" designerid=""/></msg>'
)


def jpeg_bytes(size=(160, 120)):
    image = Image.new('RGB', size, (240, 200, 60))
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG')
    return buffer.getvalue()


class _Bridge:
    self_id = 'wxid_bot'
    account_name = '鲸鱼娘'

    def display_name(self, *args):
        return '小红'


class EmotionPayloadTests(unittest.TestCase):
    def _message(self, raw):
        return message_from_raw(raw, _Bridge(), [])

    def _raw(self, content, kind='动画表情'):
        return {'username': 'wxid_b', 'type': kind, 'sender_id': 'wxid_b',
                'sender_username': 'wxid_b', 'create_time': time.time(),
                'content': content, 'local_id': 44, 'shard': 'message_0',
                'attr': 'friend', 'sender_name': '小红'}

    def test_emoji_xml_gives_a_downloadable_url(self):
        message = self._message(self._raw(EMOJI_XML))
        item = message['media'][0]
        self.assertEqual(item['type'], 'emotion')
        # CDN 直链必须还原 XML 转义（&amp; → &），否则带 filekey 的请求会失败
        self.assertTrue(item['url'].startswith('http://vweixinf.tc.qq.com/'))
        self.assertIn('&filekey=abc', item['url'])
        self.assertNotIn('&amp;', item['url'])
        self.assertEqual(item['md5'], 'e4a520c42c201bc2019f2a81fb5ceb5d')
        self.assertEqual(item['size'], 41510)
        self.assertEqual(message['text'], '[表情]')

    def test_image_xml_also_keeps_the_url_as_a_fallback(self):
        xml = ('<msg><img aeskey="abc" cdnurl="http://vweixinf.tc.qq.com/110/20401/stodownload?m=ff" '
               'md5="ff" len="2048"/></msg>')
        message = self._message(self._raw(xml, kind='图片'))
        item = message['media'][0]
        self.assertEqual(item['type'], 'image')
        self.assertIn('vweixinf.tc.qq.com', item.get('url', ''))

    def test_plain_text_sticker_notice_stays_text(self):
        message = self._message(self._raw('你好', kind='文本'))
        self.assertEqual(message['media'], [])
        self.assertEqual(message['text'], '你好')


class EmotionVisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.folder = Path(self.tmp.name)
        self.manager = PluginManager(self.folder / 'state')
        self.ai = SimpleNamespace(api_url='https://api.deepseek.com/v1/chat/completions',
                                  model='deepseek-chat',
                                  describe_images=AsyncMock(return_value='一只举着牌子打劫 token 的鲸鱼娘'))
        self.download = AsyncMock(return_value={'ok': False, 'path': ''})
        self.runtime = SimpleNamespace(
            ai=self.ai, mode='wechat', account=lambda: '鲸鱼娘', note=lambda text: None,
            transport=SimpleNamespace(call=self.download))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def emotion_message(self, url='http://vweixinf.tc.qq.com/110/20401/stodownload?m=ff'):
        item = {'type': 'emotion', 'local_id': 44, 'shard': 'message_0', 'name': '', 'text': ''}
        if url:
            item['url'] = url
        return {'kind': 'private', 'target': 'wxid_b', 'text': '[表情]', 'media': [item]}

    async def test_emotion_with_url_is_read_by_the_vision_model(self):
        message = self.emotion_message()
        with patch('src.sticker_library.fetch_image',
                   AsyncMock(return_value=jpeg_bytes())) as fetch:
            await self.manager.describe_images(message, self.runtime)
        fetch.assert_awaited_once()
        self.assertEqual(message['vision_status'], 'ok')
        self.assertIn('打劫 token', message['vision_description'])
        self.ai.describe_images.assert_awaited_once()
        # 关键：不再去调 download_media（那条路对加密表情只会白等）
        self.download.assert_not_awaited()

    async def test_emotion_without_url_says_so_and_never_calls_the_model(self):
        message = self.emotion_message(url='')
        await self.manager.describe_images(message, self.runtime)
        self.ai.describe_images.assert_not_awaited()
        self.assertIn('动画表情', message['vision_status'])
        self.assertIn('地址', message['vision_status'])
        self.assertNotIn('vision_description', message)

    async def test_failed_download_falls_back_to_the_cdn_url(self):
        message = {'kind': 'private', 'target': 'wxid_b', 'text': '[图片]',
                   'media': [{'type': 'image', 'local_id': 7, 'shard': 'message_0',
                              'url': 'http://vweixinf.tc.qq.com/110/20401/stodownload?m=ee'}]}
        self.download.return_value = {'ok': False, 'path': ''}
        with patch('src.sticker_library.fetch_image', AsyncMock(return_value=jpeg_bytes())):
            await self.manager.describe_images(message, self.runtime)
        self.assertEqual(message['vision_status'], 'ok')
        self.ai.describe_images.assert_awaited_once()

    async def test_cdn_failure_is_reported_not_faked(self):
        message = self.emotion_message()
        with patch('src.sticker_library.fetch_image',
                   AsyncMock(side_effect=ValueError('图片链接不能指向本机或内网'))):
            await self.manager.describe_images(message, self.runtime)
        self.ai.describe_images.assert_not_awaited()
        self.assertIn('失败', message['vision_status'])
