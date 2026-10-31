"""DSH主动上报：带地址成功/失败、唯一候选、0与多候选、extra字段与last_announce；全程离线。"""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.agent_authority import authority
from src.dsh_bridge import DshError
from src.wechat_app import WeChatRuntime, create_app

ROOT = Path(__file__).resolve().parent.parent
OK_PROBE = {'success': True, 'connected': True, 'preset_ready': True, 'delivery_verified': False,
            'message': 'DSH协议与专用人设可用；尚未验证完整对话'}
SAVED_FIELDS = ('enabled', 'model', 'provider', 'reasoning_effort', 'timeout', 'profile')


class AnnounceRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'styles', root / 'styles')
        self.config_file = root / 'config.wechat.yaml'
        self.config_file.write_text((ROOT / 'config.wechat.yaml').read_text('utf-8'), 'utf-8')
        self.bot = WeChatRuntime(root=root, config_path=self.config_file, db_path=root / 'm.sqlite3')
        self._cm = TestClient(create_app(self.bot))
        self.client = self._cm.__enter__()
        self.addCleanup(self._close)
        authority(self.bot).bind({'platform': 'desktop', 'account': 'mock_machine', 'subject': 'owner'})
        self.calls = []

    def _close(self):
        try:
            self._cm.__exit__(None, None, None)
        except Exception:
            pass
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass

    def config_bytes(self):
        return self.config_file.read_bytes()

    def status(self):
        return self.client.get('/api/dsh').json()

    async def probe_ok(self, cfg=None):
        self.calls.append(dict(cfg or {}))
        return dict(OK_PROBE)

    def discover_with(self, rows, state, hint):
        async def fake(service, inventory=None):
            self.calls.append({'discover': state})
            return {'success': True, 'candidates': rows, 'process_count': 1, 'checked_ports': len(rows),
                    'unverified_ports': 0, 'state': state, 'drift': None,
                    'saved_base_url': 'http://127.0.0.1:3080', 'hint': hint, 'message': '发现结果'}
        return patch('src.dsh_discovery.discover', fake)

    def test_announce_with_url_saves_only_base_url_and_reports_connected(self):
        before, before_bytes = self.bot.dsh.settings(), self.config_bytes()
        with patch.object(self.bot.dsh, 'probe', self.probe_ok):
            response = self.client.post('/api/dsh/announce', json={'base_url': '127.0.0.1:13421'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body['success'], body['state'], body['base_url'], body['changed']),
                         (True, 'connected', 'http://127.0.0.1:13421', True))
        self.assertEqual(body['probe'], OK_PROBE)
        after = self.bot.dsh.settings()
        self.assertEqual(after['base_url'], 'http://127.0.0.1:13421')
        for key in SAVED_FIELDS:
            self.assertEqual(after[key], before[key], key)
        self.assertNotEqual(self.config_bytes(), before_bytes)
        self.assertEqual(self.calls[-1]['base_url'], 'http://127.0.0.1:13421')
        self.assertEqual(self.calls[-1]['profile'], before['profile'])
        record = self.status()['last_announce']
        self.assertEqual((record['state'], record['base_url'], record['actor']),
                         ('connected', 'http://127.0.0.1:13421', 'dsh'))

    def test_invalid_address_returns_400_without_touching_config(self):
        saved, before_bytes = self.bot.dsh.settings()['base_url'], self.config_bytes()
        response = self.client.post('/api/dsh/announce', json={'base_url': 'http://192.168.1.9:13421'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('本机http地址', response.json()['detail'])
        self.assertEqual(self.config_bytes(), before_bytes)
        self.assertEqual(self.bot.dsh.settings()['base_url'], saved)
        record = self.status()['last_announce']
        self.assertEqual((record['state'], record['base_url']), ('failed', 'http://192.168.1.9:13421'))
        self.assertLessEqual(len(record['message']), 160)

    def test_probe_failure_returns_400_and_never_writes(self):
        async def boom(cfg=None):
            raise DshError('DSH接口不可用或鉴权失败，请检测地址/登录态')

        saved, before_bytes = self.bot.dsh.settings()['base_url'], self.config_bytes()
        with patch.object(self.bot.dsh, 'probe', boom):
            response = self.client.post('/api/dsh/announce', json={'base_url': '127.0.0.1:9566'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('鉴权失败', response.json()['detail'])
        self.assertEqual(self.config_bytes(), before_bytes)
        self.assertEqual(self.bot.dsh.settings()['base_url'], saved)
        self.assertEqual(self.status()['last_announce']['state'], 'failed')

    def test_discovery_single_candidate_is_saved(self):
        rows = [{'pid': 4242, 'base_url': 'http://127.0.0.1:13421', 'verified': True}]
        with self.discover_with(rows, 'drift', '端口已变更：3080 → 13421'), \
                patch.object(self.bot.dsh, 'probe', self.probe_ok):
            response = self.client.post('/api/dsh/announce', json={})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body['state'], body['base_url'], body['changed']),
                         ('connected', 'http://127.0.0.1:13421', True))
        self.assertEqual(self.bot.dsh.settings()['base_url'], 'http://127.0.0.1:13421')
        self.assertEqual(self.status()['last_announce']['base_url'], 'http://127.0.0.1:13421')

    def test_discovery_without_candidate_returns_409_with_state_and_hint(self):
        before_bytes = self.config_bytes()
        with self.discover_with([], 'process_missing', '未找到 DSH 进程，请先启动桌面 DSH'):
            response = self.client.post('/api/dsh/announce', json={})
        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()
        self.assertEqual((body['state'], body['hint']),
                         ('process_missing', '未找到 DSH 进程，请先启动桌面 DSH'))
        self.assertTrue(body['detail'])
        self.assertEqual(self.config_bytes(), before_bytes)
        record = self.status()['last_announce']
        self.assertEqual((record['state'], record['base_url']), ('failed', ''))

    def test_discovery_with_multiple_candidates_returns_409(self):
        rows = [{'pid': 4242, 'base_url': 'http://127.0.0.1:13421', 'verified': True},
                {'pid': 4243, 'base_url': 'http://127.0.0.1:13422', 'verified': True}]
        before_bytes = self.config_bytes()
        with self.discover_with(rows, 'ok', '已确认 2 个DSH本机端点'):
            response = self.client.post('/api/dsh/announce', json={})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['state'], 'ok')
        self.assertEqual(self.config_bytes(), before_bytes)
        self.assertEqual(self.bot.dsh.settings()['base_url'], 'http://127.0.0.1:3080')

    def test_extra_field_is_rejected_with_422_and_nothing_is_recorded(self):
        before_bytes = self.config_bytes()
        response = self.client.post('/api/dsh/announce',
                                    json={'base_url': '127.0.0.1:13421', 'enabled': True})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.config_bytes(), before_bytes)
        self.assertIsNone(self.status()['last_announce'])
        self.assertFalse(self.bot.dsh.settings()['enabled'])

    def test_status_keeps_existing_fields_and_exposes_last_announce(self):
        body = self.status()
        self.assertTrue({'success', 'enabled', 'base_url', 'model', 'provider', 'reasoning_effort',
                         'timeout', 'profile', 'stage', 'last_error',
                         'wechat_delivery_verified'} <= set(body))
        self.assertIsNone(body['last_announce'])
        with patch.object(self.bot.dsh, 'probe', self.probe_ok):
            self.client.post('/api/dsh/announce', json={'base_url': '127.0.0.1:13421'})
        record = self.status()['last_announce']
        self.assertEqual(set(record), {'at', 'state', 'base_url', 'actor', 'message'})
        self.assertIsInstance(record['at'], float)
        self.assertEqual(record['state'], 'connected')

    def test_repeat_announce_of_same_address_reports_changed_false(self):
        with patch.object(self.bot.dsh, 'probe', self.probe_ok):
            first = self.client.post('/api/dsh/announce', json={'base_url': '127.0.0.1:13421'})
            second = self.client.post('/api/dsh/announce', json={'base_url': 'http://127.0.0.1:13421/'})
        self.assertTrue(first.json()['changed'])
        self.assertFalse(second.json()['changed'])
        self.assertEqual(second.json()['base_url'], 'http://127.0.0.1:13421')

    def test_control_plane_registers_announce_as_manage_console(self):
        from src.agent_control import CATALOG, capability
        spec = CATALOG['dsh.announce']
        self.assertEqual((spec['method'], spec['path']), ('POST', '/api/dsh/announce'))
        self.assertEqual(capability(spec), 'manage_console')


if __name__ == '__main__':
    unittest.main()
