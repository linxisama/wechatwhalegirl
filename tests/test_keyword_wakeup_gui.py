"""关键词设置的说明、草稿操作与原保存契约保持一致。"""
import copy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QComboBox
import wechat_gui as gui
from src.plugins.keyword_trigger import KeywordTrigger

APP = QApplication.instance() or QApplication([])


class KeywordWakeupGuiTests(unittest.TestCase):
    def make(self, **settings):
        meta = KeywordTrigger.META
        values = dict(meta.default_settings, **settings)
        data = {'id': meta.id, 'name': meta.name, 'description': meta.description,
                'schema': meta.schema_dict(), 'defaults': meta.default_settings, 'settings': values}
        original = copy.deepcopy(data)
        dialog = gui.PluginConfigDialog(data)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(data, original, '打开页面不能修改已读取的配置')
        return dialog

    def test_open_preserves_empty_or_existing_keywords_and_mode_values(self):
        for values in ({}, {'mode': 'replace', 'keywords': '旧称呼\n独有名字', 'includePersona': False}):
            dialog = self.make(**values)
            self.assertEqual(dialog.values(), dict(KeywordTrigger.META.default_settings, **values))
            self.assertIsInstance(dialog._widgets['mode'][0], QComboBox)
            self.assertIn('小鲸鱼', dialog._widgets['keywords'][0].placeholderText())

    def test_explanations_are_visible_text_and_cover_persona_precedence(self):
        dialog = self.make()
        text = '\n'.join(label.text() for label in dialog.findChildren(QLabel))
        for expected in ('每个词至少 2 个字符', '不代表停止机器人', '优先', '留空不等于关闭唤醒'):
            self.assertIn(expected, text)
        self.assertNotIn('keyword_trigger', dialog.windowTitle())

    def test_examples_append_only_in_draft_without_duplicates_or_save(self):
        dialog = self.make(keywords='保留称呼\n小鲸鱼')
        with patch.object(gui, 'call_api') as request:
            button = dialog.findChild(QPushButton, 'keyword_examples')
            button.click(); button.click()
            values = dialog.values()
            self.assertEqual(values['keywords'].count('小鲸鱼'), 1)
            self.assertTrue(values['keywords'].startswith('保留称呼\n小鲸鱼'))
            self.assertIn('鲸鱼娘', values['keywords'])
            request.assert_not_called()
        self.assertFalse(dialog.saved)

    def test_summary_distinguishes_at_only_disabled_and_empty_replacement(self):
        dialog = self.make()
        dialog._widgets['keywordsEnabled'][0].setChecked(False)
        self.assertIn('只由 @', dialog.keyword_summary.text())
        dialog._widgets['enabled'][0].setChecked(False)
        self.assertIn('恢复原来的', dialog.keyword_summary.text())
        dialog._widgets['enabled'][0].setChecked(True)
        dialog._widgets['keywordsEnabled'][0].setChecked(True)
        dialog._widgets['mode'][0].setCurrentIndex(1)
        dialog._widgets['includePersona'][0].setChecked(False)
        self.assertIn('没有通用唤醒词', dialog.keyword_summary.text())
        dialog._widgets['keywords'][0].setPlainText('小鲸鱼，鲸鱼娘')
        self.assertIn('2 个词', dialog.keyword_summary.text())

    def test_single_character_is_explained_and_cannot_be_saved(self):
        dialog = self.make(keywords='鲸')
        self.assertIn('至少需要 2', dialog.keyword_summary.text())
        with patch.object(gui, 'call_api') as request, patch.object(gui.QMessageBox, 'warning') as warning:
            dialog._save()
            request.assert_not_called(); warning.assert_called_once()
        self.assertEqual(dialog.values()['keywords'], '鲸')

    def test_save_keeps_canonical_payload_and_reset_is_only_a_draft(self):
        dialog = self.make(mode='replace', keywords='自定义称呼')
        with patch.object(gui, 'call_api') as request:
            dialog._save()
            args = request.call_args.args
            self.assertEqual(args[1:3], ('POST', 'plugins/keyword_trigger'))
            self.assertEqual(args[3], dialog.values())
            self.assertEqual(args[3]['mode'], 'replace')
            self.assertEqual(set(args[3]), set(KeywordTrigger.META.default_settings))
        with patch.object(gui, 'call_api') as request:
            dialog._reset(); request.assert_not_called()
        self.assertEqual(dialog.values(), KeywordTrigger.META.default_settings)

    def test_small_window_keeps_save_button_reachable_and_editor_compact(self):
        dialog = self.make(); dialog.resize(520, 440); dialog.show(); APP.processEvents()
        button = next(b for b in dialog.findChildren(QPushButton) if b.text() == '保存设置')
        self.assertTrue(button.isVisible())
        self.assertTrue(dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight())))
        self.assertLessEqual(dialog._widgets['keywords'][0].height(), 140)
        dialog.close()

    def test_first_use_guide_gives_a_complete_filling_and_trigger_example(self):
        dialog = self.make()
        guide = dialog.findChild(QLabel, 'keyword_quick_guide').text()
        for text in ('保留原有词表', '每行', '小鲸鱼，在吗', '保存后'):
            self.assertIn(text, guide)

    def test_disabling_wakeup_keeps_existing_draft_for_reenabling(self):
        dialog = self.make(mode='replace', keywords='原有称呼', includePersona=False)
        dialog._widgets['keywordsEnabled'][0].setChecked(False)
        self.assertFalse(dialog._widgets['keywords'][0].isEnabled())
        self.assertFalse(dialog.findChild(QPushButton, 'keyword_examples').isEnabled())
        dialog._widgets['keywordsEnabled'][0].setChecked(True)
        self.assertTrue(dialog._widgets['keywords'][0].isEnabled())
        self.assertEqual(dialog.values()['keywords'], '原有称呼')
        self.assertEqual(dialog.values()['mode'], 'replace')
        self.assertFalse(dialog.values()['includePersona'])
