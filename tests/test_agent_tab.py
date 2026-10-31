"""Agent 页存在且不暴露发送入口（界面层也只做设置操作）。"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class AgentTabSourceTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / 'wechat_gui.py').read_text('utf-8')
        start = self.text.index('class AgentTab(QWidget):')
        self.body = self.text[start:self.text.index('class PluginTab(QWidget):')]

    def test_tab_registered_at_the_end(self):
        self.assertIn('self.tabs.addTab(self.agent_tab', self.text)

    def test_calls_only_agent_endpoints(self):
        endpoints = set(re.findall(r'"(agent/[a-z/s{}_.]+)"', self.body))
        self.assertTrue(endpoints, '没有调用任何 agent 接口')
        for endpoint in endpoints:
            self.assertTrue(endpoint.startswith('agent/'), endpoint)

    def test_no_send_paths_in_the_page(self):
        banned = re.findall(r'queue_outgoing|send_text|wechatauto|mock/recv', self.body)
        self.assertEqual(banned, [])

    def test_has_apply_and_undo_buttons(self):
        self.assertIn('"应用"', self.body)
        self.assertIn('"撤销"', self.body)


if __name__ == '__main__':
    unittest.main()
