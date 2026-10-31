"""概率性回复：@/关键词优先，未命中时才按概率插话（默认 0 = 不插话）。

默认 0 是刻意的：未点名就插话属封号红线（AGENTS.md 第二节"未被点名时插话概率默认 0"），
用户要开才开，开了也仍然受每群每分钟上限与冷却约束。
"""

import unittest

from tests.test_wechat_app_flow import GROUP, RuntimeTestCase


class ReplyProbabilityTests(RuntimeTestCase):
    def _controls(self, runtime, chance):
        runtime.plugins.update_settings('chat_controls', {'replyProbability': chance})
        return runtime.plugins._plugins['chat_controls']

    def _message(self, at=False):
        return {'kind': 'group', 'target': GROUP, 'at': at, 'text': '@鲸鱼娘 在吗' if at else '随便说一句'}

    async def test_zero_probability_never_replies_when_not_addressed(self):
        runtime = await self.start_runtime()
        plugin = self._controls(runtime, 0)
        self.assertFalse(plugin.should_reply(self._message(), runtime))

    async def test_full_probability_replies_without_mention(self):
        runtime = await self.start_runtime()
        plugin = self._controls(runtime, 100)
        self.assertTrue(plugin.should_reply(self._message(), runtime))

    async def test_mention_always_wins_regardless_of_probability(self):
        runtime = await self.start_runtime()
        plugin = self._controls(runtime, 0)
        self.assertTrue(plugin.should_reply(self._message(at=True), runtime))

    async def test_private_chat_is_not_probability_gated(self):
        runtime = await self.start_runtime()
        plugin = self._controls(runtime, 0)
        private = {'kind': 'private', 'target': 'wxid_a', 'text': '你好'}
        self.assertTrue(plugin.should_reply(private, runtime))


if __name__ == '__main__':
    unittest.main()
