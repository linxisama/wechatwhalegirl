"""控制台「日志三件套」的离线回归测试（需要 PyQt6，缺了整类跳过）。

覆盖：
1. 报错条与运行记录卡上的按钮都在、文案是中文、绑定到了对应回调；
2. 报错条可以鼠标选中复制，另有「复制报错」把时间戳一起带上剪贴板；
3. 导出 TXT 带 BOM、导出诊断包真有 report.txt 和日志；
4. 后台日志里新出现的 traceback 只追加一次（增量偏移），界面刷新后不丢。
"""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QPushButton
    import wechat_gui
    HAVE_QT = True
except Exception:  # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False


def _app():
    # 必须持有模块级引用：QApplication 被回收后再造 QWidget 会直接终止进程
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


_APP = None

ERROR_LOG = (
    "2026-10-26 17:04:11,123 [MainThread] INFO 启动完成\n"
    "2026-10-26 17:04:12,456 [MainThread] ERROR 发送失败：窗口未找到\n"
    "Traceback (most recent call last):\n"
    '  File "src/wechat_sender.py", line 10, in send\n'
    '    raise ValueError("boom")\n'
    "ValueError: boom\n"
)


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class ConsoleLogToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "logs").mkdir()
        # 界面里的日志目录跟工程根走，先把 ROOT 指到临时目录，别碰真实日志
        self._root_patch = patch.object(wechat_gui, "ROOT", self.root)
        self._root_patch.start()
        self.tab = wechat_gui.ConsoleTab()

    def tearDown(self):
        self.tab._tail_timer.stop()
        self.tab.deleteLater()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _save_dialog(self, target: Path):
        fake = Mock()
        fake.getSaveFileName.return_value = (str(target), "")
        return patch.object(wechat_gui, "QFileDialog", fake)

    # ── 按钮与文案（验收标准 7）────────────────────────────────────────
    def test_buttons_live_in_console_with_chinese_labels(self):
        expected = {
            "btn_copy_notice": "复制报错",
            "btn_copy_events": "复制全部",
            "btn_export_txt": "导出 TXT",
            "btn_export_bundle": "导出诊断包",
            "btn_open_bundle": "打开所在目录",
        }
        all_buttons = self.tab.findChildren(QPushButton)
        for attr, label in expected.items():
            button = getattr(self.tab, attr)
            self.assertEqual(button.text(), label)
            self.assertIn(button, all_buttons)
            # 中文文案，除固定后缀 TXT 外不夹英文
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in label), label)
            self.assertTrue(all((not ch.isascii()) or ch in " TXT" for ch in label), label)
        self.assertEqual(self.tab.btn_open_logs.text(), "打开后台日志目录")

    def test_notice_label_is_mouse_selectable(self):
        flags = self.tab.notice.textInteractionFlags()
        self.assertTrue(flags & Qt.TextInteractionFlag.TextSelectableByMouse)

    # ── 复制（验收标准：时间戳 + 报错纯文本）───────────────────────────
    def test_copy_notice_puts_timestamp_and_text_on_clipboard(self):
        self.tab.set_notice("发送失败：微信窗口未找到", ok=False)
        clipboard = Mock()
        fake_app = Mock()
        fake_app.clipboard.return_value = clipboard
        with patch.object(wechat_gui, "QApplication", fake_app):
            self.tab._copy_notice()
        text = clipboard.setText.call_args.args[0]
        self.assertRegex(text, r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ")
        self.assertIn("发送失败：微信窗口未找到", text)
        self.assertEqual(self.tab.copy_hint.text(), "已复制报错")
        # 复制不能把报错条自己覆盖掉
        self.assertIn("发送失败：微信窗口未找到", self.tab.notice.text())

    def test_copy_events_puts_whole_record_on_clipboard(self):
        self.tab.evt_view.setPlainText("记录第一行\n记录第二行")
        clipboard = Mock()
        fake_app = Mock()
        fake_app.clipboard.return_value = clipboard
        with patch.object(wechat_gui, "QApplication", fake_app):
            self.tab._copy_events()
        self.assertIn("记录第二行", clipboard.setText.call_args.args[0])
        self.assertEqual(self.tab.copy_hint.text(), "已复制运行记录")

    # ── 导出 TXT（验收标准 4）──────────────────────────────────────────
    def test_export_txt_writes_bom_with_notice_text(self):
        self.tab.set_notice("发送失败：微信窗口未找到", ok=False)
        self.tab.evt_view.setPlainText("运行记录一行")
        target = self.root / "运行记录-20261026-1704.txt"
        with self._save_dialog(target):
            self.tab._export_events_txt()
        raw = target.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # 记事本直接打开不乱码
        text = raw.decode("utf-8-sig")
        self.assertIn("发送失败：微信窗口未找到", text)
        self.assertIn("运行记录一行", text)
        self.assertIn("已导出", self.tab.copy_hint.text())

    def test_export_txt_failure_is_reported_not_silent(self):
        # 写不进去（这里拿目录当文件）必须红字说明原因，不能假装成功
        target = self.root / "这是个目录"
        target.mkdir()
        with self._save_dialog(target):
            self.tab._export_events_txt()
        self.assertIn("导出失败", self.tab.notice.text())

    # ── 导出诊断包（验收标准 3）────────────────────────────────────────
    def test_export_bundle_packs_report_and_logs(self):
        (self.root / "logs" / "backend.log").write_text(ERROR_LOG, encoding="utf-8")
        auto = self.root / "wechatauto_logs"
        auto.mkdir()
        (auto / "app_20261026.log").write_text("自动发送日志\n", encoding="utf-8")
        self.tab.set_notice("发送失败：微信窗口未找到", ok=False)
        self.tab.evt_view.setPlainText("运行记录一行")
        target = self.root / "诊断包-20261026-1704.zip"
        with self._save_dialog(target):
            self.tab._export_bundle()
        self.assertEqual(Path(self.tab._last_bundle), target)
        with zipfile.ZipFile(target) as bundle:
            names = bundle.namelist()
            report = bundle.read("report.txt").decode("utf-8-sig")
        self.assertIn("report.txt", names)
        self.assertIn("logs/backend.log", names)
        self.assertIn("wechatauto_logs/app_20261026.log", names)
        self.assertIn("发送失败：微信窗口未找到", report)
        self.assertIn("运行记录一行", report)
        self.assertIn(str(target), self.tab.notice.text())

    def test_open_bundle_folder_uses_last_export_dir(self):
        self.tab._last_bundle = self.root / "导出" / "诊断包.zip"
        started = []
        with patch.object(wechat_gui.os, "startfile", create=True,
                          side_effect=lambda p: started.append(p)):
            self.tab._open_bundle_folder()
        self.assertEqual(started, [str(self.root / "导出")])

    # ── traceback 增量显示（验收标准 5 的界面侧）───────────────────────
    def test_poll_appends_each_error_block_once(self):
        log = self.root / "logs" / "backend.log"
        log.write_text(ERROR_LOG, encoding="utf-8")
        self.tab.evt_view.clear()
        self.tab._error_blocks = []
        self.tab._error_shown = 0
        self.tab._log_offset = 0

        self.tab._poll_log_errors()
        shown = self.tab.evt_view.toPlainText()
        self.assertIn("Traceback (most recent call last)", shown)
        self.assertIn("ValueError: boom", shown)
        self.assertEqual(shown.count("ValueError: boom"), 1)

        self.tab._poll_log_errors()  # 没有新日志：不重复追加
        self.assertEqual(self.tab.evt_view.toPlainText().count("ValueError: boom"), 1)

        with log.open("a", encoding="utf-8") as handle:
            handle.write("2026-10-26 17:09:00,000 [Thread-1] ERROR 第二次失败\n")
        self.tab._poll_log_errors()
        text = self.tab.evt_view.toPlainText()
        self.assertIn("第二次失败", text)
        self.assertEqual(text.count("ValueError: boom"), 1)

    def test_refresh_keeps_error_traceback_visible(self):
        (self.root / "logs" / "backend.log").write_text(ERROR_LOG, encoding="utf-8")
        self.tab._log_offset = 0
        self.tab._error_blocks = []
        self.tab._error_shown = 0
        self.tab._poll_log_errors()
        # refresh 每 3 秒用 setHtml 重置正文，堆栈不能因此消失
        self.tab.refresh({"events": [{"time": 0, "text": "一条记录"}]})
        text = self.tab.evt_view.toPlainText()
        self.assertIn("一条记录", text)
        self.assertIn("ValueError: boom", text)
        self.assertEqual(text.count("ValueError: boom"), 1)

    def test_untrusted_angle_brackets_render_as_literal_text(self):
        self.tab.refresh({'messages': [{'text': '<img src="private.png"> & 你好',
            'room': '<room>', 'sender': '<sender>', 'reply': '<b>原文</b>', 'error': '<module>'}],
            'events': [{'time': 1, 'text': '<module> & <b>日志原文</b>'}]})
        self.assertIn('<img src="private.png">', self.tab.msg_view.toPlainText())
        self.assertIn('<module>', self.tab.msg_view.toPlainText())
        self.assertIn('<b>日志原文</b>', self.tab.evt_view.toPlainText())

    def test_refresh_preserves_selection_and_catches_up_after_deselection(self):
        from PyQt6.QtGui import QTextCursor
        first = {'time': 1, 'text': '选中这条记录'}
        self.tab.refresh({'events': [first]})
        cursor = self.tab.evt_view.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        self.tab.evt_view.setTextCursor(cursor)
        selected = cursor.selectedText()
        self.tab.refresh({'events': [first, {'time': 2, 'text': '刷新期间的新记录'}]})
        self.assertEqual(self.tab.evt_view.textCursor().selectedText(), selected)
        cursor = self.tab.evt_view.textCursor()
        cursor.clearSelection()
        self.tab.evt_view.setTextCursor(cursor)
        self.assertIn('刷新期间的新记录', self.tab.evt_view.toPlainText())

    def test_search_level_filter_and_clear_only_display(self):
        log = self.root / 'logs/backend.log'
        log.write_text(ERROR_LOG, encoding='utf-8')
        self.tab._poll_log_errors()
        event = {'time': 1, 'text': '普通记录的暗号'}
        self.tab.refresh({'events': [event]})
        self.tab.log_search.setText('暗号')
        self.assertIn('普通记录的暗号', self.tab.evt_view.toPlainText())
        self.assertNotIn('ValueError', self.tab.evt_view.toPlainText())
        self.tab.log_search.clear()
        self.tab.log_level.setCurrentIndex(self.tab.log_level.findData('ERROR'))
        self.assertIn('ValueError', self.tab.evt_view.toPlainText())
        self.assertNotIn('普通记录的暗号', self.tab.evt_view.toPlainText())
        original = log.read_bytes()
        self.tab._clear_log_display()
        self.tab.refresh({'events': [event]})
        self.tab._poll_log_errors()
        self.assertEqual(self.tab.evt_view.toPlainText(), '暂无记录')
        self.assertEqual(log.read_bytes(), original)

    def test_partial_traceback_is_updated_without_duplicate_error(self):
        log = self.root / 'logs/backend.log'
        log.write_text('2026-10-27 12:00:00,000 [MainThread] ERROR 分段堆栈\nTraceback (most recent call last):\n', encoding='utf-8')
        self.tab._poll_log_errors()
        with log.open('a', encoding='utf-8') as handle:
            handle.write('  File "x.py", line 1, in <module>\nValueError: split\n')
        self.tab._poll_log_errors()
        self.assertEqual(self.tab.evt_view.toPlainText().count('分段堆栈'), 1)
        self.assertIn('ValueError: split', self.tab.evt_view.toPlainText())

    def test_full_backend_log_can_search_info_send_phases_and_filter_errors(self):
        log = self.root / 'logs/backend.log'
        log.write_text(ERROR_LOG + '2026-10-27 12:00:00,000 [MainThread] INFO send phase=check_chat who=测试 len=3\n', encoding='utf-8')
        self.tab._poll_log_errors()
        self.tab.log_source.setCurrentIndex(self.tab.log_source.findData('backend'))
        self.tab.log_search.setText('phase=check_chat')
        self.assertIn('phase=check_chat', self.tab.evt_view.toPlainText())
        self.assertNotIn('ValueError', self.tab.evt_view.toPlainText())
        self.tab.log_search.clear()
        self.tab.log_level.setCurrentIndex(self.tab.log_level.findData('ERROR'))
        self.assertIn('ValueError', self.tab.evt_view.toPlainText())
        self.assertNotIn('phase=check_chat', self.tab.evt_view.toPlainText())

    def test_pause_scroll_keeps_position_when_new_events_arrive(self):
        self.tab.resize(1100, 700)
        self.tab.show()
        events = [{'time': i, 'text': f'记录{i}\n内容\n内容'} for i in range(80)]
        self.tab.refresh({'events': events})
        self._app.processEvents()
        self.tab.log_pause.setChecked(True)
        bar = self.tab.evt_view.verticalScrollBar()
        bar.setValue(bar.maximum() // 3)
        before = bar.value()
        self.tab.refresh({'events': events + [{'time': 81, 'text': '新记录'}]})
        self.assertEqual(bar.value(), before)
        self.tab.hide()


if __name__ == "__main__":
    unittest.main()
