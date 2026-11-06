"""1.3.92 日志优化：轮转上限、重复 handler、落盘脱敏、增量读取与结构化索引。

全部在临时目录里跑：不碰工程真实的 logs/backend.log，也不改既有测试的断言。
"""
import json
import logging
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from src import logging_setup

HEADER = '2026-11-07 12:00:{second:02d},000 [MainThread] ERROR {text}\n'


def flush_handlers():
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


def tagged(root):
    return [h for h in root.handlers if getattr(h, logging_setup._HANDLER_TAG, None)]


class _TempCase(unittest.TestCase):
    """统一的现场保存/还原：root handler、级别与 setup_logging 记住的目标。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.tmp.name)
        root_logger = logging.getLogger()
        self._saved_handlers = list(root_logger.handlers)
        self._saved_level = root_logger.level
        self._saved_target = logging_setup._LAST_TARGET
        self._saved_last_level = logging_setup._LAST_LEVEL
        self._saved_limits = (logging_setup.LOG_MAX_BYTES, logging_setup.LOG_INDEX_MAX_BYTES,
                              logging_setup.LOG_INDEX_KEEP_LINES)

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
        logging_setup._LAST_TARGET = self._saved_target
        logging_setup._LAST_LEVEL = self._saved_last_level
        (logging_setup.LOG_MAX_BYTES, logging_setup.LOG_INDEX_MAX_BYTES,
         logging_setup.LOG_INDEX_KEEP_LINES) = self._saved_limits
        self.tmp.cleanup()


class RotationLimitTests(_TempCase):
    def test_backup_files_stop_at_backup_count(self):
        """轮转上限：写爆多轮后只留 backupCount 份旧文件，磁盘占用有天花板。"""
        logging_setup.LOG_MAX_BYTES = 2048
        target = logging_setup.setup_logging(self.root_dir / 'logs')
        logger = logging.getLogger(logging_setup.RUNTIME_LOGGER_NAME)
        for index in range(600):
            logger.info('填充行 %04d %s', index, 'x' * 60)
        flush_handlers()
        folder = target.parent
        backups = sorted(folder.glob('backend.log.*'))
        self.assertEqual(len(backups), logging_setup.LOG_BACKUP_COUNT)
        self.assertFalse((folder / 'backend.log.6').exists())
        for item in [target] + backups:
            self.assertLessEqual(item.stat().st_size, logging_setup.LOG_MAX_BYTES + 1024)

    def test_rotation_keeps_writing_after_reaching_limit(self):
        """轮转不是"写满就停"：到上限后新内容仍要落在 backend.log 里。"""
        logging_setup.LOG_MAX_BYTES = 1024
        target = logging_setup.setup_logging(self.root_dir / 'logs')
        logger = logging.getLogger(logging_setup.RUNTIME_LOGGER_NAME)
        for index in range(80):
            logger.info('第一轮 %03d %s', index, 'y' * 50)
        logger.error('轮转之后仍然要写进来的错误')
        flush_handlers()
        self.assertIn('轮转之后仍然要写进来的错误', target.read_text(encoding='utf-8'))


class DuplicateHandlerTests(_TempCase):
    def test_concurrent_restore_keeps_single_handler(self):
        """并发恢复：多线程同时发现"没有 handler"时，不许各挂一份把日志写多遍。"""
        target = logging_setup.setup_logging(self.root_dir / 'logs')
        root = logging.getLogger()
        root.handlers.clear()
        barrier = threading.Barrier(8)

        def restore():
            barrier.wait()
            logging_setup.restore_backend_handler()

        workers = [threading.Thread(target=restore) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len(tagged(root)), 1)
        logging.getLogger(logging_setup.RUNTIME_LOGGER_NAME).error('并发恢复只该写一行')
        flush_handlers()
        self.assertEqual(target.read_text(encoding='utf-8').count('并发恢复只该写一行'), 1)

    def test_concurrent_setup_keeps_single_handler(self):
        """并发 setup：同一目录重复配置不能变成"一条日志写两份"。"""
        barrier = threading.Barrier(6)

        def configure(index):
            barrier.wait()
            logging_setup.setup_logging(self.root_dir / f'dir{index % 2}')

        workers = [threading.Thread(target=configure, args=(index,)) for index in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len(tagged(logging.getLogger())), 1)


class RedactionTests(_TempCase):
    def test_credentials_and_phone_numbers_are_masked_on_disk(self):
        """异常钩子与普通记录落盘前都要脱敏：诊断包会整份导出 backend.log。"""
        target = logging_setup.setup_logging(self.root_dir / 'logs')
        secret = 'sk-live-ABCDEFGH12345678'
        phone = '13800138000'
        try:
            raise RuntimeError(f'api_key={secret} token=SECRET-TOKEN-VALUE 手机号 {phone}')
        except RuntimeError:
            logging_setup.record_exception(message='槽函数兜底')
        logging.getLogger(logging_setup.RUNTIME_LOGGER_NAME).error(
            'Authorization: Bearer BEARER-VALUE-ONLY 请求失败')
        flush_handlers()
        text = target.read_text(encoding='utf-8')
        for value in (secret, 'SECRET-TOKEN-VALUE', phone, 'BEARER-VALUE-ONLY'):
            self.assertNotIn(value, text)
        self.assertIn('槽函数兜底', text)
        self.assertIn('Traceback (most recent call last)', text)

    def test_normal_lines_are_not_altered(self):
        """脱敏不能顺手改写正常日志：阶段码、计数与堆栈必须原样保留。"""
        target = logging_setup.setup_logging(self.root_dir / 'logs')
        line = 'send phase=check_chat who=测试群 len=42 reason=- channel=uia pid=1'
        logging.getLogger(logging_setup.RUNTIME_LOGGER_NAME).info(line)
        logging.getLogger(logging_setup.RUNTIME_LOGGER_NAME).error('max_tokens=400 token_count=20')
        flush_handlers()
        text = target.read_text(encoding='utf-8')
        self.assertIn(line, text)
        self.assertIn('max_tokens=400 token_count=20', text)


class IncrementalReadTests(_TempCase):
    def test_truncated_file_does_not_reuse_stale_offset(self):
        """截断后重写：偏移必须回到 0，不能拿旧偏移读错位或漏读。"""
        log = self.root_dir / 'backend.log'
        log.write_text(HEADER.format(second=0, text='旧错误' * 20), encoding='utf-8')
        _, cursor = logging_setup.read_new_errors(log)
        log.write_text(HEADER.format(second=1, text='截断后的新错误'), encoding='utf-8')
        blocks, cursor = logging_setup.read_new_errors(log, cursor)
        self.assertIn('截断后的新错误', '\n'.join(blocks))
        self.assertEqual(logging_setup.read_new_errors(log, cursor)[0], [])

    def test_rotation_then_continue_reading(self):
        """轮转后能接着读：旧文件尾部与新文件都要读到，不能因为换了 inode 就断流。"""
        log = self.root_dir / 'backend.log'
        log.write_text(HEADER.format(second=0, text='第一行'), encoding='utf-8')
        _, cursor = logging_setup.read_new_errors(log)
        with log.open('a', encoding='utf-8') as stream:
            stream.write(HEADER.format(second=1, text='轮换前最后一条错误'))
        log.rename(self.root_dir / 'backend.log.1')
        log.write_text(HEADER.format(second=2, text='新文件第一条错误') + 'detail\n' * 30,
                       encoding='utf-8')
        blocks, cursor = logging_setup.read_new_errors(log, cursor)
        joined = '\n'.join(blocks)
        self.assertIn('轮换前最后一条错误', joined)
        self.assertIn('新文件第一条错误', joined)
        self.assertEqual(logging_setup.read_new_errors(log, cursor)[0], [])

    def test_empty_and_missing_logs_are_quiet(self):
        """空日志、缺文件、目录当文件：一律安静返回，不许抛。"""
        empty = self.root_dir / 'backend.log'
        empty.write_bytes(b'')
        self.assertEqual(logging_setup.read_new_errors(empty), ([], 0))
        self.assertEqual(logging_setup.read_new_errors(self.root_dir / 'nope' / 'backend.log'), ([], 0))
        self.assertEqual(logging_setup.read_new_errors(self.root_dir), ([], 0))
        self.assertEqual(logging_setup.read_log_index(self.root_dir), [])

    def test_invalid_paths_never_raise(self):
        """非法路径（内嵌 NUL）走兜底，不能连累启动与轮询。"""
        bad = '\x00bad\\logs'
        self.assertTrue(str(logging_setup.setup_logging(bad)).endswith('backend.log'))
        self.assertEqual(logging_setup.read_new_errors(bad), ([], 0))
        self.assertEqual(logging_setup.read_log_index(bad), [])
        self.assertIsInstance(logging_setup.redact_for_log(bad), str)


class StructuredIndexTests(_TempCase):
    def _emit(self, count, level=logging.WARNING):
        logger = logging.getLogger('wechat.test.index')
        for index in range(count):
            logger.log(level, '第 %d 条索引消息 %s', index, 'z' * 20)

    def test_index_lines_have_stable_shape(self):
        """索引格式：每行一条 JSON，字段固定为 ts/level/module/msg(+trace)。"""
        logging_setup.setup_logging(self.root_dir / 'logs')
        long_text = '前缀 ' + 'a' * 400
        logging.getLogger('wechat.test.index').warning(
            'trace_id=abc-123 %s', long_text)
        flush_handlers()
        entries = logging_setup.read_log_index(self.root_dir / 'logs')
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(set(entry), {'ts', 'level', 'module', 'msg', 'trace'})
        self.assertRegex(entry['ts'], r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$')
        self.assertEqual(entry['level'], 'WARNING')
        self.assertEqual(entry['module'], 'wechat.test.index')
        self.assertEqual(entry['trace'], 'abc-123')
        self.assertLessEqual(len(entry['msg']), logging_setup.LOG_INDEX_MESSAGE_CHARS)
        self.assertNotIn('\n', entry['msg'])

    def test_index_only_keeps_warning_and_above(self):
        """索引只收 WARNING 以上：INFO 全进索引就又是一份大文件。"""
        logging_setup.setup_logging(self.root_dir / 'logs')
        self._emit(3, level=logging.INFO)
        self._emit(2, level=logging.ERROR)
        flush_handlers()
        entries = logging_setup.read_log_index(self.root_dir / 'logs')
        self.assertEqual([entry['level'] for entry in entries], ['ERROR', 'ERROR'])

    def test_index_is_trimmed_when_over_limit(self):
        """索引超上限会裁剪：只留最近 N 行，且裁剪后仍是合法 JSON 行。"""
        logging_setup.LOG_INDEX_MAX_BYTES = 4096
        logging_setup.LOG_INDEX_KEEP_LINES = 20
        logging_setup.setup_logging(self.root_dir / 'logs')
        self._emit(120, level=logging.ERROR)
        flush_handlers()
        path = logging_setup.log_index_path(self.root_dir / 'logs')
        lines = [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        # 裁剪发生在写满上限那一刻，之后还会继续追加，所以判据是"远少于写入条数"，
        # 且文件大小被压在同一个量级，不会随运行时长无限增长。
        self.assertLess(len(lines), 120)
        self.assertLessEqual(path.stat().st_size, logging_setup.LOG_INDEX_MAX_BYTES * 2)
        self.assertGreaterEqual(len(lines), 1)
        for line in lines:
            self.assertIsInstance(json.loads(line), dict)
        entries = logging_setup.read_log_index(self.root_dir / 'logs')
        self.assertIn('第 119 条索引消息', entries[-1]['msg'])

    def test_index_redacts_secrets(self):
        """索引是 GUI 直接展示的内容，脱敏必须同样生效。"""
        logging_setup.setup_logging(self.root_dir / 'logs')
        logging.getLogger('wechat.test.index').error('api_key=sk-index-ONLY-VALUE 失败')
        flush_handlers()
        entries = logging_setup.read_log_index(self.root_dir / 'logs')
        self.assertTrue(entries)
        self.assertNotIn('sk-index-ONLY-VALUE', json.dumps(entries, ensure_ascii=False))

    def test_concurrent_index_writes_never_produce_half_lines(self):
        """并发追加不产生半行：每个 handler 各自整行 O_APPEND 写入。"""
        path = self.root_dir / 'index.jsonl'
        # 一个 handler、四个线程同时 emit：验证的是"追加是整行原子的"，
        # 不是"多个 handler 写同一路径"（那本来就该写多份）。
        handler = logging_setup._LogIndexHandler(path)
        logger = logging.getLogger('wechat.test.index.concurrent')
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        saved = list(logger.handlers)
        logger.addHandler(handler)
        barrier = threading.Barrier(4)

        def write(worker):
            barrier.wait()
            for index in range(50):
                logger.error('并发 %d-%d %s', worker, index, 'q' * 80)

        workers = [threading.Thread(target=write, args=(index,)) for index in range(4)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        finally:
            logger.removeHandler(handler)
            handler.close()
            for item in saved:
                logger.addHandler(item)
        raw = path.read_text(encoding='utf-8')
        self.assertTrue(raw.endswith('\n'))
        lines = raw.splitlines()
        self.assertEqual(len(lines), 200)
        for line in lines:
            entry = json.loads(line)
            self.assertIn('msg', entry)


if __name__ == '__main__':
    unittest.main()
