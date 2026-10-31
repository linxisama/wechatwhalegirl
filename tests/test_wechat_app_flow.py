"""微信运行时的端到端流程测试（模拟后端 + 假模型，全程离线）。

参考 `src/gui/smoke.py` 的 `_run_backend_checks()`：用 `backend='mock'` 的
WeChatBridge 当微信，把 `ai.chat` 换成固定回复，然后跑完
「入站 → 触发判定 → 插件 → 模型 → 发送」这条链路，逐条断言落库状态与发送记录。

不连微信、不连模型、不联网；每个用例在自己的临时目录里建运行数据。
"""

import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

from src import wechat_app
from src.wechat_app import WeChatRuntime

ROOT = Path(__file__).resolve().parent.parent

# 每次都要跑一次真实链路，给足超时（但正常情况几十毫秒就结束）
WAIT_SECONDS = 15.0

GROUP = "12345678@chatroom"
OTHER_GROUP = "99887766@chatroom"

# 连发合并（burst_merge）默认等 8 秒才把"同一人接着发的消息"合并成一条。
# 那是给真人打字留的窗口，测试里把它压到插件允许的最小值（500ms），
# 链路照样跑，但不用每条消息都等 8 秒。
BURST_CAP_MS = 500


def write_config(root: Path, **overrides) -> Path:
    """写一份离线配置：mock 后端、零发送间隔、可控触发词。"""
    config = {
        "ai": {
            "api_url": "https://api.deepseek.com/v1/chat/completions",
            "api_key": "test-key",
            "model": "deepseek-chat",
            "api_style": "auto",
        },
        "wechat": {
            "backend": "mock",
            "send_interval": 0,
            "max_message_age": 300,
            # 显式收窄触发词：本例重点验证"@ / 触发词"两条唤醒路径，
            # 不让默认的 30 多个词干扰断言。
            "trigger_keywords": ["鲸鱼娘", "小深"],
            "target_groups": [],
            "listen_private": True,
        },
        "wechat_only": {
            "backend": "mock",
            "groups": [],
            "listen_private": True,
        },
    }
    for section, values in overrides.items():
        if isinstance(values, dict):
            config.setdefault(section, {}).update(values)
        else:
            config[section] = values
    path = root / "config.wechat.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path


def raw_message(content, *, chat=GROUP, sender="wxid_a", sender_name="阿明",
                local_id=1, shard="message_0", kind=None, at=False):
    """构造一条原始微信消息（与 wechat_bridge 的 raw 字典同口径）。"""
    data = {
        "username": chat,
        "type": "文本",
        "sender_username": sender,
        "sender_id": sender,
        "content": content,
        "local_id": local_id,
        "shard": shard,
        "create_time": time.time(),
        "attr": "friend",
        "mock_at": bool(at),
    }
    if kind:
        data["mock_kind"] = kind
    return data


class FrozenWeChatTime:
    """把 `src/wechat_app.py` 里的 `time.time()` 钉在一个固定值上。

    连发合并的判定全看入库时间戳的差值：`src/wechat_app.py:1652` 算还要等多久，
    `src/wechat_app.py:1663` 用 `child_created > created + window` 决定第二条算不算同一阵。
    测试里两次 `runtime.ingest` 跑在线程池上，负载高时两次入库的间隔会超过快速模式的
    800ms 窗口（`src/plugins/reply_preferences.py:21` 的 mergeMs，1.3.52 起默认 800），第二条就进不了合并查询——
    那是调度抖动，不是要验的合并逻辑。这里只替换 wechat_app 自己引用的 `time` 名字：
    `time.monotonic()` 仍然走真实时钟，而 asyncio 的 loop.time() 用 `time.monotonic`，
    所以 sleep、超时、事件循环都照常推进。
    """

    def __init__(self, frozen=None):
        self.value = float(time.time() if frozen is None else frozen)
        self._real = time

    def time(self):
        return self.value

    def monotonic(self):
        return self._real.monotonic()

    def __getattr__(self, name):
        # time.strftime 之类照旧走真实模块，冻结只针对 time.time()
        return getattr(self._real, name)


class FakeAI:
    """替换 ai.chat 的假模型；`boom=True` 时一旦被调用就抛异常。"""

    def __init__(self, reply="在的，怎么啦～", boom=False):
        self.reply = reply
        self.boom = boom
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.boom:
            raise AssertionError("这条消息不该调用模型（关键词插件应当就地回掉）")
        return self.reply


class RuntimeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        # 运行时要读人设目录（styles），照 smoke.py 的做法拷一份
        shutil.copytree(ROOT / "styles", self.root / "styles")

    async def asyncTearDown(self):
        runtime = getattr(self, "runtime", None)
        if runtime is not None:
            # close() 幂等：会停任务、关模型客户端、关 sqlite；
            # 不关 sqlite 的话 Windows 上临时目录删不掉（WinError 32）。
            try:
                await runtime.close()
            except Exception:
                pass
        self._tmp.cleanup()

    async def start_runtime(self, **overrides):
        config = write_config(self.root, **overrides)
        self._index = getattr(self, "_index", 0) + 1
        runtime = WeChatRuntime(root=self.root, config_path=config,
                                db_path=self.root / f"messages{self._index}.sqlite3")
        await runtime.start()
        # 压缩连发合并窗口：默认 8 秒是给真人打字留的，测试里没必要等
        runtime.plugins.update_settings("burst_merge", {"capMs": BURST_CAP_MS})
        self.runtime = runtime
        return runtime

    def install_ai(self, reply="在的，怎么啦～", boom=False):
        ai = FakeAI(reply=reply, boom=boom)
        self.runtime.ai.chat = ai.chat
        return ai

    def clear_inbox(self):
        self.runtime.db.execute("DELETE FROM inbox")
        self.runtime.db.commit()

    def inbox_rows(self):
        return self.runtime.db.execute(
            "SELECT state,payload,reply FROM inbox ORDER BY created").fetchall()

    def states(self):
        return [row[0] for row in self.inbox_rows()]

    def pending_count(self):
        return self.runtime.db.execute(
            "SELECT COUNT(*) FROM inbox WHERE state='pending'").fetchone()[0]

    async def wait_for_send(self, count=1, timeout=WAIT_SECONDS):
        """等 sent_log 至少 count 条（顺序：插件秒回或模型回复都会记在这里）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.runtime.bridge.sent_log) >= count:
                return list(self.runtime.bridge.sent_log)
            await asyncio.sleep(0.02)
        self.fail(f"等发送超时：sent_log={self.runtime.bridge.sent_log!r} inbox={self.states()!r}")

    async def wait_for_quiet(self, seconds=1.0):
        """等一段时间，确认"什么都不该发生"。"""
        await asyncio.sleep(seconds)

    async def wait_for_inbox_done(self, timeout=WAIT_SECONDS):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.inbox_rows()
            if rows and all(row[0] in {"sent", "failed", "expired", "merged", "skipped"} for row in rows):
                return rows
            await asyncio.sleep(0.02)
        self.fail(f"等处理超时：inbox={self.inbox_rows()!r}")


class AliasesTests(RuntimeTestCase):
    async def test_aliases_include_persona_and_configured_trigger_words(self):
        runtime = await self.start_runtime()
        aliases = runtime.aliases()
        # 人设显示名 + 配置触发词都要在
        self.assertIn("鲸鱼娘", aliases)
        self.assertIn("小深", aliases)
        self.assertGreaterEqual(len(aliases), 2)

    async def test_short_trigger_words_are_ignored(self):
        runtime = await self.start_runtime(
            wechat={"trigger_keywords": ["鲸鱼娘", "深"], "backend": "mock",
                    "send_interval": 0, "listen_private": True})
        self.assertNotIn("深", runtime.aliases())

    async def test_aliases_never_empty(self):
        # 拿不到人设也要有兜底称呼，否则群聊判定会变成"永远不回"
        runtime = await self.start_runtime(wechat={"trigger_keywords": []})
        self.assertTrue(runtime.aliases())


class GroupTriggerTests(RuntimeTestCase):
    """群聊唤醒：`@` 与"配置触发词"两条路径（注意别用寒暄词，那会被秒回插件截胡）。"""

    async def test_at_bot_gets_a_reply_and_inbox_marks_sent(self):
        runtime = await self.start_runtime()
        ai = self.install_ai("在的，怎么啦～")
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, "@鲸鱼娘 今天有空吗", sender="阿明", at=True)
        sent = await self.wait_for_send(1)
        self.assertIn("在的", sent[-1]["text"])
        self.assertEqual(len(ai.calls), 1)
        rows = await self.wait_for_inbox_done()
        self.assertEqual(rows[-1][0], "sent")
        self.assertEqual(rows[-1][2], "在的，怎么啦～")

    async def test_trigger_word_without_at_gets_a_reply(self):
        # 这是接口变更的重点：群里没 @，但正文喊了"鲸鱼娘"，必须回
        runtime = await self.start_runtime()
        ai = self.install_ai("在的呢～")
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, "鲸鱼娘 今天有空吗", sender="阿明")
        sent = await self.wait_for_send(1)
        self.assertIn("在的", sent[-1]["text"])
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual((await self.wait_for_inbox_done())[-1][0], "sent")

    async def test_other_trigger_word_also_works(self):
        runtime = await self.start_runtime()
        ai = self.install_ai("小深在～")
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, "小深 出来聊两句", sender="阿明")
        sent = await self.wait_for_send(1)
        self.assertEqual(len(ai.calls), 1)
        self.assertIn("小深", sent[-1]["text"])

    async def test_unrelated_name_is_not_a_trigger(self):
        runtime = await self.start_runtime()
        ai = self.install_ai()
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, "小蓝 出来聊两句", sender="阿明")
        await self.wait_for_quiet(0.8)
        self.assertEqual(runtime.bridge.sent_log, [])
        self.assertEqual(self.inbox_rows(), [])
        self.assertEqual(ai.calls, [])

    async def test_group_chatter_without_at_or_trigger_is_not_answered(self):
        runtime = await self.start_runtime()
        ai = self.install_ai()
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, "球赛看完了", sender="阿明")
        await self.wait_for_quiet(0.8)
        self.assertEqual(runtime.bridge.sent_log, [])
        self.assertEqual(self.pending_count(), 0)
        self.assertEqual(self.inbox_rows(), [])
        self.assertEqual(ai.calls, [])

    async def test_private_message_is_answered(self):
        runtime = await self.start_runtime()
        ai = self.install_ai("私聊也在～")
        # 私聊不做触发判定：不用 @ 也不用喊名字
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                "wxid_friend", "晚上有空吗", sender="阿明", kind="private")
        sent = await self.wait_for_send(1)
        self.assertIn("私聊也在", sent[-1]["text"])
        self.assertEqual(len(ai.calls), 1)


class WhitelistTests(RuntimeTestCase):
    async def test_group_outside_whitelist_never_enters_the_queue(self):
        runtime = await self.start_runtime(
            wechat_only={"groups": [GROUP], "backend": "mock", "listen_private": True})
        self.install_ai()
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                OTHER_GROUP, "@鲸鱼娘 今天有空吗", sender="阿明", at=True)
        await self.wait_for_quiet(0.6)
        self.assertEqual(runtime.bridge.sent_log, [])
        self.assertEqual(self.inbox_rows(), [])

    async def test_group_inside_whitelist_is_answered(self):
        runtime = await self.start_runtime(
            wechat_only={"groups": [GROUP], "backend": "mock", "listen_private": True})
        self.install_ai("在的～")
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, "@鲸鱼娘 今天有空吗", sender="阿明", at=True)
        await self.wait_for_send(1)

    async def test_whitelist_change_takes_effect_on_restart(self):
        runtime = await self.start_runtime()
        self.install_ai("在的～")
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                OTHER_GROUP, "@鲸鱼娘 今天有空吗", sender="阿明", at=True)
        await self.wait_for_send(1)
        self.assertEqual(len(self.inbox_rows()), 1)

        # 改配置 → 重启 runtime：白名单外的群从此不再进队列
        await runtime.close()
        write_config(self.root, wechat_only={"groups": [GROUP], "backend": "mock",
                                             "listen_private": True})
        restarted = WeChatRuntime(root=self.root, config_path=self.root / "config.wechat.yaml",
                                  db_path=self.root / "messages_restart.sqlite3")
        self.runtime = restarted
        await restarted.start()
        self.install_ai("在的～")
        self.clear_inbox()
        await asyncio.to_thread(restarted.bridge.inject_mock_message,
                                OTHER_GROUP, "@鲸鱼娘 又来了", sender="阿明", at=True)
        await self.wait_for_quiet(0.6)
        self.assertEqual(restarted.bridge.sent_log, [])
        self.assertEqual(self.inbox_rows(), [])

    async def test_listen_private_false_drops_private_messages(self):
        runtime = await self.start_runtime(
            wechat_only={"groups": [], "backend": "mock", "listen_private": False},
            wechat={"listen_private": False})
        self.install_ai()
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                "wxid_friend", "晚上有空吗", sender="阿明", kind="private")
        await self.wait_for_quiet(0.6)
        self.assertEqual(runtime.bridge.sent_log, [])
        self.assertEqual(self.inbox_rows(), [])


class DedupeTests(RuntimeTestCase):
    async def test_same_message_delivered_twice_is_processed_once(self):
        runtime = await self.start_runtime()
        ai = self.install_ai("只回一次～")
        raw = raw_message("@鲸鱼娘 今天有空吗", at=True)
        await asyncio.to_thread(runtime.ingest, raw)
        await asyncio.to_thread(runtime.ingest, dict(raw))
        sent = await self.wait_for_send(1)
        await self.wait_for_inbox_done()
        await self.wait_for_quiet(0.5)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(self.inbox_rows()), 1)
        self.assertEqual(len(ai.calls), 1)

    async def test_different_messages_are_both_processed(self):
        runtime = await self.start_runtime()
        # 本用例验的是"不同发送者的两条消息各自成一轮、都被处理"。
        # 两条回复必须**不一样**：一样的话会触发 1.2.20 起的"生成后查重→换一次说法"
        # （见 tests/test_reply_text.py），模型调用次数会变成 3，与本用例无关。
        replies = ["都回～", "回一个不一样的～"]

        async def chat(messages, **kwargs):
            ai.calls.append((messages, kwargs))
            return replies[min(len(ai.calls), len(replies)) - 1]

        ai = self.install_ai(replies[0])
        runtime.ai.chat = chat
        # 不同发送者：不会落进连发合并，两条各自成一轮
        await asyncio.to_thread(runtime.ingest, raw_message(
            "@鲸鱼娘 第一句", local_id=1, at=True, sender="wxid_a", sender_name="阿明"))
        await asyncio.to_thread(runtime.ingest, raw_message(
            "@鲸鱼娘 第二句", local_id=2, at=True, sender="wxid_b", sender_name="小红"))
        await self.wait_for_send(2)
        rows = await self.wait_for_inbox_done()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(ai.calls), 2)

    async def test_burst_from_same_person_is_merged_into_one_call(self):
        # 连发合并：同一个人连着说的两句合成一条交给模型。
        # 生效窗口是快速模式的 mergeMs=800ms（1.3.52 起；capMs=500 会被它截短），判定依据是两条消息
        # 的入库时间戳（src/wechat_app.py:1652/1657/1663）。两次 ingest 在线程池上落地，
        # 负载高时先后会被拉开到窗口之外，合并就变成"碰运气"——所以这里把 wechat_app 的
        # 时间源钉住，让"两条落在同一窗口内"这个前提稳定成立；合并逻辑与断言都不动。
        runtime = await self.start_runtime()
        ai = self.install_ai("一起回～")
        with mock.patch.object(wechat_app, "time", FrozenWeChatTime()):
            await asyncio.to_thread(runtime.ingest, raw_message(
                "@鲸鱼娘 第一句", local_id=1, at=True))
            await asyncio.to_thread(runtime.ingest, raw_message(
                "@鲸鱼娘 第二句", local_id=2, at=True))
            await self.wait_for_send(1)
            rows = await self.wait_for_inbox_done()
        self.assertEqual(len(ai.calls), 1)
        self.assertIn("第一句", ai.calls[0][0][-1]["content"])
        self.assertIn("第二句", ai.calls[0][0][-1]["content"])
        self.assertEqual(sorted(row[0] for row in rows), ["merged", "sent"])


class KeywordPluginTests(RuntimeTestCase):
    async def test_keyword_hit_replies_without_calling_the_model(self):
        runtime = await self.start_runtime()
        ai = self.install_ai(boom=True)          # 被调用就抛异常
        runtime.plugins.update_settings("keyword_reply", {
            "enabled": True,
            "rules": "问题=这是关键词回复",
        })
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, "@鲸鱼娘 问题", sender="阿明", at=True)
        sent = await self.wait_for_send(1)
        self.assertEqual(ai.calls, [], "关键词命中时不该调用模型")
        self.assertIn("关键词回复", sent[-1]["text"])
        rows = await self.wait_for_inbox_done()
        self.assertEqual(rows[-1][0], "sent")


class PacingTests(RuntimeTestCase):
    async def test_legacy_pace_markers_no_longer_split_product_reply(self):
        runtime = await self.start_runtime()
        runtime.plugins._settings['send_pacing'] = {
            "enabled": True,
            "minMsPerChar": 1, "maxMsPerChar": 1, "baseMs": 0,
            "minMs": 50, "maxMs": 100, "mediaPacing": True,
        }
        runtime.plugins.update_settings('reply_preferences', {'fastMode':False})
        self.assertNotIn('send_pacing',runtime.plugins._plugins)
        self.install_ai("第一句。\x01PACE:50\x01第二句。")
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, "@鲸鱼娘 说两句", sender="阿明", at=True)
        sent = await self.wait_for_send(1)
        await self.wait_for_quiet(0.3)
        self.assertEqual([entry["text"] for entry in sent], ["第一句。第二句。"])
        self.assertEqual((await self.wait_for_inbox_done())[-1][0], "sent")


if __name__ == "__main__":
    unittest.main()


class QueueRecoveryTests(RuntimeTestCase):
    async def test_hanging_model_is_cancelled_and_next_message_is_sent(self):
        runtime = await self.start_runtime(wechat={"reply_timeout": 0.15})
        for plugin_id in runtime.plugins._plugins:
            runtime.plugins.update_settings(plugin_id, {"enabled": False})
        cancelled = asyncio.Event()
        calls = 0

        async def chat(messages, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return "第二条正常回复"

        runtime.ai.chat = chat
        runtime.ingest(raw_message("鲸鱼娘第一条", local_id=201))
        runtime.ingest(raw_message("鲸鱼娘第二条", local_id=202))
        await self.wait_for_send(timeout=5)
        await self.wait_for_inbox_done(timeout=5)
        rows = runtime.db.execute("SELECT state,error FROM inbox ORDER BY created").fetchall()
        self.assertTrue(cancelled.is_set())
        self.assertEqual([r[0] for r in rows], ["failed", "sent"])
        self.assertIn("回复准备超时", rows[0][1])
        self.assertEqual(len(runtime.bridge.sent_log), 1)
        self.assertEqual(runtime.pipeline["stage"], "idle")

    async def test_failed_transport_is_not_recorded_as_sent(self):
        runtime = await self.start_runtime()
        for plugin_id in runtime.plugins._plugins:
            runtime.plugins.update_settings(plugin_id, {"enabled": False})
        self.install_ai(reply="本次发送失败")

        async def failed_send(*args, **kwargs):
            return False, "输入框不可用"

        runtime.transport.send = failed_send
        runtime.ingest(raw_message("鲸鱼娘测试失败", local_id=203))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = runtime.db.execute("SELECT state,error FROM inbox").fetchone()
            if row and row[0] == "uncertain":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(row[0], "uncertain")
        self.assertIn("输入框不可用", row[1])
        self.assertEqual(runtime.bridge.sent_log, [])

    async def test_background_log_survives_runtime_close(self):
        runtime = await self.start_runtime()
        runtime.note("维护诊断标记")
        log_path = runtime._runtime_log.path
        await runtime.close()
        self.assertIn("维护诊断标记", log_path.read_text(encoding="utf-8"))


class KeywordWakeRecoveryTests(RuntimeTestCase):
    async def test_string_config_uppercase_and_fullwidth_trigger_can_reply(self):
        runtime = await self.start_runtime(wechat={"trigger_keywords": "deepseek,鲸鱼娘"})
        for plugin_id in runtime.plugins._plugins:
            if plugin_id not in {"chat_controls", "mention_gate"}:
                runtime.plugins.update_settings(plugin_id, {"enabled": False})
        ai = self.install_ai(reply="大小写与全角触发成功")
        runtime.ingest(raw_message("ＤＥＥＰＳＥＥＫ帮忙分析故障", local_id=301))
        await self.wait_for_send(timeout=5)
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(runtime.bridge.sent_log[0]["text"], "大小写与全角触发成功")

    async def test_pretty_json_fixed_keyword_does_not_need_model(self):
        import json
        runtime = await self.start_runtime()
        for plugin_id in runtime.plugins._plugins:
            runtime.plugins.update_settings(plugin_id, {"enabled": False})
        runtime.plugins.update_settings("keyword_reply", {"enabled": True, "rules": json.dumps({
            "format": "keyword_reply/v1", "rules": [{"keyword": "Hello", "reply": "固定匹配成功"}]
        }, ensure_ascii=False, indent=2)})
        ai = self.install_ai(boom=True)
        runtime.ingest(raw_message("ＨＥＬＬＯ", local_id=302))
        await self.wait_for_send(timeout=5)
        self.assertEqual(ai.calls, [])
        self.assertEqual(runtime.bridge.sent_log[0]["text"], "固定匹配成功")


class WeChatSpeedDefaultsTests(RuntimeTestCase):
    async def test_wechat_defaults_are_three_seconds_without_changing_shared_plugin(self):
        from src.plugins.burst_merge import BurstMerge
        runtime = self.runtime = WeChatRuntime(root=self.root, config_path=write_config(self.root))
        self.assertEqual(runtime.plugins.get_settings("burst_merge")["capMs"], 3000)
        self.assertEqual(runtime.plugins._plugins["burst_merge"].META.default_settings["capMs"], 3000)
        self.assertEqual(BurstMerge.META.default_settings["capMs"], 8000)

    async def test_explicit_user_merge_window_survives_runtime_defaults(self):
        from src.plugins.manager import PluginManager
        saved = PluginManager(self.root / "data/wechat-only")
        saved.update_settings("burst_merge", {"capMs": 6000})
        runtime = self.runtime = WeChatRuntime(root=self.root, config_path=write_config(self.root))
        self.assertEqual(runtime.plugins.get_settings("burst_merge")["capMs"], 6000)
        runtime.plugins.reload()
        self.assertEqual(runtime.plugins.get_settings("burst_merge")["capMs"], 6000)
        self.assertEqual(runtime.plugins._plugins["burst_merge"].META.default_settings["capMs"], 3000)
