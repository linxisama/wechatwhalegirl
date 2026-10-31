"""恢复窗口不能重启机器人，也不能丢失最大化或无托盘时的唯一入口。"""
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QWidget
from src.gui.tray import RobotTray


class TrayRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window._action = Mock()
        self.window._request_exit = Mock()
        self.window._busy = False
        self.window._quitting = False
        self.tray = RobotTray(self.window, QIcon(), available=False)

    def tearDown(self):
        self.tray.icon.hide()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_show_menu_is_available_offline_and_during_bot_request(self):
        labels = [a.text() for a in self.tray.menu.actions() if not a.isSeparator()]
        self.assertEqual(labels, ['显示控制台', '开启机器人', '停止机器人', '退出程序'])
        for status, busy in [({}, False), ({'running': True}, True)]:
            self.window.hide()
            self.window._busy = busy
            self.tray.update(status)
            self.tray.show_action.trigger()
            self.assertTrue(self.window.isVisible())
        self.window._action.assert_not_called()
        self.window._request_exit.assert_not_called()

    def test_hidden_maximized_window_is_restored_maximized(self):
        self.window.showMaximized()
        self.window.hide()
        self.tray.show_action.trigger()
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window.isMaximized())
        self.window._action.assert_not_called()

    def test_activation_restores_minimized_window_without_starting_bot(self):
        for reason in (QSystemTrayIcon.ActivationReason.Trigger,
                       QSystemTrayIcon.ActivationReason.DoubleClick):
            self.window.setWindowState(Qt.WindowState.WindowMinimized | Qt.WindowState.WindowMaximized)
            self.window.hide()
            self.tray._activated(reason)
            self.assertTrue(self.window.isVisible())
            self.assertFalse(self.window.isMinimized())
            self.assertTrue(self.window.isMaximized())
        self.window._action.assert_not_called()

    def test_tray_unavailable_does_not_hide_the_last_window(self):
        self.window.show()
        self.tray.enabled = True
        with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=False):
            self.assertFalse(self.tray.hide_window())
        self.assertTrue(self.window.isVisible())

    def test_transparent_window_theme_does_not_make_tray_menu_transparent(self):
        self.window.setStyleSheet('QWidget { background: transparent; color: #142c49; }')
        self.tray.menu.ensurePolished()
        self.tray.menu.adjustSize()
        background = self.tray.menu.grab().toImage().pixelColor(3, 3)
        self.assertEqual(background.alpha(), 255)
        self.assertGreater(background.lightness(), 230)


if __name__ == '__main__':
    unittest.main()
