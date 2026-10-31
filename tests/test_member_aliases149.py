"""群友外号对照（member_aliases，1.3.49-C1）的行为锁。

这些用例锁的是移植时定的口径，不是 QQ 原版实现：
- 只认第一人称自述，四人称/动作句一律不收（"我叫你别走""我叫了外卖"）；
- 短 ASCII 外号按字母数字边界匹配（"99" 不能命中 "1990"）；
- 注入只发生在群聊且命中；没命中一个字都不加（不花 prompt 预算）；
- 坏状态文件当空表，插件不因此拒干活；
- 口令只认主人私聊，且只在 robot_admin 判定为主人时生效。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.plugins.member_aliases import (
    MemberAliases,
    HEADER,
    MAX_ALIASES_PER_MEMBER,
    build_hint,
    clean_aliases,
    learn_aliases,
    text_has_alias,
)


class FakeManager:
    """只提供插件真正用到的那几样：state_dir + get_settings。"""

    def __init__(self, root, settings=None):
        self.state_dir = Path(root)
        self._settings = {"member_aliases": dict(settings or {})}

    def get_settings(self, pid):
        stored = self._settings.get(pid) or {}
        known = dict(MemberAliases.META.default_settings)
        known.update(stored)
        return known


class FakePlugins:
    def __init__(self, control=None):
        self._plugins = {} if control is None else {"robot_admin": control}


class FakeRuntime:
    def __init__(self, control=None):
        self.plugins = FakePlugins(control)


class AdminStub:
    def __init__(self, answer):
        self._answer = answer

    def is_admin(self, message, runtime):
        return self._answer


class MemberAliasesBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aliases149-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def make(self, **settings):
        return MemberAliases(FakeManager(self.root, settings))

    def state_path(self):
        return Path(self.root) / "member_aliases" / "member_aliases.json"


class LearnTests(MemberAliasesBase):
    def test_first_person_self_intro_is_learned(self):
        cases = {
            "我叫小海": ["小海"],
            "叫我海哥吧": ["海哥"],
            "我的外号是阿紫": ["阿紫"],
            "你可以叫我老王": ["老王"],
            "叫我阿海吧": ["阿海"],
        }
        for text, expected in cases.items():
            self.assertEqual(learn_aliases(text), expected, text)

    def test_second_person_and_action_sentences_are_rejected(self):
        cases = ["我叫你别走", "我叫了外卖", "他叫小海", "老板说叫我过去"]
        for text in cases:
            self.assertEqual(learn_aliases(text), [], text)

    def test_empty_input_is_safe(self):
        self.assertEqual(learn_aliases(""), [])
        self.assertEqual(learn_aliases(None), [])


class AliasMatchTests(MemberAliasesBase):
    def test_chinese_alias_matches_inside_sentence(self):
        self.assertTrue(text_has_alias("某某今天怎么没来", "某某"))

    def test_short_ascii_alias_respects_digit_boundary(self):
        self.assertFalse(text_has_alias("1990年的老照片", "99"))
        self.assertTrue(text_has_alias("99来了", "99"))
        self.assertTrue(text_has_alias("是99啊", "99"))

    def test_ascii_case_insensitive_but_bounded(self):
        self.assertTrue(text_has_alias("叫 ab 的那个人", "AB"))
        self.assertFalse(text_has_alias("abc", "AB"))

    def test_empty_alias_never_matches(self):
        self.assertFalse(text_has_alias("随便说说", ""))
        self.assertFalse(text_has_alias("", "小海"))


class CleanTests(MemberAliasesBase):
    def test_string_is_split_and_deduped(self):
        self.assertEqual(clean_aliases("小海，海哥、小海; 阿紫"), ["小海", "海哥", "阿紫"])

    def test_length_and_count_are_capped(self):
        words = clean_aliases(["一" * 30])
        self.assertEqual(words, ["一" * 12])
        many = clean_aliases([f"名{i}" for i in range(40)])
        self.assertEqual(len(many), MAX_ALIASES_PER_MEMBER)

    def test_non_list_input_is_empty(self):
        self.assertEqual(clean_aliases(None), [])
        self.assertEqual(clean_aliases(123), [])


class HintTests(MemberAliasesBase):
    def test_speaker_line_is_emitted(self):
        hint = build_hint({"sender_id": "wxid_speaker", "text": ""},
                          {"wxid_speaker": ["小海"]})
        self.assertIn(HEADER, hint)
        self.assertIn("这轮说话的是", hint)
        self.assertIn("小海", hint)
        self.assertNotIn("wxid_speaker", hint)   # 只留前 6 字，别泄整串 id

    def test_mention_line_is_emitted(self):
        hint = build_hint({"sender_id": "wxid_other", "text": "阿紫今天没来"},
                          {"wxid_target": ["阿紫"]})
        self.assertIn("有人提到「阿紫」", hint)

    def test_no_hit_returns_empty_string(self):
        self.assertEqual(build_hint({"sender_id": "x", "text": "早上好"},
                                    {"wxid_target": ["阿紫"]}), "")

    def test_empty_table_returns_empty_string(self):
        self.assertEqual(build_hint({"sender_id": "x", "text": "随便"}, {}), "")

    def test_lines_are_capped(self):
        table = {f"u{i}": [f"别名{i}"] for i in range(10)}
        text = " ".join(f"别名{i}" for i in range(10))
        hint = build_hint({"sender_id": "zzz", "text": text}, table, limit=3)
        self.assertEqual(len(hint.splitlines()) - 1, 3)


class PreAiTests(MemberAliasesBase):
    def test_group_hit_injects_hint(self):
        plugin = self.make(injectAliases=True)
        plugin.save_table({"wxid_speaker": ["小海"]})
        out = plugin.on_pre_ai({"kind": "group", "sender_id": "wxid_speaker", "text": ""}, [], None)
        self.assertIsInstance(out, dict)
        self.assertIn(HEADER, out["extra_system"])

    def test_private_chat_is_never_injected(self):
        plugin = self.make(injectAliases=True)
        plugin.save_table({"wxid_speaker": ["小海"]})
        self.assertIsNone(plugin.on_pre_ai({"kind": "private", "sender_id": "wxid_speaker", "text": ""}, [], None))

    def test_injection_switch_off_keeps_silent(self):
        plugin = self.make(injectAliases=False)
        plugin.save_table({"wxid_speaker": ["小海"]})
        self.assertIsNone(plugin.on_pre_ai({"kind": "group", "sender_id": "wxid_speaker", "text": ""}, [], None))

    def test_group_without_hit_costs_nothing(self):
        plugin = self.make(injectAliases=True)
        self.assertIsNone(plugin.on_pre_ai({"kind": "group", "sender_id": "wxid_speaker", "text": "早上好"}, [], None))


class IngestTests(MemberAliasesBase):
    def test_learning_is_off_by_default(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_ingest({"kind": "group", "sender_id": "wxid_a", "text": "我叫小海"}, None))
        self.assertEqual(plugin.table(), {})

    def test_learning_writes_table_when_enabled(self):
        plugin = self.make(rememberAliases=True)
        plugin.on_ingest({"kind": "group", "sender_id": "wxid_a", "text": "我叫小海"}, None)
        self.assertEqual(plugin.table(), {"wxid_a": ["小海"]})
        self.assertEqual(json.loads(self.state_path().read_text("utf-8")), {"aliases": {"wxid_a": ["小海"]}})

    def test_private_message_is_never_learned(self):
        plugin = self.make(rememberAliases=True)
        plugin.on_ingest({"kind": "private", "sender_id": "wxid_a", "text": "我叫小海"}, None)
        self.assertEqual(plugin.table(), {})

    def test_per_member_cap_from_settings(self):
        plugin = self.make(rememberAliases=True, maxPerGroup=2)
        for text in ("我叫小海", "叫我海哥", "叫我阿海"):
            plugin.on_ingest({"kind": "group", "sender_id": "wxid_a", "text": text}, None)
        self.assertEqual(plugin.table()["wxid_a"], ["小海", "海哥"])


class CommandTests(MemberAliasesBase):
    def test_list_command_returns_table_to_keeper(self):
        plugin = self.make()
        plugin.save_table({"wxid_a": ["小海"]})
        reply = plugin.on_ingest({"kind": "private", "sender_id": "o", "target": "o", "text": "外号"}, FakeRuntime())
        self.assertIn("小海", reply)

    def test_add_and_remove_roundtrip(self):
        plugin = self.make()
        runtime = FakeRuntime()
        self.assertIn("已记下", plugin.on_ingest(
            {"kind": "private", "sender_id": "o", "target": "o", "text": "外号 加 wxid_a 小海"}, runtime))
        self.assertEqual(plugin.table(), {"wxid_a": ["小海"]})
        self.assertIn("已经在表里", plugin.on_ingest(
            {"kind": "private", "sender_id": "o", "target": "o", "text": "外号 加 wxid_a 小海"}, runtime))
        self.assertIn("已删掉", plugin.on_ingest(
            {"kind": "private", "sender_id": "o", "target": "o", "text": "外号 删 wxid_a"}, runtime))
        self.assertEqual(plugin.table(), {})

    def test_command_in_group_is_ignored(self):
        plugin = self.make()
        self.assertIsNone(plugin.on_ingest(
            {"kind": "group", "sender_id": "o", "text": "外号 加 wxid_a 小海"}, FakeRuntime()))
        self.assertEqual(plugin.table(), {})

    def test_robot_admin_veto_blocks_command(self):
        plugin = self.make()
        reply = plugin.on_ingest({"kind": "private", "sender_id": "x", "target": "x", "text": "外号 加 wxid_a 小海"},
                                 FakeRuntime(AdminStub(False)))
        self.assertIsNone(reply)
        self.assertEqual(plugin.table(), {})

    def test_robot_admin_approval_allows_command(self):
        plugin = self.make()
        reply = plugin.on_ingest({"kind": "private", "sender_id": "o", "target": "o", "text": "外号 加 wxid_a 小海"},
                                 FakeRuntime(AdminStub(True)))
        self.assertIn("已记下", reply)

    def test_unknown_command_returns_usage(self):
        plugin = self.make()
        reply = plugin.on_ingest({"kind": "private", "sender_id": "o", "target": "o", "text": "外号 什么是"}, FakeRuntime())
        self.assertIn("用法", reply)


class RobustnessTests(MemberAliasesBase):
    def test_broken_state_file_reads_as_empty_table(self):
        plugin = self.make(injectAliases=True)
        self.state_path().parent.mkdir(parents=True, exist_ok=True)
        self.state_path().write_text("{ 这不是 json", "utf-8")
        self.assertEqual(plugin.table(), {})
        self.assertIsNone(plugin.on_pre_ai({"kind": "group", "sender_id": "a", "text": ""}, [], None))

    def test_wrong_shape_is_treated_as_empty(self):
        plugin = self.make()
        self.state_path().parent.mkdir(parents=True, exist_ok=True)
        self.state_path().write_text(json.dumps({"aliases": ["小海"]}), "utf-8")
        self.assertEqual(plugin.table(), {})

    def test_plugin_is_disabled_by_default(self):
        self.assertFalse(MemberAliases.META.enabled_by_default)
        self.assertEqual(MemberAliases.META.id, "member_aliases")


if __name__ == "__main__":
    unittest.main()
