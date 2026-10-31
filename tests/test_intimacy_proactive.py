"""主动开口的亲密度门槛：只对"聊得热"的群说话。"""
import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.plugins.group_ambient import intimacy_score
from src.plugins.manager import PluginManager


class ScoreTests(unittest.TestCase):
    def test_zero_replies_is_zero(self):
        self.assertEqual(intimacy_score(0), 0)

    def test_cap_is_full_score(self):
        self.assertEqual(intimacy_score(20), 100)
        self.assertEqual(intimacy_score(999), 100)

    def test_middle_is_proportional(self):
        self.assertEqual(intimacy_score(5), 25)
        self.assertEqual(intimacy_score(10), 50)

    def test_custom_cap(self):
        self.assertEqual(intimacy_score(4, cap=8), 50)

    def test_bad_input_is_safe(self):
        self.assertEqual(intimacy_score(-3), 0)
        self.assertEqual(intimacy_score(None), 0)


class GateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.plugin = self.manager._plugins['group_ambient']
        self.db = sqlite3.connect(Path(self.tmp.name) / 'm.sqlite3')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS conversation_log(mode TEXT,account TEXT,kind TEXT,target TEXT,
          content TEXT,reply TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS chat_ambient(scope TEXT,created REAL);
        ''')
        self.sent = []
        self.notes = []
        self.runtime = SimpleNamespace(
            db=self.db, mode='wechat', account=lambda: 'acc1',
            queue_outgoing=lambda key, kind, target, text: (self.sent.append(text) or True),
            note=self.notes.append, groups=lambda: ['g1@chatroom'],
            config={}, running=True)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _add_replies(self, group, count):
        now = time.time()
        for i in range(count):
            self.db.execute('INSERT INTO conversation_log VALUES(?,?,?,?,?,?,?)',
                            ('wechat', 'acc1', 'group', group, 'x', '好的', now - i * 60))
        self.db.execute("INSERT INTO chat_ambient VALUES(?,?)", (f'group:{group}', now))
        self.db.commit()

    def test_intimacy_reads_recent_replies(self):
        self._add_replies('g1@chatroom', 10)
        self.assertEqual(self.plugin.intimacy(self.runtime, 'g1@chatroom'), 50)

    def test_messages_without_reply_do_not_count(self):
        now = time.time()
        self.db.execute('INSERT INTO conversation_log VALUES(?,?,?,?,?,?,?)',
                        ('wechat', 'acc1', 'group', 'g1@chatroom', 'x', '', now))
        self.db.commit()
        self.assertEqual(self.plugin.intimacy(self.runtime, 'g1@chatroom'), 0)

    async def test_cold_group_is_skipped_when_gate_on(self):
        self.manager.update_settings('group_ambient', {
            'enabled': True, 'intimacyEnabled': True, 'intimacyMin': 30,
            'intervalMin': 5, 'quietHours': '', 'activeWithinMin': 0,
            'echoEnabled': False, 'maxPerHour': 3})
        self._add_replies('g1@chatroom', 2)
        self.plugin._groups = lambda runtime: ['g1@chatroom']
        await self.plugin.tick(self.runtime)
        self.assertEqual(self.sent, [])

    def test_disabled_plugin_never_sends(self):
        self.plugin._groups = lambda runtime: ['g1@chatroom']
        asyncio.get_event_loop()
        self.db.execute("INSERT INTO chat_ambient VALUES('group:g1@chatroom',?)", (time.time(),))
        self.db.commit()
        self.assertEqual(self.plugin.get('enabled'), False)


if __name__ == '__main__':
    unittest.main()
