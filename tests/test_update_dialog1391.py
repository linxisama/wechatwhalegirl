"""1.3.91 更新对话框「高级设置」与控制台 24 小时周期重查的离线回归测试。

三件在打包后才容易坏的事，钉在这里：
1. 高级设置默认收起，展开后四个控件必须取到服务端初值（status 里没有
   interval_hours 键，只能从 last_check_at / next_check_at 反推）；
2. 保存必须把 feed_url / auto_check / auto_download / interval_hours 四个参数
   一起交给 configure，漏一个后端就当没改；
3. 启动那次自动检查之外还要有一条 24 小时的**重复**定时器 —— 以前是永久的
   一次性标记，第二次自动检查永远不会来；downloaded 状态也要有提示，且同一
   版本只提示一次。

全部走假服务对象与假 call_api，不联网、不起后台。
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover - 环境缺 PyQt6 时整类跳过
    HAVE_QT = False

if HAVE_QT:
    from src.gui.update_dialog import UpdateDialog
    from tests.gui_offline_helpers1336 import OfflineGuiIsolation

_APP = None


def _app():
    # 必须持有模块级引用：QApplication 被回收后再建 QWidget 会直接崩进程
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


# 一份"完整状态快照"：两次检查相隔 12 小时，用来验证间隔反推。
BASE = {
    'state': 'idle', 'current': '1.3.91', 'latest': '', 'notes': '',
    'progress': {'received': 0, 'total': 0, 'percent': 0}, 'error': None,
    'feed_url': 'https://example.com/update.json', 'auto': True,
    'auto_download': False,
    'last_check_at': 1000.0, 'next_check_at': 1000.0 + 12 * 3600,
}


class FakeService:
    """鸭子类型的更新服务：只实现对话框真正用到的两个方法。"""

    def __init__(self, status=None):
        self._status = dict(status or {})
        self.configured = []

    def status(self):
        return dict(self._status)

    def configure(self, **kwargs):
        self.configured.append(dict(kwargs))
        self._status.update({
            'feed_url': kwargs.get('feed_url') or '',
            'auto': bool(kwargs.get('auto_check')),
            'auto_download': bool(kwargs.get('auto_download')),
        })
        return dict(self._status, ok=True)


@unittest.skipUnless(HAVE_QT, '需要 PyQt6')
class AdvancedSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.service = FakeService(BASE)
        self.dialog = UpdateDialog(None, self.service)

    def tearDown(self):
        self.dialog._poll.stop()
        self.dialog.close()
        self.dialog.deleteLater()
        _app().processEvents()

    def test_advanced_section_is_collapsed_by_default(self):
        self.assertFalse(self.dialog.advanced_toggle.isChecked())
        self.assertTrue(self.dialog.advanced_panel.isHidden())
        self.assertEqual(self.dialog.advanced_toggle.arrowType(), Qt.ArrowType.RightArrow)
        self.assertTrue(self.dialog.settings_hint.isHidden())

    def test_expanding_fills_the_four_controls_from_status(self):
        self.dialog.advanced_toggle.setChecked(True)
        self.assertFalse(self.dialog.advanced_panel.isHidden())
        self.assertEqual(self.dialog.advanced_toggle.arrowType(), Qt.ArrowType.DownArrow)
        self.assertEqual(self.dialog.feed_edit.text(), BASE['feed_url'])
        self.assertTrue(self.dialog.auto_check_box.isChecked())
        self.assertFalse(self.dialog.auto_download_box.isChecked())
        self.assertEqual(self.dialog.interval_spin.value(), 12)
        self.assertEqual(self.dialog.interval_spin.suffix(), ' 小时')

    def test_save_passes_all_four_settings_to_configure(self):
        self.dialog.advanced_toggle.setChecked(True)
        self.dialog.feed_edit.setText('https://cdn.example.com/feed.json')
        self.dialog.auto_check_box.setChecked(False)
        self.dialog.auto_download_box.setChecked(True)
        self.dialog.interval_spin.setValue(6)
        self.dialog.save_settings_button.click()
        self.assertEqual(self.service.configured, [{
            'feed_url': 'https://cdn.example.com/feed.json',
            'auto_check': False,
            'auto_download': True,
            'interval_hours': 6,
        }])
        self.assertEqual(self.dialog.settings_hint.text(), '设置已保存')
        self.assertFalse(self.dialog.settings_hint.isHidden())
        self.assertTrue(self.dialog.error_label.isHidden())

    def test_missing_auto_download_key_does_not_break_the_dialog(self):
        status = dict(BASE)
        status.pop('auto_download')
        service = FakeService(status)
        dialog = UpdateDialog(None, service)
        try:
            self.assertTrue(dialog.auto_download_box.isChecked())
            dialog.advanced_toggle.setChecked(True)
            self.assertTrue(dialog.auto_download_box.isChecked())
        finally:
            dialog._poll.stop()
            dialog.close()
            dialog.deleteLater()

    def test_interval_falls_back_to_24_without_timestamps(self):
        status = dict(BASE)
        status.pop('last_check_at')
        status.pop('next_check_at')
        service = FakeService(status)
        dialog = UpdateDialog(None, service)
        try:
            self.assertEqual(dialog.interval_spin.value(), 24)
        finally:
            dialog._poll.stop()
            dialog.close()
            dialog.deleteLater()

    def test_polling_does_not_overwrite_what_the_user_typed(self):
        self.dialog.advanced_toggle.setChecked(True)
        # textEdited 只在真人输入时发出（setText 不发），这里手动补一次，
        # 等价于"用户敲进去了"，用来验证轮询不会把它改回去。
        self.dialog.feed_edit.setText('https://typing.example.com/feed.json')
        self.dialog.feed_edit.textEdited.emit('https://typing.example.com/feed.json')
        self.dialog.interval_spin.setValue(3)
        self.dialog.refresh(BASE)
        self.assertEqual(self.dialog.feed_edit.text(), 'https://typing.example.com/feed.json')
        self.assertEqual(self.dialog.interval_spin.value(), 3)

    def test_partial_status_does_not_clear_the_feed_url(self):
        self.dialog.advanced_toggle.setChecked(True)
        self.dialog.refresh({'ok': True})
        self.assertEqual(self.dialog.feed_edit.text(), BASE['feed_url'])


@unittest.skipUnless(HAVE_QT, '需要 PyQt6')
class RecheckTimerTests(unittest.TestCase):
    """控制台侧的 24 小时重复检查：定时器存在，且闸门只挡本轮。"""

    @classmethod
    def setUpClass(cls):
        _app()
        import wechat_gui
        cls.gui = wechat_gui

    def setUp(self):
        self._isolation = OfflineGuiIsolation(self, self.gui, _APP)

    def tearDown(self):
        self._isolation.close()

    def test_recheck_timer_is_a_24_hour_repeating_timer(self):
        window = self._isolation.new_window()
        timer = getattr(window, '_update_recheck_timer', None)
        self.assertIsNotNone(timer, '缺少 24 小时周期重查定时器')
        self.assertEqual(timer.interval(), 86400000)
        self.assertFalse(timer.isSingleShot(), '周期重查必须能重复触发')
        # 首次检查仍是 9 秒单发，两条定时器分工不同
        self.assertTrue(window._update_timer.isSingleShot())
        self.assertEqual(window._update_timer.interval(), 9000)

    def test_second_cycle_checks_again_and_notices_once_per_version(self):
        window = self._isolation.new_window()
        window.show()
        notices = []
        window.console_tab.set_notice = lambda text, ok=True, **kw: notices.append((text, ok))
        pending = []

        def fake_call(owner, method, path, data=None, timeout=10, on_done=None):
            pending.append(on_done)
            return None

        with patch.object(self.gui, 'call_api', fake_call):
            window._auto_check_update()
            self.assertEqual(len(pending), 1, '首次自动检查没有发起')
            pending[0]({'ok': True, 'status': {'state': 'available', 'latest': '1.3.91'}})
            self.assertEqual(len(notices), 1)
            self.assertIn('发现新版本 1.3.91', notices[0][0])

            # 同一个版本再来一次：闸门已放开（会重新查），但提示不重复刷屏
            window._auto_check_update()
            self.assertEqual(len(pending), 2, '第二次自动检查被一次性标记挡住了')
            pending[1]({'ok': True, 'status': {'state': 'available', 'latest': '1.3.91'}})
            self.assertEqual(len(notices), 1)

            # 走到 downloaded：必须提示可以安装（不能拿 available 的标记去重）
            window._auto_check_update()
            pending[2]({'ok': True, 'status': {'state': 'downloaded', 'latest': '1.3.91'}})
            self.assertEqual(len(notices), 2)
            self.assertIn('已下载并通过 SHA256 校验', notices[1][0])
            self.assertTrue(notices[1][1])

            window._auto_check_update()
            pending[3]({'ok': True, 'status': {'state': 'downloaded', 'latest': '1.3.91'}})
            self.assertEqual(len(notices), 2, 'downloaded 提示也必须按版本去重')


if __name__ == '__main__':
    unittest.main()
