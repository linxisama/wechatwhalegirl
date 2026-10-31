"""群聊主动行为：默认关闭、走安全通道、有节流与免打扰。

⚠️ 主动外发属封号高风险（`AGENTS.md` 第二节）。用户 2026-10-20 明确要求加回来，
所以这里除了功能，最要紧的是把"不会失控"钉死：
1. 默认 `enabled=false`；
2. 发送必须经过 `runtime.queue_outgoing()`（它自己强制 `wechat.proactive.enabled`、
   群白名单、180 秒窗口、去重）；
3. 每小时上限、每群最小间隔、免打扰时段、只在"最近有人说话"的群里开口。
"""

import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.plugins.group_ambient import GroupAmbient, in_quiet_hours, parse_quiet
from src.plugins.manager import PluginManager
from src.wechat_app import WeChatRuntime
from src.chat_state import scope_of
from tests.test_wechat_app_flow import GROUP, write_config


class QuietHoursTests(unittest.TestCase):
    def test_overnight_window(self):
        window = parse_quiet('23-8')
        self.assertTrue(in_quiet_hours(23, window))
        self.assertTrue(in_quiet_hours(2, window))
        self.assertFalse(in_quiet_hours(12, window))

    def test_same_day_window(self):
        window = parse_quiet('8-20')
        self.assertTrue(in_quiet_hours(10, window))
        self.assertFalse(in_quiet_hours(21, window))

    def test_garbage_means_no_quiet_hours(self):
        self.assertIsNone(parse_quiet(''))
        self.assertIsNone(parse_quiet('随便'))
        self.assertFalse(in_quiet_hours(3, parse_quiet('')))


class _Runtime:
    """最小的 runtime：只提供插件用到的 queue_outgoing / db / note / style。"""

    def __init__(self, groups=(), rows=(), allow=True):
        self.queued = []
        self.notes = []
        self._groups = list(groups)
        self._allow = allow
        self.bridge = SimpleNamespace(self_id='wxid_bot')
        self.db = _Db(rows)

    def _configured_groups(self):
        return list(self._groups)

    def queue_outgoing(self, key, kind, target, text):
        if not self._allow:
            return False
        self.queued.append((kind, target, text))
        return True

    def note(self, text):
        self.notes.append(text)


class _Cursor:
    """sqlite3 的游标支持 fetchone/fetchall，假库也要给同形接口。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Db:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append(sql.strip().split()[0].upper())
        if 'COUNT(*)' in sql:
            return _Cursor([(len(self.rows),)])
        if 'ORDER BY created DESC' in sql:
            return _Cursor(self.rows)
        return _Cursor([])

    def commit(self):
        pass


class GroupAmbientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name))
        self.plugin = self.manager._plugins['group_ambient']

    def tearDown(self):
        self.tmp.cleanup()

    def enable(self, **overrides):
        settings = {'enabled': True, 'quietHours': '', 'intervalMin': 5,
                    'echoEnabled': True, 'echoIntervalMin': 5, 'activeWithinMin': 0,
                    'maxPerHour': 3, 'echoMaxChars': 60}
        settings.update(overrides)
        self.manager.update_settings('group_ambient', settings)

    async def test_disabled_plugin_never_sends(self):
        runtime = _Runtime(groups=[GROUP], rows=[('wxid_a', '在吗')])
        await self.plugin.tick(runtime)
        self.assertEqual(runtime.queued, [])

    async def test_echo_reposts_a_short_group_message(self):
        self.enable()
        runtime = _Runtime(groups=[GROUP], rows=[('wxid_a', '今天好热啊')])
        await self.plugin.tick(runtime)
        self.assertEqual(runtime.queued, [('group', GROUP, '今天好热啊')])
        self.assertTrue(any('复读' in note for note in runtime.notes))

    async def test_long_messages_and_commands_are_not_reposted(self):
        self.enable()
        runtime = _Runtime(groups=[GROUP], rows=[('wxid_a', 'x' * 200), ('wxid_a', '/表情')])
        await self.plugin.tick(runtime)
        self.assertEqual(runtime.queued, [])

    async def test_bot_own_lines_are_not_reposted(self):
        self.enable()
        runtime = _Runtime(groups=[GROUP], rows=[('wxid_bot', '这是我说过的')])
        await self.plugin.tick(runtime)
        self.assertEqual(runtime.queued, [])

    async def test_proactive_channel_refusal_is_respected(self):
        # 运行时不让发（wechat.proactive.enabled 关 / 不在白名单）→ 插件必须安静
        self.enable()
        runtime = _Runtime(groups=[GROUP], rows=[('wxid_a', '在吗')], allow=False)
        await self.plugin.tick(runtime)
        self.assertEqual(runtime.queued, [])

    async def test_hourly_cap_stops_further_sends(self):
        self.enable(maxPerHour=1)
        runtime = _Runtime(groups=[GROUP], rows=[('wxid_a', '在吗')])
        await self.plugin.tick(runtime)
        await self.plugin.tick(runtime)
        self.assertEqual(len(runtime.queued), 1)

    async def test_quiet_hours_block_sending(self):
        # 直接构造一个"现在一定落在免打扰里"的窗口：当前小时的起止都等于当前小时
        hour = time.localtime().tm_hour
        self.enable(quietHours=f'{hour}-{hour + 1 if hour < 23 else 0}')
        runtime = _Runtime(groups=[GROUP], rows=[('wxid_a', '在吗')])
        await self.plugin.tick(runtime)
        self.assertEqual(runtime.queued, [])

    async def test_no_groups_means_nothing_to_do(self):
        self.enable()
        runtime = _Runtime(groups=[], rows=[('wxid_a', '在吗')])
        await self.plugin.tick(runtime)
        self.assertEqual(runtime.queued, [])


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """真实运行时：默认配置下（proactive 关）绝不可能发出去。"""

    async def test_proactive_stays_blocked_with_default_config(self):
        root = Path(tempfile.mkdtemp(prefix='ambient-'))
        shutil.copytree(Path(__file__).resolve().parent.parent / 'styles', root / 'styles')
        runtime = WeChatRuntime(root=root, config_path=write_config(root),
                                db_path=root / 'm.sqlite3')
        await runtime.start()
        runtime.plugins.update_settings('group_ambient',
                                        {'enabled': True, 'quietHours': '', 'activeWithinMin': 0,
                                         'echoEnabled': True, 'echoIntervalMin': 5})
        # 灌一条群聊背景，让复读有素材
        scope = scope_of({'kind': 'group', 'target': GROUP}, runtime)
        runtime.chat_state.observe(scope,
                                   {'id': 'm1', 'kind': 'group', 'target': GROUP,
                                    'sender_id': 'wxid_a', 'sender_name': '阿明',
                                    'text': '今天好热啊'})
        plugin = runtime.plugins._plugins['group_ambient']
        await plugin.tick(runtime)
        # 默认 wechat.proactive.enabled=false → queue_outgoing 拒绝，出队为空
        self.assertEqual(runtime.db.execute(
            "SELECT COUNT(*) FROM chat_outbox").fetchone()[0], 0)
        await runtime.close()

    async def test_proactive_allowed_only_after_enabling_the_gate(self):
        root = Path(tempfile.mkdtemp(prefix='ambient2-'))
        shutil.copytree(Path(__file__).resolve().parent.parent / 'styles', root / 'styles')
        runtime = WeChatRuntime(root=root, config_path=write_config(root),
                                db_path=root / 'm.sqlite3')
        await runtime.start()
        runtime.config.set('wechat.proactive.enabled', True)
        runtime.plugins.update_settings('group_monitor',
                                        {'enabled': True, 'replyEnabled': True})
        runtime.plugins.update_settings('group_ambient',
                                        {'enabled': True, 'quietHours': '', 'activeWithinMin': 0,
                                         'echoEnabled': True, 'echoIntervalMin': 5,
                                         'maxPerHour': 3})
        scope = scope_of({'kind': 'group', 'target': GROUP}, runtime)
        runtime.chat_state.observe(scope,
                                   {'id': 'm1', 'kind': 'group', 'target': GROUP,
                                    'sender_id': 'wxid_a', 'sender_name': '阿明',
                                    'text': '今天好热啊'})
        plugin = runtime.plugins._plugins['group_ambient']
        await plugin.tick(runtime)
        pending = runtime.db.execute(
            "SELECT text FROM chat_outbox WHERE state='pending'").fetchall()
        self.assertEqual([row[0] for row in pending], ['今天好热啊'])
        await runtime.close()


if __name__ == '__main__':
    unittest.main()
