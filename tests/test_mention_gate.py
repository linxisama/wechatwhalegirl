"""mention_gate（点名闸门）的离线测试（微信口径）。

对齐 JS 版 mention-gate 的三条规则：① 群闸门（名单里的群只在被 @ 或被叫到名字时开口）、
② 只回指定的人、③ 名单外群的待遇。

微信差异：判定依据是 ``message["at"]``（宿主算好的布尔值）与 ``runtime.aliases()``
的别名命中；**不再解析数字 QQ 号**，也没有 poke/notice 分支 —— 微信不产生通知事件
（契约第 1 节：``notice`` 恒为 None）。
"""

import unittest

from src.plugins.mention_gate import MentionGate, parse_ids, parse_user_rules


class ParseHelpersTests(unittest.TestCase):
    def test_parse_ids_tolerates_separators_and_deduplicates(self):
        self.assertEqual(
            parse_ids("12345678@chatroom, wxid_abc，99887766@chatroom、12345678@chatroom"),
            ["12345678@chatroom", "wxid_abc", "99887766@chatroom"])

    def test_parse_ids_accepts_any_non_empty_token(self):
        # 微信标识是 username/wxid，不是数字，所以不做数字校验
        self.assertEqual(parse_ids("abc 12 wxid_x"), ["abc", "12", "wxid_x"])

    def test_parse_ids_strips_quotes_around_tokens(self):
        self.assertEqual(parse_ids('"12345678@chatroom" “小明”'), ["12345678@chatroom", "小明"])

    def test_parse_user_rules_multiline(self):
        rules = parse_user_rules("12345678@chatroom=wxid_a,wxid_b\n99887766@chatroom＝wxid_c")
        self.assertEqual(rules["12345678@chatroom"], ["wxid_a", "wxid_b"])
        self.assertEqual(rules["99887766@chatroom"], ["wxid_c"])

    def test_parse_user_rules_keeps_nicknames_too(self):
        # 拿不到 wxid 时标识会退化为显示名，所以昵称也要能写进规则
        self.assertEqual(parse_user_rules("g=张三"), {"g": ["张三"]})

    def test_parse_user_rules_skips_blank_and_comment_lines(self):
        rules = parse_user_rules("\n# 说明\nbad line\n12345678@chatroom=wxid_a\n")
        self.assertEqual(rules, {"12345678@chatroom": ["wxid_a"]})

    def test_parse_user_rules_ignores_lines_without_ids(self):
        self.assertEqual(parse_user_rules("group="), {})
        self.assertEqual(parse_user_rules("=wxid_a"), {})


class FakeManager:
    """最小管理器。

    ``_settings`` 的结构必须与真实 ``PluginManager`` 一致：
    **``{plugin_id: {key: value}}``**（不是扁平的 ``{key: value}``）。
    否则 ``PluginBase.raw_settings``（读 ``_manager._settings[META.id]``）拿到 None，
    旧键迁移会静默失效 —— 这个细节曾经把测试和实现一起带偏。
    """

    def __init__(self, settings=None):
        merged = dict(MentionGate.META.default_settings)
        merged.update({"enabled": True})
        merged.update(settings or {})
        self._settings = {MentionGate.META.id: merged}
        self.updates = []

    def get_settings(self, plugin_id):
        return dict(self._settings.get(plugin_id, {}))

    def update_settings(self, plugin_id, settings):
        self.updates.append(dict(settings))
        self._settings[plugin_id] = dict(settings)


GROUP = "12345678@chatroom"
OTHER_GROUP = "99887766@chatroom"


def group_msg(text="随便聊聊", sender_id="wxid_10001", at=False, group=GROUP, at_ids=None):
    return {"id": "m1", "kind": "group", "target": group, "text": text, "sender": "某人",
            "sender_name": "某人", "sender_id": sender_id, "at": at, "at_ids": at_ids or []}


class _Runtime:
    """最小 runtime：只提供机器人被叫的昵称列表（契约第 4 节 runtime.aliases()）。"""

    def __init__(self, aliases=None):
        self._aliases = list(aliases if aliases is not None else ["鲸鱼娘", "小鲸鱼"])

    def aliases(self):
        return list(self._aliases)


class _BrokenRuntime:
    """拿不到别名时不能因此把闸门关死（异常要被兜住）。"""

    def aliases(self):
        raise RuntimeError("boom")


class GroupGateTests(unittest.TestCase):
    def make(self, **settings):
        return MentionGate(FakeManager(settings))

    def test_unmanaged_group_is_untouched_in_allowlist_mode(self):
        plugin = self.make(group_ids=OTHER_GROUP)
        self.assertFalse(plugin.on_block(group_msg(group=GROUP), _Runtime()))

    def test_unmanaged_group_is_blocked_in_only_mode(self):
        plugin = self.make(group_ids=OTHER_GROUP, mode="only")
        self.assertTrue(plugin.on_block(group_msg(group=GROUP), _Runtime()))

    def test_managed_group_requires_at_or_alias(self):
        plugin = self.make(group_ids=GROUP)
        self.assertTrue(plugin.on_block(group_msg(at=False), _Runtime()))

    def test_managed_group_allows_at(self):
        plugin = self.make(group_ids=GROUP)
        self.assertFalse(plugin.on_block(group_msg(at=True), _Runtime()))

    def test_managed_group_allows_being_called_by_name(self):
        # 微信里"被叫到名字"是仅次于 @ 的唤醒方式（contract：at 或命中 aliases）
        plugin = self.make(group_ids=GROUP)
        self.assertFalse(plugin.on_block(group_msg(text="小鲸鱼 在吗", at=False), _Runtime()))

    def test_alias_owned_by_persona_is_honoured(self):
        plugin = self.make(group_ids=GROUP)
        self.assertFalse(plugin.on_block(group_msg(text="蓝鲸酱 帮个忙"), _Runtime(["蓝鲸酱"])))

    def test_other_people_being_at_is_not_enough(self):
        # 群里有人 @ 别人（不是机器人）：at=False 就该闸住
        plugin = self.make(group_ids=GROUP)
        self.assertTrue(plugin.on_block(group_msg(at=False, at_ids=["wxid_other"]), _Runtime()))

    # 设置里承诺"可以填会话 username 或群名"，两处都要能命中。
    # 修法（已实施）：mention_gate 比对 `target` 与 `target_name` 两个候选标识。
    def test_group_name_can_be_used_as_identifier(self):
        plugin = self.make(group_ids="测试群")
        message = group_msg(text="随便聊聊")
        message["target_name"] = "测试群"
        self.assertTrue(plugin.on_block(message, _Runtime()))

    def test_broken_aliases_do_not_crash_the_gate(self):
        plugin = self.make(group_ids=GROUP)
        self.assertTrue(plugin.on_block(group_msg(at=False), _BrokenRuntime()))

    def test_private_chat_never_blocked(self):
        plugin = self.make(group_ids=GROUP)
        message = group_msg()
        message["kind"] = "private"
        self.assertFalse(plugin.on_block(message, _Runtime()))

    def test_disabled_plugin_never_blocks(self):
        plugin = MentionGate(FakeManager({"enabled": False, "group_ids": GROUP}))
        self.assertFalse(plugin.on_block(group_msg(at=False), _Runtime()))


class UserRuleTests(unittest.TestCase):
    def make(self, **settings):
        return MentionGate(FakeManager(settings))

    def test_listed_sender_is_ignored_even_when_at(self):
        plugin = self.make(user_rules=f"{GROUP}=wxid_10001")
        self.assertTrue(plugin.on_block(group_msg(sender_id="wxid_10001", at=True), _Runtime()))

    def test_other_senders_reply_normally(self):
        plugin = self.make(user_rules=f"{GROUP}=wxid_10001")
        self.assertFalse(plugin.on_block(group_msg(sender_id="wxid_20002", at=True), _Runtime()))

    def test_user_rule_applies_even_when_group_not_in_group_ids(self):
        plugin = self.make(group_ids="", user_rules=f"{GROUP}=wxid_10001")
        self.assertTrue(plugin.on_block(group_msg(sender_id="wxid_10001", at=True), _Runtime()))

    def test_missing_sender_id_does_not_block(self):
        # 拿不到发送者标识时宁可照常回复，也不要静默吃掉消息
        plugin = self.make(user_rules=f"{GROUP}=wxid_10001")
        self.assertFalse(plugin.on_block(group_msg(sender_id="", at=True), _Runtime()))

    def test_rules_are_per_group(self):
        plugin = self.make(user_rules=f"{GROUP}=wxid_10001")
        self.assertFalse(plugin.on_block(
            group_msg(sender_id="wxid_10001", group=OTHER_GROUP, at=True), _Runtime()))

    def test_nickname_based_rule_works_when_id_degraded_to_name(self):
        # 契约允许 sender_id 退化为显示名，此时用昵称写的规则照样生效
        plugin = self.make(user_rules=f"{GROUP}=张三")
        self.assertTrue(plugin.on_block(group_msg(sender_id="张三", at=True), _Runtime()))


class PromptNoteTests(unittest.TestCase):
    def make(self, **settings):
        return MentionGate(FakeManager(settings))

    def test_note_added_for_managed_group(self):
        plugin = self.make(group_ids=GROUP)
        hints = plugin.on_pre_ai(group_msg(), [], _Runtime())
        self.assertIn("点名闸门", hints["extra_system"])

    def test_no_note_for_unmanaged_group(self):
        plugin = self.make(group_ids=OTHER_GROUP)
        self.assertIsNone(plugin.on_pre_ai(group_msg(), [], _Runtime()))

    def test_note_can_be_disabled(self):
        plugin = self.make(group_ids=GROUP, prompt_note=False)
        self.assertIsNone(plugin.on_pre_ai(group_msg(), [], _Runtime()))


class LegacyMigrationTests(unittest.TestCase):
    """旧键迁移。

    注意调用点是 ``post_load()`` 而不是构造时：``__init__`` 跑在管理器注册实例**之前**，
    那时 ``update_settings`` 会抛 ``Unknown plugin``（迁移曾经就是这么静默失败的）。
    想让插件在"已注册"状态下迁移，必须显式调 ``post_load()``。
    真实的注册/迁移顺序在 tests/test_plugin_lifecycle.py 里端到端覆盖。
    """

    def test_legacy_only_groups_migrates_to_group_ids(self):
        manager = FakeManager({"onlyGroups": "12345678@chatroom,99887766@chatroom"})
        MentionGate(manager).post_load()
        self.assertEqual(manager.updates[-1]["group_ids"],
                         "12345678@chatroom,99887766@chatroom")
        self.assertNotIn("onlyGroups", manager.updates[-1])

    def test_new_value_wins_over_legacy(self):
        manager = FakeManager({"onlyGroups": GROUP, "group_ids": OTHER_GROUP})
        MentionGate(manager).post_load()
        self.assertEqual(manager.get_settings("mention_gate")["group_ids"], OTHER_GROUP)

    def test_legacy_keyword_is_dropped_and_logged(self):
        manager = FakeManager({"allowKeyword": "鲸鱼娘"})
        MentionGate(manager).post_load()
        self.assertNotIn("allowKeyword", manager.updates[-1])

    def test_no_legacy_keys_means_no_update(self):
        manager = FakeManager({})
        MentionGate(manager).post_load()
        self.assertEqual(manager.updates, [])


if __name__ == "__main__":
    unittest.main()
