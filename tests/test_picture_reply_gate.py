"""带图/表情的消息不该被"未点名概率"挡掉。"""
import tempfile
import unittest
from pathlib import Path

from src.plugins.chat_controls import has_picture
from src.plugins.manager import PluginManager
from src.image_input import unread_image_reply


class HasPictureTests(unittest.TestCase):
    def test_plain_text_is_not_a_picture(self):
        self.assertFalse(has_picture({'text': '你好'}))
        self.assertFalse(has_picture({}))

    def test_placeholder_texts_count(self):
        for text in ('[图片]', '[表情]', '看图 [动画表情]', '[视频]'):
            self.assertTrue(has_picture({'text': text}), text)

    def test_media_counts(self):
        self.assertTrue(has_picture({'text': '', 'media': [{'type': 'image'}]}))
        self.assertFalse(has_picture({'text': '', 'media': [{'type': 'file'}]}))


class FailureTextTests(unittest.TestCase):
    def test_unsupported_model_is_explained(self):
        text = unread_image_reply({'text': '[图片]', 'vision_status': '失败：接口拒绝图片输入'})
        self.assertIn('不支持看图片', text)

    def test_timeout_is_softly_worded(self):
        text = unread_image_reply({'text': '[图片]', 'vision_status': '失败：识图请求超时'})
        self.assertIn('超时', text)


class ProbabilityGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.plugin = self.manager._plugins['chat_controls']

    def tearDown(self):
        self.tmp.cleanup()

    def test_picture_bypass_is_a_reply_only_switch(self):
        """带图放行只作用于"回复"，不改变"是否下载/识图"的判定。"""
        self.assertTrue(self.plugin.picture_bypass({'text': '[图片]'}))
        self.assertFalse(self.plugin.picture_bypass({'text': '普通聊天'}))
        self.manager.update_settings('chat_controls', {'pictureAlwaysReply': False})
        self.assertFalse(self.plugin.picture_bypass({'text': '[图片]'}))

    def test_probability_gate_still_strict_for_downloads(self):
        self.manager.update_settings('chat_controls', {'replyProbability': 0})
        runtime = type('R', (), {'aliases': lambda self=None: ['鲸鱼娘']})()
        ok, reason = self.plugin.reply_decision(
            {'kind': 'group', 'target': 'g', 'text': '[图片]'}, runtime)
        self.assertFalse(ok)
        self.assertIn('概率', reason)


if __name__ == '__main__':
    unittest.main()
