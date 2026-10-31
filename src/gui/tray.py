"""托盘只控制现有后台，不能额外创建机器人或监听线程。"""
from PyQt6.QtCore import QObject, Qt
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


class RobotTray(QObject):
    def __init__(self, window, icon, available=None):
        super().__init__(window)
        self.window = window
        self.enabled = (QSystemTrayIcon.isSystemTrayAvailable()
                        and QApplication.platformName() != 'offscreen') if available is None else available
        self.running = False
        self.online = False
        self.icon = QSystemTrayIcon(icon, self)
        self.menu = QMenu(window)
        # 主窗口允许透明背景，托盘弹出菜单必须独立保持可读，不能透出桌面壁纸。
        self.menu.setStyleSheet('''
            QMenu { background-color: #ffffff; color: #142c49;
                    border: 1px solid #cdd8e6; padding: 6px; min-width: 160px; }
            QMenu::item { padding: 7px 18px; background-color: transparent; }
            QMenu::item:selected { background-color: #e2f5f2; color: #142c49; }
            QMenu::item:disabled { color: #7d8998; }
            QMenu::separator { height: 1px; background-color: #dce3ec; margin: 4px 8px; }
        ''')
        self.show_action = self.menu.addAction('显示控制台')
        self.menu.addSeparator()
        self.start_action = self.menu.addAction('开启机器人')
        self.stop_action = self.menu.addAction('停止机器人')
        self.menu.addSeparator()
        self.exit_action = self.menu.addAction('退出程序')
        self.show_action.triggered.connect(self.show_window)
        self.start_action.triggered.connect(lambda: window._action('start'))
        self.stop_action.triggered.connect(lambda: window._action('stop'))
        self.exit_action.triggered.connect(window._request_exit)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.menu.aboutToShow.connect(self.refresh_actions)
        self.update({})
        if self.enabled:
            self.icon.show()

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def show_window(self):
        # showNormal 会把原先最大化的控制台缩回去；只解除最小化，保留原窗口尺寸状态。
        self.window.setWindowState(self.window.windowState() & ~Qt.WindowState.WindowMinimized)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def update(self, status):
        self.online = bool(status) and status.get('success') is not False
        self.running = bool(status.get('running')) if self.online else False
        state = ('运行中' if self.running else '已停止') if self.online else '后台未连接'
        self.icon.setToolTip(f'鲸鱼娘 · {state}\n单击打开控制台，右键显示、开启、停止或退出')
        self.refresh_actions()

    def refresh_actions(self):
        busy = bool(getattr(self.window, '_busy', False) or getattr(self.window, '_quitting', False))
        self.start_action.setEnabled(self.online and not self.running and not busy)
        self.stop_action.setEnabled(self.online and self.running and not busy)
        self.exit_action.setEnabled(not getattr(self.window, '_quitting', False))

    def hide_window(self):
        # Explorer 重启/托盘不可用时不能把唯一入口藏起来。
        if not self.enabled or not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        self.window.hide()
        return True
