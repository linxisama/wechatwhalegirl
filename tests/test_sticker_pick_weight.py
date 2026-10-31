"""挑表情包：按"名字 + 含义"跟当前话题的相关度排序，都不相关时保持轮转。"""
import unittest

from src.plugins.sticker_cadence import rank_labels


class RankLabelsTests(unittest.TestCase):
    def test_label_match_wins(self):
        order = rank_labels(['笑脸', '下雨', '猫'], {}, '外面下雨了好烦', 0)
        self.assertEqual(order[0], '下雨')

    def test_description_match_also_counts(self):
        descriptions = {'猫猫': '一只在雨里淋湿的猫', '笑脸': '开心地笑'}
        order = rank_labels(['笑脸', '猫猫'], descriptions, '浑身淋湿了', 0)
        self.assertEqual(order[0], '猫猫')

    def test_label_beats_description(self):
        descriptions = {'A': '下雨', '下雨': ''}
        order = rank_labels(['A', '下雨'], descriptions, '下雨了', 0)
        self.assertEqual(order[0], '下雨')

    def test_unrelated_keeps_rotation(self):
        self.assertEqual(rank_labels(['a', 'b', 'c'], {}, '嗯嗯', 1), ['b', 'c', 'a'])

    def test_rotation_is_preserved_between_equal_scores(self):
        descriptions = {'甲': '下雨', '乙': '下雨'}
        order = rank_labels(['甲', '乙', '丙'], descriptions, '下雨', 0)
        self.assertEqual(order[:2], ['甲', '乙'])

    def test_empty_text_is_pure_rotation(self):
        self.assertEqual(rank_labels(['x', 'y'], {}, '', 0), ['x', 'y'])


if __name__ == '__main__':
    unittest.main()
