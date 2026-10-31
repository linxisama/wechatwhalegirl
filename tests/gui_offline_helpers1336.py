"""Keep GUI unit-test windows and deferred callbacks inside offline barriers."""
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QTimer


class OfflineGuiIsolation:
    def __init__(self, case, gui, app, root=None):
        self.case, self.gui, self.app = case, gui, app
        self.temp = tempfile.TemporaryDirectory(prefix='gui-window-offline-') if root is None else None
        self.root = Path(self.temp.name) if self.temp is not None else Path(root)
        self.windows = []
        self.patches = [patch.object(gui, 'ROOT', self.root),
                        patch.object(gui, 'GUI_CFG', self.root / 'gui.json'),
                        patch.object(gui.sys, 'argv', ['gui-test', '--no-backend'])]
        self.boundary_patches = {}
        for name in ('api_get', 'api_post', 'api_delete', 'terminate_conflicting_backend'):
            item = patch.object(gui, name, side_effect=AssertionError('offline GUI boundary: ' + name))
            self.patches.append(item)
            self.boundary_patches[name] = item
        for target in ('subprocess.Popen', 'subprocess.run', 'requests.sessions.Session.request'):
            item = patch(target, side_effect=AssertionError('offline GUI boundary: ' + target))
            self.patches.append(item)
            self.boundary_patches[target] = item
        self.boundaries = {}
        for item in self.patches:
            result = item.start()
            for name, boundary in self.boundary_patches.items():
                if boundary is item:
                    self.boundaries[name] = result

    def new_window(self):
        window = self.gui.MainWindow()
        self.windows.append(window)
        self.stop_timers(window)
        return window

    @staticmethod
    def stop_timers(window):
        window._timer.stop()
        for timer in window.findChildren(QTimer):
            timer.stop()

    def destroy_window(self, window):
        if sip.isdeleted(window):
            return
        self.stop_timers(window)
        def busy():
            workers = [window._poll_worker, window._action_worker, *window._api_jobs]
            return window._polling or any(w is not None and w.isRunning() for w in workers)
        deadline = time.monotonic() + 5
        while busy() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.case.assertFalse(busy(), 'GUI 测试请求线程未清理')
        window._quitting = True
        window.close()
        window.deleteLater()
        # processEvents 单独使用时不保证处理 DeferredDelete；须在屏障解除前确定销毁。
        QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)
        self.case.assertTrue(sip.isdeleted(window), 'GUI 测试窗口尚未销毁')
        self.app.processEvents()

    def assert_no_side_effects(self):
        for name, boundary in self.boundaries.items():
            self.case.assertEqual(boundary.call_count, 0, 'GUI 测试触及隔离屏障：' + name)

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assert_no_side_effects()

    def close(self):
        try:
            for window in self.windows:
                self.destroy_window(window)
            self.app.processEvents()
            self.assert_no_side_effects()
        finally:
            for item in reversed(self.patches):
                item.stop()
            if self.temp is not None:
                self.temp.cleanup()
