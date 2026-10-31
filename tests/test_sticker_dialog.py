"""表情库窗口的接线测试：按钮点下去调的是哪个接口、参数对不对。

不发真实 HTTP：把 `wechat_gui.call_api` 换成记录器，验证界面这一层的行为
（接口本身的正确性由 tests/test_sticker_library.py 用真实 FastAPI 应用覆盖）。
"""

import base64
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication, QMessageBox

import wechat_gui as gui
from tests.gui_offline_helpers1336 import OfflineGuiIsolation


def thumbnail():
    buffer = io.BytesIO()
    Image.new('RGB', (100, 100), (20, 160, 120)).save(buffer, 'PNG')
    return base64.b64encode(buffer.getvalue()).decode()


PAGE = {'items': [{'id': 'a' * 64, 'label': '开心', 'description': '笑得很开心',
                   'uses': 2, 'thumbnail': thumbnail()},
                  {'id': 'b' * 64, 'label': '疑惑', 'description': '歪头', 'uses': 0,
                   'thumbnail': thumbnail()}],
        'total': 2, 'page': 1, 'pages': 1, 'collecting': False}


class StickerDialogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app = QApplication.instance() or QApplication([])
        self.calls = []

        def fake_call(owner, method, path, data=None, timeout=10, on_done=None):
            self.calls.append({'method': method, 'path': path, 'data': data})
            return None

        self._patcher = patch.object(gui, 'call_api', side_effect=fake_call)
        self._patcher.start()
        self._isolation = OfflineGuiIsolation(self, gui, self.app, root=Path(self._tmp.name))
        self.window = self._isolation.new_window()
        self.dialog = gui.StickerLibraryDialog(self.window)
        self.calls.clear()          # 构造时的那次 GET 不算

    def tearDown(self):
        try:
            self.dialog.close()
            self._isolation.close()
        finally:
            self._patcher.stop()
            self._tmp.cleanup()

    def last(self):
        return self.calls[-1] if self.calls else {}

    def test_columns_and_initial_state(self):
        self.assertEqual([self.dialog.table.horizontalHeaderItem(i).text() for i in range(4)],
                         ['预览', '标签', '图片含义', '使用次数'])
        self.assertFalse(self.dialog.collect_box.isChecked())
        for button in (self.dialog.save_button, self.dialog.recognize_button,
                       self.dialog.delete_button):
            self.assertFalse(button.isEnabled())

    def test_refresh_uses_search_and_page(self):
        self.dialog.search.setText('开心')
        self.dialog.search_now()
        self.assertEqual(self.last()['method'], 'GET')
        self.assertEqual(self.last()['path'], 'stickers?q=%E5%BC%80%E5%BF%83&page=1')

    def test_rows_render_and_selection_drives_buttons(self):
        self.dialog._fill(PAGE)
        self.assertEqual(self.dialog.table.rowCount(), 2)
        self.assertEqual(self.dialog.page_label.text(), '1/1 页 · 共 2 张')
        self.assertEqual(self.dialog.table.item(0, 1).text(), '开心')
        self.assertEqual(self.dialog.table.item(0, 3).text(), '2')
        self.assertEqual(self.dialog.table.currentRow(), 0)
        self.assertTrue(self.dialog.save_button.isEnabled())
        self.dialog.table.selectRow(1)
        self.assertEqual(self.dialog.label.text(), '疑惑')

    def test_save_posts_label_and_description(self):
        self.dialog._fill(PAGE)
        self.dialog.table.selectRow(1)
        self.dialog.label.setText('超疑惑')
        self.dialog.description.setPlainText('歪着头看不懂')
        self.dialog.save()
        self.assertEqual(self.last()['method'], 'POST')
        self.assertEqual(self.last()['path'], 'stickers/' + 'b' * 64)
        self.assertEqual(self.last()['data'], {'label': '超疑惑', 'description': '歪着头看不懂'})

    def test_recognize_uses_a_long_timeout_and_restores_buttons(self):
        self.dialog._fill(PAGE)
        self.dialog.recognize()
        self.assertEqual(self.last()['path'], 'stickers/' + 'a' * 64 + '/recognize')
        self.assertIn('正在识别', self.dialog.notice.text())
        # 识别期间按钮禁用，回来之后恢复
        self.assertFalse(self.dialog.recognize_button.isEnabled())
        self.dialog._recognize_done({'success': True, 'message': '识别完成，已更新图片说明'})
        self.assertTrue(self.dialog.recognize_button.isEnabled())
        self.assertIn('识别完成', self.dialog.notice.text())

    def test_delete_asks_first(self):
        self.dialog._fill(PAGE)
        with patch.object(gui.QMessageBox, 'question',
                          return_value=QMessageBox.StandardButton.No) as asked:
            self.dialog.remove()
        asked.assert_called_once()
        self.assertEqual(self.calls, [])
        with patch.object(gui.QMessageBox, 'question',
                          return_value=QMessageBox.StandardButton.Yes):
            self.dialog.remove()
        self.assertEqual(self.last()['method'], 'DELETE')
        self.assertEqual(self.last()['path'], 'stickers/' + 'a' * 64)

    def test_collect_toggle_posts_plugin_setting_and_reverts_on_failure(self):
        self.dialog.collect_box.setChecked(True)
        self.assertEqual(self.last()['method'], 'POST')
        self.assertEqual(self.last()['path'], 'plugins/sticker_steal')
        self.assertEqual(self.last()['data'], {'enabled': True})
        # 保存失败要把勾选拨回去，并且说清原因
        self.dialog._collect_done({'success': False, 'message': '后台没起来'})
        self.assertFalse(self.dialog.collect_box.isChecked())
        self.assertIn('没保存成功', self.dialog.notice.text())
        self.assertIn('后台没起来', self.dialog.notice.text())

    def test_read_failure_shows_the_reason_not_a_bare_message(self):
        self.dialog._fill({'success': False, 'message': '后台还没起来'})
        self.assertIn('后台还没起来', self.dialog.notice.text())

    def test_plugin_cards_offer_the_library_entry(self):
        for plugin_id in ('sticker_admin', 'sticker_steal', 'image_library'):
            self.assertIn(plugin_id, gui.STICKER_LIBRARY_PLUGINS)
        self.assertTrue(self.window.plugin_tab.btn_sticker_library.text())
        self.assertTrue(hasattr(self.window.plugin_tab, 'open_sticker_library'))
