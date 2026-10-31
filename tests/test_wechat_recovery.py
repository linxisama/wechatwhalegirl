"""回归测试：界面卡死 / 连不上微信 / 收不到也发不出消息。

用户实测报告（2026-10-14）：控制台「界面会异常卡死，然后连接不上微信，连接不成功，
无法回复信息」。逐条定位到的可复现原因，与下面每个测试类一一对应：

1. 界面卡死
   - 微信连接是十几到二十秒的键鼠自动化，而 1.1.0 的界面把这整段等待放在 Qt 主线程里
     （状态轮询、启动/停止/测试、保存配置全是同步 requests）→ 窗口停止响应。
     → `CallApiDispatchTests` / `ApiJobThreadingTests`：请求必须在后台线程跑，
       结果排队回主线程。
   - wechatauto 发送前会**最小化所有与微信重叠的窗口**（控制台自己也在内）且不还原，
     用户看到的是"程序自己消失了"。
     → `RestoreMinimizedWindowsTests`。
2. 连不上微信 / 连错账号
   - 多账号机器上靠 .db 最近修改时间挑账号，会挑到已经退出的旧账号（本机实测：
     实际登录的是鲸鱼娘，却被挑成旧的"西莲寺紫"）。
     → `AccountHintTests`。
   - 账号判定的单次抖动会触发重连风暴（每次重连 15~20 秒，界面像是一直卡着）。
     → `AccountSwitchHysteresisTests`。
3. 无法回复信息
   - 保存配置时**无条件断开重连微信**：只为改一个模型名，机器人就断线二十秒。
     → `SaveKeepsWechatConnectionTests`。
   - 模型名填成服务商界面上的显示名（"Kimi K2.7 Code"）→ HTTP 404，用户无从下手。
     → `ModelIdGuardTests` / `ListModelsTests`。
"""

import asyncio
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import yaml

from src.ai_engine import AIEngine
from src.wechat_bridge import WeChatBridge

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication, QWidget

    import wechat_gui as gui

    APP = QApplication.instance() or QApplication([])
    HAS_QT = True
except Exception:                                    # pragma: no cover - 无 Qt 环境
    HAS_QT = False


def _write_config(root: Path, **wechat_only) -> Path:
    config = root / "config.wechat.yaml"
    data = {
        "ai": {"api_url": "https://api.deepseek.com/v1", "api_key": "fake",
               "model": "deepseek-chat"},
        "wechat": {"backend": "mock"},
        "wechat_only": {"backend": "mock", "groups": [], "listen_private": True,
                        **wechat_only},
    }
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return config


# ── 1. 卡死：窗口被微信发送流程按下去之后必须还原 ────────────────────────────────
class RestoreMinimizedWindowsTests(unittest.TestCase):
    def _bridge(self, tmp):
        return WeChatBridge(Path(tmp) / "data", backend="mock")

    def test_after_send_restores_windows_and_invalidates_front_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            fake_gui = SimpleNamespace(release_foreground=mock.Mock(return_value=2),
                                       _last_visible_ok=True, _last_visible_ts=123.0)
            bridge._wx = SimpleNamespace(_gui=fake_gui)

            bridge._after_send(True, "已发送", "wxid_a")

            fake_gui.release_foreground.assert_called_once_with(True)
            self.assertEqual(bridge.restored_count, 2)
            # 缓存必须作废：否则下一次发送会以为微信还在最前面，键鼠点到别的窗口上
            self.assertFalse(fake_gui._last_visible_ok)
            self.assertEqual(fake_gui._last_visible_ts, 0.0)

    def test_restore_is_attempted_even_when_send_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            fake_gui = SimpleNamespace(release_foreground=mock.Mock(return_value=1),
                                       _last_visible_ok=True, _last_visible_ts=1.0)
            bridge._wx = SimpleNamespace(_gui=fake_gui)
            bridge._after_send(False, "发送接口返回失败", "wxid_a")
            fake_gui.release_foreground.assert_called_once()

    def test_release_failure_falls_back_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            fake_gui = SimpleNamespace(release_foreground=mock.Mock(side_effect=OSError("boom")),
                                       restore_blockers=mock.Mock(return_value=3))
            bridge._wx = SimpleNamespace(_gui=fake_gui)
            self.assertEqual(bridge._restore_minimized_windows(), 3)
            self.assertEqual(bridge.restored_count, 3)

    def test_without_gui_object_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp)
            bridge._wx = SimpleNamespace()          # wxauto4 这类后端没有 _gui
            self.assertEqual(bridge._restore_minimized_windows(), 0)
            bridge._wx = None
            self.assertEqual(bridge._restore_minimized_windows(), 0)


# ── 2. 连不上微信：账号选择必须稳定 ────────────────────────────────────────────
class AccountHintTests(unittest.TestCase):
    def test_hint_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = WeChatBridge(root / "data", backend="mock")
            (root / "wx" / "wxid_active_abcd").mkdir(parents=True)
            bridge.write_account_hint("wxid_active_abcd")
            self.assertEqual(bridge.read_account_hint(str(root / "wx")), "wxid_active_abcd")

    def test_hint_is_ignored_when_that_account_directory_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = WeChatBridge(root / "data", backend="mock")
            account_dir = root / "wx" / "wxid_old_abcd"
            account_dir.mkdir(parents=True)
            bridge.write_account_hint("wxid_old_abcd")
            shutil.rmtree(account_dir)
            self.assertEqual(bridge.read_account_hint(str(root / "wx")), "")

    def test_missing_or_broken_hint_file_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = WeChatBridge(root / "data", backend="mock")
            self.assertEqual(bridge.read_account_hint(str(root)), "")
            (root / "data" / "account_hint.json").write_text("{不是 json", encoding="utf-8")
            self.assertEqual(bridge.read_account_hint(str(root)), "")


class AccountSwitchHysteresisTests(unittest.TestCase):
    """单次账号判定抖动不许触发重连（每次重连要 15~20 秒）。"""

    def _runtime(self, tmp):
        from src.wechat_app import WeChatRuntime

        root = Path(tmp)
        return WeChatRuntime(root=root, config_path=_write_config(root),
                             db_path=root / "m.sqlite3")

    def test_one_off_mismatch_does_not_reconnect(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = self._runtime(tmp)
            try:
                self.assertFalse(runtime._note_account_switch("old_abcd", "new_abcd"))
                self.assertTrue(runtime._note_account_switch("old_abcd", "new_abcd"))
                # 判定成立后计数复位，下一次抖动又要重新数两次
                self.assertFalse(runtime._note_account_switch("old_abcd", "new_abcd"))
            finally:
                runtime.db.close()

    def test_matching_or_unknown_account_resets_the_counter(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = self._runtime(tmp)
            try:
                self.assertFalse(runtime._note_account_switch("a", "b"))
                # 中间读不到账号（psutil 拿不到句柄时就会这样）→ 计数清零，不许攒着
                self.assertFalse(runtime._note_account_switch("", "b"))
                self.assertFalse(runtime._note_account_switch("a", "b"))
                self.assertFalse(runtime._note_account_switch("a", "a"))
                self.assertFalse(runtime._note_account_switch("a", "b"))
                self.assertTrue(runtime._note_account_switch("a", "b"))
            finally:
                runtime.db.close()


# ── 2b. 只改模型不该把微信连接拆掉 ──────────────────────────────────────────────
class SaveKeepsWechatConnectionTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, tmp, **wechat_only):
        from src.wechat_app import WeChatRuntime

        root = Path(tmp)
        return WeChatRuntime(root=root, config_path=_write_config(root, **wechat_only),
                             db_path=root / "m.sqlite3")

    async def test_ai_only_change_keeps_the_bot_connected(self):
        from src.wechat_app import Settings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = self._runtime(tmp)
            try:
                await runtime.start()
                self.assertTrue(runtime.running)
                with mock.patch.object(runtime, "stop", new=mock.AsyncMock()) as stopped, \
                        mock.patch.object(runtime, "start", new=mock.AsyncMock()) as started:
                    # 只改模型：微信接入那几项原样回填（界面上"设置→保存"就是这个行为）
                    await runtime.save(Settings(api_url="https://api.deepseek.com/v1",
                                                model="deepseek-reasoner",
                                                backend="mock"))
                stopped.assert_not_awaited()
                started.assert_not_awaited()
                self.assertTrue(runtime.running)
                self.assertEqual(runtime.ai.model, "deepseek-reasoner")
            finally:
                await runtime.close()

    async def test_wechat_change_still_restarts_the_connection(self):
        from src.wechat_app import Settings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = self._runtime(tmp)
            try:
                await runtime.start()
                with mock.patch.object(runtime, "stop", new=mock.AsyncMock()) as stopped, \
                        mock.patch.object(runtime, "start", new=mock.AsyncMock()) as started:
                    await runtime.save(Settings(api_url="https://api.deepseek.com/v1",
                                                model="deepseek-chat", backend="mock",
                                                groups=["12345678@chatroom"]))
                stopped.assert_awaited_once()
                started.assert_awaited_once()
            finally:
                await runtime.close()


# ── 3. 无法回复：模型名与错误提示 ──────────────────────────────────────────────
class ModelIdGuardTests(unittest.TestCase):
    def test_model_ids_with_spaces_are_accepted(self):
        """带空格的 model id 必须放行（1.2.19 起）。

        早期版本假设"带空格 = 服务商界面上的显示名"并据此**拦住**测试与保存。
        实测有中转服务（本机：workbuddy.tokenmao.cn）的 model id 自带空格，
        用户报的正是"这个无法识别空格，但这个模型需要空格" —— 那条判据让合法配置
        连试都试不了。名字对不对由服务商说了算（失败时会把服务端原话带出来）。
        """
        for name in ("Kimi K2.7 Code", "TM DeepSeek V4", "TM-DeepSeek-V4 免费版",
                     "gpt 4o mini"):
            with self.subTest(name=name):
                self.assertEqual(AIEngine.model_id_problem(name), "")

    def test_real_model_ids_pass(self):
        for name in ("deepseek-chat", "kimi-k2-0905-preview", "gpt-4o-mini", "m"):
            with self.subTest(name=name):
                self.assertEqual(AIEngine.model_id_problem(name), "")

    def test_empty_model_is_rejected(self):
        self.assertIn("不能为空", AIEngine.model_id_problem("   "))
        self.assertIn("不能为空", AIEngine.model_id_problem(""))

    def test_overlong_model_is_rejected(self):
        self.assertIn("过长", AIEngine.model_id_problem("m" * 121))

    def test_404_error_says_which_model_and_url_were_used(self):
        engine = AIEngine("https://api.moonshot.cn/v1", "fake", model="Kimi K2.7 Code",
                          max_retries=1)
        response = httpx.Response(404, request=httpx.Request("POST", engine.api_url),
                                  json={"error": {"message": "model not found"}})
        engine._get_client = mock.AsyncMock(return_value=SimpleNamespace(
            post=mock.AsyncMock(side_effect=httpx.HTTPStatusError(
                "404", request=response.request, response=response))))

        async def run():
            try:
                return await engine.chat([{"role": "user", "content": "hi"}])
            finally:
                await engine.close()

        self.assertEqual(asyncio.run(run()), "")
        self.assertIn("Kimi K2.7 Code", engine.last_error)
        self.assertIn(engine.api_url, engine.last_error)
        # 服务端的原话要说给用户，而不是我们替它下"空格=显示名"的结论
        self.assertIn("model not found", engine.last_error)
        self.assertNotIn("显示名", engine.last_error)


class ListModelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_models_url_is_derived_from_the_chat_endpoint(self):
        for given, expected in (
            ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/models"),
            ("https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/models"),
            ("https://x.test/v1/chat/completions", "https://x.test/v1/models"),
        ):
            with self.subTest(given=given):
                engine = AIEngine(given, "fake", model="m")
                self.assertEqual(engine._models_url(), expected)

    async def test_models_are_parsed_from_the_service_response(self):
        engine = AIEngine("https://api.deepseek.com/v1", "fake", model="m")
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"},
                                   {"id": "deepseek-chat"}]})
        engine._get_client = mock.AsyncMock(return_value=SimpleNamespace(
            get=mock.AsyncMock(return_value=response)))
        self.assertEqual(await engine.list_models(),
                         ["deepseek-chat", "deepseek-reasoner"])

    async def test_failure_is_reported_instead_of_raising(self):
        engine = AIEngine("https://api.deepseek.com/v1", "fake", model="m")
        response = httpx.Response(404, request=httpx.Request("GET", "https://api.deepseek.com/v1/models"))
        engine._get_client = mock.AsyncMock(return_value=SimpleNamespace(
            get=mock.AsyncMock(side_effect=httpx.HTTPStatusError(
                "404", request=response.request, response=response))))
        self.assertEqual(await engine.list_models(), [])
        self.assertIn("404", engine.last_error)

    async def test_401_points_at_the_key_not_at_a_missing_endpoint(self):
        """401 必须说成"Key 不对"，不能说成"服务商没有 /models"。

        用户实际卡在这里：Kimi 的开放平台 / Kimi Code / Kimi 会员三套 API Key 互不通用，
        把别的产品的 Key 填进开放平台端点就是 401。提示里必须给出这条线索。
        """
        engine = AIEngine("https://api.moonshot.cn/v1", "fake", model="m")
        response = httpx.Response(401, request=httpx.Request("GET", "https://api.moonshot.cn/v1/models"))
        engine._get_client = mock.AsyncMock(return_value=SimpleNamespace(
            get=mock.AsyncMock(side_effect=httpx.HTTPStatusError(
                "401", request=response.request, response=response))))
        self.assertEqual(await engine.list_models(), [])
        self.assertIn("401", engine.last_error)
        self.assertIn("API Key", engine.last_error)
        self.assertNotIn("不提供", engine.last_error)


class SaveModelNameTests(unittest.IsolatedAsyncioTestCase):
    """保存配置时的模型名校验：只拦空值/超长，**不再拦空格**。"""

    def _runtime(self, tmp):
        from src.wechat_app import WeChatRuntime

        root = Path(tmp)
        return WeChatRuntime(root=root, config_path=_write_config(root),
                             db_path=root / "m.sqlite3")

    async def test_save_accepts_a_model_id_with_spaces(self):
        """用户实测：中转服务的 model id 自带空格，必须能存进去。"""
        from src.wechat_app import Settings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = self._runtime(tmp)
            try:
                await runtime.save(Settings(api_url="https://workbuddy.tokenmao.cn",
                                            model="TM DeepSeek V4"))
                self.assertEqual(runtime.config.get("ai.model"), "TM DeepSeek V4")
                self.assertEqual(runtime.ai.model, "TM DeepSeek V4")
            finally:
                await runtime.close()

    async def test_save_still_refuses_an_empty_model(self):
        from src.wechat_app import Settings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = self._runtime(tmp)
            runtime.running = True
            before = runtime.config.config_path.read_bytes()
            try:
                with self.assertRaisesRegex(ValueError, "不能为空"):
                    await runtime.save(Settings(api_url="https://api.deepseek.com/v1",
                                                model="   "))
                # 拒绝必须是"没动过任何东西"：连接还在、配置文件原样
                self.assertTrue(runtime.running)
                self.assertEqual(runtime.config.config_path.read_bytes(), before)
            finally:
                await runtime.close()


class AccountSettingTests(unittest.IsolatedAsyncioTestCase):
    """「机器人使用哪个微信号」这个设置要能存进配置并触达 bridge。"""

    def _runtime(self, tmp, **wechat_only):
        from src.wechat_app import WeChatRuntime

        root = Path(tmp)
        return WeChatRuntime(root=root, config_path=_write_config(root, **wechat_only),
                             db_path=root / "m.sqlite3")

    async def test_save_persists_the_account_and_hands_it_to_the_bridge(self):
        from src.wechat_app import Settings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = self._runtime(tmp)
            try:
                await runtime.save(Settings(api_url="https://api.deepseek.com/v1",
                                            model="deepseek-chat", backend="mock",
                                            account="wxid_live_aaaa"))
                self.assertEqual(runtime.config.get("wechat_only.account"), "wxid_live_aaaa")
                self.assertEqual(runtime.config.get("wechat.account"), "wxid_live_aaaa")
                self.assertEqual(runtime.bridge.configured_account, "wxid_live_aaaa")
                self.assertEqual(runtime.public_settings()["account"], "wxid_live_aaaa")
            finally:
                await runtime.close()

    async def test_changing_the_account_counts_as_a_wechat_change(self):
        """改账号必须重连（否则会继续读着旧账号的库）。"""
        from src.wechat_app import Settings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = self._runtime(tmp)
            try:
                await runtime.start()
                with mock.patch.object(runtime, "stop", new=mock.AsyncMock()) as stopped, \
                        mock.patch.object(runtime, "start", new=mock.AsyncMock()) as started:
                    await runtime.save(Settings(api_url="https://api.deepseek.com/v1",
                                                model="deepseek-chat", backend="mock",
                                                account="wxid_live_aaaa"))
                stopped.assert_awaited_once()
                started.assert_awaited_once()
            finally:
                await runtime.close()

    async def test_runtime_reads_the_account_from_config_on_startup(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = self._runtime(tmp, account="wxid_from_config")
            try:
                self.assertEqual(runtime.bridge.configured_account, "wxid_from_config")
                self.assertEqual(runtime.public_settings()["account"], "wxid_from_config")
            finally:
                runtime.db.close()


class WeChatAccountsRouteTests(unittest.TestCase):
    def test_route_lists_accounts_and_reports_the_current_choice(self):
        from fastapi.testclient import TestClient

        from src.wechat_app import WeChatRuntime, create_app

        rows = [{"account": "wxid_live_aaaa", "wxid": "wxid_live", "name": "鲸鱼娘",
                 "opened": True},
                {"account": "wxid_dead_bbbb", "wxid": "wxid_dead", "name": "西莲寺紫",
                 "opened": False}]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            runtime = WeChatRuntime(root=root,
                                    config_path=_write_config(root, account="wxid_live_aaaa"),
                                    db_path=root / "m.sqlite3")
            try:
                with TestClient(create_app(runtime)) as client:
                    with mock.patch("src.wechat_bridge.describe_accounts", return_value=rows):
                        result = client.get("/api/wechat/accounts").json()
                self.assertTrue(result["success"])
                self.assertEqual(result["current"], "wxid_live_aaaa")
                self.assertEqual(len(result["accounts"]), 2)
                self.assertEqual(result["accounts"][0]["name"], "鲸鱼娘")
                self.assertTrue(result["accounts"][0]["opened"])
            finally:
                runtime.db.close()


# ── 3b. 模型列表必须能从界面上拿到 ─────────────────────────────────────────────
class ModelsRouteTests(unittest.TestCase):
    def test_models_route_returns_the_service_list(self):
        from fastapi.testclient import TestClient

        from src.wechat_app import WeChatRuntime, create_app

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            runtime = WeChatRuntime(root=root, config_path=_write_config(root),
                                    db_path=root / "m.sqlite3")
            try:
                with TestClient(create_app(runtime)) as client:
                    with mock.patch.object(runtime.ai, "list_models",
                                           new=mock.AsyncMock(return_value=["deepseek-chat"])):
                        result = client.get("/api/models").json()
                    self.assertTrue(result["success"])
                    self.assertEqual(result["models"], ["deepseek-chat"])
                    with mock.patch.object(runtime.ai, "list_models",
                                           new=mock.AsyncMock(return_value=[])):
                        runtime.ai.last_error = "服务商没有返回可用的模型列表，请按文档手填 model id"
                        failed = client.get("/api/models").json()
                    self.assertFalse(failed["success"])
                    self.assertIn("model id", failed["message"])
            finally:
                runtime.db.close()


# ── 1b. 界面：请求只能在后台线程跑 ─────────────────────────────────────────────
class CallApiDispatchTests(unittest.TestCase):
    def test_owner_without_window_runner_falls_back_to_sync(self):
        owner = QWidget() if HAS_QT else SimpleNamespace()
        with mock.patch.object(gui, "api_get", return_value={"ok": True}) as got:
            seen = {}
            result = gui.call_api(owner, "GET", "status", timeout=3,
                                  on_done=seen.update)
        got.assert_called_once_with("status", timeout=3)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen, {"ok": True})

    @unittest.skipUnless(HAS_QT, "需要 PyQt6")
    def test_owner_with_window_runner_is_dispatched_not_executed_inline(self):
        class FakeWindow(QWidget):
            def __init__(self):
                super().__init__()
                self.calls = []

            def _run_api(self, method, path, data, timeout, on_done):
                self.calls.append((method, path, data, timeout, on_done))

        window = FakeWindow()
        with mock.patch.object(gui, "api_get") as got:
            result = gui.call_api(window, "GET", "status", timeout=3)
        self.assertIsNone(result)
        got.assert_not_called()          # 同步路径不该被走到
        self.assertEqual(window.calls[0][:4], ("GET", "status", None, 3))


@unittest.skipUnless(HAS_QT, "需要 PyQt6")
class ApiJobThreadingTests(unittest.TestCase):
    """结果的回调必须在**主线程**执行。

    踩过的坑：把信号连到普通 lambda 时 Qt 会在**发射信号的线程**里直接调用它，
    回调一碰控件就是跨线程操作，界面会随机崩或者干脆再也刷不出来。
    所以结果派发必须走 ApiJob 自己的方法（对象属于主线程 → 自动排队）。
    """

    def test_callback_runs_on_the_main_thread(self):
        main_thread = threading.get_ident()
        seen = {}
        with mock.patch.object(gui, "_request", return_value={"success": True}):
            job = gui.ApiJob("GET", "status", None, 1)
            job.set_callback(lambda result: seen.update(
                result=result, thread=threading.get_ident()))
            job.done.connect(job.deliver)
            job.start()
            deadline = time.time() + 10
            while time.time() < deadline and "result" not in seen:
                APP.processEvents()
                time.sleep(0.01)
            job.wait(2000)

        self.assertEqual(seen.get("result"), {"success": True})
        self.assertEqual(seen.get("thread"), main_thread)

    def test_request_exception_becomes_a_failed_result(self):
        seen = {}
        with mock.patch.object(gui, "_request", side_effect=RuntimeError("boom")):
            job = gui.ApiJob("GET", "status", None, 1)
            job.set_callback(seen.update)
            job.done.connect(job.deliver)
            job.start()
            deadline = time.time() + 10
            while time.time() < deadline and not seen:
                APP.processEvents()
                time.sleep(0.01)
            job.wait(2000)
        self.assertFalse(seen.get("success"))
        self.assertIn("boom", str(seen.get("message")))


# ── 配置自备份：整个数据目录被清空过一次 ───────────────────────────────────────
class ConfigBackupTests(unittest.IsolatedAsyncioTestCase):
    """config.wechat.yaml 里存着加密后的 API Key、群白名单与触发词。

    本机实测发生过 `%LOCALAPPDATA%\\WhaleGirlWeChat` 被整体清空（配置、聊天库一起消失）：
    用户唯一的 API Key 与五十来个群名都在那一个文件里。所以每次保存都留一份上一版，
    启动时发现主文件不在就自动取回。
    """

    def _runtime(self, tmp, **wechat_only):
        from src.wechat_app import WeChatRuntime

        root = Path(tmp)
        return WeChatRuntime(root=root, config_path=_write_config(root, **wechat_only),
                             db_path=root / "m.sqlite3")

    async def test_every_save_keeps_the_previous_version(self):
        from src.wechat_app import Settings, backup_config, config_backup_path

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = self._runtime(tmp)
            try:
                destination = runtime.config.config_path
                before = destination.read_text(encoding="utf-8")
                self.assertFalse(config_backup_path(destination).exists())
                await runtime.save(Settings(api_url="https://api.deepseek.com/v1",
                                            model="deepseek-reasoner", backend="mock"))
                backup = config_backup_path(destination)
                self.assertTrue(backup.is_file())
                # 备份里必须是**保存前**的那一版，否则等于没备份
                self.assertEqual(backup.read_text(encoding="utf-8"), before)
                self.assertIn("deepseek-reasoner", destination.read_text(encoding="utf-8"))
            finally:
                await runtime.close()
            self.assertTrue(backup_config(destination))   # 已有主文件 → 可再备份

    async def test_startup_recovers_the_config_from_the_backup(self):
        from src.wechat_app import CONFIG_BACKUP_SUFFIX, WeChatRuntime

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            config = _write_config(root, groups=["12345678@chatroom"])
            backup = config.with_name(config.name + CONFIG_BACKUP_SUFFIX)
            shutil.copy2(config, backup)
            original = config.read_text(encoding="utf-8")
            config.unlink()                      # 模拟"整个目录被清空，只剩备份"

            runtime = WeChatRuntime(root=root, config_path=config, db_path=root / "m.sqlite3")
            try:
                self.assertTrue(config.is_file(), "启动时必须从备份取回配置")
                self.assertEqual(config.read_text(encoding="utf-8"), original)
                self.assertEqual(runtime._configured_groups(), ["12345678@chatroom"])
                self.assertTrue(any("备份" in item["text"] for item in runtime.events))
            finally:
                runtime.db.close()

    def test_restore_is_a_noop_when_there_is_nothing_to_restore(self):
        from src.wechat_app import restore_config_backup

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.wechat.yaml"
            self.assertFalse(restore_config_backup(config))       # 主文件与备份都没有
            config.write_text("ai: {}\n", encoding="utf-8")
            (root / "config.wechat.yaml.bak").write_text("old\n", encoding="utf-8")
            # 主文件在 → 不许用备份覆盖用户当前的配置
            self.assertFalse(restore_config_backup(config))
            self.assertEqual(config.read_text(encoding="utf-8"), "ai: {}\n")


if __name__ == "__main__":                            # pragma: no cover
    unittest.main()


class JobLifetimeTests(unittest.TestCase):
    def test_result_keeps_thread_alive_until_finished(self):
        from PyQt6.QtCore import QObject
        release = threading.Event()
        seen = []

        class SlowExitJob(gui.ApiJob):
            def run(self):
                self.done.emit({"success": True})
                release.wait(5)

        class Owner(QObject):
            def __init__(self):
                super().__init__()
                self._api_jobs = set()

            def _release_job(self):
                gui.MainWindow._release_job(self)

        owner = Owner()
        try:
            with mock.patch.object(gui, "ApiJob", SlowExitJob):
                gui.MainWindow._run_api(owner, "GET", "status", on_done=seen.append)
            deadline = time.time() + 3
            while not seen and time.time() < deadline:
                APP.processEvents()
                time.sleep(0.01)
            self.assertTrue(seen)
            self.assertEqual(len(owner._api_jobs), 1)
            job = next(iter(owner._api_jobs))
            self.assertTrue(job.isRunning())
            release.set()
            job.wait(2000)
            APP.processEvents()
            self.assertEqual(len(owner._api_jobs), 0)
        finally:
            release.set()
            for job in list(owner._api_jobs):
                job.wait(2000)
            APP.processEvents()
