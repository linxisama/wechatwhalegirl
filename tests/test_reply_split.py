"""长回复拆成几条短消息发（真人连发的样子）。"""
import tempfile
import unittest
from pathlib import Path

from src.plugins.chat_controls import split_reply
from src.plugins.manager import PluginManager

MARK = '\x01PACE:'


class SplitReplyTests(unittest.TestCase):
    def test_short_single_sentence_stays_one(self):
        self.assertEqual(split_reply('就一句', 80, 2), ['就一句'])

    def test_two_sentences_become_two_pieces(self):
        pieces = split_reply('第一句。第二句！', 80, 2)
        self.assertEqual(pieces, ['第一句。', '第二句！'])

    def test_pieces_respect_the_char_limit(self):
        pieces = split_reply('小夜最喜欢啦。感觉像隔着屏幕偷偷笑了一下呢。既然觉得厉害那就多看点。', 30, 3)
        self.assertTrue(all(len(piece) <= 30 for piece in pieces), pieces)

    def test_cap_merges_the_rest(self):
        pieces = split_reply('一。二。三。四。五。', 80, 2)
        self.assertEqual(len(pieces), 2)
        self.assertTrue(pieces[-1].endswith('五。'))

    def test_one_segment_means_no_split(self):
        self.assertEqual(split_reply('一句。两句。', 80, 1), ['一句。两句。'])

    def test_overlong_single_sentence_is_shortened(self):
        pieces = split_reply('很长' * 60 + '。', 20, 2)
        self.assertTrue(pieces)
        self.assertLessEqual(len(pieces[0]), 21)

    def test_comma_only_reply_is_split(self):
        """模型整段只用逗号连着说时也要拆开（用户实测：一直是一个大气泡）。"""
        text = '还是语音？老鲸耳朵不是给你当免费转写工用的，字打几个会咋，非逼我骂你？'
        pieces = split_reply(text, 80, 2)
        self.assertGreaterEqual(len(pieces), 2)
        self.assertEqual(pieces[0], '还是语音？')

    def test_short_comma_sentence_is_not_split(self):
        self.assertEqual(split_reply('短句，就一句', 80, 2), ['短句，就一句'])


class PluginWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.plugin = self.manager._plugins['chat_controls']

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        self.assertEqual(self.plugin.get('replySegments'), 1)
        self.assertEqual(self.plugin.get('replyMaxChars'), 80)

    async def test_post_reply_inserts_pace_markers(self):
        # 1.3.57: default is a single message, so ask for two here
        # to keep covering the PACE marker wiring.
        self.manager.update_settings('chat_controls', {'replySegments': 2})
        out = await self.plugin.on_post_reply({'kind': 'group', 'text': 'x', 'id': 'm1'},
                                        '小夜最喜欢啦。感觉像隔着屏幕偷偷笑了一下呢。', None)
        self.assertIn(MARK, out)
        self.assertFalse(out.startswith('@'))

    async def test_segments_can_be_disabled(self):
        self.manager.update_settings('chat_controls', {'replySegments': 1})
        out = await self.plugin.on_post_reply({'kind': 'group', 'text': 'x', 'id': 'm1'},
                                        '小夜最喜欢啦。感觉像隔着屏幕偷偷笑了一下呢。', None)
        self.assertNotIn(MARK, out)


if __name__ == '__main__':
    unittest.main()
