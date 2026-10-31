"""回复整形：别太长、别 @ 人（用户实测反馈：一段一大片、还带 @昵称）。"""
import tempfile
import unittest
from pathlib import Path

from src.plugins.chat_controls import shorten_reply, strip_at_mentions
from src.plugins.manager import PluginManager


class StripAtTests(unittest.TestCase):
    def test_leading_mention_is_removed(self):
        self.assertEqual(strip_at_mentions('@西莲寺紫 哈哈这对话太有戏剧性了'), '哈哈这对话太有戏剧性了')

    def test_multiple_leading_mentions_are_removed(self):
        self.assertEqual(strip_at_mentions('@小田 @西莲寺紫 你好'), '你好')

    def test_fullwidth_at_and_colon_forms(self):
        self.assertEqual(strip_at_mentions('＠小田：在的'), '在的')
        self.assertEqual(strip_at_mentions('@小田: 在的'), '在的')

    def test_text_without_mention_is_untouched(self):
        self.assertEqual(strip_at_mentions('小夜在呢'), '小夜在呢')

    def test_mail_address_is_not_eaten(self):
        self.assertEqual(strip_at_mentions('发到 a@b.com 就行'), '发到 a@b.com 就行')


class ShortenTests(unittest.TestCase):
    def test_short_reply_is_unchanged(self):
        self.assertEqual(shorten_reply('好呀', 80), '好呀')

    def test_zero_limit_disables_the_cap(self):
        long = '小夜' * 100
        self.assertEqual(shorten_reply(long, 0), long)

    def test_cuts_at_sentence_end(self):
        text = '小夜最喜欢啦。感觉像隔着屏幕偷偷笑了一下呢。既然觉得厉害那就多看点。'
        out = shorten_reply(text, 20)
        self.assertLessEqual(len(out), 21)
        self.assertTrue(out.endswith(('。', '…')))

    def test_hard_cut_gets_ellipsis(self):
        out = shorten_reply('一二三四五六七八九十' * 5, 12)
        self.assertTrue(out.endswith('…'))
        self.assertLessEqual(len(out), 13)


class PluginDefaultsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.plugin = self.manager._plugins['chat_controls']

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_are_short_and_quiet(self):
        self.assertEqual(self.plugin.get('replyMaxChars'), 80)
        self.assertTrue(self.plugin.get('noAtInReply'))

    def test_prompt_asks_for_short_replies_without_mentions(self):
        runtime = type('R', (), {'chat_state': None, 'plugins': None, 'conversations': None})()
        hint = self.plugin.on_pre_ai({'kind': 'group', 'text': 'hi', 'id': 'm1'}, [], runtime)['extra_system']
        self.assertIn('80', hint)
        self.assertIn('不要', hint)

    async def test_on_post_reply_shapes_the_text(self):
        reply = '@西莲寺紫 ' + '小夜最喜欢啦。' * 20
        out = await self.plugin.on_post_reply({'kind': 'group', 'text': 'x', 'id': 'm1'}, reply, None)
        self.assertFalse(out.startswith('@'))
        pieces = [piece for piece in out.split('\x01PACE:800\x01') if piece]
        self.assertTrue(all(len(piece) <= 81 for piece in pieces), pieces)


if __name__ == '__main__':
    unittest.main()


class SendLayerTests(unittest.TestCase):
    """发送前的最后一道闸：任何来源的回复都不能带开头 @昵称。"""

    def test_strip_is_idempotent_and_safe(self):
        from src.plugins.chat_controls import strip_at_mentions
        once = strip_at_mentions('@戏络 哟，这都怪到本鲸头上了？')
        self.assertEqual(strip_at_mentions(once), once)
        self.assertFalse(once.startswith('@'))

    def test_send_path_calls_strip_before_pacing(self):
        import pathlib
        code = pathlib.Path(__file__).resolve().parent.parent.joinpath('src/wechat_app.py').read_text('utf-8')
        marker = 'await _send_with_pacing(self.transport, message, reply, meta)'
        index = code.index(marker)
        window = code[max(0, index - 700):index]
        self.assertIn('strip_at_mentions', window)
