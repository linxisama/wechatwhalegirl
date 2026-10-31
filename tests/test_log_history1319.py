"""文件日志日期须与今日运行记录区分；离屏 Qt、临时日志，不操作真实应用。"""
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication
import wechat_gui as gui
from src.logging_setup import ErrorBlock


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 28, 9, 13)


class HistoricalLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'logs').mkdir()
        self.patches = [patch.object(gui, 'ROOT', self.root),
                        patch.object(gui, '_datetime', FixedDatetime, create=True)]
        for item in self.patches:
            item.start()
        self.tab = gui.ConsoleTab()
        self.tab._tail_timer.stop()

    def tearDown(self):
        self.tab.close(); self.tab.deleteLater(); self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    @staticmethod
    def block(day='2026-10-27', body='submission_uncertain', identity=1, level='ERROR'):
        return ErrorBlock(f'{day} 21:04:45,032 [MainThread] {level} {body}',
                          ('file', 0, identity), level)

    def render(self, *blocks):
        self.tab._error_blocks = list(blocks)
        self.tab._render_logs(force=True)
        return self.tab.evt_view.toPlainText()

    def test_today_runtime_record_does_not_make_old_file_errors_look_current(self):
        self.tab._runtime_events = [(FixedDatetime(2026, 10, 28, 9, 13).timestamp(), '今日运行记录')]
        old = self.block('2026-10-26', 'minimized', identity=1)
        recent = self.block('2026-10-27', 'submission_uncertain', identity=2)
        text = self.render(old, recent)
        self.assertIn('历史错误 · 2026-10-26', text)
        self.assertIn('历史错误 · 2026-10-27', text)
        self.assertIn(str(old), text)
        self.assertIn(str(recent), text)
        self.assertLess(text.index('今日运行记录'), text.index('minimized'))
        self.assertLess(text.index('minimized'), text.index('submission_uncertain'))
        self.assertNotIn('完整堆栈', text)
        self.assertEqual(self.tab._error_blocks, [old, recent])
        self.assertEqual(self.tab._cleared_error_ids, set())

    def test_same_day_warning_and_error_are_labeled_today(self):
        text = self.render(self.block('2026-10-28', identity=1),
                           self.block('2026-10-28', 'warning-body', identity=2, level='WARNING'))
        self.assertIn('今日错误 · 2026-10-28', text)
        self.assertIn('今日警告 · 2026-10-28', text)
        self.assertNotIn('历史', text)

    def test_past_warning_keeps_warning_filter_and_identity(self):
        block = self.block(level='WARNING')
        self.render(block)
        self.tab.log_level.setCurrentIndex(self.tab.log_level.findData('WARNING'))
        text = self.tab.evt_view.toPlainText()
        self.assertIn('历史警告 · 2026-10-27', text)
        self.assertIn(str(block), text)
        self.assertEqual(block.record_id, ('file', 0, 1))

    def test_future_date_is_pending_review_not_history_or_today(self):
        text = self.render(self.block('2026-10-29'))
        self.assertIn('2026-10-29', text)
        self.assertIn('待核对', text)
        self.assertNotIn('历史错误', text)
        self.assertNotIn('今日错误', text)

    def test_invalid_date_or_time_is_not_misclassified_as_history(self):
        for line in ('2026-02-30 09:01:00,000 [Main] ERROR invalid-date',
                     '2026-10-26 99:01:00,000 [Main] ERROR invalid-time',
                     'not-a-timestamp ERROR invalid-header'):
            with self.subTest(line=line):
                text = self.render(ErrorBlock(line, ('file', 0, 1)))
                self.assertIn('日期未确认', text)
                self.assertNotIn('历史错误', text)
                self.assertIn(line, text)

    def test_date_inside_bare_traceback_cannot_supply_record_date(self):
        body = 'Traceback (most recent call last):\n  File "2026-10-26.py", line 1\nValueError: body'
        text = self.render(ErrorBlock(body, ('file', 0, 1)))
        self.assertIn('日期未确认', text)
        self.assertNotIn('历史错误', text)
        self.assertIn(body, text)

    def test_clear_retains_watermark_and_new_error_appears_once(self):
        path = self.root / 'logs/backend.log'
        first = '2026-10-27 21:04:45,032 [MainThread] ERROR old-file-error\n'
        path.write_text(first, 'utf-8')
        self.tab._poll_log_errors()
        old_identity = self.tab._error_blocks[0].record_id
        self.assertIn('历史错误 · 2026-10-27', self.tab.evt_view.toPlainText())
        original = path.read_bytes()
        self.tab._clear_log_display()
        self.assertEqual(path.read_bytes(), original)
        self.assertIn(old_identity, self.tab._cleared_error_ids)
        self.tab._poll_log_errors()
        self.assertNotIn('old-file-error', self.tab.evt_view.toPlainText())
        with path.open('a', encoding='utf-8') as stream:
            stream.write('2026-10-28 09:13:00,000 [MainThread] ERROR new-file-error\n')
        self.tab._poll_log_errors(); self.tab._poll_log_errors()
        text = self.tab.evt_view.toPlainText()
        self.assertIn('今日错误 · 2026-10-28', text)
        self.assertEqual(text.count('new-file-error'), 1)
        self.assertNotIn('old-file-error', text)

    def test_backend_full_view_still_uses_unmodified_original_blocks(self):
        block = self.block()
        self.tab._error_blocks = [block]
        self.tab._backend_records = [block]
        self.tab.log_source.setCurrentIndex(self.tab.log_source.findData('backend'))
        self.tab._render_logs(force=True)
        self.assertEqual(self.tab.evt_view.toPlainText(), str(block))

    def test_midnight_changes_caption_without_changing_record_identity(self):
        block = self.block('2026-10-28')
        self.assertIn('今日错误', self.render(block))
        class Tomorrow(FixedDatetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 29, 0, 1)
        with patch.object(gui, '_datetime', Tomorrow):
            self.tab._render_logs(force=True)
        self.assertIn('历史错误 · 2026-10-28', self.tab.evt_view.toPlainText())
        self.assertEqual(block.record_id, ('file', 0, 1))
        self.assertEqual(self.tab._cleared_error_ids, set())


if __name__ == '__main__':
    unittest.main()
