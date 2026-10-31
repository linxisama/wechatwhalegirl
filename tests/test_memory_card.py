"""控制台第四张卡：记忆与省 token。字段缺失时必须降级，不能崩。"""
import unittest

from wechat_gui import memory_card


class MemoryCardTests(unittest.TestCase):
    def test_counts_are_summarised(self):
        title, subtitle = memory_card({
            'memory': {'manual': 2, 'confirmed': 5, 'candidate': 3, 'learned': 12, 'people': 4},
            'usage': {'total_tokens': 1234, 'request_count': 7}})
        self.assertIn('7', title)
        self.assertIn('12', title)
        self.assertIn('4 位群友', subtitle)
        self.assertIn('3 条待确认', subtitle)
        self.assertIn('1234 token', subtitle)

    def test_empty_memory_is_worded_honestly(self):
        title, subtitle = memory_card({'memory': {}, 'usage': {}})
        self.assertEqual(title, '记忆为空')
        self.assertIn('还没有调用记录', subtitle)

    def test_missing_fields_never_crash(self):
        title, subtitle = memory_card({})
        self.assertEqual(title, '记忆为空')
        self.assertIsInstance(subtitle, str)

    def test_candidate_only_does_not_claim_zero(self):
        title, _ = memory_card({'memory': {'candidate': 3}})
        self.assertEqual(title, '记忆为空')


if __name__ == '__main__':
    unittest.main()
