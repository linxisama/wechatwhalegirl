"""1.3.60：没有管理员权限也要能找到本机 DSH（端口归属读不到时按线索端口核验）。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import dsh_discovery as discovery
from src.dsh_bridge import DshError


class FakeService:
    """只实现 discover 用到的两处：已保存设置与只读 rpc。"""

    def __init__(self, urls=(), saved='http://127.0.0.1:3080'):
        self.urls = set(urls)
        self.saved = saved
        self.calls = []

    def settings(self):
        return {'base_url': self.saved, 'enabled': True}

    async def rpc(self, client, base, method, request=None):
        self.calls.append((base, method))
        if base not in self.urls:
            raise DshError('连不上')
        if method == 'settings/describe':
            return {'models': [], 'providers': []}
        return {'presets': [{'id': 'whale-wechat-chat'}]}


def run(coro):
    return asyncio.run(coro)


class HintPortTests(unittest.TestCase):
    def test_ports_are_mined_from_dsh_own_files_but_never_from_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / '.dsh'
            (home / 'logs').mkdir(parents=True)
            (home / 'logs' / 'web.log').write_text(
                'ready on http://127.0.0.1:9527\n', encoding='utf-8')
            (home / '.credentials.yaml').write_text(
                'records: http://127.0.0.1:1234\n', encoding='utf-8')
            (home / 'notes.bin').write_bytes(b'http://127.0.0.1:7654')
            ports = discovery.hint_ports('http://127.0.0.1:3080', home=home)
        self.assertEqual(ports[0], 3080)                      # 已保存地址最优先
        self.assertIn(discovery.DEFAULT_PORT, ports)           # 默认端口只是候选之一
        self.assertIn(9527, ports)
        self.assertNotIn(1234, ports, '凭据文件不参与端口挖掘')
        self.assertNotIn(7654, ports, '只扫文本类文件，不读二进制')

    def test_saved_port_and_default_come_first_even_without_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            ports = discovery.hint_ports('http://127.0.0.1:13421', home=Path(tmp) / 'none')
        self.assertEqual(ports, [13421, discovery.DEFAULT_PORT])

    def test_hint_list_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / '.dsh'
            home.mkdir()
            text = ' '.join('http://127.0.0.1:%d' % (20000 + i) for i in range(40))
            (home / 'a.log').write_text(text, encoding='utf-8')
            ports = discovery.hint_ports('', home=home)
        self.assertLessEqual(len(ports), discovery.HINT_PORT_LIMIT)


class SnapshotTests(unittest.TestCase):
    def test_port_owner_denied_degrades_to_empty_instead_of_raising(self):
        import psutil
        with patch.object(psutil, 'process_iter', return_value=[]), \
             patch.object(psutil, 'net_connections', side_effect=psutil.AccessDenied(0)):
            processes, ports = discovery.snapshot()
        self.assertEqual(processes, [])
        self.assertEqual(ports, [])


class DiscoverFallbackTests(unittest.TestCase):
    def discover(self, service, hints, home):
        with patch.object(discovery, 'snapshot', return_value=([], [])), \
             patch.object(discovery, 'hint_ports', return_value=hints), \
             patch.object(discovery, 'dsh_home', return_value=home), \
             patch.object(discovery, 'cookie_for', return_value='opaque'):
            return run(discovery.discover(service, hints=hints))

    def test_probe_found_endpoint_is_used_when_ports_cannot_be_attributed(self):
        service = FakeService({'http://127.0.0.1:9527'}, saved='http://127.0.0.1:9527')
        with tempfile.TemporaryDirectory() as tmp:
            data = self.discover(service, [9527], Path(tmp))
        self.assertEqual(data['state'], 'ok')
        self.assertEqual([row['base_url'] for row in data['candidates']], ['http://127.0.0.1:9527'])
        self.assertEqual(data['probed_ports'], 1)
        self.assertFalse(data['port_owner_known'])
        self.assertTrue(any(method == 'agentPresets/list' for _, method in service.calls))

    def test_drift_is_reported_when_probed_port_differs_from_saved(self):
        service = FakeService({'http://127.0.0.1:9527'}, saved='http://127.0.0.1:3080')
        with tempfile.TemporaryDirectory() as tmp:
            data = self.discover(service, [9527], Path(tmp))
        self.assertEqual(data['state'], 'drift')
        self.assertEqual(data['drift']['to_port'], 9527)

    def test_missing_dsh_home_is_named_as_not_installed(self):
        service = FakeService()
        with tempfile.TemporaryDirectory() as tmp:
            data = self.discover(service, [], Path(tmp) / 'no-such-dir')
        self.assertEqual(data['state'], 'dsh_missing')
        self.assertIn('没找到 DSH', data['hint'])

    def test_existing_home_without_endpoint_is_named_as_not_running(self):
        service = FakeService()
        with tempfile.TemporaryDirectory() as tmp:
            data = self.discover(service, [9527], Path(tmp))
        self.assertEqual(data['state'], 'dsh_offline')
        self.assertEqual(data['probed_ports'], 1)
        self.assertIn('打开桌面 DSH', data['hint'])

    def test_injected_inventory_never_sweeps_ports(self):
        service = FakeService()
        with patch.object(discovery, 'hint_ports', side_effect=AssertionError('不该扫端口')):
            data = run(discovery.discover(service, inventory=([], [])))
        self.assertEqual(data['state'], 'process_missing')
        self.assertEqual(data['probed_ports'], 0)


class ClassifyTests(unittest.TestCase):
    def test_verified_saved_endpoint_is_ok(self):
        rows = [{'base_url': 'http://127.0.0.1:3080', 'pid': 1}]
        state, drift, hint, _ = discovery.classify(rows, 'http://127.0.0.1:3080', 1, 1, 1)
        self.assertEqual((state, drift), ('ok', None))
        self.assertIn('端点可用', hint)

    def test_no_process_no_home_is_missing(self):
        state, _, hint, _ = discovery.classify([], 'http://127.0.0.1:3080', '', 0, 0, home_present=False)
        self.assertEqual(state, 'dsh_missing')
        self.assertIn('安装', hint)

    def test_probe_without_result_is_offline(self):
        state, _, hint, _ = discovery.classify([], 'http://127.0.0.1:3080', '', 0, 3, home_present=True, probed=3)
        self.assertEqual(state, 'dsh_offline')
        self.assertIn('3 个', hint)


if __name__ == '__main__':
    unittest.main()
