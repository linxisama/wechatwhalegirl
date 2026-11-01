"""维护类对话框的线程亲和回归（1.3.73）。

真机症状：在后台线程里直接回调 on_done 更新控件，Qt 会打印
QObject: Cannot create children for a parent that is in a different thread，
严重时对话框直接崩。修复后所有回调都经 _ThreadCallback 的队列回到 GUI 线程。
"""

import os
import time
import unittest
from pathlib import Path

try:
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _pump(condition, timeout=3.0):
    app = _app()
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


STATE = {
    "success": True, "configured": "wxid_a_ab12", "logged_in": "wxid_b_cd34",
    "configured_name": "鲸鱼娘", "logged_in_name": "西莲寺紫", "match": "different",
    "hint": "微信登录的是「西莲寺紫」，机器人账号是「鲸鱼娘」。",
    "can_rebind": True, "account_dir": "wxid_b_cd34", "nickname": "西莲寺紫",
    "accounts": [{"account": "wxid_a_ab12", "name": "鲸鱼娘"}],
}


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class DialogThreadAffinityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        from src.gui import maintenance_dialogs
        cls.md = maintenance_dialogs

    def _guard(self, get=None, post=None):
        return self.md.AccountGuardDialog(
            None,
            get or (lambda path, timeout=3: dict(STATE)),
            post or (lambda path, data=None, timeout=10: {"success": True, "message": "已改绑"}),
        )

    def test_deliver_defers_until_drain(self):
        dialog = self._guard()
        seen = []
        dialog._deliver(seen.append, {"ok": True})
        self.assertEqual(seen, [])
        self.assertEqual(len(dialog._deliveries), 1)
        dialog._drain_deliveries()
        self.assertEqual(seen, [{"ok": True}])
        self.assertEqual(dialog._deliveries, [])

    def test_refresh_does_not_touch_widgets_from_worker(self):
        dialog = self._guard()
        initial = dialog.state_label.text()
        dialog.refresh()
        # 回调还没被搬到 GUI 线程，控件必须保持原样（之前这里会被后台线程直接改）
        self.assertEqual(dialog.state_label.text(), initial)
        self.assertTrue(_pump(lambda: dialog.state_label.text() != initial))
        self.assertIn("不一致", dialog.state_label.text())
        self.assertTrue(dialog.btn_rebind.isEnabled())

    def test_failing_service_still_returns_to_gui_thread(self):
        def boom(path, timeout=3):
            raise RuntimeError("后端没起来")

        dialog = self._guard(get=boom)
        initial = dialog.state_label.text()
        dialog.refresh()
        self.assertEqual(dialog.state_label.text(), initial)
        self.assertTrue(_pump(lambda: dialog.state_label.text() != initial))
        self.assertTrue(dialog.btn_refresh.isEnabled())

    def test_drain_is_idempotent_without_deliveries(self):
        dialog = self._guard()
        dialog._drain_deliveries()
        self.assertEqual(dialog._deliveries, [])

    def test_all_three_dialogs_use_deferred_delivery(self):
        path = Path(self.md.__file__)
        text = path.read_text("utf-8")
        self.assertIn("class _ThreadCallback(QDialog):", text)
        for name in ("DshConnectDialog", "PluginPacksDialog", "AccountGuardDialog"):
            self.assertIn("class %s(_ThreadCallback):" % name, text)
        self.assertEqual(text.count("self._deliver("), 3)
        self.assertNotIn("on_done(result)", text)
