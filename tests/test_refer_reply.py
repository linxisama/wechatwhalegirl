"""引用回复（appmsg type=57）：正文与引用内容不能丢成 [文件/链接]。"""
import unittest

from src.wechat_transports import _refer_reply, message_from_raw


class _Bridge:
    account_name = '鲸鱼娘'
    self_id = 'wxid_self'
    backend = 'mock'

    def display_name(self, *args, **kwargs):
        return '群'


def quoted(text, quoted_content, kind=3, who='大肥鲸'):
    return (f'<msg><appmsg><title>{text}</title><type>57</type>'
            f'<refermsg><content>{quoted_content}</content><type>{kind}</type>'
            f'<displayname>{who}</displayname></refermsg></appmsg></msg>')


class ReferParseTests(unittest.TestCase):
    def test_quoted_image_keeps_title_and_marks_kind(self):
        data = _refer_reply(quoted('我再考考你 这个是谁', '&lt;msgs&gt;&lt;img md5="x"/&gt;&lt;/msgs&gt;'))
        self.assertEqual(data['title'], '我再考考你 这个是谁')
        self.assertEqual(data['quoted_kind'], 'image')
        self.assertEqual(data['quoted'], '')
        self.assertEqual(data['quoted_from'], '大肥鲸')

    def test_quoted_text_is_extracted(self):
        data = _refer_reply(quoted('对啊', '你今天吃了吗', kind=1, who='小田'))
        self.assertEqual(data['quoted'], '你今天吃了吗')
        self.assertEqual(data['quoted_kind'], 'text')

    def test_plain_message_has_no_refer(self):
        self.assertEqual(_refer_reply('<msg><appmsg><title>x</title></appmsg></msg>'), {})


class MessageTests(unittest.TestCase):
    def _parse(self, content, raw_type='文件/链接/卡片'):
        raw = {'type': raw_type, 'content': content, 'local_id': 1, 'shard': 's',
               'username': 'g@chatroom', 'sender': 'wxid_a', 'sender_name': '小田'}
        return message_from_raw(raw, _Bridge())

    def test_text_is_title_not_placeholder(self):
        message = self._parse(quoted('我再考考你 这个是谁', '&lt;img/&gt;'))
        self.assertEqual(message['text'], '我再考考你 这个是谁')
        self.assertEqual(message['media'], [])
        self.assertEqual(message['refer']['quoted_kind'], 'image')

    def test_text_quoted_reply_keeps_both(self):
        message = self._parse(quoted('对啊', '你今天吃了吗', kind=1))
        self.assertEqual(message['text'], '对啊')
        self.assertEqual(message['refer']['quoted'], '你今天吃了吗')


if __name__ == '__main__':
    unittest.main()
