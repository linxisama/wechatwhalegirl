"""1.3.85 只读发送诊断面板：假异步接口，不起后台、不碰真日志。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication
from src.gui.send_panel import SendPanel, _short, _stamp

APP = None
PAYLOAD = {
    'ok': True,
    'counts': {'phases': 12, 'failures': 1, 'timings': 2},
    'failure_codes': {'send_failed': 1},
    'last_failure': {'ts': '2026-11-02 18:43:05', 'kind': 'failed', 'reason': 'send_failed',
                     'who': '西莲寺紫…', 'len': 19, 'text': '「坐标+OCR」发送失败',
                     'advice': '请核对聊天记录；输入框里的残留草稿需要手动清空'},
    'last_phase': {'ts': '2026-11-02 18:43:01', 'kind': 'phase', 'phase': 'guia_input', 'who': '西莲寺紫…'},
    'last_timing': {'ts': '2026-11-02 18:43:06', 'kind': 'timing', 'scope': 'guest',
                    'metrics': {'total_ms': 4514, 'receive_ms': 100}, 'total_ms': 4514},
    'items': [
        {'ts': '2026-11-02 18:43:01', 'kind': 'phase', 'phase': 'guia_input', 'who': '西莲寺紫…',
         'len': 19, 'reason': '', 'channel': 'guia'},
        {'ts': '2026-11-02 18:43:05', 'kind': 'failed', 'reason': 'send_failed', 'who': '西莲寺紫…',
         'len': 19, 'pid': 16444},
        {'ts': '2026-11-02 18:43:06', 'kind': 'timing', 'scope': 'worker',
         'metrics': {'spawn_ms': 120, 'rpc_ms': 30}},
    ],
}


class SendPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        def request(*args, **kwargs):
            self.calls.append((args, kwargs))
        self.panel = SendPanel(request)

    def tearDown(self):
        self.panel.timer.stop()
        self.panel.close()

    def deliver(self, data):
        self.calls.pop(0)[1]['on_done'](data)

    def test_constructor_does_not_call_backend(self):
        self.assertEqual(self.calls, [])
        self.assertIn('尚未读取', self.panel.summary.text())
        self.assertTrue(self.panel.text.isReadOnly())

    def test_refresh_issues_one_read_only_get(self):
        self.panel.refresh()
        self.assertEqual(len(self.calls), 1)
        args, kwargs = self.calls[0]
        self.assertEqual(args[1], 'GET')
        self.assertEqual(args[2], 'send/history?limit=50')
        self.assertEqual(kwargs['timeout'], 10)
        self.assertTrue(self.panel.busy)
        self.assertFalse(self.panel.refresh_button.isEnabled())

    def test_render_summary_and_detail(self):
        self.panel.refresh()
        self.deliver(PAYLOAD)
        self.assertFalse(self.panel.busy)
        self.assertTrue(self.panel.refresh_button.isEnabled())
        self.assertIn('阶段 12 条', self.panel.summary.text())
        self.assertIn('失败 1 条', self.panel.summary.text())
        self.assertIn('send_failed×1', self.panel.summary.text())
        self.assertIn('最近一次失败', self.panel.failure.text())
        self.assertIn('建议：', self.panel.failure.text())
        body = self.panel.text.toPlainText()
        self.assertIn('阶段 guia_input', body)
        self.assertIn('失败 send_failed', body)
        self.assertIn('耗时 worker', body)
        self.assertIn('总计 4514ms', body)

    def test_render_without_failure(self):
        self.panel.refresh()
        data = dict(PAYLOAD)
        data['last_failure'] = None
        data['failure_codes'] = {}
        self.deliver(data)
        self.assertIn('无（日志尾部没有失败记录）', self.panel.failure.text())

    def test_empty_items(self):
        self.panel.refresh()
        self.deliver({'ok': True, 'counts': {}, 'items': []})
        self.assertIn('还没有记录', self.panel.text.toPlainText())

    def test_failure_response_shows_message_and_unlocks(self):
        self.panel.refresh()
        self.deliver({'ok': False, 'message': '后台忙'})
        self.assertIn('后台忙', self.panel.notice.text())
        self.assertFalse(self.panel.busy)

    def test_request_exception_unlocks(self):
        def boom(*args, **kwargs):
            raise RuntimeError('断线')
        self.panel.request = boom
        self.panel.refresh()
        self.assertIn('读取失败', self.panel.notice.text())
        self.assertFalse(self.panel.busy)
        self.assertTrue(self.panel.refresh_button.isEnabled())

    def test_timer_interval_and_show_hide(self):
        self.assertEqual(self.panel.timer.interval(), 15000)
        self.panel.show()
        self.assertTrue(self.panel.timer.isActive())
        self.assertGreaterEqual(len(self.calls), 1)  # showEvent 里主动刷一次
        self.panel.hide()
        self.assertFalse(self.panel.timer.isActive())

    def test_helpers(self):
        self.assertEqual(_stamp('2026-11-02 18:43:05'), '11-02 18:43')
        self.assertEqual(_stamp(None), '未知')
        self.assertEqual(_short('西莲寺紫'), '西莲寺紫')
        self.assertEqual(_short('x' * 20), 'x' * 12 + '…')


if __name__ == '__main__':
    unittest.main()
