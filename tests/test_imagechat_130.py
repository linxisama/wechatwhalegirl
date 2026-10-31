"""图片中心、回复整形及实验提交通道的离线回归，不联系真实模型或微信。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.image_chat_tags import annotation, rank
from src.sticker_library import StickerLibrary
from src.wechat_background import inspect_background, send_background
from src.wechat_sender import IsolatedWeChatSender
from src.wechat_send_worker import execute
from tests import test_sticker_library as library_fixtures
from tests import test_desktop_140 as desktop_fixtures
from tests import test_wechat_app_flow as flow


class TagTests(unittest.TestCase):
    def test_structured_features_and_plain_description_are_compatible(self):
        data = annotation(json.dumps({'description': '猫咪举爪点赞', 'features': {
            'subject': ['猫咪'], 'emotion': ['开心'], 'action': ['点赞'], 'text': ['谢谢']}}))
        self.assertIn('谢谢', data['auto_tags'])
        self.assertIn('点赞', annotation('猫咪微笑着举爪点赞')['auto_tags'])
        with self.assertRaises(ValueError):
            annotation('{bad-json')

    def test_same_bytes_race_and_manual_edits_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            store = StickerLibrary(folder)
            raw = library_fixtures.png_bytes(130)
            import hashlib
            key = hashlib.sha256(raw).hexdigest()
            store.record_description(key, '小猫开心地点赞')
            self.assertEqual(store.entries(), [])
            entry, _ = store.put(raw)
            self.assertIn('开心', entry['auto_tags'])
            store.update(key, entry['label'], '人工保留', tags='日常,人工')
            store.record_description(key, '不应覆盖')
            self.assertEqual(store.get(key)['description'], '人工保留')
            self.assertFalse(store.annotate(key, annotation('猫咪'), expected_revision=0, overwrite=True))
            saved = StickerLibrary(folder).get(key)
            self.assertEqual(saved['manual_tags'], ['日常', '人工'])
            saved['manual_tags'].append('外部修改')
            self.assertNotIn('外部修改', store.get(key)['manual_tags'])

    def test_relevant_meaning_is_ranked_ahead_of_hash_labels(self):
        values = [{'label': '收集_a', 'description': '委屈哭泣的小猫'},
                  {'label': '收集_b', 'description': '微笑的小猫，表达感谢', 'auto_tags': ['感谢']}]
        self.assertEqual(rank(values, '谢谢你呀')[0]['label'], '收集_b')

    def test_tidy_preserves_mention_boundary_before_removal(self):
        from src.reply_text import tidy_reply
        from src.plugins.chat_controls import strip_at_mentions
        text = tidy_reply('第一句。@海峤载清歌 哼，本小姐在呢。')
        self.assertIn('海峤载清歌 哼', text)
        self.assertEqual(strip_at_mentions(text.split('。', 1)[1]), '哼，本小姐在呢。')


class QueueTests(flow.RuntimeTestCase):
    async def wait_jobs(self, bot):
        for _ in range(100):
            counts = bot.image_chat.status()['counts']
            if not counts.get('pending') and not counts.get('running'):
                return
            await asyncio.sleep(.05)
        self.fail('标注队列未在离线测试预算内结束')

    async def test_fail_one_skip_one_continue_and_deduplicate(self):
        bot = await self.start_runtime()
        keys = [bot.stickers.put(library_fixtures.png_bytes(i))[0]['id'] for i in (131,132)]
        calls = []
        async def recognize(key, **kw):
            calls.append(key)
            if key == keys[0]:
                raise ValueError('bad image')
            return bot.stickers.annotate(key, annotation('猫咪微笑'))
        bot.image_chat.recognize = recognize
        self.assertEqual(bot.image_chat.enqueue(keys)['queued'], 2)
        self.assertEqual(bot.image_chat.enqueue(keys)['skipped'], 2)
        await self.wait_jobs(bot)
        self.assertEqual(bot.image_chat.status()['counts'], {'done': 1, 'failed': 1})
        self.assertEqual(len(calls), 2)
        self.assertEqual(bot.image_chat.enqueue([keys[0]])['queued'], 0)
        bot.image_chat.enqueue([keys[0]], retry=True)
        await self.wait_jobs(bot)
        self.assertEqual(len(calls), 3)

    async def test_background_uses_dedicated_endpoint_without_chat_lock(self):
        from src.ai_engine import AIEngine
        bot = await self.start_runtime()
        bot.plugins.update_settings('image_understanding', {'visionApiUrl': 'https://vision.example/v1',
            'visionApiKey': 'test-vision-key', 'visionModel': 'vision-test'})
        key = bot.stickers.put(library_fixtures.png_bytes(133))[0]['id']
        calls = []
        async def describe(engine, sources, **kwargs):
            calls.append((engine, kwargs))
            return json.dumps({'description':'小猫开心', 'features':{'subject':['猫'], 'emotion':['开心']}})
        with patch.object(AIEngine, 'describe_images', describe):
            async with bot.ai_lock:
                self.assertTrue(await asyncio.wait_for(bot.image_chat.recognize(key, background=True), 1))
        self.assertIsNot(calls[0][0], bot.ai)
        self.assertIs(calls[0][0].usage, bot.ai.usage)
        self.assertEqual(calls[0][1]['api_url'], 'https://vision.example/v1')
        self.assertTrue(calls[0][1]['annotation'])
        self.assertIn('开心', bot.stickers.get(key)['auto_tags'])

    async def test_manual_edit_during_model_call_is_not_overwritten(self):
        from src.ai_engine import AIEngine
        bot = await self.start_runtime()
        entry, _ = bot.stickers.put(library_fixtures.png_bytes(134))
        async def describe(*args, **kwargs):
            bot.stickers.update(entry['id'], entry['label'], '人工刚改的', tags='保留')
            return '模型旧结果'
        with patch.object(AIEngine, 'describe_images', describe):
            self.assertFalse(await bot.image_chat.recognize(entry['id'], background=True))
        self.assertEqual(bot.stickers.get(entry['id'])['description'], '人工刚改的')

    async def test_restart_unknown_running_job_does_not_auto_retry(self):
        from src.image_chat_hub import ImageChatHub
        bot = await self.start_runtime()
        key = bot.stickers.put(library_fixtures.png_bytes(135))[0]['id']
        bot.db.execute('INSERT INTO imagechat_jobs VALUES(?,?,?,?,?,?,?)',
                       (key, 'running', 'auto', bot.account(), 1, 1, ''))
        bot.db.commit()
        await bot.image_chat.close()
        bot.image_chat = ImageChatHub(bot)
        self.assertEqual(bot.image_chat.status()['items'][0]['error'], 'interrupted')
        self.assertEqual(bot.image_chat.status()['counts'], {'failed': 1})

    async def test_untriggered_observed_image_is_not_queued_for_later_collection(self):
        bot = await self.start_runtime()
        plugin = bot.plugins._plugins['sticker_steal']
        original = bot.plugins.run_on_observed
        seen = []
        def observe(message, runtime):
            seen.append(message.get('_observation_only'))
            return original(message, runtime)
        with patch.object(bot.plugins, 'run_on_observed', observe):
            raw = flow.raw_message('没有点名的表情')
            raw['media'] = [{'type':'emotion','url':'https://example.com/unused.gif'}]
            bot.ingest(raw)
        self.assertEqual(seen, [True])
        self.assertFalse(plugin._queue)
        self.assertEqual(bot.image_chat.status()['counts'], {})

    async def test_cancel_active_annotation_preserves_picture_and_no_late_write(self):
        from src.ai_engine import AIEngine
        bot = await self.start_runtime()
        entry, _ = bot.stickers.put(library_fixtures.png_bytes(138))
        started, hold = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await hold.wait()
            return '不应该保存'
        with patch.object(AIEngine, 'describe_images', slow):
            bot.image_chat.enqueue([entry['id']])
            await asyncio.wait_for(started.wait(), 2)
            await bot.image_chat.cancel()
        self.assertEqual(bot.stickers.get(entry['id'])['description'], '')
        self.assertEqual(bot.image_chat.status()['counts'], {'cancelled': 1})

    async def test_unknown_sender_prefix_removed_with_chat_controls_disabled(self):
        bot = await self.start_runtime()
        bot.plugins.update_settings('chat_controls', {'enabled': False})
        self.install_ai('\u200b@海峤载清歌 哼，本小姐在呢。')
        bot.ingest(flow.raw_message('鲸鱼娘 说句话'))
        await self.wait_for_send()
        self.assertEqual(bot.bridge.sent_log[0]['text'], '哼，本小姐在呢。')
        self.assertFalse(bot.bridge.sent_log[0]['at'])


class PostedEnterTests(unittest.TestCase):
    def setUp(self):
        desktop_fixtures.BackgroundTests.setUp(self)
        self.job['background_submit'] = 'posted_enter'
        self.access.post_enter = Mock(side_effect=lambda hwnd: setattr(self.value, 'Value', '') or True)

    def test_posted_enter_is_a_single_alternative_never_invoke_or_focus(self):
        self.assertTrue(send_background(self.job, self.phase, self.access, self.desktop)[0])
        self.access.post_enter.assert_called_once_with(100)
        self.invoke.Invoke.assert_not_called()
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'confirmed')

    def test_unconfirmed_posted_enter_does_not_try_invoke(self):
        clock = iter((0, 3))
        posted = False
        def post_once(hwnd):
            nonlocal posted
            posted = True
            return True
        self.access.post_enter.side_effect = post_once
        # 写入前新增了限时控件复核；只在单次投递之后快进确认等待。
        now = lambda: next(clock) if posted else 0
        info, controls = inspect_background('test', '测试群', self.access, self.desktop, 'posted_enter')
        with patch('src.wechat_background.inspect_background', return_value=(info, controls)), \
             patch('src.wechat_background.time.monotonic', side_effect=now):
            self.assertFalse(send_background(self.job, self.phase, self.access, self.desktop)[0])
        self.access.post_enter.assert_called_once()
        self.invoke.Invoke.assert_not_called()

    def test_unverified_account_never_writes_or_posts(self):
        self.access.matches_account = lambda pid, path: False
        self.assertFalse(send_background(self.job, self.phase, self.access, self.desktop)[0])
        self.value.SetValue.assert_not_called()
        self.access.post_enter.assert_not_called()

    def test_explicit_foreground_skips_background_attempt_at_start(self):
        with patch('src.interactive_desktop.interactive_desktop_status', return_value=(True, '')), \
             patch('wechatauto.uia_driver.WeChatUIA', return_value=Mock()), \
             patch('src.wechat_send_worker.enable_send_compat', return_value=True), \
             patch('src.wechat_background.send_background', side_effect=AssertionError('must not leave a draft')), \
             patch('src.wechat_send_worker.send_text', return_value=(True, 'foreground')) as send, \
             patch('src.wechat_window.install_window_discovery'), \
             patch('src.wechat_send_worker._foreground_window', return_value=1), \
             patch('src.wechat_send_worker._restore_foreground'):
            job = IsolatedWeChatSender(Path('.'), compat=lambda: True)._prepare_job(
                {'action':'text', 'who':'测试群', 'text':'正文'})
            self.assertTrue(execute(job)[0])
            send.assert_called_once()

    def test_native_post_targets_only_bound_window_and_one_enter_pair(self):
        from types import SimpleNamespace
        from src.wechat_window import NativeWindowAccess
        user = SimpleNamespace(PostMessageW=Mock(return_value=True), MapVirtualKeyW=Mock(return_value=28))
        with patch('ctypes.WinDLL', return_value=user):
            self.assertTrue(NativeWindowAccess.post_enter(object(), 4321))
        self.assertEqual([call.args[:3] for call in user.PostMessageW.call_args_list],
                         [(4321, 0x100, 13), (4321, 0x101, 13)])

    def test_account_changed_after_typing_does_not_post(self):
        self.access.matches_account = Mock(side_effect=[True, False])
        self.assertFalse(send_background(self.job, self.phase, self.access, self.desktop)[0])
        self.access.post_enter.assert_not_called()
        self.assertEqual(self.job['_diagnostic']['submission_state'], 'not_submitted')


class OCRTests(unittest.TestCase):
    def test_replacement_garbage_is_not_a_picture_description(self):
        from src.plugins.image_ocr import usable_ocr
        self.assertEqual(usable_ocr('token �� �� �� ��'), '')
        self.assertEqual(usable_ocr('Error 403: 模型不可用'), 'Error 403: 模型不可用')


class HubRouteTests(unittest.TestCase):
    def setUp(self):
        library_fixtures.StickerRouteTests.setUp(self)

    def tearDown(self):
        library_fixtures.StickerRouteTests.tearDown(self)

    def test_batch_jobs_use_real_routes_and_do_not_send_messages(self):
        import time
        entry, _ = self.runtime.stickers.put(library_fixtures.png_bytes(136))
        async def recognize(key, **kwargs):
            return self.runtime.stickers.annotate(key, annotation('小猫开心地点赞'))
        self.runtime.image_chat.recognize = recognize
        result = self.client.post('/api/stickers/annotation-jobs', json={}).json()
        self.assertEqual(result['queued'], 1)
        for _ in range(50):
            status = self.client.get('/api/stickers/annotation-jobs').json()
            if status['counts'].get('done'):
                break
            time.sleep(.05)
        self.assertEqual(status['counts'].get('done'), 1)
        self.assertEqual(self.runtime.bridge.sent_log, [])
        self.assertEqual(self.client.get('/api/stickers?q=点赞').json()['total'], 1)
        self.assertTrue(self.client.post('/api/stickers/annotation-jobs/cancel').json()['success'])

    def test_invalid_batch_does_not_partly_enqueue_and_manual_tags_survive(self):
        entry, _ = self.runtime.stickers.put(library_fixtures.png_bytes(137))
        response = self.client.post('/api/stickers/annotation-jobs', json={'keys':[entry['id'], '../bad']})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.client.get('/api/stickers/annotation-jobs').json()['counts'])
        self.assertTrue(self.client.post('/api/stickers/'+entry['id'], json={
            'label':entry['label'], 'description':'人工说明', 'tags':'感谢,开心'}).json()['success'])
        self.assertEqual(self.client.get('/api/stickers?q=感谢').json()['total'], 1)

    def test_plain_agent_question_is_answered_without_binding_or_applying(self):
        session = self.client.post('/api/agent/sessions', json={}).json()['session']
        for question in ('你好', '你是谁'):
            result = self.client.post('/api/agent/plan', json={'session_id':session['id'], 'text':question}).json()
            self.assertEqual(result['card']['state'], 'answered')
            self.assertIn('助手', result['card']['reply'])
            self.assertFalse(self.runtime.bridge.sent_log)
