"""1.3.85 发送记录解析：只解析日志尾部，不碰真日志、不起后台。"""
import pathlib
import tempfile
import unittest

from src import send_history as sh

PHASE = ('2026-11-02 18:43:01,123 [MainThread] INFO send phase=guia_input '
         'who=西莲寺紫… len=19 reason= channel=guia pid=16444')
FAILED = ('2026-11-02 18:43:05,456 [MainThread] ERROR send failed channel=guia reason=send_failed '
          'who=西莲寺紫… len=19 title=西莲寺紫… hint=请核对聊天记录；输入框里的残留草稿需要手动清空 pid=16444')
TIMING_GUEST = '2026-11-02 18:43:06,000 [MainThread] INFO send timing guest total_ms=4514 receive_ms=100 agent_ms=8860'
TIMING_WORKER = '2026-11-02 18:43:06,100 [MainThread] INFO send timing worker spawn_ms=120 rpc_ms=30 guest_total_ms=4514'


class ParseTests(unittest.TestCase):
    def test_phase_line(self):
        row = sh.parse_line(PHASE)
        self.assertEqual(row['kind'], 'phase')
        self.assertEqual(row['ts'], '2026-11-02 18:43:01')
        self.assertEqual(row['phase'], 'guia_input')
        self.assertEqual(row['who'], '西莲寺紫…')
        self.assertEqual(row['len'], 19)
        self.assertEqual(row['reason'], '')
        self.assertEqual(row['channel'], 'guia')
        self.assertEqual(row['pid'], 16444)

    def test_failed_line_carries_text_and_advice(self):
        row = sh.parse_line(FAILED)
        self.assertEqual(row['kind'], 'failed')
        self.assertEqual(row['reason'], 'send_failed')
        self.assertEqual(row['text'], '「坐标+OCR」发送失败')
        self.assertEqual(row['advice'], '请核对聊天记录；输入框里的残留草稿需要手动清空')
        self.assertEqual(row['title'], '西莲寺紫…')
        self.assertEqual(row['foreground'], '')

    def test_timing_guest_and_worker(self):
        guest = sh.parse_line(TIMING_GUEST)
        self.assertEqual((guest['kind'], guest['scope']), ('timing', 'guest'))
        self.assertEqual(guest['total_ms'], 4514)
        self.assertEqual(guest['metrics']['agent_ms'], 8860)
        worker = sh.parse_line(TIMING_WORKER)
        self.assertEqual((worker['kind'], worker['scope']), ('timing', 'worker'))
        self.assertEqual(worker['total_ms'], 4514)
        self.assertIn('spawn_ms', worker['metrics'])

    def test_non_send_lines_ignored(self):
        for line in ('2026-11-02 18:43:01,123 [MainThread] INFO 普通日志',
                     '2026-11-02 18:43:01,123 [MainThread] INFO sending phase=1',
                     '', None, 123, 'send phase='):
            self.assertIsNone(sh.parse_line(line))

    def test_bad_numbers_become_zero(self):
        row = sh.parse_line('2026-11-02 18:43:01,123 [MainThread] INFO send phase=guia_input who=甲 len=abc reason= pid=xyz')
        self.assertEqual(row['len'], 0)
        self.assertEqual(row['pid'], 0)

    def test_describe_and_hint(self):
        self.assertEqual(sh.describe('blocked_by_window'), '微信窗口被其他程序遮挡')
        self.assertEqual(sh.describe('new_code'), 'new_code')
        self.assertEqual(sh.describe(''), '')
        self.assertEqual(sh.describe(None), '')
        self.assertIn('手动点开', sh.hint_for('target_not_open'))
        self.assertEqual(sh.hint_for('new_code'), '')

    def test_name_stays_masked(self):
        row = sh.parse_line(PHASE.replace('西莲寺紫…', '西莲寺紫'))
        self.assertEqual(row['who'], '西莲寺紫')  # 解析层不做任何还原或再打码


class RecentTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name) / 'backend.log'

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_returns_empty(self):
        self.assertEqual(sh.recent(self.path), [])

    def test_orders_and_limits(self):
        lines = [PHASE.replace('18:43:01', '18:43:%02d' % (i % 60)) for i in range(300)]
        self.path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        rows = sh.recent(self.path, 5)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row['kind'] == 'phase' for row in rows))
        self.assertEqual([row['ts'][-2:] for row in rows], ['55', '56', '57', '58', '59'])

    def test_reads_tail_without_whole_file_read(self):
        big = '\n'.join([PHASE] * 5000) + '\n' + FAILED + '\n'
        self.path.write_text(big, encoding='utf-8')
        original = pathlib.Path.read_text
        def boom(*args, **kwargs):
            raise AssertionError('不该整文件读取')
        pathlib.Path.read_text = boom
        try:
            rows = sh.recent(self.path, 10)
        finally:
            pathlib.Path.read_text = original
        self.assertTrue(rows)
        self.assertEqual(rows[-1]['kind'], 'failed')

    def test_limits_are_clamped(self):
        self.path.write_text('\n'.join([PHASE] * 5) + '\n', encoding='utf-8')
        self.assertEqual(len(sh.recent(self.path, 1)), 1)
        self.assertEqual(len(sh.recent(self.path, 0)), 5)
        self.assertEqual(len(sh.recent(self.path, 999)), 5)


class SummarizeTests(unittest.TestCase):
    def test_counts_codes_and_last(self):
        rows = sh.parse_lines([PHASE, FAILED, FAILED.replace('send_failed', 'blocked_by_window'), TIMING_GUEST])
        summary = sh.summarize(rows)
        self.assertEqual(summary['counts'], {'phases': 1, 'failures': 2, 'timings': 1})
        self.assertEqual(summary['failure_codes'], {'send_failed': 1, 'blocked_by_window': 1})
        self.assertEqual(summary['last_failure']['reason'], 'blocked_by_window')
        self.assertEqual(summary['last_phase']['phase'], 'guia_input')
        self.assertEqual(summary['last_timing']['scope'], 'guest')

    def test_empty(self):
        summary = sh.summarize([])
        self.assertEqual(summary['counts'], {'phases': 0, 'failures': 0, 'timings': 0})
        self.assertIsNone(summary['last_failure'])
        self.assertIsNone(summary['last_phase'])
        self.assertEqual(summary['failure_codes'], {})


if __name__ == '__main__':
    unittest.main()
