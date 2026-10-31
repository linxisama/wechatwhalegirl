"""DSH地址规范化、发现分因、HTTP失败明细与提供商目录；全程离线，不打真实网络。"""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from src.agent_authority import authority
from src.dsh_bridge import (DshBridge, DshError, DshHttpError, HINT_CREDENTIAL, HINT_EFFORT,
                            HINT_MODEL, HINT_PROVIDER, error_detail, local_url)
from src.dsh_discovery import discover
from src.wechat_app import WeChatRuntime, create_app

ROOT = Path(__file__).resolve().parent.parent


class LocalUrlTests(unittest.TestCase):
    def test_bare_host_port_gets_http_prefix(self):
        for value, expected in [('127.0.0.1:13421', 'http://127.0.0.1:13421'),
                                ('  localhost:13421  ', 'http://localhost:13421'),
                                ('[::1]:13421', 'http://[::1]:13421'),
                                ('http://127.0.0.1:13421/', 'http://127.0.0.1:13421')]:
            with self.subTest(value=value):
                self.assertEqual(local_url(value), expected)

    def test_https_and_other_schemes_rejected_with_http_hint(self):
        for value in ('https://127.0.0.1:13421', 'ws://127.0.0.1:13421'):
            with self.subTest(value=value), self.assertRaisesRegex(DshError, 'http://'):
                local_url(value)

    def test_path_is_rejected(self):
        with self.assertRaisesRegex(DshError, '只能是本机http地址'):
            local_url('http://127.0.0.1:13421/api')

    def test_non_loopback_host_is_rejected(self):
        for value in ('example.com:13421', 'http://192.168.1.9:13421'):
            with self.subTest(value=value), self.assertRaisesRegex(DshError, '只能是本机http地址'):
                local_url(value)

    def test_missing_port_is_reported_as_missing_not_invalid(self):
        with self.assertRaisesRegex(DshError, '请填写DSH端口'):
            local_url('localhost')

    def test_invalid_port_is_reported_as_invalid(self):
        for value in ('localhost:99999', 'localhost:abc'):
            with self.subTest(value=value), self.assertRaisesRegex(DshError, 'DSH端口无效'):
                local_url(value)


class FakeService:
    """只实现discover用到的两处：已保存设置与只读rpc。"""

    def __init__(self, base_url='http://127.0.0.1:3080', fails=False):
        self.base_url = base_url
        self.fails = fails

    def settings(self):
        return {'enabled': False, 'base_url': self.base_url}

    async def rpc(self, client, base, method, request=None):
        if self.fails:
            raise DshError('DSH拒绝请求：unknown')
        if method == 'agentPresets/list':
            return {'presets': [{'id': 'whale-wechat-chat'}]}
        return {'namespaces': {}}


DSH = {'pid': 4242, 'parent': 0, 'name': 'DeepSeek Harness.exe'}


def listening(port, address='127.0.0.1', pid=4242):
    return {'pid': pid, 'address': address, 'port': port}


class DiscoverStateTests(unittest.TestCase):
    def setUp(self):
        # 不读真实 ~/.dsh 凭据；patch 一律登记到 addCleanup，避免泄漏到其它用例。
        patcher = patch('src.dsh_discovery.cookie_for', return_value='opaque')
        patcher.start()
        self.addCleanup(patcher.stop)

    def scan(self, inventory, service):
        return asyncio.run(discover(service, inventory=inventory))

    def test_ok_when_saved_address_is_verified(self):
        data = self.scan(([DSH], [listening(3080)]), FakeService())
        self.assertEqual(data['state'], 'ok')
        self.assertIsNone(data['drift'])
        self.assertIn('端点可用', data['hint'])
        self.assertEqual(data['saved_base_url'], 'http://127.0.0.1:3080')
        self.assertEqual(data['process_count'], 1)
        self.assertEqual(data['checked_ports'], 1)
        self.assertEqual(data['unverified_ports'], 0)
        self.assertEqual([row['base_url'] for row in data['candidates']], ['http://127.0.0.1:3080'])

    def test_drift_prefers_same_host_then_lowest_port(self):
        # ::1 端口更小，但已保存地址在 127.0.0.1，必须优先同主机的候选。
        data = self.scan(([DSH], [listening(13421, '::1'), listening(13422)]), FakeService())
        self.assertEqual(data['state'], 'drift')
        self.assertEqual(data['drift'], {'from': 'http://127.0.0.1:3080',
                                        'to': 'http://127.0.0.1:13422',
                                        'from_port': 3080, 'to_port': 13422})
        self.assertIn('端口已变更：3080 → 13422', data['hint'])
        self.assertIn('更新为 13422', data['hint'])

    def test_protocol_failed_when_process_listens_but_rpc_rejected(self):
        data = self.scan(([DSH], [listening(3080)]), FakeService(fails=True))
        self.assertEqual(data['state'], 'protocol_failed')
        self.assertEqual(data['candidates'], [])
        self.assertEqual(data['checked_ports'], 1)
        self.assertEqual(data['process_count'], 1)
        self.assertIn('PID 4242', data['hint'])
        self.assertIn('登录', data['hint'])

    def test_port_unreachable_when_process_has_no_listening_port(self):
        data = self.scan(([DSH], []), FakeService())
        self.assertEqual(data['state'], 'port_unreachable')
        self.assertEqual(data['checked_ports'], 0)
        self.assertIn('PID 4242', data['hint'])
        self.assertIn('等待', data['hint'])

    def test_process_missing_when_no_dsh_process(self):
        data = self.scan(([], []), FakeService())
        self.assertEqual(data['state'], 'process_missing')
        self.assertEqual(data['process_count'], 0)
        self.assertIn('请先启动桌面 DSH', data['hint'])
        self.assertTrue(data['message'])


class HttpDetailTests(unittest.TestCase):
    def make(self, body, status=500, code='http_500'):
        return DshHttpError('DSH接口不可用或鉴权失败，请检测地址/登录态',
                            status=status, rpc_id='rid-1', code=code, body=body)

    def test_provider_hint(self):
        self.assertEqual(error_detail(self.make('unknown provider acme'))['hints'], [HINT_PROVIDER])

    def test_model_hint(self):
        self.assertEqual(error_detail(self.make('model deepseek-v4.1-flash-1017 not found'))['hints'],
                         [HINT_MODEL])

    def test_effort_hint(self):
        self.assertEqual(error_detail(self.make('unsupported reasoning effort high'))['hints'], [HINT_EFFORT])

    def test_credential_hint(self):
        detail = error_detail(self.make('denied', status=401, code='unauthorized'))
        self.assertEqual(detail['hints'], [HINT_CREDENTIAL])
        self.assertEqual(detail['status'], 401)
        self.assertEqual(detail['rpc_id'], 'rid-1')

    def test_hints_keep_fixed_order_and_cap_at_four(self):
        detail = error_detail(self.make('provider model reasoning credential', code='unauthorized'))
        self.assertEqual(detail['hints'], [HINT_PROVIDER, HINT_MODEL, HINT_EFFORT, HINT_CREDENTIAL])

    def test_plain_dsherror_has_no_status_or_hints(self):
        detail = error_detail(DshError('DSH地址只能是本机http地址，不带路径、凭据或查询参数'))
        self.assertIsNone(detail['status'])
        self.assertEqual(detail['hints'], [])

    def test_http_body_is_redacted_before_it_reaches_the_ui(self):
        def handler(request):
            return httpx.Response(500, text='{"error":{"code":"bad"},'
                                            '"note":"dsh-auth-abc123secret token: tok-9999 cookie=cook-1234"}')

        async def call():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await DshBridge.rpc(SimpleNamespace(), client, 'http://127.0.0.1:3080', 'session/prompt', {})

        with self.assertRaises(DshHttpError) as failed:
            asyncio.run(call())
        exc = failed.exception
        self.assertEqual((exc.status, exc.code), (500, 'bad'))
        for secret in ('dsh-auth-abc123secret', 'tok-9999', 'cook-1234'):
            self.assertNotIn(secret, exc.body)
            self.assertNotIn(secret, str(error_detail(exc)))
        self.assertIn('<redacted>', exc.body)

    def test_connect_failure_stays_plain_dsherror_with_address_message(self):
        def handler(request):
            raise httpx.ConnectError('refused', request=request)

        async def call():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await DshBridge.rpc(SimpleNamespace(), client, 'http://127.0.0.1:3080', 'session/create', {})

        with self.assertRaises(DshError) as failed:
            asyncio.run(call())
        self.assertNotIsInstance(failed.exception, DshHttpError)
        self.assertIn('无法连接 DSH 地址', str(failed.exception))


class DshCatalogRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'styles', root / 'styles')
        config = root / 'config.wechat.yaml'
        config.write_text((ROOT / 'config.wechat.yaml').read_text('utf-8'), 'utf-8')
        self.bot = WeChatRuntime(root=root, config_path=config, db_path=root / 'm.sqlite3')
        self._cm = TestClient(create_app(self.bot))
        self.client = self._cm.__enter__()
        self.addCleanup(self._close)
        authority(self.bot).bind({'platform': 'desktop', 'account': 'mock_machine', 'subject': 'owner'})

    def _close(self):
        try:
            self._cm.__exit__(None, None, None)
        except Exception:
            pass
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass

    def get_catalog(self):
        with patch('src.dsh_bridge.cookie_for', return_value='opaque'):
            return self.client.get('/api/dsh/catalog')

    def test_catalog_route_merges_providers_and_exposes_current_selection(self):
        async def rpc(client, base, method, request=None):
            self.assertEqual(method, 'settings/describe')
            return {'namespaces': {
                'llm-a': {'providers': {'p1': {'displayName': '一号', 'models': [
                    {'id': 'm1', 'name': '模型一'}, {'id': 'm2'}]}}},
                'llm-b': {'providers': {'p1': {'models': [{'id': 'm1'}, {'id': 'm3'}]},
                                        'p2': {'models': [{'name': 'm4'}]}}},
                'agent-default-model': {'provider': 'p1', 'model': 'm1', 'reasoningEffort': 'high'},
                'tools': {'providers': {'ignored': {}}}}}

        with patch.object(self.bot.dsh, 'rpc', rpc):
            response = self.get_catalog()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['source'], 'settings/describe')
        self.assertEqual(body['message'], '')
        rows = {row['id']: row for row in body['providers']}
        self.assertEqual(sorted(rows), ['p1', 'p2'])
        self.assertEqual(rows['p1']['displayName'], '一号')
        self.assertEqual([m['id'] for m in rows['p1']['models']], ['m1', 'm2', 'm3'])
        self.assertEqual(rows['p1']['models'][1]['name'], 'm2')
        self.assertEqual(rows['p2']['displayName'], 'p2')
        self.assertEqual([m['id'] for m in rows['p2']['models']], ['m4'])
        self.assertEqual(body['agent_default'],
                         {'provider': 'p1', 'model': 'm1', 'reasoningEffort': 'high'})
        self.assertEqual(set(body['current']), {'enabled', 'base_url', 'model', 'provider',
                                                'reasoning_effort', 'timeout', 'profile'})

    def test_catalog_route_degrades_to_200_when_rpc_fails(self):
        async def rpc(*args, **kwargs):
            raise DshError('DSH接口不可用或鉴权失败，请检测地址/登录态')

        with patch.object(self.bot.dsh, 'rpc', rpc):
            response = self.get_catalog()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['providers'], [])
        self.assertIn('地址', body['message'])

    def test_catalog_route_requires_bound_admin(self):
        auth = authority(self.bot)
        auth.unbind(auth.status()['revision'])
        self.assertEqual(self.get_catalog().status_code, 403)

    def test_probe_failure_returns_detail_instead_of_only_one_sentence(self):
        async def probe(*args, **kwargs):
            raise DshHttpError('DSH接口不可用或鉴权失败，请检测地址/登录态', status=500,
                               rpc_id='rid-2', code='http_500', body='model xx not found')

        with patch.object(self.bot.dsh, 'probe', probe):
            response = self.client.post('/api/dsh/probe', json={})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['message'], 'DSH接口不可用或鉴权失败，请检测地址/登录态')
        self.assertEqual(body['detail']['status'], 500)
        self.assertEqual(body['detail']['hints'], [HINT_MODEL])


if __name__ == '__main__':
    unittest.main()
