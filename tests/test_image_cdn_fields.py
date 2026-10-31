"""图片消息要挑最大的 CDN 地址，不能退成本地缩略图。

真实事故：用户存下来的图片只有 180×149 / 6KB —— 因为代码只认 `cdnurl`/`thumburl`，
而聊天图片的 XML 用的是 `cdnbigimgurl`/`cdnmidimgurl`/`cdnthumburl`，于是取到了缩略图。
"""
import unittest

from src.wechat_transports import message_from_raw


class _Bridge:
    account_name = '鲸鱼娘'
    self_id = 'wxid_self'
    backend = 'mock'

    def display_name(self, *args, **kwargs):
        return '群'


def image_xml(**fields):
    attrs = ' '.join(f'{k}="{v}"' for k, v in fields.items())
    return ('<msg><img aeskey="x" cdnthumburl="https://cdn.example/thumb.jpg" '
            'cdnmidimgurl="https://cdn.example/mid.jpg" cdnbigimgurl="https://cdn.example/big.jpg" '
            'length="123456" hdlength="987654" md5="abc" /></msg>')


class CdnFieldTests(unittest.TestCase):
    def _parse(self, content):
        raw = {'type': '图片', 'content': content, 'local_id': 1, 'shard': 's',
               'username': 'g@chatroom', 'sender': 'wxid_a', 'sender_name': '小田'}
        return message_from_raw(raw, _Bridge())

    def test_big_image_url_wins(self):
        message = self._parse(image_xml())
        url = (message.get('media') or [{}])[0].get('url')
        self.assertEqual(url, 'https://cdn.example/big.jpg')

    def test_mid_image_used_when_no_big(self):
        xml = image_xml().replace('cdnbigimgurl="https://cdn.example/big.jpg" ', '')
        url = (self._parse(xml).get('media') or [{}])[0].get('url')
        self.assertEqual(url, 'https://cdn.example/mid.jpg')

    def test_thumb_is_last_resort(self):
        xml = ('<msg><img cdnthumburl="https://cdn.example/thumb.jpg" length="6000" md5="abc" /></msg>')
        url = (self._parse(xml).get('media') or [{}])[0].get('url')
        self.assertEqual(url, 'https://cdn.example/thumb.jpg')

    def test_original_size_prefers_hdlength(self):
        item = (self._parse(image_xml()).get('media') or [{}])[0]
        self.assertEqual(item.get('size'), 987654)


if __name__ == '__main__':
    unittest.main()
