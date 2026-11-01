"""1.3.74：账号守卫读账号昵称的提速与兜底（目录为空不猜、结果缓存、超时不卡面板）。"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import wechat_app
from src.wechat_app import create_app


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
    def __init__(self, db_dir="C:/fake/db", account_dir="wxid_b_cd34"):
        self.db_dir = db_dir
        self._db_dir_hint = db_dir
        self.account_dir = account_dir
        self.configured_account = "wxid_a_ab12"

    def status(self):
        return {"online": True, "account": "西莲寺紫", "account_dir": self.account_dir}


class FakeBot:
    def __init__(self, db_dir="C:/fake/db", account_dir="wxid_b_cd34"):
        self.config = FakeConfig({"wechat_only.account": "wxid_a_ab12",
                                  "wechat.account": "wxid_a_ab12"})
        self.bridge = FakeBridge(db_dir, account_dir)
        self._account_mismatch = {}
        self._account_mismatch_hits = 0
        self.running = False
        self.notes = []
        self.calls = []
        self.lock = asyncio.Lock()

    def note(self, text):
        self.notes.append(str(text))

    async def stop(self):
        self.calls.append("stop")

    async def start(self):
        self.calls.append("start")


ACCOUNTS = [{"account": "wxid_a_ab12", "name": "鲸鱼娘", "wxid": "wxid_a", "opened": False},
            {"account": "wxid_b_cd34", "name": "西莲寺紫", "wxid": "wxid_b", "opened": True}]


class GuardFastTests(unittest.TestCase):
    def make(self, bot=None):
        app = create_app()
        app.state.bot = bot or FakeBot()
        return TestClient(app)

    def test_empty_directory_never_guesses(self):
        client = self.make(FakeBot(db_dir="", account_dir=""))
        with patch("src.wechat_bridge.describe_accounts") as discover, \
                patch("src.wechat_bridge.active_database_account", return_value=""):
            body = client.get("/api/wechat/account-guard").json()
        self.assertTrue(body["success"])
        self.assertEqual(body["accounts"], [])
        discover.assert_not_called()

    def test_names_are_cached_between_calls(self):
        client = self.make()
        with patch("src.wechat_bridge.describe_accounts", return_value=ACCOUNTS) as discover, \
                patch("src.wechat_bridge.active_database_account", return_value="wxid_b_cd34"):
            first = client.get("/api/wechat/account-guard").json()
            second = client.get("/api/wechat/account-guard").json()
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(len(first["accounts"]), 2)
        self.assertEqual(first["accounts"], second["accounts"])
        self.assertIn("西莲寺紫", first["hint"])

    def test_new_app_does_not_reuse_old_cache(self):
        client = self.make()
        with patch("src.wechat_bridge.describe_accounts", return_value=ACCOUNTS) as discover, \
                patch("src.wechat_bridge.active_database_account", return_value=""):
            client.get("/api/wechat/account-guard")
            self.make().get("/api/wechat/account-guard")
        self.assertEqual(discover.call_count, 2)

    def test_slow_lookup_does_not_hang_the_panel(self):
        client = self.make()

        def slow(directory):
            time.sleep(0.4)
            return ACCOUNTS

        with patch("src.wechat_bridge.describe_accounts", side_effect=slow), \
                patch("src.wechat_bridge.active_database_account", return_value="wxid_b_cd34"), \
                patch.object(wechat_app, "_GUARD_NAMES_TIMEOUT", 0.05):
            started = time.perf_counter()
            body = client.get("/api/wechat/account-guard").json()
            elapsed = time.perf_counter() - started
        self.assertTrue(body["success"])
        self.assertEqual(body["accounts"], [])
        self.assertLess(elapsed, 1.5)
        self.assertEqual(body["match"], "different")

    def test_directory_falls_back_to_connected_account_dir(self):
        bot = FakeBot(db_dir="", account_dir="wxid_b_cd34")
        client = self.make(bot)
        with patch("src.wechat_bridge.describe_accounts", return_value=ACCOUNTS) as discover, \
                patch("src.wechat_bridge.active_database_account", return_value=""):
            body = client.get("/api/wechat/account-guard").json()
        self.assertEqual(discover.call_args.args[0], "wxid_b_cd34")
        self.assertEqual(len(body["accounts"]), 2)


if __name__ == "__main__":
    unittest.main()
