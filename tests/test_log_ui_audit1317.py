"""增量日志和导出目标保护的真实缺陷回归；仅临时日志/离屏界面。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.diagnostics_bundle import build_bundle, write_text_with_bom
from src.logging_setup import read_new_errors


COMMON_TAIL = ('Traceback (most recent call last):\n'
               '  File "same.py", line 9, in same\nValueError: shared message\n')


def error(second, message):
    return f'2026-10-27 12:00:{second:02d},000 [MainThread] ERROR {message}\n' + COMMON_TAIL


class LogTailAuditTests(unittest.TestCase):
    def test_equal_length_middle_rewrite_with_identical_edge_anchors_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'backend.log'
            prefix = '2026-10-27 12:00:00,000 [MainThread] INFO ' + 'common prefix ' * 15 + '\n'
            old, new = prefix + error(0, 'old error'), prefix + error(0, 'new error')
            self.assertEqual(old[:64], new[:64])
            self.assertEqual(old[-64:], new[-64:])
            log.write_text(old, 'utf-8')
            first, cursor = read_new_errors(log)
            old_stat = log.stat()
            log.write_text(new, 'utf-8')
            os.utime(log, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000_000))
            second, cursor = read_new_errors(log, cursor)
            self.assertIn('new error', '\n'.join(second))
            self.assertNotEqual(first[0].record_id, second[-1].record_id)
            self.assertEqual(read_new_errors(log, cursor)[0], [])

    def test_touch_without_content_change_does_not_repeat_the_error(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'backend.log'
            log.write_text(error(0, 'unchanged error'), 'utf-8')
            _, cursor = read_new_errors(log)
            generation = cursor.generation
            old_stat = log.stat()
            os.utime(log, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000_000))
            blocks, cursor = read_new_errors(log, cursor)
            self.assertEqual(blocks, [])
            self.assertEqual(cursor.generation, generation)

    def test_append_fingerprinting_reads_only_new_content_after_initial_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'backend.log'
            log.write_text('2026-10-27 12:00:00,000 [MainThread] INFO ' + 'x' * 300_000 + '\n' +
                           error(0, 'initial error'), 'utf-8')
            _, cursor = read_new_errors(log)
            appended = error(1, 'appended error').encode()
            with log.open('ab') as writer:
                writer.write(appended)
            opened = Path.open
            read_bytes = []

            class CountedReader:
                def __init__(self, handle):
                    self.handle = handle
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self.handle.close()
                def __getattr__(self, name):
                    return getattr(self.handle, name)
                def read(self, count=-1):
                    data = self.handle.read(count)
                    read_bytes.append(len(data))
                    return data

            def counted_open(path, *args, **kwargs):
                handle = opened(path, *args, **kwargs)
                return CountedReader(handle) if path == log else handle

            with patch.object(Path, 'open', counted_open):
                blocks, cursor = read_new_errors(log, cursor)
            self.assertIn('appended error', '\n'.join(blocks))
            self.assertLessEqual(sum(read_bytes), len(appended) * 2 + 512)

    def test_equal_length_rewrite_with_identical_traceback_tail_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'backend.log'
            old, new = error(0, 'old error'), error(1, 'new error')
            self.assertEqual(len(old), len(new))
            self.assertEqual(old[-64:], new[-64:])
            log.write_text(old, 'utf-8')
            first, cursor = read_new_errors(log)
            log.write_text(new, 'utf-8')
            second, cursor = read_new_errors(log, cursor)
            self.assertIn('new error', '\n'.join(second))
            self.assertNotEqual(first[0].record_id, second[-1].record_id)
            self.assertEqual(read_new_errors(log, cursor)[0], [])

    def test_appended_short_prefix_and_split_utf8_do_not_start_a_new_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'backend.log'
            log.write_bytes(b'')
            _, cursor = read_new_errors(log)
            data = error(0, '中文分段错误').encode()
            seen = {}
            for end in range(1, len(data)+1):
                log.write_bytes(data[:end])
                blocks, cursor = read_new_errors(log, cursor, max_bytes=9)
                seen.update((block.record_id, str(block)) for block in blocks)
            self.assertEqual(len(seen), 1)
            self.assertNotIn('\ufffd', '\n'.join(seen.values()))
            self.assertTrue(all(block_id[1] == 0 for block_id in seen))


class ExportTargetAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local = patch.dict(os.environ, {'LOCALAPPDATA':self.tmp.name})
        self.local.start()
        (self.root/'logs').mkdir()
        self.log = self.root/'logs/backend.log'
        self.log.write_text(error(0, 'keep original'), 'utf-8')
        self.original = self.log.read_bytes()

    def tearDown(self):
        self.local.stop()
        self.tmp.cleanup()

    def test_bundle_cannot_overwrite_one_of_its_source_logs(self):
        with self.assertRaises(OSError):
            build_bundle(self.log, root=self.root)
        self.assertEqual(self.log.read_bytes(), self.original)

    def test_bundle_rejects_a_hardlink_alias_of_a_source_log(self):
        alias = self.root/'chosen-export.zip'
        try:
            os.link(self.log, alias)
        except OSError:
            self.skipTest('文件系统不支持测试用硬链接')
        with self.assertRaises(OSError):
            build_bundle(alias, root=self.root)
        self.assertEqual(self.log.read_bytes(), self.original)

    def test_txt_with_known_root_cannot_overwrite_an_input_log(self):
        with self.assertRaises(OSError):
            write_text_with_bom(self.log, 'replacement', root=self.root)
        self.assertEqual(self.log.read_bytes(), self.original)


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PyQt6.QtWidgets import QApplication
    import wechat_gui
    HAVE_QT = True
except Exception:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, '需要 PyQt6')
class LogUIAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_clear_then_equal_length_rewrite_shows_the_new_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'logs').mkdir()
            log = root/'logs/backend.log'
            log.write_text(error(0, 'old error'), 'utf-8')
            with patch.object(wechat_gui, 'ROOT', root):
                tab = wechat_gui.ConsoleTab()
                tab._tail_timer.stop()
                try:
                    tab._clear_log_display()
                    log.write_text(error(1, 'new error'), 'utf-8')
                    tab._poll_log_errors()
                    self.assertIn('new error', tab.evt_view.toPlainText())
                    self.assertNotIn('old error', tab.evt_view.toPlainText())
                finally:
                    tab.deleteLater()

    def test_txt_dialog_chosen_log_reports_failure_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'logs').mkdir()
            log = root/'logs/backend.log'
            log.write_text(error(0, 'keep original'), 'utf-8')
            original = log.read_bytes()
            with patch.object(wechat_gui, 'ROOT', root):
                tab = wechat_gui.ConsoleTab()
                tab._tail_timer.stop()
                try:
                    dialog = Mock()
                    dialog.getSaveFileName.return_value = (str(log), '')
                    with patch.object(wechat_gui, 'QFileDialog', dialog):
                        tab._export_events_txt()
                    self.assertEqual(log.read_bytes(), original)
                    self.assertIn('导出失败', tab.notice.text())
                finally:
                    tab.deleteLater()
