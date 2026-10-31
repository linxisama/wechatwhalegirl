"""Agent 白名单动作：能预览、能应用、能撤销；没有发送路径。"""
import re
import tempfile
import unittest
from pathlib import Path

from src.agent_sessions import MAX_CARDS, MAX_SESSIONS, AgentSessions
from src.wechat_agent import (AGENT_PLUGINS, AgentError, answer, apply_card, plan,
                              undo_card)
from src.plugins.manager import PluginManager

ROOT = Path(__file__).resolve().parent.parent


class FakeStyle:
    def __init__(self):
        self.active_name = 'default'

    def set_active(self, name):
        if name not in ('default', 'yandere'):
            raise ValueError('没有人设')
        self.active_name = name


class FakeBot:
    def __init__(self, plugins):
        self.plugins = plugins
        self.style = FakeStyle()
        self.running = True

    def status(self):
        return {'running': self.running, 'plugins': len(self.plugins._plugins),
                'model': 'fake', 'memory': {'confirmed': 2, 'manual': 1, 'learned': 5}}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.bot = FakeBot(self.manager)
        self.sessions = AgentSessions(Path(self.tmp.name) / 'agent_sessions.json')
        self.session = self.sessions.create('测试')

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, text):
        result = plan(self.bot, text)
        return self.sessions.add_card(self.session['id'], result)

    def test_plan_length(self):
        result = plan(self.bot, '回复太长了，改成 40 字')
        self.assertEqual(result['actions'][0]['action'], 'set_reply_length')
        self.assertEqual(result['actions'][0]['params']['chars'], 40)

    def test_plan_picture(self):
        result = plan(self.bot, '表情包不要每条都回')
        self.assertEqual(result['actions'][0]['action'], 'set_picture_reply')
        self.assertFalse(result['actions'][0]['params']['enabled'])

    def test_unknown_request_falls_back_to_answer(self):
        result = plan(self.bot, '现在怎么样')
        self.assertEqual(result['actions'][0]['action'], 'answer')

    def test_apply_changes_setting(self):
        card = self.card('回复太长了，改成 50 字')
        applied = apply_card(self.bot, self.session, card, self.sessions)
        self.assertEqual(applied['state'], 'applied')
        self.assertEqual(self.manager._plugins['chat_controls'].get('replyMaxChars'), 50)

    def test_undo_restores_setting(self):
        before = self.manager._plugins['chat_controls'].get('replyMaxChars')
        card = apply_card(self.bot, self.session, self.card('回复太长了，改成 50 字'), self.sessions)
        undo_card(self.bot, self.session, card, self.sessions)
        self.assertEqual(self.manager._plugins['chat_controls'].get('replyMaxChars'), before)

    def test_undo_refuses_when_value_changed_afterwards(self):
        card = apply_card(self.bot, self.session, self.card('回复太长了，改成 50 字'), self.sessions)
        self.manager.update_settings('chat_controls', {'replyMaxChars': 70})
        with self.assertRaises(AgentError) as ctx:
            undo_card(self.bot, self.session, card, self.sessions)
        self.assertIn('又被改过', str(ctx.exception))

    def test_apply_twice_is_refused(self):
        card = apply_card(self.bot, self.session, self.card('回复太长了，改成 50 字'), self.sessions)
        with self.assertRaises(AgentError):
            apply_card(self.bot, self.session, card, self.sessions)

    def test_market_plugin_cannot_be_toggled(self):
        card = self.sessions.add_card(self.session['id'], {
            'title': 't', 'actions': [{'action': 'toggle_plugin',
                                       'params': {'plugin_id': 'sticker_steal', 'enabled': True}}]})
        with self.assertRaises(AgentError):
            apply_card(self.bot, self.session, card, self.sessions)

    def test_persona_switch_and_undo(self):
        card = apply_card(self.bot, self.session, self.card('切换到 yandere 人设'), self.sessions)
        self.assertEqual(self.bot.style.active_name, 'yandere')
        undo_card(self.bot, self.session, card, self.sessions)
        self.assertEqual(self.bot.style.active_name, 'default')

    def test_answer_is_readonly_summary(self):
        self.assertIn('记忆', answer(self.bot))

    def test_agent_plugins_only_basic(self):
        from src.plugin_catalog import BASIC_PLUGINS
        self.assertEqual(AGENT_PLUGINS, set(BASIC_PLUGINS))


class SafetyTests(unittest.TestCase):
    """静态扫描：Agent 模块里不允许出现任何发送通道。"""

    def setUp(self):
        from src.plugin_installer import _code_without_comments
        # 只扫"会执行的代码"：文档字符串里写着"不许出现 queue_outgoing"本身不该算违规
        # （和安装预检踩的是同一个坑）。
        self.code = _code_without_comments(
            (ROOT / 'src' / 'wechat_agent.py').read_text('utf-8'))

    def test_no_send_channels(self):
        banned = re.findall(r'queue_outgoing|can_send_proactive|send_to\b|send_text|'
                            r'\.send\(|wechatauto|wxauto', self.code)
        self.assertEqual(banned, [])

    def test_no_model_calls(self):
        self.assertNotIn('ai.chat', self.code)

    def test_service_action_does_not_start_or_stop(self):
        self.assertNotIn('bot.start()', self.code)
        self.assertNotIn('bot.stop()', self.code)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = AgentSessions(Path(self.tmp.name) / 's.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_list(self):
        session = self.sessions.create('第一段')
        listing = self.sessions.list_sessions()
        self.assertEqual(listing[0]['id'], session['id'])
        self.assertEqual(listing[0]['cards'], 0)

    def test_delete(self):
        session = self.sessions.create('x')
        self.assertTrue(self.sessions.delete(session['id']))
        self.assertFalse(self.sessions.delete(session['id']))

    def test_cards_are_capped(self):
        session = self.sessions.create('x')
        for index in range(MAX_CARDS + 5):
            self.sessions.add_card(session['id'], {'title': str(index)})
        self.assertEqual(len(self.sessions.get(session['id'])['cards']), MAX_CARDS)

    def test_sessions_are_capped(self):
        for index in range(MAX_SESSIONS + 3):
            self.sessions.create(f's{index}')
        self.assertEqual(len(self.sessions.list_sessions()), MAX_SESSIONS)

    def test_update_missing_card_raises(self):
        session = self.sessions.create('x')
        with self.assertRaises(KeyError):
            self.sessions.update_card(session['id'], 'nope', state='applied')


if __name__ == '__main__':
    unittest.main()
