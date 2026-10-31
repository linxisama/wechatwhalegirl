"""回复文本整理与"别复读"：单元 + 真实链路（模拟后端、假模型，离线）。

对应用户报的两件事：
* **"回答重复度很高"** —— 只有提示词的软约束压不住，现在补上"生成后查重 → 换一次说法"；
* **"一次回答两次语句还有空格"** —— 模型吐出来的多余空格与句内复读在发送前统一整理。

发送层刻意不做 strip（`tests/test_pacing_markers.py` 钉住了那个行为），所以整理必须发生在
"模型返回值 → 插件收尾"这一段，测试也照这个边界来断言。
"""

import asyncio
import unittest

from src.reply_text import dedupe_sentences, normalize_key, similar, tidy_reply
from tests.test_wechat_app_flow import GROUP, RuntimeTestCase


class TidyReplyTests(unittest.TestCase):
    def test_chinese_gaps_are_collapsed_but_english_and_code_are_kept(self):
        self.assertEqual(tidy_reply("你好 呀，今天  天气不错  "), "你好呀，今天天气不错")
        self.assertEqual(tidy_reply("The quick brown fox."), "The quick brown fox.")
        self.assertEqual(tidy_reply('print("a  b")  # 注释'), 'print("a  b")  # 注释')

    def test_leading_trailing_whitespace_is_trimmed(self):
        self.assertEqual(tidy_reply("\n  好的。  \n\n"), "好的。")

    def test_paragraph_breaks_are_joined_into_one_line(self):
        # 用户实测：模型用"空一行"分段，微信气泡中间就空一截，看着像多余空格
        self.assertEqual(tidy_reply("第一段。\n\n第二段。"), "第一段。第二段。")
        self.assertEqual(tidy_reply("第一行   \n第二行"), "第一行第二行")
        self.assertNotIn('\n', tidy_reply("甲。\n\n乙。"))

    def test_english_word_boundaries_keep_one_space(self):
        # 中文直接连；英文单词之间必须留一个空格，否则会粘成一个词
        self.assertEqual(tidy_reply("hello\nworld"), "hello world")
        self.assertEqual(tidy_reply("第一句。\nOK 好的"), "第一句。OK 好的")

    def test_repeated_sentence_inside_one_reply_is_dropped_once(self):
        doubled = "哼，主人又拿表情糊弄本鲸鱼娘！刚刚不是才说喜欢吗？哼，主人又拿表情糊弄本鲸鱼娘！"
        self.assertEqual(tidy_reply(doubled), "哼，主人又拿表情糊弄本鲸鱼娘！刚刚不是才说喜欢吗？")

    def test_short_interjections_may_repeat(self):
        # "哈哈""嗯嗯"这种重复是语气，不是复读；短句不参与去重
        self.assertEqual(tidy_reply("哈哈哈哈，别闹了。哈哈哈哈"), "哈哈哈哈，别闹了。哈哈哈哈")

    def test_punctuation_variants_count_as_the_same_sentence(self):
        self.assertEqual(dedupe_sentences("你今天真好看。你今天真好看！"), "你今天真好看。")

    def test_normalize_key_drops_whitespace_and_punctuation(self):
        self.assertEqual(normalize_key("你好，世界！ Hello."), "你好世界hello")
        self.assertEqual(normalize_key(""), "")


class SimilarReplyTests(unittest.TestCase):
    def test_same_text_or_punctuation_only_difference_is_similar(self):
        self.assertTrue(similar("你好呀，今天天气不错", "你好呀今天天气不错！"))

    def test_different_text_is_not_similar(self):
        self.assertFalse(similar("你好呀，今天天气不错", "帮我看看这段代码为什么报错"))

    def test_short_strings_are_not_compared(self):
        # "在吗/在的"这种短句不该被判成复读
        self.assertFalse(similar("在吗", "在的"))

    def test_empty_input_never_matches(self):
        self.assertFalse(similar("", "任何东西"))
        self.assertFalse(similar("任何东西", ""))


class ReplyDedupeAccessorTests(RuntimeTestCase):
    async def test_recent_replies_are_exposed_newest_first(self):
        runtime = await self.start_runtime()
        plugin = runtime.plugins._plugins["reply_dedupe"]
        message = {"kind": "group", "target": GROUP}
        plugin.on_sent(message, "第一句", runtime)
        plugin.on_sent(message, "第二句", runtime)
        self.assertEqual(plugin.recent_replies(message)[:2], ["第二句", "第一句"])


class SentReplyIsTidiedTests(RuntimeTestCase):
    async def test_pipeline_sends_the_tidied_text(self):
        runtime = await self.start_runtime()
        self.install_ai("在的 呀，今天  也不错！在的 呀，今天  也不错！")
        # 注意别用"在吗/早上好"这类寒暄：那会被 quick_reply 秒回插件截胡，模型根本不会被调用
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                GROUP, "@鲸鱼娘 推荐一部电影", sender="阿明", at=True)
        sent = await self.wait_for_send(1)
        self.assertEqual(sent[-1]["text"], "在的呀，今天也不错！")
        rows = await self.wait_for_inbox_done()
        self.assertEqual(rows[-1][2], "在的呀，今天也不错！")


class _SequenceAI:
    """按顺序吐出多条回复的假模型，记录每次调用参数。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if not self.replies:
            return ""
        return self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)


class AvoidRepeatingTests(RuntimeTestCase):
    async def _send(self, text):
        await asyncio.to_thread(self.runtime.bridge.inject_mock_message,
                                GROUP, text, sender="阿明", at=True)
        return await self.wait_for_send(len(self.runtime.bridge.sent_log) + 1)

    async def test_repeat_gets_regenerated_once_with_the_banned_sentence(self):
        runtime = await self.start_runtime()
        ai = _SequenceAI(["那部片子的节奏很慢，适合睡前看。", "那部片子的节奏很慢，适合睡前看。",
                          "换一部吧，我想推个快节奏的。"])
        runtime.ai.chat = ai.chat
        await self._send("@鲸鱼娘 推荐一部电影")
        first = list(runtime.bridge.sent_log)[-1]["text"]
        self.assertIn("睡前看", first)
        await self._send("@鲸鱼娘 再推荐一部")
        texts = [item["text"] for item in runtime.bridge.sent_log]
        self.assertIn("快节奏", texts[-1], texts)
        # 第 3 次调用才是"重生成"，且提示词里明确禁用了上一句
        self.assertEqual(len(ai.calls), 3)
        banned = str(ai.calls[-1][1].get("system_prompt") or "")
        self.assertIn("睡前看", banned)
        self.assertIn("不要使用", banned)

    async def test_regeneration_happens_only_once_even_if_still_similar(self):
        runtime = await self.start_runtime()
        ai = _SequenceAI(["那部片子的节奏很慢，适合睡前看。", "那部片子的节奏很慢，适合睡前看。",
                          "那部片子的节奏很慢，适合睡前看！"])
        runtime.ai.chat = ai.chat
        await self._send("@鲸鱼娘 推荐一部电影")
        await self._send("@鲸鱼娘 再推荐一部")
        # 重生成还是像 → 不再继续折腾，正常发出去（宁可重复也不发不出去）
        self.assertEqual(len(ai.calls), 3)
        self.assertEqual(len(runtime.bridge.sent_log), 2)
        self.assertEqual((await self.wait_for_inbox_done())[-1][0], "sent")

    async def test_distinct_reply_does_not_cost_an_extra_model_call(self):
        runtime = await self.start_runtime()
        ai = _SequenceAI(["那部片子的节奏很慢，适合睡前看。"])
        runtime.ai.chat = ai.chat
        await self._send("@鲸鱼娘 推荐一部电影")
        # 只发了一条消息，模型应只被调用一次（没有触发重生成）
        self.assertEqual(len(ai.calls), 1)

    async def test_regeneration_failure_keeps_the_original_reply(self):
        runtime = await self.start_runtime()
        plugin = runtime.plugins._plugins["reply_dedupe"]
        plugin.on_sent({"kind": "group", "target": GROUP}, "那部片子的节奏很慢，适合睡前看。", runtime)

        class Boom:
            def __init__(self):
                self.calls = 0

            async def chat(self, messages, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return "那部片子的节奏很慢，适合睡前看。"
                raise RuntimeError("模型第二次调用失败")

        boom = Boom()
        runtime.ai.chat = boom.chat
        await self._send("@鲸鱼娘 推荐一部电影")
        self.assertEqual(len(runtime.bridge.sent_log), 1)
        self.assertIn("睡前看", runtime.bridge.sent_log[-1]["text"])
