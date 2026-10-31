"""统一日志内核的离线回归测试：幂等、全局兜底钩子、错误块提取与增量读取。

这些用例全部在临时目录里跑，不碰工程真实的 logs/backend.log。
"""

import logging
import os
import sys
import tempfile
import threading
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src import logging_setup

SAMPLE_LOG = (
    "2026-10-26 17:04:11,123 [MainThread] INFO 启动完成\n"
    "2026-10-26 17:04:12,456 [MainThread] ERROR 发送失败：窗口未找到\n"
    "Traceback (most recent call last):\n"
    '  File "src/wechat_sender.py", line 10, in send\n'
    '    raise ValueError("boom")\n'
    "ValueError: boom\n"
    "2026-10-26 17:04:13,789 [MainThread] INFO 继续轮询\n"
)


def flush_handlers():
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


class LoggingSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        root_logger = logging.getLogger()
        self._saved_handlers = list(root_logger.handlers)
        self._saved_level = root_logger.level
        self._hooks = None

    def tearDown(self):
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            if handler not in self._saved_handlers:
                root_logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        for handler in self._saved_handlers:
            if handler not in root_logger.handlers:
                root_logger.addHandler(handler)
        root_logger.setLevel(self._saved_level)
        self._restore_hooks()
        self.tmp.cleanup()

    # ── 全局钩子：装了就一定要还原，否则会污染同进程里其它测试 ──────────────
    def _install_hooks(self):
        self._hooks = (sys.excepthook, getattr(threading, "excepthook", None))
        logging_setup._hooks_installed = False
        return logging_setup.install_excepthook()

    def _restore_hooks(self):
        if not self._hooks:
            return
        sys.excepthook = self._hooks[0]
        if self._hooks[1] is not None:
            threading.excepthook = self._hooks[1]
        self._hooks = None
        logging_setup._hooks_installed = False

    # ── 统一 logging 配置（验收标准 1）──────────────────────────────────
    def test_repeated_setup_keeps_exactly_one_handler(self):
        first = logging_setup.setup_logging(self.root / "logs")
        second = logging_setup.setup_logging(self.root / "logs")
        self.assertEqual(first, second)
        handlers = [h for h in logging.getLogger().handlers
                    if isinstance(h, RotatingFileHandler)]
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        self.assertEqual(os.path.normcase(handler.baseFilename),
                         os.path.normcase(str(second)))
        self.assertEqual(handler.maxBytes, logging_setup.LOG_MAX_BYTES)
        self.assertEqual(handler.backupCount, logging_setup.LOG_BACKUP_COUNT)
        self.assertIn("%(threadName)s", handler.formatter._fmt)
        self.assertIn("%(asctime)s", handler.formatter._fmt)
        self.assertIn("%(levelname)s", handler.formatter._fmt)
        self.assertTrue(logging_setup.is_backend_log_configured(self.root / "logs"))

    def test_switching_folder_leaves_single_handler(self):
        logging_setup.setup_logging(self.root / "a")
        target = logging_setup.setup_logging(self.root / "b")
        handlers = [h for h in logging.getLogger().handlers
                    if isinstance(h, RotatingFileHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(os.path.normcase(handlers[0].baseFilename),
                         os.path.normcase(str(target)))
        self.assertFalse(logging_setup.is_backend_log_configured(self.root / "a"))

    def test_logger_writes_to_backend_log(self):
        target = logging_setup.setup_logging(self.root / "logs")
        logging.getLogger(logging_setup.RUNTIME_LOGGER_NAME).info("一条运行日志")
        flush_handlers()
        text = target.read_text(encoding="utf-8")
        self.assertIn("INFO 一条运行日志", text)
        self.assertIn("[MainThread]", text)

    def test_listener_log_restores_handler_cleared_by_dependency(self):
        from src.runtime_logs import RuntimeLog
        runtime_log = RuntimeLog(self.root / 'logs')
        removed = list(logging.getLogger().handlers)
        logging.getLogger().handlers.clear()
        try:
            runtime_log.write('依赖导入后仍保留监听记录')
            flush_handlers()
            self.assertIn('依赖导入后仍保留监听记录', runtime_log.path.read_text('utf-8'))
        finally:
            for handler in removed:
                if handler not in logging.getLogger().handlers:
                    handler.close()

    def test_runtime_log_restores_handler_removed_by_driver(self):
        from src.runtime_logs import RuntimeLog
        runtime_log = RuntimeLog(self.root / 'logs')
        root = logging.getLogger()
        detached = list(root.handlers)
        root.handlers.clear()
        try:
            runtime_log.write('驱动导入后的运行记录仍应落盘')
            self.assertIn('驱动导入后的运行记录仍应落盘', runtime_log.path.read_text('utf-8'))
        finally:
            for handler in detached:
                if handler not in self._saved_handlers:
                    handler.close()

    # ── 全局未捕获异常落盘（验收标准 2）────────────────────────────────
    def test_main_excepthook_writes_full_traceback(self):
        target = logging_setup.setup_logging(self.root / "logs")
        self.assertTrue(self._install_hooks())
        try:
            try:
                raise RuntimeError("测试用的未捕获异常")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
            flush_handlers()
        finally:
            self._restore_hooks()
        text = target.read_text(encoding="utf-8")
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("RuntimeError: 测试用的未捕获异常", text)
        self.assertIn("未捕获异常（主线程）", text)

    def test_thread_excepthook_writes_full_traceback(self):
        target = logging_setup.setup_logging(self.root / "logs")
        self._install_hooks()
        try:
            def boom():
                raise ValueError("线程里炸了")

            worker = threading.Thread(target=boom, name="测试线程")
            worker.start()
            worker.join()
            flush_handlers()
        finally:
            self._restore_hooks()
        text = target.read_text(encoding="utf-8")
        self.assertIn("ValueError: 线程里炸了", text)
        self.assertIn("未捕获异常（线程 测试线程）", text)

    def test_install_excepthook_is_idempotent(self):
        self.assertTrue(self._install_hooks())
        installed = sys.excepthook
        logging_setup._hooks_installed = True
        self.assertFalse(logging_setup.install_excepthook())
        self.assertIs(sys.excepthook, installed)

    def test_record_exception_never_raises(self):
        target = logging_setup.setup_logging(self.root / "logs")
        text = logging_setup.record_exception(RuntimeError("手工记录"), message="槽函数兜底")
        self.assertIn("RuntimeError: 手工记录", text)
        # 传进来的不是异常：必须安静返回空串，绝不能二次抛错
        self.assertEqual(logging_setup.record_exception("这不是一个异常对象"), "")
        flush_handlers()
        self.assertIn("槽函数兜底", target.read_text(encoding="utf-8"))

    # ── 错误块提取（验收标准 5）────────────────────────────────────────
    def test_extract_error_blocks_keeps_traceback(self):
        blocks = logging_setup.extract_error_blocks(SAMPLE_LOG)
        self.assertEqual(len(blocks), 1)
        self.assertIn("发送失败：窗口未找到", blocks[0])
        self.assertIn("Traceback (most recent call last)", blocks[0])
        self.assertIn("raise ValueError", blocks[0])
        self.assertIn("ValueError: boom", blocks[0])
        self.assertNotIn("继续轮询", blocks[0])

    def test_extract_error_blocks_without_error_is_empty(self):
        only_info = "2026-10-26 17:04:11,123 [MainThread] INFO 一切正常\n"
        self.assertEqual(logging_setup.extract_error_blocks(only_info), [])
        self.assertEqual(logging_setup.extract_error_blocks(""), [])

    def test_read_new_errors_is_incremental(self):
        log = self.root / "logs" / "backend.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(SAMPLE_LOG, encoding="utf-8")

        blocks, offset = logging_setup.read_new_errors(log, 0)
        self.assertEqual(len(blocks), 1)
        self.assertIn("ValueError: boom", blocks[0])
        self.assertEqual(offset, log.stat().st_size)

        again, same_offset = logging_setup.read_new_errors(log, offset)
        self.assertEqual(again, [])
        self.assertEqual(same_offset, offset)

        with log.open("a", encoding="utf-8") as handle:
            handle.write("2026-10-26 17:05:00,000 [Thread-1] ERROR 第二次失败\n")
        third, final_offset = logging_setup.read_new_errors(log, same_offset)
        self.assertEqual(len(third), 1)
        self.assertIn("第二次失败", third[0])
        self.assertGreater(final_offset, same_offset)

    def test_read_new_errors_missing_file_is_quiet(self):
        missing = self.root / "nope" / "backend.log"
        self.assertEqual(logging_setup.read_new_errors(missing, 7), ([], 7))

    def test_error_traceback_split_across_polls_keeps_record_identity(self):
        log = self.root / 'backend.log'
        log.write_text('2026-10-27 12:00:00,000 [MainThread] ERROR 失败\nTraceback (most recent call last):\n', encoding='utf-8')
        first, cursor = logging_setup.read_new_errors(log)
        with log.open('a', encoding='utf-8') as handle:
            handle.write('  File "probe.py", line 1, in <module>\nValueError: 中文异常\n')
        second, cursor = logging_setup.read_new_errors(log, cursor)
        self.assertEqual(first[0].record_id, second[0].record_id)
        self.assertIn('Traceback', second[0])
        self.assertIn('ValueError: 中文异常', second[0])
        self.assertEqual(logging_setup.read_new_errors(log, cursor)[0], [])

    def test_rotation_reads_old_tail_and_new_file_even_when_new_file_is_larger(self):
        log = self.root / 'backend.log'
        log.write_text('2026-10-27 12:00:00,000 [MainThread] INFO start\n', encoding='utf-8')
        _, cursor = logging_setup.read_new_errors(log)
        with log.open('a', encoding='utf-8') as handle:
            handle.write('2026-10-27 12:00:01,000 [MainThread] ERROR 轮换前最后错误\n')
        log.rename(self.root / 'backend.log.1')
        log.write_text('2026-10-27 12:00:02,000 [MainThread] ERROR 新文件第一条错误\n' + 'detail\n' * 40, encoding='utf-8')
        blocks, cursor = logging_setup.read_new_errors(log, cursor)
        self.assertIn('轮换前最后错误', '\n'.join(blocks))
        self.assertIn('新文件第一条错误', '\n'.join(blocks))
        self.assertEqual(logging_setup.read_new_errors(log, cursor)[0], [])

    def test_bounded_reads_never_skip_backlog_or_split_utf8(self):
        log = self.root / 'backend.log'
        log.write_bytes(b'')
        _, cursor = logging_setup.read_new_errors(log)
        text = ''.join(f'2026-10-27 12:00:{i:02d},000 [MainThread] ERROR 错误编号{i}\n' for i in range(10))
        log.write_text(text, encoding='utf-8')
        seen = {}
        for _ in range(100):
            blocks, cursor = logging_setup.read_new_errors(log, cursor, max_bytes=23)
            seen.update((block.record_id, str(block)) for block in blocks)
            if int(cursor) == log.stat().st_size:
                break
        self.assertEqual(len(seen), 10)
        self.assertNotIn('\ufffd', '\n'.join(seen.values()))

    def test_truncate_and_regrow_is_detected_by_anchor(self):
        log = self.root / 'backend.log'
        log.write_text('2026-10-27 12:00:00,000 [MainThread] INFO first\n', encoding='utf-8')
        _, cursor = logging_setup.read_new_errors(log)
        log.write_text('2026-10-27 12:00:01,000 [MainThread] ERROR 改写后的第一行错误\n' + 'detail\n' * 30, encoding='utf-8')
        blocks, _ = logging_setup.read_new_errors(log, cursor)
        self.assertIn('改写后的第一行错误', '\n'.join(blocks))


if __name__ == "__main__":
    unittest.main()
