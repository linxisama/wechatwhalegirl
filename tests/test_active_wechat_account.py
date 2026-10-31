import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from src.wechat_bridge import (account_dir_names, account_wxid, active_database_account,
                               describe_accounts, newest_message_account,
                               resolve_account_dir, WeChatBridge)
from src.wechat_app import WeChatRuntime

class ActiveAccountTests(unittest.TestCase):
    def test_live_database_wins_over_unrelated_files(self):
        root=Path(tempfile.gettempdir())/'wechat-accounts'
        process=SimpleNamespace(info={'name':'weixin.exe'},open_files=lambda:[
            SimpleNamespace(path=str(root/'active'/'db_storage'/'message'/'message_0.db')),
            SimpleNamespace(path=str(root/'old'/'other'/'a.db')),
            SimpleNamespace(path=str(root.parent/'outside'/'db_storage'/'a.db'))])
        with patch('psutil.process_iter',return_value=[process]):
            self.assertEqual(active_database_account(str(root)),'active')

    def test_two_open_accounts_are_not_guessed(self):
        root=Path(tempfile.gettempdir())/'wechat-accounts'
        process=SimpleNamespace(info={'name':'weixin.exe'},open_files=lambda:[
            SimpleNamespace(path=str(root/name/'db_storage'/'a.db')) for name in ['a','b']])
        with patch('psutil.process_iter',return_value=[process]):
            self.assertEqual(active_database_account(str(root)),'')

    def test_repeated_listen_does_not_duplicate_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge=WeChatBridge(Path(tmp),backend='mock');bridge.connect()
            callback=Mock()
            bridge.start_listening(callback);bridge.start_listening(callback)
            bridge._emit({'id':'one'})
            callback.assert_called_once()

class StartFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_failure_is_not_reported_as_running(self):
        runtime=SimpleNamespace(running=False,ai=SimpleNamespace(configured=True,model='m'),
            _lifecycle_lock=asyncio.Lock(),transport=SimpleNamespace(resume_sends=Mock()),
            bridge=SimpleNamespace(connect=Mock(return_value=(True,'')),
                start_listening=Mock(return_value=(False,'数据库监听失败'))),
            _configured_groups=lambda:[],on_bridge_message=Mock())
        runtime._start_locked = lambda: WeChatRuntime._start_locked(runtime)
        with self.assertRaisesRegex(ValueError,'消息监听启动失败'):
            runtime.dsh = SimpleNamespace(settings=lambda: {'enabled': False})
            await WeChatRuntime.start(runtime)
        self.assertFalse(runtime.running)


class DisconnectSafetyTests(unittest.TestCase):
    """断开连接**绝不能结束用户的微信进程**。

    wechatauto 的 ``WeChat.ShutDown()`` 名字像普通的收尾清理，实现却是
    ``os.system('taskkill /f /pid <微信主进程>')``。本方法原来把 "Close" 与 "ShutDown"
    挨个调一遍，后果是：

    * 关掉控制台 → 微信被强杀；
    * 连接守护每次自动重连（先 disconnect）→ 微信再被杀一次；
    * 微信重启期间数据库还没打开，"哪个账号在登录"判不出来 → 退化成按文件时间挑
      → 连到已退出的旧账号 → 收不到消息、发出去也失败。

    这就是用户报的"连接不上微信 / 连接不成功 / 无法回复信息"的真凶，所以在这里钉死。
    """

    def test_disconnect_does_not_call_any_destructive_closer(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = WeChatBridge(Path(tmp) / "data", backend="mock")
            calls = []

            class Client:
                def ShutDown(self):            # noqa: N802 - 库里的真实方法名
                    calls.append("ShutDown")

                def Close(self):               # noqa: N802 - 库里的真实方法名
                    calls.append("Close")

                def StopListening(self, remove=True):   # noqa: N802
                    calls.append(("StopListening", remove))

            bridge._wx = Client()
            bridge.online = True
            bridge.disconnect()

            self.assertNotIn("ShutDown", calls, "disconnect 不许强杀微信")
            self.assertNotIn("Close", calls, "disconnect 也不该把微信窗口拽到前台")
            self.assertIn(("StopListening", False), calls)
            self.assertIsNone(bridge._wx)
            self.assertFalse(bridge.online)

    def test_reconnect_does_not_kill_wechat_either(self):
        """重连走的是 disconnect + connect，同样不许出现强杀。"""
        with tempfile.TemporaryDirectory() as tmp:
            bridge = WeChatBridge(Path(tmp) / "data", backend="mock")
            bridge.connect()

            class Killer:
                def ShutDown(self):            # noqa: N802
                    raise AssertionError("重连时不该调用 ShutDown")

            bridge._wx = Killer()
            with patch.object(bridge, "connect", return_value=(True, "ok")), \
                    patch("src.wechat_bridge.time.sleep"):
                self.assertEqual(bridge.reconnect(), (True, "ok"))


class AccountChoiceTests(unittest.TestCase):
    """账号选择的可靠性排序：进程打开的库 > 可靠记忆 > 会话时间 > 弱记忆 > 库启发式。"""

    def _bridge(self, tmp, accounts=("wxid_live_aaaa", "wxid_dead_bbbb")):
        root = Path(tmp) / "wx"
        for name in accounts:
            (root / name / "db_storage").mkdir(parents=True)
        bridge = WeChatBridge(Path(tmp) / "data", backend="mock")
        bridge._db_dir_hint = str(root)
        return bridge, root

    def test_account_dir_names_needs_db_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wx"
            (root / "wxid_a_1111" / "db_storage").mkdir(parents=True)
            (root / "all_users").mkdir(parents=True)
            (root / "not_an_account").mkdir(parents=True)
            self.assertEqual(account_dir_names(str(root)), ["wxid_a_1111"])
            self.assertEqual(account_dir_names(""), [])

    def test_process_signal_wins_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, root = self._bridge(tmp)
            bridge.write_account_hint("wxid_dead_bbbb", "上次记录（来源不确定）")
            with patch("src.wechat_bridge.active_database_account",
                       return_value="wxid_live_aaaa"), \
                    patch("src.wechat_bridge.newest_message_account",
                          return_value="wxid_dead_bbbb"):
                account, basis = bridge.choose_account(str(root))
            self.assertEqual(account, "wxid_live_aaaa")
            self.assertIn("进程", basis)

    def test_reliable_hint_beats_message_recency(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, root = self._bridge(tmp)
            bridge.write_account_hint("wxid_live_aaaa", "微信进程打开的库")
            with patch("src.wechat_bridge.active_database_account", return_value=""), \
                    patch("src.wechat_bridge.newest_message_account",
                          return_value="wxid_dead_bbbb"):
                account, basis = bridge.choose_account(str(root))
            self.assertEqual(account, "wxid_live_aaaa")
            self.assertIn("上次", basis)

    def test_message_recency_used_when_process_signal_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, root = self._bridge(tmp)
            with patch("src.wechat_bridge.active_database_account", return_value=""), \
                    patch("src.wechat_bridge.newest_message_account",
                          return_value="wxid_live_aaaa"):
                account, basis = bridge.choose_account(str(root))
            self.assertEqual(account, "wxid_live_aaaa")
            self.assertIn("会话", basis)

    def test_weak_hint_only_after_recency(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, root = self._bridge(tmp)
            bridge.write_account_hint("wxid_dead_bbbb", "library")
            with patch("src.wechat_bridge.active_database_account", return_value=""), \
                    patch("src.wechat_bridge.newest_message_account", return_value=""):
                account, basis = bridge.choose_account(str(root))
            self.assertEqual(account, "wxid_dead_bbbb")
            self.assertIn("不确定", basis)

    def test_nothing_known_falls_back_to_the_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, root = self._bridge(tmp)
            with patch("src.wechat_bridge.active_database_account", return_value=""), \
                    patch("src.wechat_bridge.newest_message_account", return_value=""):
                account, _ = bridge.choose_account(str(root))
            self.assertEqual(account, "")

    def test_hint_records_which_signal_chose_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, root = self._bridge(tmp)
            bridge.write_account_hint("wxid_live_aaaa", "微信进程打开的库")
            record = bridge.read_account_hint_record(str(root))
            self.assertEqual(record["account"], "wxid_live_aaaa")
            self.assertEqual(record["source"], "微信进程打开的库")
            self.assertEqual(bridge.read_account_hint(str(root)), "wxid_live_aaaa")


class ConfiguredAccountTests(unittest.TestCase):
    """用户在设置里指定的账号优先于任何自动判断。

    一台机器同时登录两个微信号时，"哪个账号在登录"没有权威信号（进程句柄要管理员权限，
    两个号都在跑时库自己也不判定），只能让用户指定；猜错的表现是"连上了却收不到消息"。
    """

    def _bridge(self, tmp, accounts=("wxid_live_aaaa", "wxid_dead_bbbb")):
        root = Path(tmp) / "wx"
        for name in accounts:
            (root / name / "db_storage").mkdir(parents=True)
        bridge = WeChatBridge(Path(tmp) / "data", backend="mock")
        bridge._db_dir_hint = str(root)
        return bridge, root

    def test_resolve_accepts_full_dir_name_or_wxid(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, root = self._bridge(tmp)
            self.assertEqual(resolve_account_dir(str(root), "wxid_live_aaaa"), "wxid_live_aaaa")
            self.assertEqual(resolve_account_dir(str(root), "  wxid_dead_bbbb  "),
                             "wxid_dead_bbbb")
            self.assertEqual(resolve_account_dir(str(root), "wxid_unknown"), "")
            self.assertEqual(resolve_account_dir(str(root), ""), "")

    def test_wxid_strips_only_the_four_char_hash(self):
        """不能按 '_' 切第一段 —— 那会把 wxid_xxx_1a2b 切成 "wxid"。"""
        self.assertEqual(account_wxid("wxid_rerhua6zafil22_7ba3"), "wxid_rerhua6zafil22")
        self.assertEqual(account_wxid("wxid_2rc4z7dz591922_c185"), "wxid_2rc4z7dz591922")
        self.assertEqual(account_wxid(""), "")
        with tempfile.TemporaryDirectory() as tmp:
            _, root = self._bridge(tmp, accounts=("wxid_live_a1b2",))
            # 用户按文档填不带哈希的 wxid 也要能解析到
            self.assertEqual(resolve_account_dir(str(root), "wxid_live"), "wxid_live_a1b2")

    def test_configured_account_beats_every_other_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, root = self._bridge(tmp)
            bridge.configured_account = "wxid_dead_bbbb"
            bridge.write_account_hint("wxid_dead_bbbb", "微信进程打开的库")
            with patch("src.wechat_bridge.active_database_account",
                       return_value="wxid_live_aaaa"), \
                    patch("src.wechat_bridge.newest_message_account",
                          return_value="wxid_live_aaaa"):
                account, basis = bridge.choose_account(str(root))
            self.assertEqual(account, "wxid_dead_bbbb")
            self.assertIn("指定", basis)

    def test_missing_configured_account_falls_back_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, root = self._bridge(tmp)
            bridge.configured_account = "wxid_gone_9999"
            with patch("src.wechat_bridge.active_database_account",
                       return_value="wxid_live_aaaa"):
                account, basis = bridge.choose_account(str(root))
            self.assertEqual(account, "wxid_live_aaaa")
            self.assertTrue(any("没找到" in item["text"] for item in bridge.logs))


class NewestMessageAccountTests(unittest.TestCase):
    def test_picks_the_account_with_the_newest_session_and_skips_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wx"
            for name in ("wxid_old_1111", "wxid_new_2222", "wxid_broken_3333"):
                (root / name / "db_storage").mkdir(parents=True)

            class FakeDB:
                ROWS = {"wxid_old_1111": [{"last_time": 100}],
                        "wxid_new_2222": [{"last_time": 999}, {"last_time": 5}]}

                def __init__(self, db_dir=None, account=None, **kw):
                    if account == "wxid_broken_3333":
                        raise RuntimeError("旧账号的密钥已经没了")
                    self._rows = self.ROWS[account]

                def get_sessions(self, limit=8):
                    return self._rows

            with patch("wechatauto.db.WeChatDB", FakeDB):
                self.assertEqual(newest_message_account(str(root)), "wxid_new_2222")

    def test_single_account_does_not_open_any_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wx"
            (root / "wxid_only_1111" / "db_storage").mkdir(parents=True)
            with patch("wechatauto.db.WeChatDB") as fake:
                self.assertEqual(newest_message_account(str(root)), "")
            fake.assert_not_called()

    def test_no_account_dirs_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(newest_message_account(str(Path(tmp) / "missing")), "")


class DescribeAccountsTests(unittest.TestCase):
    def test_empty_db_dir_is_auto_detected(self):
        """数据库目录留空是默认配置，这时账号列表不能是空的。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wx"
            (root / "wxid_live_aaaa" / "db_storage").mkdir(parents=True)

            class FakeDB:
                def __init__(self, db_dir=None, account=None, **kw):
                    pass

                def get_self_info(self):
                    return {"nick_name": "鲸鱼娘", "remark": ""}

            with patch("wechatauto.db.auto_detect_db_dir", return_value=str(root)), \
                    patch("wechatauto.db.WeChatDB", FakeDB), \
                    patch("src.wechat_bridge.active_database_account",
                          return_value="wxid_live_aaaa"):
                rows = describe_accounts("")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["account"], "wxid_live_aaaa")
            self.assertEqual(rows[0]["name"], "鲸鱼娘")
            self.assertTrue(rows[0]["opened"])

    def test_unreadable_account_still_listed_without_nickname(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wx"
            (root / "wxid_broken_bbbb" / "db_storage").mkdir(parents=True)

            class BrokenDB:
                def __init__(self, db_dir=None, account=None, **kw):
                    raise RuntimeError("密钥没了")

            with patch("wechatauto.db.WeChatDB", BrokenDB), \
                    patch("src.wechat_bridge.active_database_account", return_value=""):
                rows = describe_accounts(str(root))
            self.assertEqual([r["account"] for r in rows], ["wxid_broken_bbbb"])
            self.assertEqual(rows[0]["name"], "")
            self.assertFalse(rows[0]["opened"])

    def test_no_directory_at_all_returns_empty(self):
        with patch("wechatauto.db.auto_detect_db_dir", return_value=None), \
                patch('src.wechat_paths._known_candidates', return_value=iter(())), \
                patch.dict('os.environ', {'WECHATAUTO_DB_DIR': ''}):
            self.assertEqual(describe_accounts(""), [])
