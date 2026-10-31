"""普通插件显示移除和恢复，核心组件与缺失来源不会误给删除入口。"""
import os
import tempfile
import unittest
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication
import wechat_gui as gui
from src.plugins.manager import PluginManager

APP=QApplication.instance() or QApplication([])


class RemovalGuiTests(unittest.TestCase):
    def test_catalogue_has_remove_and_restore_and_correct_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager=PluginManager(Path(tmp));tab=gui.PluginTab()
            tab._plugins=manager.list_plugins();tab._render(tab._plugins,{})
            self.assertEqual(tab._rows['quick_reply']['remove'].text(),'移除')
            self.assertIsNone(tab._rows['conversation_memory']['remove'])
            manager.set_removed('quick_reply')
            tab._plugins=manager.list_plugins();tab._render(tab._plugins,{})
            row=tab._rows['quick_reply']
            self.assertEqual(row['remove'].text(),'恢复')
            self.assertFalse(row['toggle'].isEnabled());self.assertTrue(row['frame'].isHidden())
            tab.state_filter.setCurrentIndex(tab.state_filter.findData('removed'))
            self.assertFalse(row['frame'].isHidden())
            self.assertTrue(tab._rows['keyword_reply']['frame'].isHidden())
            tab.deleteLater();APP.processEvents()

    def test_about_page_adapts_and_copy_feedback_does_not_reserve_blank_space(self):
        tab=gui.AboutTab();tab.resize(1200,850);tab.show();APP.processEvents()
        self.assertEqual(tab.card_grid.getItemPosition(tab.card_grid.indexOf(tab.thanks_card))[:2],(0,1))
        self.assertTrue(tab.copy_hint.isHidden())
        tab.resize(780,850);APP.processEvents()
        self.assertEqual(tab.card_grid.getItemPosition(tab.card_grid.indexOf(tab.thanks_card))[:2],(1,0))
        self.assertLessEqual(tab.content.width(),1120)
        tab.btn_copy_qq.click();self.assertFalse(tab.copy_hint.isHidden())
        tab.close();tab.deleteLater();APP.processEvents()
