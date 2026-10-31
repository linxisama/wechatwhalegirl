"""定时任务：只做本地检查与本地日报，绝不外发。"""
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from src.agent_scheduler import MAX_INTERVAL, MIN_INTERVAL, AgentScheduler, SchedulerError

ROOT = Path(__file__).resolve().parent.parent


class FakeAI:
    class usage:
        summary = {'request_count': 3, 'total_tokens': 1200,
                   'prompt_tokens': 1000, 'completion_tokens': 200}


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.ai = FakeAI()

    def status(self):
        return {'running': True, 'pending': 2, 'model': 'm', 'plugins': 21}

    def memory_overview(self):
        return {'confirmed': 2, 'manual': 1, 'candidate': 3, 'learned': 96}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = sqlite3.connect(self.dir / 'm.sqlite3')
        self.db.execute('CREATE TABLE inbox(state TEXT, created REAL)')
        self.db.executemany('INSERT INTO inbox VALUES(?,?)',
                            [('sent', time.time()), ('sent', time.time()),
                             ('failed', time.time()), ('expired', time.time())])
        self.db.commit()
        self.bot = FakeBot(self.db)
        self.scheduler = AgentScheduler(self.dir)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_add_validates_action_and_interval(self):
        with self.assertRaises(SchedulerError):
            self.scheduler.add('x', 'send_message')
        with self.assertRaises(SchedulerError):
            self.scheduler.add('x', 'connection_check', MIN_INTERVAL - 1)
        with self.assertRaises(SchedulerError):
            self.scheduler.add('x', 'connection_check', MAX_INTERVAL + 1)

    def test_due_respects_interval_and_flag(self):
        task = self.scheduler.add('t', 'connection_check', 30)
        self.assertTrue(self.scheduler.due(task, now=time.time() + 31 * 60))
        self.scheduler.update(task['id'], enabled=False)
        task = self.scheduler.tasks()[0]
        self.assertFalse(self.scheduler.due(task, now=time.time() + 999 * 60))

    def test_connection_check_is_readonly(self):
        result = self.scheduler.run(self.bot, 'connection_check')
        self.assertIn('连接检查', result)

    def test_digest_is_written_locally(self):
        result = self.scheduler.run(self.bot, 'daily_digest')
        self.assertIn('日报已写入', result)
        files = list((self.dir / 'agent_reports').glob('daily-*.txt'))
        self.assertEqual(len(files), 1)
        text = files[0].read_text('utf-8')
        self.assertIn('已回复：2', text)
        self.assertIn('token 合计：1200', text)
        self.assertIn('不会发送到微信', text)

    def test_tick_runs_due_tasks_once(self):
        task = self.scheduler.add('t', 'connection_check', 5)
        now = time.time()
        executed = self.scheduler.tick(self.bot, now=now + 6 * 60)
        self.assertEqual(len(executed), 1)
        self.assertEqual(self.scheduler.tasks()[0]['last_run'], now + 6 * 60)
        self.assertEqual(self.scheduler.tick(self.bot, now=now + 6 * 60 + 60), [])

    def test_delete(self):
        task = self.scheduler.add('t', 'connection_check')
        self.assertTrue(self.scheduler.delete(task['id']))
        self.assertFalse(self.scheduler.delete(task['id']))

    def test_unknown_action_rejected(self):
        with self.assertRaises(SchedulerError):
            self.scheduler.run(self.bot, 'send_daily_to_group')


class SafetyTests(unittest.TestCase):
    def test_no_send_paths(self):
        from src.plugin_installer import _code_without_comments
        code = _code_without_comments((ROOT / 'src' / 'agent_scheduler.py').read_text('utf-8'))
        self.assertEqual(re.findall(r'queue_outgoing|send_text|wechatauto|proactive', code), [])


if __name__ == '__main__':
    unittest.main()
