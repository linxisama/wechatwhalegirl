"""1.3.84 只读用量/巡检面板：假异步接口，不起后台、不碰真数据。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication
from src.gui.usage_panel import UsagePanel, _stamp

APP = None
USAGE = {'ok': True, 'usage': {'rounds': 12, 'prompt_tokens': 3000, 'reply_tokens': 1800,
                               'by_source': {'dsh': 9, 'local': 3}, 'days': 7, 'last_ts': 1700000000}}
GUARD = {'ok': True, 'summary': {'total': 4, 'warn': 2, 'codes': {'DUP': 1, 'TOO_VAGUE': 1},
                                 'last_ts': 1700000500, 'mode': 'report'},
         'items': [{'ts': 1700000500, 'codes': ['DUP'], 'scope': 'private', 'source': 'chat',
                    'similarity': 0.97, 'duplicate_of': '记忆A', 'text': '主人住在杭州'},
                   {'ts': 1700000400, 'codes': ['TOO_VAGUE'], 'scope': 'group', 'source': 'dialog',
                    'text': '好的'}]}


class UsagePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        def request(*args, **kwargs):
            self.calls.append((args, kwargs))
        self.panel = UsagePanel(request)

    def tearDown(self):
        self.panel.timer.stop(); self.panel.close()

    def deliver(self, data, index=0):
        self.calls.pop(index)[1]['on_done'](data)

    def test_refresh_asks_two_read_only_endpoints(self):
        self.panel.refresh()
        paths = [call[0][2] for call in self.calls]
        self.assertEqual(paths, ['usage/summary', 'memory/guard'])
        for call in self.calls:
            self.assertEqual(call[0][1], 'GET')
            self.assertEqual(call[1]['timeout'], 10)
        self.assertTrue(self.panel.busy)
        self.assertFalse(self.panel.refresh_button.isEnabled())

    def test_renders_usage_and_guard_after_both_arrive(self):
        self.panel.refresh(); self.deliver(USAGE); self.deliver(GUARD)
        self.assertFalse(self.panel.busy)
        self.assertTrue(self.panel.refresh_button.isEnabled())
        self.assertEqual(self.panel.refresh_button.text(), '刷新')
        self.assertIn('12 轮', self.panel.usage.text())
        self.assertIn('DSH 9', self.panel.usage.text())
        self.assertIn('提示约 3000 tok', self.panel.usage.text())
        self.assertIn('共 4 条', self.panel.guard.text())
        self.assertIn('需留意 2 条', self.panel.guard.text())
        self.assertIn('DUP×1', self.panel.guard.text())
        body = self.panel.text.toPlainText()
        self.assertIn('与已存记忆几乎重复', body)
        self.assertIn('与已存记忆重复度 0.97', body)
        self.assertIn('信息太薄', body)

    def test_refresh_only_renders_when_both_returned(self):
        self.panel.refresh(); self.deliver(GUARD, 1)
        self.assertTrue(self.panel.busy)
        self.assertIn('尚未读取', self.panel.usage.text())
        self.deliver(USAGE)
        self.assertFalse(self.panel.busy)
        self.assertIn('12 轮', self.panel.usage.text())

    def test_failed_endpoint_shows_message_and_unlocks(self):
        self.panel.refresh(); self.deliver({'ok': False, 'message': '尚未连接微信'}, 0)
        self.deliver(GUARD, 0)
        self.assertIn('尚未连接微信', self.panel.notice.text())
        self.assertFalse(self.panel.busy)
        self.assertTrue(self.panel.refresh_button.isEnabled())

    def test_empty_state_text(self):
        self.panel.refresh(); self.deliver({'ok': True, 'usage': {}}, 0)
        self.deliver({'ok': True, 'summary': {}, 'items': []}, 0)
        self.assertIn('账本为空', self.panel.usage.text())
        self.assertIn('还没有记录', self.panel.guard.text())
        self.assertEqual(self.panel.text.toPlainText(), '')

    def test_success_key_is_also_accepted(self):
        self.panel.refresh(); self.deliver({'success': True, 'usage': USAGE['usage']}, 0)
        self.deliver({'success': True, 'summary': GUARD['summary'], 'items': []}, 0)
        self.assertIn('12 轮', self.panel.usage.text())
        self.assertIn('共 4 条', self.panel.guard.text())

    def test_request_raising_unlocks_and_reports(self):
        def boom(*args, **kwargs):
            raise RuntimeError('后台未启动')
        panel = UsagePanel(boom)
        try:
            panel.refresh()
            self.assertIn('后台未启动', panel.notice.text())
            self.assertFalse(panel.busy)
        finally:
            panel.timer.stop(); panel.close()

    def test_timer_only_ticks_while_visible(self):
        self.assertEqual(self.panel.timer.interval(), 15000)
        self.assertFalse(self.panel.timer.isActive())
        self.panel.show(); self.assertTrue(self.panel.timer.isActive())
        self.panel.hide(); self.assertFalse(self.panel.timer.isActive())

    def test_apply_theme_keeps_read_only(self):
        self.panel.apply_theme({'panel_transparency': 0.5})
        self.assertIn('rgba(255,255,255,127)', self.panel.text.styleSheet())
        self.assertTrue(self.panel.text.isReadOnly())

    def test_stamp_handles_bad_values(self):
        self.assertEqual(_stamp(None), '无')
        self.assertEqual(_stamp('abc'), '未知')
        self.assertRegex(_stamp(1700000000), r'^\d\d-\d\d \d\d:\d\d$')
