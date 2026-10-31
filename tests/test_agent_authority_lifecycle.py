"""凭据生命周期字段：issued/last_used 的写入、60秒节流与外露，且不泄漏 digest/token。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.agent_authority import AgentAuthority

ADMIN = {'platform': 'desktop', 'account': 'local_console', 'subject': 'owner'}


class CredentialLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data' / 'agent_authority.json'
        self.auth = AgentAuthority(self.path)
        self.auth.bind(dict(ADMIN))

    def rows(self):
        return json.loads(self.path.read_text('utf-8'))['credentials']

    def edit_rows(self, change):
        data = json.loads(self.path.read_text('utf-8'))
        change(data['credentials'])
        self.path.write_text(json.dumps(data), 'utf-8')

    def only_status_row(self):
        return self.auth.status()['credentials'][0]

    def test_issue_records_issued_and_never_exposes_digest(self):
        issued = self.auth.issue('dsh-app', ['read_console'])
        self.assertIs(type(issued['issued']), int)
        self.assertNotIn('digest', issued)
        row = self.only_status_row()
        self.assertEqual(set(row), {'id', 'peer', 'capabilities', 'expires', 'issued'})
        self.assertIs(type(row['issued']), int)
        self.assertEqual(row['issued'], self.rows()[0]['issued'])
        self.assertNotIn('token', json.dumps(row))

    def test_last_used_written_once_then_throttled_for_sixty_seconds(self):
        token = self.auth.issue('dsh-app', ['read_console'])['token']
        actor = self.auth.authenticate(token)
        self.assertEqual(actor['peer'], 'dsh-app')
        first = self.rows()[0]['last_used']
        self.assertIs(type(first), int)
        stamp = self.path.stat().st_mtime_ns
        self.auth.authenticate(token)
        self.assertEqual(self.only_status_row()['last_used'], first)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)

    def test_last_used_refreshed_after_the_window_has_passed(self):
        token = self.auth.issue('dsh-app', ['read_console'])['token']
        self.edit_rows(lambda rows: rows[0].__setitem__('last_used', int(time.time()) - 120))
        self.auth.authenticate(token)
        self.assertGreaterEqual(self.only_status_row()['last_used'], int(time.time()) - 60)

    def test_legacy_credential_keeps_missing_lifecycle_fields(self):
        token = self.auth.issue('legacy-app', ['read_console'])['token']

        def drop(rows):
            rows[0].pop('issued', None)
            rows[0].pop('last_used', None)

        self.edit_rows(drop)
        self.assertEqual(set(self.only_status_row()),
                         {'id', 'peer', 'capabilities', 'expires'})
        # 旧记录鉴权后只补 last_used，issued 不伪造。
        self.auth.authenticate(token)
        self.assertNotIn('issued', self.only_status_row())
        self.assertIn('last_used', self.rows()[0])

    def test_save_failure_only_logs_and_never_blocks_authentication(self):
        token = self.auth.issue('dsh-app', ['read_console'])['token']
        with patch('src.agent_authority.atomic_text', side_effect=OSError('磁盘只读')):
            actor = self.auth.authenticate(token)
        self.assertEqual(actor['peer'], 'dsh-app')
        self.assertNotIn('last_used', self.rows()[0])


if __name__ == '__main__':
    unittest.main()
