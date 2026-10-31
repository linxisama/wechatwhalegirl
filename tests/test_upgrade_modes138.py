"""旧配置刷新与覆盖后的实际插件行为，防止只验证配置文件尚在。"""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication
import wechat_gui as gui
from src.plugins.manager import PluginManager
from src.plugin_catalog import BASIC_PLUGINS, MARKET_PLUGINS

_APP=None


class UpgradeModes138(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP=QApplication.instance() or QApplication([])

    def test_refresh_foreground_config_after_previous_background_selection(self):
        with patch.object(gui,'call_api'),patch.object(gui,'api_get',return_value={}):
            tab=gui.SettingsTab()
            tab.populate(dict(send_compat=False,background_send=True))
            self.assertTrue(tab.background_send.isChecked())
            tab.populate(dict(send_compat=True,background_send=False))
            self.assertTrue(tab.send_compat.isChecked())
            self.assertFalse(tab.background_send.isChecked())
            for _ in range(2):
                tab.populate(dict(send_compat=True,background_send=False))
                self.assertTrue(tab.send_compat.isChecked())
            tab.close()

    def test_supported_plugins_settings_survive_reload_and_execute(self):
        with tempfile.TemporaryDirectory() as folder:
            manager=PluginManager(folder)
            self.assertTrue(set(BASIC_PLUGINS+MARKET_PLUGINS)<=set(manager._plugins))
            manager.update_settings('keyword_reply',dict(enabled=True,rules='升级暗号=升级仍可回复'))
            manager.update_settings('minimal_mode',dict(enabled=True))
            manager.set_removed('reply_pace')
            new=PluginManager(folder)
            self.assertEqual(new._plugins['keyword_reply'].on_ingest(dict(kind='private',text='升级暗号'),None),'升级仍可回复')
            self.assertTrue(new._plugins['minimal_mode'].enabled)
            self.assertFalse(new._plugins['reply_pace'].enabled)


if __name__=='__main__':unittest.main()
