"""「一人一框」编辑器：外号/口令、黑名单都改成一框一个。

用户反馈："外号口令这个太难配置，弄得通俗易懂、更简单，一个框对应一个人的那种"。
原来 `aliases` 是"逗号分隔一长串"，用户不知道该在哪断句；这里钉住新控件的行为：
中文逗号/换行也能拆、空值也给一个框、加减行正常、保存回逗号字符串。
"""

import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication

from src.gui.dialogue_rules import NameListEditor


class NameListEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_chinese_and_english_separators_both_split(self):
        editor = NameListEditor('鲸鱼娘，小夜, 夜夜')
        self.assertEqual([box.text() for box in editor.rows], ['鲸鱼娘', '小夜', '夜夜'])
        self.assertEqual(editor.names(), ['鲸鱼娘', '小夜', '夜夜'])

    def test_empty_value_still_shows_one_box(self):
        editor = NameListEditor('')
        self.assertEqual(len(editor.rows), 1)
        self.assertEqual(editor.text(), '')

    def test_add_and_remove_rows(self):
        editor = NameListEditor('一个')
        editor._append('两个')
        self.assertEqual(editor.names(), ['一个', '两个'])
        editor._remove_last()
        self.assertEqual(editor.names(), ['一个'])
        # 只剩一行时"删除"是清空，不是把界面删没
        editor._remove_last()
        self.assertEqual(len(editor.rows), 1)
        self.assertEqual(editor.names(), [])

    def test_blank_boxes_are_ignored_when_saving(self):
        editor = NameListEditor('甲,  ,乙')
        self.assertEqual(editor.text(), '甲,乙')

    def test_to_plain_text_is_supported_for_generic_payload(self):
        """通用表单取值时会把它当文本框读，必须能给出字符串。"""
        editor = NameListEditor('甲,乙')
        self.assertEqual(editor.toPlainText(), '甲,乙')


if __name__ == '__main__':
    unittest.main()
