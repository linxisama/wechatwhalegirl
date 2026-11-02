"""1.3.82：控制台端口落在 Windows 预留段（WinNAT/Hyper-V）时要说清楚，而不是「被别的进程占着」。"""
import subprocess
import unittest
from unittest.mock import patch

from src import site_profile as sp

SAMPLE = """
协议 tcp 端口排除范围

开始端口    结束端口
==========    ==========
        8023          8122
        9411          9510
"""


class ExcludedRangeParsingTests(unittest.TestCase):
    def run_netsh(self, stdout=b'', error=None):
        def fake_run(*_a, **_k):
            if error is not None:
                raise error
            return subprocess.CompletedProcess(['netsh'], 0, stdout, b'')
        with patch.object(sp.subprocess, 'run', side_effect=fake_run):
            return sp.excluded_port_ranges()

    def test_parses_start_and_end_columns(self):
        ranges = self.run_netsh(SAMPLE.encode('utf-8'))
        self.assertEqual(ranges, [(8023, 8122), (9411, 9510)])

    def test_ignores_headers_and_impossible_rows(self):
        text = '开始端口 结束端口\n----------  ----------\n  9000 8000\n  70000 70001\n  10 20\n'
        self.assertEqual(self.run_netsh(text.encode('utf-8')), [(10, 20)])

    def test_missing_netsh_is_not_an_error(self):
        self.assertEqual(self.run_netsh(error=FileNotFoundError('netsh')), [])

    def test_timeout_is_not_an_error(self):
        self.assertEqual(self.run_netsh(error=subprocess.TimeoutExpired('netsh', 2.0)), [])

    def test_empty_output(self):
        self.assertEqual(self.run_netsh(b''), [])


class PortInReservedTests(unittest.TestCase):
    def test_boundaries(self):
        ranges = [(8023, 8122)]
        self.assertEqual(sp.port_in_reserved(8023, ranges), (8023, 8122))
        self.assertEqual(sp.port_in_reserved(8122, ranges), (8023, 8122))
        self.assertIsNone(sp.port_in_reserved(8123, ranges))
        self.assertIsNone(sp.port_in_reserved(8022, ranges))


class ProbePortTests(unittest.TestCase):
    def probe(self, identity=None, free=False, ranges=((8023, 8122),)):
        with patch.object(sp, '_runtime_identity', return_value=identity), \
                patch.object(sp, '_port_free', return_value=free), \
                patch.object(sp, 'excluded_port_ranges', return_value=list(ranges)) as used:
            item = sp._probe_port()
        return item, used

    def test_reserved_range_gets_its_own_hint(self):
        item, _used = self.probe()
        self.assertEqual(item['status'], 'warn')
        self.assertIn('8023-8122', item['detail'])
        self.assertIn('net stop winnat', item['detail'])
        self.assertNotIn('被别的进程占着', item['detail'])
        self.assertEqual(item['data']['reserved_range'], [8023, 8122])

    def test_plain_occupier_keeps_old_wording(self):
        item, _used = self.probe(ranges=((7000, 7010),))
        self.assertIn('被别的进程占着', item['detail'])
        self.assertNotIn('reserved_range', item['data'])

    def test_busy_port_skips_netsh_when_ours(self):
        item, used = self.probe(identity={'version': '1.3.82'}, free=False)
        self.assertEqual(item['status'], 'ok')
        used.assert_not_called()

    def test_free_port_never_asks_windows(self):
        item, used = self.probe(free=True)
        self.assertEqual(item['status'], 'ok')
        used.assert_not_called()


if __name__ == '__main__':
    unittest.main()
