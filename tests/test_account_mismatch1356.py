"""账号错位判定（1.3.56）：登录的不是机器人账号时不再每 15 秒重连一次。

真实故障：控制台的 `_health` 每 15 秒用 psutil 读微信进程打开的数据库目录，
发现和机器人自己的账号不同就把 online 置 False 触发重连；可用户把微信登成别的账号时，
重连到原账号永远不会成功，于是日志里每 ~37 秒刷一条"检测到微信切换账号"，
一晚上 60 多条，而真正该做的是提示用户切回来并停止重连。
"""
from __future__ import annotations

import unittest

from src.wechat_app import WeChatRuntime


def make_runtime() -> WeChatRuntime:
    """只装配判定需要的两个字段，避免为一个纯函数起整个后端。"""
    rt = WeChatRuntime.__new__(WeChatRuntime)
    rt._account_mismatch_hits = 0
    rt._account_mismatch = {}
    return rt


class MismatchDecisionTests(unittest.TestCase):
    def test_single_hit_is_none(self):
        rt = make_runtime()
        self.assertEqual(rt._mismatch_decision('wxid_bot', 'wxid_other'), 'none')

    def test_second_hit_enters(self):
        rt = make_runtime()
        rt._mismatch_decision('wxid_bot', 'wxid_other')
        self.assertEqual(rt._mismatch_decision('wxid_bot', 'wxid_other'), 'enter')
        self.assertEqual(rt._account_mismatch['current'], 'wxid_bot')
        self.assertEqual(rt._account_mismatch['active'], 'wxid_other')
        self.assertGreater(rt._account_mismatch['since'], 0)

    def test_hold_while_still_other_account(self):
        rt = make_runtime()
        rt._mismatch_decision('wxid_bot', 'wxid_other')
        rt._mismatch_decision('wxid_bot', 'wxid_other')
        for _ in range(5):
            self.assertEqual(rt._mismatch_decision('wxid_bot', 'wxid_other'), 'hold')
        # hold 期间现场保持不变，界面提示才能稳定显示
        self.assertEqual(rt._account_mismatch['active'], 'wxid_other')

    def test_hold_when_psutil_reads_nothing(self):
        # 读不到（active 为空）既不算切回来也不算还是别人：保持错位态，不重连
        rt = make_runtime()
        rt._mismatch_decision('wxid_bot', 'wxid_other')
        rt._mismatch_decision('wxid_bot', 'wxid_other')
        for _ in range(3):
            self.assertEqual(rt._mismatch_decision('wxid_bot', ''), 'hold')
        self.assertEqual(rt._account_mismatch['active'], 'wxid_other')
        # 先读不到、后确认切回来，仍然要能自动恢复
        self.assertEqual(rt._mismatch_decision('wxid_bot', 'wxid_bot'), 'clear')

    def test_clear_when_back_to_bot_account(self):
        rt = make_runtime()
        rt._mismatch_decision('wxid_bot', 'wxid_other')
        rt._mismatch_decision('wxid_bot', 'wxid_other')
        self.assertEqual(rt._mismatch_decision('wxid_bot', 'wxid_bot'), 'clear')
        self.assertEqual(rt._account_mismatch, {})
        # 清干净后，抖动一次不会立刻判成换号
        self.assertEqual(rt._mismatch_decision('wxid_bot', 'wxid_other'), 'none')

    def test_unknown_bot_account_never_enters(self):
        # 机器人自己还没连上、读不到 current 时不能判错位，否则启动即卡死
        rt = make_runtime()
        for _ in range(4):
            self.assertEqual(rt._mismatch_decision('', 'wxid_other'), 'none')
        self.assertEqual(rt._account_mismatch, {})


if __name__ == '__main__':
    unittest.main()
