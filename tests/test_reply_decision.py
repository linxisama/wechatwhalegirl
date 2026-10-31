"""「回复管理策略未放行」必须说清是哪条规则拦的。

用户实测（另一台账号）：控制台显示「收 7 / 发 0」，运行记录里 7 条全是
"已观察，回复管理策略未放行"—— 但那条日志没有说明是私聊关闭、群不在名单、
点名闸门、还是暂停回复。这里的断言就是"每种拦截都要给出可操作的原因"。
"""

import asyncio
import unittest

from tests.test_wechat_app_flow import GROUP, RuntimeTestCase


class ReplyDecisionReasonTests(RuntimeTestCase):
    def decision(self, runtime, **message):
        base = {'kind': 'group', 'target': GROUP, 'target_name': '测试群',
                'sender_id': 'wxid_a', 'sender_name': '阿明', 'at': True}
        base.update(message)
        return runtime.reply_decision(base)

    async def test_private_is_allowed_by_default(self):
        runtime = await self.start_runtime()
        allowed, reason = self.decision(runtime, kind='private', target='wxid_a')
        self.assertTrue(allowed, reason)

    async def test_private_switch_off_explains_where_to_turn_it_on(self):
        runtime = await self.start_runtime(wechat_only={'listen_private': False,
                                                       'backend': 'mock'})
        allowed, reason = self.decision(runtime, kind='private', target='wxid_a')
        self.assertFalse(allowed)
        self.assertIn('私聊回复已关闭', reason)
        self.assertIn('回复私聊消息', reason)

    async def test_group_outside_the_reply_list_names_the_group(self):
        runtime = await self.start_runtime(wechat_only={'groups': ['另一个群'],
                                                       'backend': 'mock'})
        allowed, reason = self.decision(runtime, target_name='没登记的群')
        self.assertFalse(allowed)
        self.assertIn('回复群列表', reason)
        self.assertIn('没登记的群', reason)

    async def test_group_in_the_reply_list_is_allowed(self):
        runtime = await self.start_runtime(wechat_only={'groups': ['测试群'], 'backend': 'mock'})
        allowed, reason = self.decision(runtime)
        self.assertTrue(allowed, reason)

    async def test_paused_state_is_named(self):
        runtime = await self.start_runtime()
        runtime.plugins.update_settings('robot_admin', {'paused': True})
        allowed, reason = self.decision(runtime)
        self.assertFalse(allowed)
        self.assertIn('暂停回复', reason)

    async def test_group_monitor_reply_switch_is_named(self):
        runtime = await self.start_runtime(wechat_only={'groups': [], 'backend': 'mock'})
        runtime.plugins.update_settings('group_monitor', {'replyEnabled': False})
        allowed, reason = self.decision(runtime)
        self.assertFalse(allowed)
        self.assertIn('回复', reason)
        self.assertTrue('关闭' in reason)

    async def test_reason_reaches_the_runtime_log(self):
        runtime = await self.start_runtime(wechat_only={'groups': ['另一个群'],
                                                       'backend': 'mock'})
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, '@鲸鱼娘 在吗', sender='阿明', at=True)
        await self.wait_for_quiet(1.0)
        notes = [str(event.get('text') or '') for event in runtime.events]
        self.assertTrue(any('未回复' in note and '回复群列表' in note for note in notes),
                        notes[-6:])
