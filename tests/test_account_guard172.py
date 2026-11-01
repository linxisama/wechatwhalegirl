"""账号守卫（1.3.72）：机器人账号 vs 微信当前登录账号。

真机上「能收不能发」最常见的根因不是发送代码坏了，而是微信换了号：配置里还是机器人
账号，实际登录的是另一个号，发送通道在核验阶段被拦，用户只看到一句「发送失败」。
这组测试盯三件事：判定不能猜（读不到就 unknown）、提示必须能照做、改绑只改一个配置项。
"""
import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import account_guard as guard
from src.wechat_app import create_app


class JudgeTests(unittest.TestCase):
    def test_directory_name_and_wxid_are_the_same_account(self):
        self.assertTrue(guard.same_account("wxid_abc12345_ab12", "wxid_abc12345"))

    def test_two_different_accounts_are_not_the_same(self):
        self.assertFalse(guard.same_account("wxid_abc12345_ab12", "wxid_zzz99999_cd34"))

    def test_empty_side_is_never_treated_as_same(self):
        self.assertFalse(guard.same_account("", "wxid_abc12345"))
        self.assertFalse(guard.same_account("wxid_abc12345", ""))

    def test_different_accounts_can_rebind(self):
        state = guard.judge("wxid_a_ab12", "wxid_b_cd34")
        self.assertEqual(state["match"], guard.DIFFERENT)
        self.assertTrue(state["can_rebind"])
        self.assertIn("发不出去", state["hint"])

    def test_same_account_cannot_rebind(self):
        state = guard.judge("wxid_a_ab12", "wxid_a")
        self.assertEqual(state["match"], guard.SAME)
        self.assertFalse(state["can_rebind"])

    def test_without_configured_account_it_is_auto_not_different(self):
        state = guard.judge("", "wxid_b_cd34")
        self.assertEqual(state["match"], guard.AUTO)
        self.assertFalse(state["can_rebind"])

    def test_unknown_wins_over_auto_when_nothing_is_known(self):
        self.assertEqual(guard.judge("", "")["match"], guard.UNKNOWN)
        self.assertEqual(guard.judge("wxid_a_ab12", "")["match"], guard.UNKNOWN)

    def test_hint_names_both_sides_so_the_user_knows_who_is_who(self):
        state = guard.judge("wxid_a_ab12", "wxid_b_cd34", "鲸鱼娘", "西莲寺紫")
        self.assertIn("西莲寺紫", state["hint"])
        self.assertIn("鲸鱼娘", state["hint"])
        self.assertEqual(state["configured_name"], "鲸鱼娘")

    def test_display_prefers_nickname_then_falls_back_to_wxid(self):
        self.assertEqual(guard.display("wxid_a_ab12", "鲸鱼娘"), "鲸鱼娘")
        self.assertEqual(guard.display("wxid_a_ab12"), "wxid_a")


class RebindTargetTests(unittest.TestCase):
    def test_rebind_target_is_the_logged_in_account(self):
        target = guard.rebind_target("wxid_a_ab12", "wxid_b_cd34")
        self.assertTrue(target["ok"])
        self.assertEqual(target["account"], "wxid_b_cd34")

    def test_already_bound_is_refused_even_with_different_spelling(self):
        target = guard.rebind_target("wxid_a_ab12", "wxid_a")
        self.assertFalse(target["ok"])
        self.assertEqual(target["reason"], "already_bound")

    def test_no_active_account_is_refused(self):
        target = guard.rebind_target("wxid_a_ab12", "")
        self.assertFalse(target["ok"])
        self.assertEqual(target["reason"], "no_active_account")


class MismatchDetailTests(unittest.TestCase):
    def test_empty_state_has_no_detail(self):
        self.assertEqual(guard.mismatch_detail({}), "")

    def test_detail_names_both_sides_and_says_what_to_do(self):
        text = guard.mismatch_detail({"current": "wxid_a_ab12", "active": "wxid_b_cd34"})
        self.assertIn("wxid_a", text)
        self.assertIn("wxid_b", text)
        self.assertIn("切回机器人账号", text)


class FakeConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.saved = 0

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save(self):
        self.saved += 1


class FakeBridge:
    def __init__(self, account_dir="wxid_b_cd34"):
        self.db_dir = "C:/fake/db"
        self._db_dir_hint = "C:/fake/db"
        self.account_dir = account_dir
        self.configured_account = "wxid_a_ab12"

    def status(self):
        return {"online": True, "account": "西莲寺紫", "account_dir": self.account_dir}


class FakeBot:
    def __init__(self, account_dir="wxid_b_cd34", running=False):
        self.config = FakeConfig({"wechat_only.account": "wxid_a_ab12",
                                  "wechat.account": "wxid_a_ab12"})
        self.bridge = FakeBridge(account_dir)
        self._account_mismatch = {"current": "wxid_a_ab12", "active": "wxid_b_cd34"}
        self._account_mismatch_hits = 3
        self.running = running
        self.notes = []
        self.calls = []
        self.lock = asyncio.Lock()

    def note(self, text):
        self.notes.append(str(text))

    async def stop(self):
        self.calls.append("stop")
        self.running = False

    async def start(self):
        self.calls.append("start")
        self.running = True


ACCOUNTS = [{"account": "wxid_a_ab12", "name": "鲸鱼娘", "wxid": "wxid_a", "opened": False},
            {"account": "wxid_b_cd34", "name": "西莲寺紫", "wxid": "wxid_b", "opened": True}]


class GuardRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.bot = FakeBot()
        self.app.state.bot = self.bot
        self.client = TestClient(self.app)
        self.patches = [patch("src.wechat_bridge.active_database_account", return_value="wxid_b_cd34"),
                        patch("src.wechat_bridge.describe_accounts", return_value=ACCOUNTS)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_state_reports_different_and_offers_rebind(self):
        body = self.client.get("/api/wechat/account-guard").json()
        self.assertTrue(body["success"])
        self.assertEqual(body["match"], "different")
        self.assertTrue(body["can_rebind"])
        self.assertIn("西莲寺紫", body["hint"])
        self.assertIn("鲸鱼娘", body["hint"])
        self.assertEqual(len(body["accounts"]), 2)

    def test_state_falls_back_to_connected_account_dir(self):
        with patch("src.wechat_bridge.active_database_account", return_value=""):
            body = self.client.get("/api/wechat/account-guard").json()
        self.assertEqual(body["logged_in"], "wxid_b_cd34")
        self.assertEqual(body["match"], "different")

    def test_state_is_unknown_when_nothing_can_be_read(self):
        self.bot.bridge.account_dir = ""
        with patch("src.wechat_bridge.active_database_account", return_value=""):
            body = self.client.get("/api/wechat/account-guard").json()
        self.assertEqual(body["match"], "unknown")
        self.assertFalse(body["can_rebind"])

    def test_rebind_requires_explicit_confirm(self):
        body = self.client.post("/api/wechat/account-guard/rebind", json={}).json()
        self.assertFalse(body["success"])
        self.assertEqual(body["reason"], "not_confirmed")
        self.assertEqual(self.bot.config.saved, 0)

    def test_rebind_writes_both_keys_and_clears_the_mismatch(self):
        body = self.client.post("/api/wechat/account-guard/rebind", json={"confirm": True}).json()
        self.assertTrue(body["success"])
        self.assertEqual(self.bot.config.values["wechat_only.account"], "wxid_b_cd34")
        self.assertEqual(self.bot.config.values["wechat.account"], "wxid_b_cd34")
        self.assertEqual(self.bot.config.saved, 1)
        self.assertEqual(self.bot.bridge.configured_account, "wxid_b_cd34")
        self.assertEqual(self.bot._account_mismatch, {})
        self.assertEqual(self.bot._account_mismatch_hits, 0)
        self.assertTrue(self.bot.notes)
        self.assertFalse(body["restarted"])
        self.assertEqual(self.bot.calls, [])

    def test_rebind_restarts_a_running_bot(self):
        self.bot.running = True
        body = self.client.post("/api/wechat/account-guard/rebind", json={"confirm": True}).json()
        self.assertTrue(body["success"])
        self.assertTrue(body["restarted"])
        self.assertEqual(self.bot.calls, ["stop", "start"])

    def test_rebind_refuses_when_already_bound(self):
        self.bot.config.values["wechat_only.account"] = "wxid_b"
        body = self.client.post("/api/wechat/account-guard/rebind", json={"confirm": True}).json()
        self.assertFalse(body["success"])
        self.assertEqual(body["reason"], "already_bound")
        self.assertEqual(self.bot.config.saved, 0)

    def test_rebind_refuses_when_no_account_is_logged_in(self):
        self.bot.bridge.account_dir = ""
        with patch("src.wechat_bridge.active_database_account", return_value=""):
            body = self.client.post("/api/wechat/account-guard/rebind", json={"confirm": True}).json()
        self.assertFalse(body["success"])
        self.assertEqual(body["reason"], "no_active_account")
        self.assertEqual(self.bot.config.saved, 0)


class FakeTransport:
    online = True

    def __init__(self, ok=False, detail="未唯一核验账号与微信窗口，禁止切换到坐标+OCR通道"):
        self.ok = ok
        self.detail = detail
        self.calls = []

    async def send_to(self, target, text, kind="group", target_name=""):
        self.calls.append((target, text))
        return self.ok, self.detail


class SelfTestMismatchTests(unittest.TestCase):
    """账号错位时，发送自检要把「未唯一核验账号」翻成能照做的说明。"""

    def setUp(self):
        self.app = create_app()
        self.bot = FakeBot()
        self.bot.transport = FakeTransport()
        self.app.state.bot = self.bot
        self.client = TestClient(self.app)

    def test_selftest_failure_becomes_account_mismatch(self):
        body = self.client.post("/api/wechat/selftest-send", json={"confirm": True}).json()
        self.assertFalse(body["success"])
        self.assertEqual(body["reason"], "account_mismatch")
        self.assertIn("切回机器人账号", body["message"])
        self.assertIn("未唯一核验", body["detail"])

    def test_selftest_without_mismatch_keeps_the_original_reason(self):
        self.bot._account_mismatch = {}
        body = self.client.post("/api/wechat/selftest-send", json={"confirm": True}).json()
        self.assertEqual(body["reason"], "send_failed")
        self.assertNotIn("detail", body)


if __name__ == "__main__":
    unittest.main()
