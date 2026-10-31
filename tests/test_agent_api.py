"""Agent 接口冒烟：建会话 → 计划 → 应用 → 撤销，全程不碰发送通道。"""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.wechat_app import WeChatRuntime, create_app

ROOT = Path(__file__).resolve().parent.parent


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'styles', root / 'styles')
        config = root / 'config.wechat.yaml'
        config.write_text((ROOT / 'config.wechat.yaml').read_text('utf-8'), 'utf-8')
        self.runtime = WeChatRuntime(root=root, config_path=config, db_path=root / 'm.sqlite3')
        # 必须用上下文管理器进入：app.state.bot 是在 lifespan 里设置的，
        # 直接 TestClient(app) 不会触发 lifespan（实测报 'State' has no attribute 'bot'）。
        self._client_cm = TestClient(create_app(self.runtime))
        self.client = self._client_cm.__enter__()
        from src.agent_authority import authority
        authority(self.runtime).bind({'platform': 'qq', 'account': '123456', 'subject': '654321'})

    def tearDown(self):
        try:
            self._client_cm.__exit__(None, None, None)
        except Exception:
            pass
        try:
            asyncio.run(self.runtime.close())
        except Exception:
            pass
        self.tmp.cleanup()

    def test_full_cycle(self):
        session = self.client.post('/api/agent/sessions', json={'title': '验证'}).json()['session']
        card = self.client.post('/api/agent/plan',
                                json={'session_id': session['id'], 'text': '回复太长了，改成 45 字'}).json()['card']
        self.assertEqual(card['actions'][0]['action'], 'set_reply_length')
        applied = self.client.post('/api/agent/apply',
                                   json={'session_id': session['id'], 'card_id': card['id']}).json()
        self.assertTrue(applied['success'])
        self.assertEqual(self.runtime.plugins._plugins['chat_controls'].get('replyMaxChars'), 45)
        undone = self.client.post('/api/agent/undo',
                                  json={'session_id': session['id'], 'card_id': card['id']}).json()
        self.assertEqual(undone['card']['state'], 'undone')

    def test_unknown_session_is_404(self):
        response = self.client.post('/api/agent/plan', json={'session_id': 'nope', 'text': '改短'})
        self.assertEqual(response.status_code, 404)

    def test_answer_route_is_readonly(self):
        data = self.client.post('/api/agent/answer', json={}).json()
        self.assertTrue(data['success'])
        self.assertIn('插件', data['text'])

    def test_sessions_are_listed(self):
        self.client.post('/api/agent/sessions', json={'title': 'A'})
        listing = self.client.get('/api/agent/sessions').json()['sessions']
        self.assertEqual(listing[0]['title'], 'A')


if __name__ == '__main__':
    unittest.main()


class ScheduleApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'styles', root / 'styles')
        config = root / 'config.wechat.yaml'
        config.write_text((ROOT / 'config.wechat.yaml').read_text('utf-8'), 'utf-8')
        self.runtime = WeChatRuntime(root=root, config_path=config, db_path=root / 'm.sqlite3')
        self._cm = TestClient(create_app(self.runtime))
        self.client = self._cm.__enter__()
        from src.agent_authority import authority
        authority(self.runtime).bind({'platform': 'qq', 'account': '123456', 'subject': '654321'})

    def tearDown(self):
        try:
            self._cm.__exit__(None, None, None)
        except Exception:
            pass
        try:
            asyncio.run(self.runtime.close())
        except Exception:
            pass
        self.tmp.cleanup()

    def test_create_list_run_delete(self):
        task = self.client.post('/api/agent/schedules',
                                json={'name': '日报', 'action': 'daily_digest',
                                      'every_minutes': 60}).json()['task']
        self.assertEqual(len(self.client.get('/api/agent/schedules').json()['tasks']), 1)
        result = self.client.post('/api/agent/schedules/run', json={'task_id': task['id']}).json()
        self.assertTrue(result['success'])
        self.assertIn('日报', result['result'])
        toggled = self.client.post('/api/agent/schedules/toggle', json={'task_id': task['id']}).json()
        self.assertFalse(toggled['task']['enabled'])
        self.assertTrue(self.client.delete(f"/api/agent/schedules/{task['id']}").json()['success'])

    def test_invalid_action_is_400(self):
        response = self.client.post('/api/agent/schedules',
                                    json={'action': 'send_message', 'every_minutes': 30})
        self.assertEqual(response.status_code, 400)
