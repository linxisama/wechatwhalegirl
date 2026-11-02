"""1.3.83 记忆写入守卫 + token 账本的单元测试。

守卫是 report-only：命中可疑特征只写 JSONL / 只回计数，绝不改 text、绝不删记忆。
账本是估算口径：中文字符按 1 token、ASCII 按 4 字符 1 token，标 source 区分 dsh/local。
"""
import json
import os
import sqlite3
import tempfile
import time
import unittest

from src import memory_guard as mg
from src import token_ledger as tl
from src.chat_state import ChatState


def _lines(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read().splitlines()


class MemoryGuardVerdictTests(unittest.TestCase):
    def test_secret_is_flagged(self):
        result = mg.inspect('密码是 abc12345', [])
        self.assertEqual(result['verdict'], 'warn')
        self.assertIn('SECRET', result['codes'])

    def test_plain_fact_is_ok(self):
        result = mg.inspect('我住在杭州西湖区', [])
        self.assertEqual(result['verdict'], 'ok', result['codes'])
        self.assertEqual(result['codes'], [])

    def test_relative_time_is_flagged(self):
        result = mg.inspect('我们上周去了三亚', [])
        self.assertIn('RELATIVE_TIME', result['codes'])

    def test_diary_smell_needs_length_and_first_person(self):
        long_diary = ('我今天心情很好，但是也有点累，晚上又睡不着觉，想找个人随便聊聊天说说话，'
                     '顺便把这一周的事情都说一遍，说完大概就会好受一些了')
        self.assertIn('DIARY_SMELL', mg.inspect(long_diary, [])['codes'])
        short = '我开心'
        self.assertNotIn('DIARY_SMELL', mg.inspect(short, [])['codes'])

    def test_duplicate_points_at_existing_row(self):
        base = '我住在杭州市西湖区文三路'
        result = mg.inspect(base, [{'id': 'm1', 'content': base}])
        self.assertIn('DUP', result['codes'])
        self.assertEqual(result['duplicate_of'], 'm1')
        self.assertGreaterEqual(result['similarity'], mg.DUP_THRESHOLD)

    def test_different_content_is_not_duplicate(self):
        result = mg.inspect('我住在杭州西湖区', [{'id': 'm2', 'content': '她喜欢喝拿铁咖啡'}])
        self.assertNotIn('DUP', result['codes'])

    def test_too_vague_only_for_very_short_text(self):
        self.assertIn('TOO_VAGUE', mg.inspect('好的', [])['codes'])
        self.assertNotIn('TOO_VAGUE', mg.inspect('他养了一只橘猫', [])['codes'])

    def test_mode_off_reports_nothing(self):
        result = mg.inspect('密码是 abc12345', [], {'mode': 'off'})
        self.assertEqual(result['verdict'], 'ok')
        self.assertEqual(result['codes'], [])
        self.assertEqual(result['mode'], 'off')

    def test_inspect_never_raises_on_junk(self):
        for value in (None, '', 123, {'a': 1}):
            for existing in (None, (), [None, 123, {'id': 'x'}], ['纯字符串']):
                result = mg.inspect(value, existing)
                self.assertIn(result['verdict'], ('ok', 'warn'))

    def test_secret_is_masked_in_report(self):
        masked = mg.mask_secret('key sk-abcdefgh1234')
        self.assertNotIn('sk-abcdefgh1234', masked)
        self.assertIn('***', masked)

    def test_config_bad_type_falls_back_to_report(self):
        self.assertEqual(mg.inspect('好的', [], 'not-a-dict')['mode'], 'report')


class MemoryGuardLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'memory-guard.jsonl')

    def test_note_appends_jsonl_with_timestamp(self):
        self.assertTrue(mg.note(self.path, {'codes': ['SECRET'], 'text': '***'}, now=1700000000.0))
        row = json.loads(_lines(self.path)[0])
        self.assertEqual(row['ts'], 1700000000.0)
        self.assertEqual(row['codes'], ['SECRET'])

    def test_note_returns_false_when_path_unusable(self):
        self.assertFalse(mg.note(self.tmp, {'codes': []}))
        self.assertFalse(mg.note(None, {'codes': []}))

    def test_summary_counts_codes_and_skips_broken_lines(self):
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('{"codes":["SECRET"],"ts":1}\n')
            handle.write('not json\n')
            handle.write('{"codes":[],"ts":2}\n')
        out = mg.summary(self.path)
        self.assertEqual(out['total'], 2)
        self.assertEqual(out['warn'], 1)
        self.assertEqual(out['codes'], {'SECRET': 1})
        self.assertEqual(out['last_ts'], 2)

    def test_summary_missing_file_is_empty(self):
        out = mg.summary(os.path.join(self.tmp, 'nope.jsonl'))
        self.assertEqual((out['total'], out['warn'], out['codes']), (0, 0, {}))

    def test_recent_returns_newest_first_and_respects_limit(self):
        mg.note(self.path, {'codes': ['A']}, now=1.0)
        mg.note(self.path, {'codes': ['B']}, now=2.0)
        rows = mg.recent(self.path, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['codes'], ['B'])


class TokenLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'token-usage.jsonl')

    def test_estimate_tokens_mixes_cjk_and_ascii(self):
        self.assertEqual(tl.estimate_tokens(''), 0)
        self.assertEqual(tl.estimate_tokens('abcd'), 1)
        self.assertEqual(tl.estimate_tokens('你好'), 2)
        self.assertEqual(tl.estimate_tokens('你好abcd'), 3)

    def test_path_for_uses_wechat_only_dir(self):
        path = str(tl.path_for(self.tmp))
        self.assertTrue(path.endswith(os.path.join('data', 'wechat-only', 'token-usage.jsonl')))

    def test_record_round_marks_source(self):
        local = tl.record_round(self.tmp, {'id': 'm1', 'kind': 'private', 'text': '你好'}, '你好呀',
                                prompt_text='系统提示', model='local-model')
        self.assertEqual(local['source'], 'local')
        self.assertEqual(local['model'], 'local-model')
        dsh = tl.record_round(self.tmp, {'id': 'm2', 'kind': 'private', '_dsh_generated': True}, '收到',
                              prompt_text='系统提示', model='DSH/x')
        self.assertEqual(dsh['source'], 'dsh')
        lines = _lines(str(tl.path_for(self.tmp)))
        self.assertEqual(len(lines), 2)

    def test_record_round_never_raises(self):
        # 没有 root 时写不进去（path_for 返回 None），但绝不能抛异常，也要回一行可读账目。
        entry = tl.record_round(None, None, None)
        self.assertEqual(entry['source'], 'local')
        self.assertEqual(entry['prompt_tokens'], 0)
        self.assertEqual(tl.record_round(self.tmp, {}, '')['source'], 'local')

    def test_summarize_splits_by_source_and_days(self):
        now = time.time()
        tl.record_round(self.tmp, {'_dsh_generated': True}, 'a', prompt_text='千字', now=now)
        tl.record_round(self.tmp, {}, 'b', now=now - 15 * 86400)
        week = tl.summarize(str(tl.path_for(self.tmp)), days=7, now=now)
        month = tl.summarize(str(tl.path_for(self.tmp)), days=30, now=now)
        self.assertEqual(week['rounds'], 1)
        self.assertEqual(week['by_source'], {'dsh': 1})
        self.assertEqual(month['rounds'], 2)

    def test_summarize_missing_file_is_empty(self):
        out = tl.summarize(os.path.join(self.tmp, 'none.jsonl'))
        self.assertEqual((out['rounds'], out['prompt_tokens'], out['reply_tokens']), (0, 0, 0))


class ChatStateGuardWiringTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.state = ChatState(self.db)
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'memory-guard.jsonl')

    def tearDown(self):
        self.db.close()

    def test_no_guard_path_writes_nothing(self):
        digest = self.state.remember('private|a|private:b', 'u1', '我们上周去了三亚', 'manual')
        self.assertTrue(digest)
        self.assertFalse(os.path.exists(self.path))

    def test_warn_is_reported_but_memory_still_saved(self):
        self.state.guard_path = self.path
        text = '我们上周去了三亚'
        self.state.remember('private|a|private:b', 'u1', text, 'manual')
        rows = self.db.execute('SELECT content,status FROM chat_memories').fetchall()
        self.assertEqual(rows[0][0], text)
        report = [json.loads(line) for line in _lines(self.path)]
        self.assertEqual(len(report), 1)
        self.assertIn('RELATIVE_TIME', report[0]['codes'])
        self.assertEqual(report[0]['subject_tail'], 'u1')

    def test_clean_fact_writes_no_report(self):
        self.state.guard_path = self.path
        self.state.remember('private|a|private:b', 'u1', '他养了一只橘猫', 'manual')
        self.assertFalse(os.path.exists(self.path))

    def test_broken_guard_path_does_not_block_remember(self):
        self.state.guard_path = self.tmp
        digest = self.state.remember('private|a|private:b', 'u1', '我们上周去了三亚', 'manual')
        self.assertTrue(digest)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM chat_memories').fetchone()[0], 1)

    def test_guard_off_mode_skips(self):
        self.state.guard_path = self.path
        self.state.guard_config = {'mode': 'off'}
        self.state.remember('private|a|private:b', 'u1', '我们上周去了三亚', 'manual')
        self.assertFalse(os.path.exists(self.path))


if __name__ == '__main__':
    unittest.main()
