"""省 token：极简模式（裁历史+封顶输出）与 Token 节流（压缩长历史）。

移植自 QQ 版 minimal_mode / token_saver。三条必须守住的底线：
1. **最后一条消息永远保留**（它就是要回应的内容）；
2. 正常长度的对话**一个字都不改**（只在不超预算时才动）；
3. 封顶输出只有在插件明确要求时才生效，且多插件取**更严**的那个。
"""

import unittest
from pathlib import Path
from types import SimpleNamespace
import tempfile

from src.plugins.manager import PluginManager
from src.plugins.minimal_mode import MinimalMode, history_limit, match_command
from src.plugins.token_saver import compact


def turns(count, size=20):
    """造 count 条历史（最后一条是"本轮要回应的"）。"""
    rows = []
    for index in range(count):
        rows.append({'role': 'user' if index % 2 == 0 else 'assistant',
                     'content': f'第{index}条' + 'x' * size})
    return rows


class HistoryLimitTests(unittest.TestCase):
    def test_last_message_is_never_dropped(self):
        messages = turns(20)
        kept, dropped = history_limit(messages, 5)
        self.assertEqual(len(kept), 5)
        self.assertEqual(kept[-1], messages[-1])
        self.assertEqual(dropped, 15)

    def test_short_conversation_is_untouched(self):
        messages = turns(3)
        kept, dropped = history_limit(messages, 10)
        self.assertEqual(kept, messages)
        self.assertEqual(dropped, 0)


class CommandTests(unittest.TestCase):
    def test_commands_and_slash_prefix(self):
        self.assertEqual(match_command('极简开'), 'on')
        self.assertEqual(match_command('/极简关'), 'off')
        self.assertEqual(match_command(' 极限 '), 'ultra')
        self.assertEqual(match_command('敢死'), 'kamikaze')
        self.assertEqual(match_command('极简'), 'status')
        self.assertIsNone(match_command('今天天气不错'))


class MinimalModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name))
        self.plugin = self.manager._plugins['minimal_mode']
        self.notes = []
        self.runtime = SimpleNamespace(note=lambda text: self.notes.append(text),
                                       plugins=self.manager)

    def tearDown(self):
        self.tmp.cleanup()

    def test_off_tier_changes_nothing(self):
        self.manager.update_settings('minimal_mode', {'enabled': True, 'tier': 'off'})
        messages = turns(20)
        self.assertIsNone(self.plugin.on_pre_ai({'kind': 'private'}, messages, self.runtime))
        self.assertEqual(len(messages), 20)

    def test_minimal_tier_trims_history_and_caps_output(self):
        self.manager.update_settings('minimal_mode', {'enabled': True, 'tier': 'minimal', 'minimalMsgs': 4,
                                     'minimalMaxTokens': 120})
        messages = turns(20)
        hints = self.plugin.on_pre_ai({'kind': 'private'}, messages, self.runtime)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[-1]['content'], turns(20)[-1]['content'])
        self.assertEqual(hints['max_tokens'], 120)
        self.assertIn('极简档', hints['extra_system'])
        self.assertTrue(any('极简模式' in note for note in self.notes))

    def test_kamikaze_has_the_strictest_cap(self):
        self.manager.update_settings('minimal_mode', {'enabled': True, 'tier': 'kamikaze'})
        hints = self.plugin.on_pre_ai({'kind': 'private'}, turns(10), self.runtime)
        self.assertEqual(hints['max_tokens'], 80)

    def test_tier_is_persisted_between_instances(self):
        self.manager.update_settings('minimal_mode', {'enabled': True})
        self.plugin.set_tier('ultra')
        again = PluginManager(Path(self.tmp.name))._plugins['minimal_mode']
        self.assertEqual(again.tier(), 'ultra')

    def test_command_requires_private_and_owner(self):
        self.manager.update_settings('minimal_mode', {'enabled': True, 'ownerIds': 'wxid_owner'})
        self.assertIsNone(self.plugin.on_ingest({'kind': 'group', 'text': '极简开',
                                                 'sender_id': 'wxid_owner'}, self.runtime))
        refused = self.plugin.on_ingest({'kind': 'private', 'text': '极简开',
                                          'sender_id': 'wxid_stranger'}, self.runtime)
        self.assertIn('管理员', refused)
        ok = self.plugin.on_ingest({'kind': 'private', 'text': '极简开',
                                     'sender_id': 'wxid_owner'}, self.runtime)
        self.assertIn('极简', ok)
        self.assertEqual(self.plugin.tier(), 'minimal')


class TokenSaverTests(unittest.TestCase):
    def test_old_history_is_digested_into_one_line(self):
        source = turns(20)
        packed, stats = compact(source, keep=6, digest_chars=120)
        # 19 条历史里留最近 6 条，其余 13 条压成 1 行摘要；再加最后一条 = 8 条
        self.assertEqual(len(packed), 8)
        self.assertEqual(stats['dropped'], 13)
        self.assertTrue(packed[0]['content'].startswith('（更早 13 条消息已省略'))
        self.assertEqual(packed[-1], source[-1])

    def test_duplicate_turns_are_removed(self):
        rows = [{'role': 'user', 'content': '在吗'}, {'role': 'user', 'content': '在吗'},
                {'role': 'assistant', 'content': '在'}, {'role': 'user', 'content': '本轮问题'}]
        packed, stats = compact(rows, keep=10)
        self.assertEqual(stats['deduped'], 1)
        self.assertEqual(packed[-1], rows[-1])

    def test_long_single_message_is_truncated(self):
        rows = [{'role': 'user', 'content': 'x' * 500}, {'role': 'user', 'content': '本轮'}]
        packed, stats = compact(rows, keep=10, max_content_chars=100)
        self.assertEqual(stats['truncated'], 1)
        self.assertIn('已截断', packed[0]['content'])
        self.assertEqual(packed[-1], rows[-1])

    def test_prompt_budget_never_eats_the_current_message(self):
        rows = turns(10, size=200)
        packed, stats = compact(rows, keep=10, max_prompt_chars=300)
        self.assertEqual(packed[-1], rows[-1])
        self.assertGreater(stats['dropped'], 0)

    def test_plugin_only_acts_when_over_budget(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            manager = PluginManager(Path(tmp.name))
            plugin = manager._plugins['token_saver']
            manager.update_settings('token_saver', {'enabled': True, 'keepRecentMessages': 10})
            short = turns(5)
            self.assertIsNone(plugin.on_pre_ai({}, short, SimpleNamespace(note=lambda text: None)))
            self.assertEqual(len(short), 5)     # 没动
            long = turns(30)
            plugin.on_pre_ai({}, long, SimpleNamespace(note=lambda text: None))
            self.assertLess(len(long), 30)
            self.assertEqual(long[-1]['content'], turns(30)[-1]['content'])
        finally:
            tmp.cleanup()


class HintContractTests(unittest.TestCase):
    """`max_tokens` 是 1.2.35 新增的插件契约：多插件取更严的那个。"""

    def test_smallest_cap_wins(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            manager = PluginManager(Path(tmp.name))
            runtime = SimpleNamespace(note=lambda text: None, plugins=manager)
            loose = SimpleNamespace(META=SimpleNamespace(id='loose', name='宽松'),
                                    enabled=True, settings={'enabled': True},
                                    get=lambda key, default=None: {'enabled': True}.get(key, default),
                                    on_pre_ai=lambda *a: {'max_tokens': 800})
            strict = SimpleNamespace(META=SimpleNamespace(id='strict', name='严格'),
                                     enabled=True, settings={'enabled': True},
                                     get=lambda key, default=None: {'enabled': True}.get(key, default),
                                     on_pre_ai=lambda *a: {'max_tokens': 100})
            manager._plugins['loose'] = loose
            manager._plugins['strict'] = strict
            hints = manager.run_on_pre_ai({}, [{'role': 'user', 'content': 'x'}], runtime)
            self.assertEqual(hints['max_tokens'], 100)
        finally:
            tmp.cleanup()

    def test_no_plugin_means_no_override(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            manager = PluginManager(Path(tmp.name))
            for key in list(manager._plugins):
                manager.update_settings(key, {'enabled': False})
            hints = manager.run_on_pre_ai({}, [], SimpleNamespace(note=lambda text: None))
            self.assertEqual(hints['max_tokens'], 0)
        finally:
            tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
