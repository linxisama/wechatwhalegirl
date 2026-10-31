"""回复速度三档：真正决定"等多久"的是历史轮数和输出字数。

实测（用户的中转 `TM ·Auto`）：≈1.4 秒固定开销 + 每字 0.12 秒。
所以提速只能靠"少带历史、少让模型写字"，而不是玄学调参。
"""

import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from src.wechat_app import WeChatRuntime
from tests.test_wechat_app_flow import GROUP, raw_message, write_config


class SpeedPlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = WeChatRuntime(root=self.root, config_path=write_config(self.root),
                                     db_path=self.root / 'm.sqlite3')

    def tearDown(self):
        self.runtime.db.close()
        self._tmp.cleanup()

    def test_each_tier_has_stricter_limits_than_the_next(self):
        plans = {}
        for name in ('turbo', 'fast', 'normal'):
            self.runtime.config.set('wechat.reply_speed', name)
            plans[name] = self.runtime.speed_plan()
        self.assertEqual(plans['turbo']['turns'], 2)
        self.assertEqual(plans['fast']['turns'], 4)
        self.assertEqual(plans['normal']['turns'], 6)
        self.assertLess(plans['turbo']['max_tokens'], plans['fast']['max_tokens'])
        self.assertEqual(plans['normal']['max_tokens'], 0)      # 0=不封顶
        self.assertLess(plans['turbo']['vision_timeout'], plans['normal']['vision_timeout'])
        self.assertFalse(plans['turbo']['auto_retry'])
        self.assertTrue(plans['normal']['auto_retry'])

    def test_unknown_value_falls_back_to_fast(self):
        self.runtime.config.set('wechat.reply_speed', '不存在的档位')
        self.assertEqual(self.runtime.speed_plan()['name'], 'fast')


class HistoryDepthTests(unittest.IsolatedAsyncioTestCase):
    """极速档要真的少带历史：直接看取历史那条 SQL 的 LIMIT 参数。"""

    async def _turns_used(self, speed):
        root = Path(tempfile.mkdtemp(prefix='speed-'))
        shutil.copytree(Path(__file__).resolve().parent.parent / 'styles', root / 'styles')
        runtime = WeChatRuntime(root=root, config_path=write_config(root),
                                db_path=root / 'm.sqlite3')
        await runtime.start()
        runtime.config.set('wechat.reply_speed', speed)
        runtime.plugins.update_settings('burst_merge', {'capMs': 500})
        recorded = []

        class Spy:
            """sqlite3.Connection 的 execute 是只读属性，只能包一层。"""

            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=()):
                if 'ORDER BY created DESC LIMIT ?' in sql:
                    recorded.append(params[-1] if params else None)
                return self._conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        runtime.db = Spy(runtime.db)

        async def chat(messages, **kwargs):
            return '好的。'

        runtime.ai.chat = chat
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, '@鲸鱼娘 说句话', sender='阿明', at=True)
        await asyncio.sleep(1.4)
        await runtime.close()
        return recorded[-1] if recorded else None

    async def test_each_tier_queries_a_different_history_depth(self):
        self.assertEqual(await self._turns_used('turbo'), 2)
        self.assertEqual(await self._turns_used('fast'), 4)
        self.assertEqual(await self._turns_used('normal'), 6)


if __name__ == '__main__':
    unittest.main()
