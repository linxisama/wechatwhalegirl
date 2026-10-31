"""1.3.43 发送耗时归因：阶段收集器、客人端 timings、宿主容错（纯观测，不碰真机）。"""
import time
import unittest

from src.logging_setup import drain_send_phases, mark_send_phase, start_send_phases
from src.isolated_wechat import guest_service
from src.isolated_wechat.host_bridge import SandboxBridge

GUIA_PHASES = ('guia_probe', 'guia_check_title', 'guia_input', 'guia_click_send', 'guia_verify')
TIMING_KEYS = {'total', 'receive', 'agent', 'gui_ready', 'press', 'confirm', 'write'}


class SendPhaseCollectorTest(unittest.TestCase):
    def test_marks_are_ordered_and_drained_once(self):
        self.assertEqual(drain_send_phases(), [])
        start_send_phases()
        for phase in GUIA_PHASES:
            self.assertTrue(mark_send_phase(phase))
        marks = drain_send_phases()
        self.assertEqual([item['phase'] for item in marks], ['begin'] + list(GUIA_PHASES))
        times = [item['at_ms'] for item in marks]
        self.assertEqual(times, sorted(times))
        # drain 之后不再收集，也不会把上一轮的时刻带进下一次发送
        self.assertFalse(mark_send_phase('guia_verify'))
        self.assertEqual(drain_send_phases(), [])


class GuestSendTimingsTest(unittest.TestCase):
    def _service(self, diagnostic=None):
        class Result:
            diagnostic = {}

            def __getitem__(self, index):
                return (True, '已发送')[index]

        class Bridge:
            def send_text(self, target, text, name, at):
                time.sleep(0.005)          # 让 total 稳定大于 0，不靠机器速度碰运气
                result = Result()
                result.diagnostic = dict(diagnostic or {})
                return result

            def status(self):
                return {}

        service = object.__new__(guest_service.GuestService)
        service._overflow = False
        service.bridge = Bridge()
        return service

    def test_send_text_response_carries_timings(self):
        service = self._service({'timings': {'press': 12.5, 'total': 999.0}})
        value = service.dispatch('send_text', {'target': '文件传输助手', 'text': '好',
                                              'target_name': '', 'at': []})
        timings = value['timings']
        self.assertGreater(timings['total'], 0)
        self.assertGreaterEqual(timings['total'], timings['agent'])
        self.assertEqual(timings['press'], 12.5)      # 客人端 guia 阶段字段随响应带回
        self.assertNotEqual(timings['total'], 999.0)  # 外层 total 不被内层覆盖
        self.assertTrue(set(timings) <= TIMING_KEYS)


class HostTimingLogTest(unittest.TestCase):
    def test_missing_timings_is_silent(self):
        for value in (None, {}, 'x', {'total': 'bad'}):
            SandboxBridge._log_send_timings(None, value)   # 老客人端不带 timings 也必须不抛

    def test_present_timings_writes_one_line(self):
        with self.assertLogs('wechat.runtime', level='INFO') as captured:
            SandboxBridge._log_send_timings(None, {'total': 26980.0, 'agent': 26900.0})
        self.assertEqual(len(captured.output), 1)
        self.assertIn('send timing guest total_ms=26980.0 agent_ms=26900.0', captured.output[0])


if __name__ == '__main__':
    unittest.main()
