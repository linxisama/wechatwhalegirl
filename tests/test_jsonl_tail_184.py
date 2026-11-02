"""1.3.84 尾部/流式读取：大 JSONL 不再整文件载入内存，语义与原实现一致。"""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from src import jsonl_tail, memory_guard, token_ledger


def _write(path, rows):
    with open(path, 'w', encoding='utf-8', newline='') as handle:
        for row in rows:
            handle.write(row + '\n')
    return path


class JsonlTailTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='jsonl184-')

    def path(self, name='a.jsonl'):
        return os.path.join(self.dir, name)

    def test_iter_lines_basic_and_crlf(self):
        target = _write(self.path(), ['one', 'two', 'three'])
        self.assertEqual(list(jsonl_tail.iter_lines(target)), ['one', 'two', 'three'])
        crlf = self.path('b.jsonl')
        with open(crlf, 'wb') as handle:
            handle.write('甲\r\n乙\r\n'.encode('utf-8'))
        self.assertEqual(list(jsonl_tail.iter_lines(crlf)), ['甲', '乙'])

    def test_iter_lines_without_trailing_newline_and_empty_file(self):
        target = self.path('c.jsonl')
        with open(target, 'w', encoding='utf-8') as handle:
            handle.write('last line')
        self.assertEqual(list(jsonl_tail.iter_lines(target)), ['last line'])
        empty = self.path('d.jsonl')
        open(empty, 'w', encoding='utf-8').close()
        self.assertEqual(list(jsonl_tail.iter_lines(empty)), [])

    def test_iter_lines_missing_path_never_raises(self):
        self.assertEqual(list(jsonl_tail.iter_lines(None)), [])
        self.assertEqual(list(jsonl_tail.iter_lines(self.path('nope.jsonl'))), [])
        self.assertEqual(list(jsonl_tail.iter_lines(self.dir)), [])

    def test_iter_lines_survives_chunk_boundary_inside_multibyte(self):
        rows = [f'第{i}行中文内容' for i in range(40)]
        target = _write(self.path('e.jsonl'), rows)
        for chunk in (1, 2, 3, 7, 16):
            self.assertEqual(list(jsonl_tail.iter_lines(target, chunk_bytes=chunk)), rows)

    def test_tail_lines_keeps_last_rows_in_order(self):
        rows = [f'row{i}' for i in range(50)]
        target = _write(self.path('f.jsonl'), rows)
        self.assertEqual(jsonl_tail.tail_lines(target, 5), rows[-5:])
        self.assertEqual(jsonl_tail.tail_lines(target, 0), [])
        self.assertEqual(jsonl_tail.tail_lines(target, 999), rows)
        self.assertEqual(jsonl_tail.tail_lines(None, 5), [])

    def test_token_summarize_matches_old_semantics_without_reading_whole_file(self):
        now = time.time()
        target = self.path('token-usage.jsonl')
        rows = []
        for i in range(3000):
            rows.append(json.dumps({'ts': now - i * 60, 'prompt_tokens': 10, 'reply_tokens': 20,
                                    'source': 'dsh' if i % 2 else 'local'}, ensure_ascii=False))
        _write(target, rows + ['not json'])
        with patch('pathlib.Path.read_text', side_effect=AssertionError('不该整文件读取')):
            result = token_ledger.summarize(target, days=7, now=now)
        self.assertEqual(result['rounds'], 3000)
        self.assertEqual(result['prompt_tokens'], 30000)
        self.assertEqual(result['reply_tokens'], 60000)
        self.assertEqual(result['by_source'], {'dsh': 1500, 'local': 1500})

    def test_token_summarize_skips_old_rows_and_bad_paths(self):
        now = time.time()
        target = self.path('t2.jsonl')
        rows = [json.dumps({'ts': now - 86400 * 30, 'prompt_tokens': 1, 'reply_tokens': 1}),
                json.dumps({'ts': now, 'prompt_tokens': 4, 'reply_tokens': 5})]
        _write(target, rows)
        result = token_ledger.summarize(target, days=7, now=now)
        self.assertEqual((result['rounds'], result['prompt_tokens']), (1, 4))
        self.assertEqual(token_ledger.summarize(self.path('missing.jsonl'), days=7, now=now)['rounds'], 0)
        self.assertEqual(token_ledger.summarize(None)['rounds'], 0)

    def test_guard_summary_and_recent_without_reading_whole_file(self):
        target = self.path('memory-guard.jsonl')
        rows = [json.dumps({'ts': 1000 + i, 'codes': ['DUP'] if i % 3 == 0 else [],
                            'scope': 'private', 'text': f'第{i}条'}, ensure_ascii=False) for i in range(500)]
        _write(target, ['bad'] + rows)  # 坏行放最前，保证最近 200 行是干净的 200 条
        with patch('pathlib.Path.read_text', side_effect=AssertionError('不该整文件读取')):
            summary = memory_guard.summary(target, limit=200)
            items = memory_guard.recent(target, limit=3)
        self.assertEqual(summary['total'], 200)
        self.assertEqual(summary['codes'], {'DUP': 67})
        self.assertEqual(summary['warn'], 67)
        self.assertEqual([item['ts'] for item in items], [1499, 1498, 1497])
        self.assertEqual(memory_guard.summary(None)['total'], 0)
        self.assertEqual(memory_guard.recent(None), [])
