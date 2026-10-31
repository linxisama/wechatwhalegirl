"""reply_guards（复读接梗）插件契约测试。

这个插件此前在 tests/ 里 0 次引用：规则（2 个以上不同的人重复同一句短文本）、
配置缺省值、非法配置的容错、以及它会不会改写/吞掉 AI 回复，都没有回归网。

本文件全部用**真实** ReplyGuards 实例 + 真实 PluginManager 分发（run_on_pre_ai /
run_on_post_reply / run_on_block），只冻结时钟——梗窗口是时间相关的，不冻结就只能
靠 sleep 或 sleep 不够而偶发。被测对象本身不 mock。
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.plugins import PluginBase
from src.plugins.manager import PluginManager

MEME = '典中典'
ROOM = 'room@chatroom'
_KEEP = object()


def group_message(sender, text=MEME, target=ROOM, kind='group'):
    return {'text': text, 'sender_id': sender, 'target': target, 'kind': kind}


class ReplyGuardsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.plugin = self.manager._plugins['reply_guards']
        self.runtime = SimpleNamespace(reply_meta=None, note=lambda value: None)

    # ── 小工具：全部走宿主的真实分发，不直接调插件 ──────────────────────────
    def enable(self, manager=None):
        (manager or self.manager).update_settings('reply_guards', {'enabled': True})

    def dispatch(self, message, manager=None, plugin=None, runtime=_KEEP):
        manager = manager or self.manager
        manager._plugins = {'reply_guards': plugin or manager._plugins['reply_guards']}
        return manager.run_on_pre_ai(
            message, [], self.runtime if runtime is _KEEP else runtime)

    def say(self, *senders, **kwargs):
        """按顺序喂几条同文消息，返回最后一条消息收到的提示。"""
        manager = kwargs.get('manager')
        plugin = kwargs.get('plugin')
        hints = None
        for sender in senders:
            hints = self.dispatch(group_message(sender, kwargs.get('text', MEME)), manager, plugin)
        return hints

    # ── 配置缺省值 ────────────────────────────────────────────────────────────
    def test_defaults_are_off_and_window_is_120_seconds(self):
        self.assertEqual(self.manager.get_settings('reply_guards'),
                         {'enabled': False, 'memeWindowSec': 120})
        self.assertFalse(self.plugin.enabled, '复读接梗默认必须关闭：它会改变群里的说话风格')
        self.assertEqual(self.plugin.get('memeWindowSec'), 120)
        # 默认关闭时，即使三个人真的复读，插件也不介入（不发提示、不拦消息）
        hints = self.say('wxid_a', 'wxid_b', 'wxid_c')
        self.assertEqual(hints['extra_system'], '')
        self.assertFalse(hints['block'])

    # ── 命中规则 ──────────────────────────────────────────────────────────────
    def test_two_other_people_saying_the_same_short_text_adds_the_hint(self):
        self.enable()
        self.assertEqual(self.say('wxid_a')['extra_system'], '', '第一次出现只是观察')
        self.assertEqual(self.say('wxid_b')['extra_system'], '', '第二个人还不够，要到第三个')
        hints = self.say('wxid_c')
        self.assertIn('跟风', hints['extra_system'])
        self.assertIn(f'「{MEME}」', hints['extra_system'])
        self.assertEqual([pid for pid, _ in hints['extra_system_parts']], ['reply_guards'],
                         '宿主必须把这段提示记在 reply_guards 名下')

    # ── 被规则拦下的人际语义 ──────────────────────────────────────────────────
    def test_one_person_repeating_alone_is_not_a_meme(self):
        self.enable()
        hints = self.say('wxid_a', 'wxid_a', 'wxid_a')
        self.assertEqual(hints['extra_system'], '', '同一个人刷屏不算大家接梗')

    def test_texts_over_20_chars_and_empty_texts_never_count(self):
        self.enable()
        long_text = '这是一句远远超过二十个字符长度限制的长文本所以不该被当成梗'
        self.assertGreater(len(long_text), 20)
        hints = self.say('wxid_a', 'wxid_b', 'wxid_c', text=long_text)
        self.assertEqual(hints['extra_system'], '')
        hints = self.say('wxid_a', 'wxid_b', 'wxid_c', text='   ')
        self.assertEqual(hints['extra_system'], '', '空白文本连观察都不该记')
        # 被规则挡住的消息没有进历史，真正的短梗仍然照常触发
        self.assertIn('跟风', self.say('wxid_a', 'wxid_b', 'wxid_c')['extra_system'])

    def test_meme_history_does_not_leak_across_groups(self):
        self.enable()
        self.dispatch(group_message('wxid_a', target='room_a@chatroom'))
        self.dispatch(group_message('wxid_b', target='room_b@chatroom'))
        hints = self.dispatch(group_message('wxid_c', target='room_c@chatroom'))
        self.assertEqual(hints['extra_system'], '', '每个群的梗必须分开记账')

    # ── 窗口（默认 120 秒）真的会过期 ─────────────────────────────────────────
    def test_default_window_prunes_history_older_than_120_seconds(self):
        self.enable()
        clock = {'now': 1000.0}
        with patch('src.plugins.reply_guards.time', SimpleNamespace(time=lambda: clock['now'])):
            self.assertEqual(self.say('wxid_a', 'wxid_b')['extra_system'], '')
            clock['now'] = 1000.0 + 121
            self.assertEqual(self.say('wxid_c')['extra_system'], '',
                             '超过窗口的旧消息必须先被剪掉，否则明天复读昨天的梗也算')
            self.assertEqual(self.say('wxid_d')['extra_system'], '')
            self.assertIn('跟风', self.say('wxid_e')['extra_system'],
                          '窗口内凑到第三个不同的人就该触发')

    # ── 配置非法值的容错 ──────────────────────────────────────────────────────
    def test_illegal_window_settings_are_rejected_and_do_not_break_the_plugin(self):
        self.enable()
        with self.assertRaisesRegex(ValueError, 'below min 10'):
            self.manager.update_settings('reply_guards', {'memeWindowSec': 5})
        with self.assertRaisesRegex(ValueError, 'int field requires int'):
            self.manager.update_settings('reply_guards', {'memeWindowSec': 'abc'})
        with self.assertRaisesRegex(ValueError, 'unknown setting field'):
            self.manager.update_settings('reply_guards', {'memeWindow': 60})
        self.assertEqual(self.manager.get_settings('reply_guards')['memeWindowSec'], 120,
                         '三次被拒的写入不能留下半截配置')
        self.assertIn('跟风', self.say('wxid_a', 'wxid_b', 'wxid_c')['extra_system'],
                      '配置被拒之后插件必须还能正常工作')

    def test_corrupt_stored_window_is_reconciled_to_default_on_load(self):
        root = Path(self.tmp.name) / 'dirty'
        root.mkdir()
        (root / 'plugin_settings.json').write_text(json.dumps({
            'reply_guards': {'enabled': True, 'memeWindowSec': 'abc', 'onlyGroups': True}},
            ensure_ascii=False), 'utf-8')
        manager = PluginManager(root)
        plugin = manager._plugins['reply_guards']
        self.assertEqual(manager.get_settings('reply_guards'),
                         {'enabled': True, 'memeWindowSec': 120},
                         '落盘的非法值回退默认，废弃的旧键不再出现在设置里')
        self.assertTrue(plugin.enabled)
        hints = self.say('wxid_a', 'wxid_b', 'wxid_c', manager=manager, plugin=plugin)
        self.assertIn('跟风', hints['extra_system'], '回退出来的窗口值必须真的能用')

    # ── AI 回复：既不改写也不吞掉 ─────────────────────────────────────────────
    async def test_ai_reply_is_neither_rewritten_nor_dropped(self):
        self.enable()
        self.manager._plugins = {'reply_guards': self.plugin}
        message = group_message('wxid_a')
        reply = await self.manager.run_on_post_reply(message, '这是 AI 的原话', self.runtime)
        self.assertEqual(reply, '这是 AI 的原话', '复读接梗只提示模型，不许改最终回复')
        self.assertIs(type(self.plugin).on_post_reply, PluginBase.on_post_reply,
                      '它不该实现 on_post_reply；一旦实现就必须有改写契约的测试')
        self.assertFalse(self.manager.run_on_block(message, self.runtime),
                         '它也不许把消息静默拦掉')

    # ── 与 runtime / message 的交互 ───────────────────────────────────────────
    async def test_dispatch_leaves_message_and_runtime_meta_untouched(self):
        self.enable()
        self.runtime.reply_meta = {'media': ['keep-me']}
        message = group_message('wxid_a')
        before = json.loads(json.dumps(message, ensure_ascii=False))
        self.say('wxid_a', 'wxid_b', 'wxid_c')
        self.assertEqual(message, before, '插件只读消息，不许往消息里塞字段')
        self.assertEqual(self.runtime.reply_meta, {'media': ['keep-me']},
                         '宿主必须把 reply_meta 还原，插件没动它就不能被拖走')
        # 另一个群、完全没有 runtime：插件不依赖 runtime，必须照常工作而不是被隔离
        fresh = 'room_without_runtime@chatroom'
        for sender in ('wxid_a', 'wxid_b'):
            self.dispatch(group_message(sender, target=fresh), runtime=None)
        hints = self.dispatch(group_message('wxid_c', target=fresh), runtime=None)
        self.assertIn('跟风', hints['extra_system'], '拿不到 runtime 也必须照常工作')
        self.assertFalse(self.manager.is_quarantined('reply_guards'))


if __name__ == '__main__':
    unittest.main()
