"""定时任务面板：存在、只调 agent/schedules 接口、不暴露发送入口。"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ScheduleDialogSourceTests(unittest.TestCase):
    def setUp(self):
        text = (ROOT / 'wechat_gui.py').read_text('utf-8')
        start = text.index('class ScheduleDialog(QDialog):')
        end = text.index('class PluginTab(QWidget):')
        self.body = text[start:end]
        self.whole = text

    def test_dialog_registered(self):
        self.assertIn('class ScheduleDialog(QDialog):', self.whole)

    def test_agent_tab_has_the_button(self):
        self.assertIn('self.btn_schedule', self.whole)
        self.assertIn('ScheduleDialog(self).exec()', self.whole)

    def test_only_schedule_endpoints(self):
        endpoints = set(re.findall(r"'(agent/schedules[^']*)'", self.body))
        self.assertTrue(endpoints)
        for endpoint in endpoints:
            self.assertTrue(endpoint.startswith('agent/schedules'), endpoint)

    def test_no_send_paths(self):
        banned = re.findall(r'queue_outgoing|send_text|wechatauto|mock/recv|proactive', self.body)
        self.assertEqual(banned, [])

    def test_mentions_the_local_only_promise(self):
        self.assertIn('不发微信', self.body)


if __name__ == '__main__':
    unittest.main()
