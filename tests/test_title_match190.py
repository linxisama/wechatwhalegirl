# -*- coding: utf-8 -*-
"""1.3.90-B 发送闸门判定：成员数后缀、ocr_budget 诊断码、profile 透传。

真机三个缺陷都让发送恒失败或丢掉现场线索：
  1) 像素层超预算会把 evidence state 设成 'ocr_budget'，发送层的 states 白名单里没有
     这个码，于是被降级成 'ocr_empty'，控制台看不到真因；
  2) title_diagnostic 的 profile 白名单只有 medium/legacy，新档（boundary / small /
     medium_fallback / medium_binarized）被静默丢弃；
  3) 微信群标题栏是「群名（成员数）」，title_matches 精确相等导致群聊恒 title_unverified。
这些用例只跑纯函数与假驱动，不读窗口、不连微信、不点击、不发送。
"""
import unittest

from src.wechat_guia_send import GuiaChannel, _strip_member_suffix


class MemberSuffixStripTests(unittest.TestCase):
    def test_only_trailing_numeric_member_suffix_is_stripped(self):
        self.assertEqual(_strip_member_suffix('极客先锋社招新群（77）'), '极客先锋社招新群')
        self.assertEqual(_strip_member_suffix('极客先锋社招新群(77)'), '极客先锋社招新群')
        # 不是结尾、不是纯数字、混搭括号都不算成员数后缀。
        self.assertEqual(_strip_member_suffix('极客先锋社招新群'), '极客先锋社招新群')
        self.assertEqual(_strip_member_suffix('（77）极客先锋'), '（77）极客先锋')
        self.assertEqual(_strip_member_suffix('极客先锋（77人）'), '极客先锋（77人）')
        self.assertEqual(_strip_member_suffix('极客先锋（77)'), '极客先锋（77)')
        self.assertEqual(_strip_member_suffix('极客先锋（）'), '极客先锋（）')
        self.assertEqual(_strip_member_suffix(''), '')

    def test_non_string_is_returned_untouched(self):
        # 驱动/适配器可能给出 None：helper 不抛异常，交给 title_matches 的空值分支拒绝。
        self.assertIsNone(_strip_member_suffix(None))


class TitleMatchesTests(unittest.TestCase):
    def test_group_title_matches_after_member_suffix(self):
        self.assertIs(GuiaChannel.title_matches('极客先锋社招新群（77）', '极客先锋社招新群'), True)
        self.assertIs(GuiaChannel.title_matches('极客先锋社招新群(77)', '极客先锋社招新群'), True)

    def test_shorter_target_name_is_still_rejected(self):
        # 剥掉后缀只能相等，不能变成前缀匹配：否则「社招新」会误中「社招新群」。
        self.assertIs(GuiaChannel.title_matches('极客先锋社招新群（77）', '极客先锋社招新'), False)
        self.assertIs(GuiaChannel.title_matches('极客先锋社招新群', '极客先锋社招新'), False)

    def test_empty_side_never_matches(self):
        self.assertIs(GuiaChannel.title_matches('', 'x'), False)
        self.assertIs(GuiaChannel.title_matches('x', ''), False)
        self.assertIs(GuiaChannel.title_matches('', ''), False)
        self.assertIs(GuiaChannel.title_matches(None, 'x'), False)

    def test_plain_title_still_needs_exact_equality(self):
        self.assertIs(GuiaChannel.title_matches('西莲寺紫', '西莲寺紫'), True)
        self.assertIs(GuiaChannel.title_matches('西莲寺紫（77）', '西莲寺紫'), True)
        self.assertIs(GuiaChannel.title_matches('西莲寺紫', '西莲寺紫（77）'), False)
        self.assertIs(GuiaChannel.title_matches('西莲寺', '西莲寺紫'), False)
        self.assertIs(GuiaChannel.title_matches('西莲寺紫紫', '西莲寺紫'), False)


class TitleDiagnosticStateTests(unittest.TestCase):
    def test_ocr_budget_state_reaches_console_instead_of_ocr_empty(self):
        channel = GuiaChannel()
        channel._title_evidence = {'state': 'ocr_budget'}
        self.assertEqual(channel.title_diagnostic()['state'], 'ocr_budget')

    def test_ocr_budget_survives_the_current_chat_whitelist(self):
        # current_chat 的白名单决定 evidence 能不能进 self._title_evidence：漏了这个码，
        # 超预算就被降级成 ocr_empty，控制台再也看不到真因。
        class BudgetGUI:
            def current_chat_title(self):
                return ''

            def title_diagnostic(self):
                return {'state': 'ocr_budget', 'profile': 'small'}

        channel = GuiaChannel()
        self.assertEqual(channel.current_chat(BudgetGUI()), '')
        evidence = channel.title_diagnostic()
        self.assertEqual(evidence['state'], 'ocr_budget')
        self.assertEqual(evidence['profile'], 'small')

    def test_unknown_state_still_degrades_to_ocr_empty(self):
        # 白名单只加 ocr_budget 这一个码：未知码照旧降级，不把任意字符串透给控制台。
        channel = GuiaChannel()
        channel._title_evidence = {'state': 'made_up_code'}
        self.assertEqual(channel.title_diagnostic()['state'], 'ocr_empty')


class TitleDiagnosticProfileTests(unittest.TestCase):
    def test_every_profile_is_passed_through(self):
        channel = GuiaChannel()
        for profile in ('medium', 'legacy', 'boundary', 'small',
                        'medium_fallback', 'medium_binarized'):
            channel._title_evidence = {'state': 'ocr_empty', 'profile': profile}
            self.assertEqual(channel.title_diagnostic()['profile'], profile, profile)

    def test_profile_keeps_dict_copy_and_passes_shape(self):
        channel = GuiaChannel()
        evidence = {'state': 'verified', 'profile': 'boundary',
                    'passes': [{'rows': 1, 'characters': 6, 'complete': True}]}
        channel._title_evidence = evidence
        result = channel.title_diagnostic()
        self.assertIsNot(result, evidence)               # 返回副本，不把内部字典交出去
        self.assertEqual(result['passes'], [{'rows': 1, 'characters': 6, 'complete': True}])
        self.assertEqual(set(result), {'state', 'profile', 'passes'})

    def test_missing_profile_is_not_invented(self):
        channel = GuiaChannel()
        channel._title_evidence = {'state': 'ocr_empty'}
        self.assertNotIn('profile', channel.title_diagnostic())

    def test_non_string_profile_is_ignored(self):
        channel = GuiaChannel()
        channel._title_evidence = {'state': 'ocr_empty', 'profile': 3}
        self.assertNotIn('profile', channel.title_diagnostic())


if __name__ == '__main__':
    unittest.main()
