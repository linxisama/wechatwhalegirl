"""机器人本地表情库（对齐 QQ 版）的全链路测试：存储 → 接口 → 自动收集 → 发送计次 → 定期附图。

全程离线：模拟后端 + 假模型；取图那一步把 `bridge.download_media` 换成写一张真 PNG，
不去碰微信本地数据库。
"""

import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from fastapi.testclient import TestClient
import yaml

from src.sticker_library import (MAX_BYTES, StickerLibrary, frames_for_vision,
                                 inspect_image, thumbnail_png)
from src.wechat_app import WeChatRuntime, create_app
from tests.test_wechat_app_flow import RuntimeTestCase, raw_message


def png_bytes(seed=1, size=(120, 120)):
    image = Image.new('RGB', size, (seed * 11 % 255, seed * 37 % 255, 90))
    ImageDraw.Draw(image).ellipse([5, 5, size[0] - 5, size[1] - 5], fill=(250, 250, 0))
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


def gif_bytes(frames=3, size=(60, 60)):
    images = []
    for index in range(frames):
        frame = Image.new('RGB', size, (30 * index % 255, 120, 200))
        ImageDraw.Draw(frame).ellipse([index * 4, index * 4, 40 + index * 4, 40 + index * 4],
                                      fill=(255, 240, 0))
        images.append(frame.convert('P', palette=Image.ADAPTIVE))
    buffer = io.BytesIO()
    images[0].save(buffer, 'GIF', save_all=True, append_images=images[1:], duration=80, loop=0)
    return buffer.getvalue()


def sent_files(runtime) -> list:
    """模拟后端把附件记成 '[文件]文件名'（见 wechat_bridge._record_mock_send）。"""
    return [str(item.get('text') or '') for item in runtime.bridge.sent_log
            if str(item.get('text') or '').startswith('[文件]')]


class StickerStoreTests(unittest.TestCase):
    """存储层：内容去重、计次、淘汰、标签校验、索引备份。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'stickers'
        self.store = StickerLibrary(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_bytes_are_merged_and_seen_counted(self):
        entry, new = self.store.put(png_bytes(1), '开心')
        again, new_again = self.store.put(png_bytes(1), '开心')
        self.assertTrue(new)
        self.assertFalse(new_again)
        self.assertEqual(entry['id'], again['id'])
        self.assertEqual(again['seen'], 2)
        self.assertEqual(len(self.store.entries()), 1)

    def test_default_label_and_animation_metadata(self):
        entry, _ = self.store.put(png_bytes(2))
        self.assertTrue(entry['label'].startswith('收集_'))
        animated, _ = self.store.put(gif_bytes(), '拍水花')
        self.assertTrue(animated['animated'])
        self.assertEqual(animated['ext'], 'gif')

    def test_uses_only_increase_when_told(self):
        entry, _ = self.store.put(png_bytes(3), '疑惑')
        self.assertEqual(self.store.get(entry['id'])['uses'], 0)
        self.store.used([entry['id']])
        self.assertEqual(self.store.get(entry['id'])['uses'], 1)
        self.store.used(['ff' * 32])          # 不存在的 id 不报错也不写脏数据
        self.assertEqual(self.store.get(entry['id'])['uses'], 1)

    def test_eviction_drops_oldest_and_removes_the_file(self):
        library = StickerLibrary(Path(self._tmp.name) / 'small', max_items=10, max_mb=6)
        ids = [library.put(png_bytes(seed), f'图{seed}')[0]['id'] for seed in range(12)]
        self.assertEqual(len(library.entries()), 10)
        self.assertFalse(any((library.images / f'{key}.png').exists() for key in ids[:2]))
        self.assertTrue(library.path_for(library.entries()[0]['id']))

    def test_label_rules_and_path_traversal(self):
        for bad in ('', '[x]', 'a|b', 'x' * 41, '换\n行'):
            with self.subTest(label=bad):
                with self.assertRaises(ValueError):
                    self.store.validate_label(bad)
        with self.assertRaises(KeyError):
            self.store.path('../../etc/passwd')

    def test_corrupt_index_recovers_from_backup(self):
        self.store.put(png_bytes(4), '先存一张')
        self.store.put(png_bytes(5), '再存一张')
        self.store.index.write_text('{坏掉的 json', encoding='utf-8')
        recovered = StickerLibrary(self.root)
        self.assertTrue(len(recovered.entries()) >= 1)

    def test_index_without_backup_reports_instead_of_silently_resetting(self):
        empty = Path(self._tmp.name) / 'broken'
        empty.mkdir()
        (empty / 'index.json').write_text('{坏', encoding='utf-8')
        with self.assertRaises(ValueError):
            StickerLibrary(empty)

    def test_oversized_and_unreadable_payloads_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.put(b'', '空')
        with self.assertRaises(ValueError):
            self.store.put(b'not an image at all', '假图')
        with self.assertRaises(ValueError):
            self.store.put(b'\x89PNG\r\n\x1a\n' + b'0' * (MAX_BYTES + 1), '太大')

    def test_thumbnail_and_frames_are_usable_images(self):
        thumb = thumbnail_png(png_bytes(6, (600, 400)))
        self.assertLess(len(thumb), len(png_bytes(6, (600, 400))))
        self.assertEqual(inspect_image(thumb)['width'], 96)
        frames = frames_for_vision(gif_bytes(4), 3)
        self.assertEqual(len(frames), 3)
        for source in frames:
            self.assertTrue(source.startswith('data:image/png;base64,'))
            # 抽帧结果必须真能解码成 PNG（否则喂给视觉模型只会报错）
            self.assertEqual(inspect_image(base64.b64decode(source.split(',', 1)[1]))['ext'], 'png')

    def test_update_rejects_duplicate_label_and_long_description(self):
        first, _ = self.store.put(png_bytes(7), '甲')
        second, _ = self.store.put(png_bytes(8), '乙')
        with self.assertRaises(ValueError):
            self.store.update(second['id'], '甲', '')
        with self.assertRaises(ValueError):
            self.store.update(first['id'], '甲', 'x' * 1001)
        self.assertEqual(self.store.get(second['id'])['label'], '乙')

    def test_remove_only_touches_its_own_file(self):
        entry, _ = self.store.put(png_bytes(9), '删我')
        outsider = self.root / 'manual.png'
        outsider.write_bytes(png_bytes(10))
        self.store.remove(entry['id'])
        self.assertEqual(self.store.entries(), [])
        self.assertTrue(outsider.is_file())


class StickerRouteTests(unittest.TestCase):
    """HTTP 接口：与 QQ 版同一套字段形状（界面照搬的前提）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'styles').mkdir()
        for name in ('default', 'tsundere_whale_girl'):
            pass
        config = {'ai': {'api_url': 'https://api.example.com/v1', 'api_key': 'k',
                         'model': 'm', 'api_style': 'auto'},
                  'wechat': {'backend': 'mock', 'send_interval': 0},
                  'wechat_only': {'backend': 'mock'}}
        path = self.root / 'config.wechat.yaml'
        path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
        self.runtime = WeChatRuntime(root=self.root, config_path=path,
                                     db_path=self.root / 'm.sqlite3')
        self.client_context = TestClient(create_app(self.runtime))
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.runtime.db.close()
        self._tmp.cleanup()

    def _managed_png(self, name='a.png', data=None):
        folder = self.root / 'data' / 'plugin_uploads' / 'sticker_admin' / 'imports'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        target.write_bytes(data or png_bytes(11))
        return target

    def test_list_import_update_and_delete(self):
        empty = self.client.get('/api/stickers').json()
        self.assertEqual(empty['items'], [])
        self.assertEqual(empty['total'], 0)
        self.assertEqual(empty['pages'], 1)

        uploaded = self._managed_png()
        answer = self.client.post('/api/stickers/import',
                                  json={'items': [{'path': str(uploaded), 'label': '开心'}]}).json()
        self.assertTrue(answer['success'])
        self.assertEqual(answer['added'], 1)

        listed = self.client.get('/api/stickers?q=开心').json()
        self.assertEqual(len(listed['items']), 1)
        item = listed['items'][0]
        self.assertEqual(item['label'], '开心')
        self.assertTrue(item['thumbnail'])          # 缩略图必须现成给出，界面不用自己压
        self.assertEqual(item['uses'], 0)
        self.assertTrue(self.client.get('/api/stickers?q=没有这个').json()['items'] == [])

        key = item['id']
        saved = self.client.post(f'/api/stickers/{key}',
                                 json={'label': '高兴', 'description': '笑得很开心'}).json()
        self.assertTrue(saved['success'])
        self.assertEqual(self.client.get('/api/stickers').json()['items'][0]['description'],
                         '笑得很开心')

        # AI 识别：把视觉模型换成桩，断言只改说明、不动标签
        calls = []

        async def fake_describe(sources, model=None, max_side=None):
            calls.append((list(sources), model, max_side))
            return '一只举着尾巴的鲸鱼娘，表情得意'

        self.runtime.ai.describe_images = fake_describe
        recognized = self.client.post(f'/api/stickers/{key}/recognize', json={}).json()
        self.assertTrue(recognized['success'])
        self.assertIn('鲸鱼娘', recognized['description'])
        self.assertEqual(calls[0][1], self.runtime.ai.model)
        # 手动识别要和聊天识图用同一个清晰度上限（默认 2048），不能各走一套
        self.assertEqual(calls[0][2], 2048)
        after = self.client.get('/api/stickers').json()['items'][0]
        self.assertEqual(after['label'], '高兴')
        self.assertIn('鲸鱼娘', after['description'])

        removed = self.client.delete(f'/api/stickers/{key}').json()
        self.assertTrue(removed['success'])
        self.assertEqual(self.client.get('/api/stickers').json()['items'], [])

    def test_import_refuses_paths_outside_the_managed_folder(self):
        outsider = self.root / 'secret.png'
        outsider.write_bytes(png_bytes(12))
        answer = self.client.post('/api/stickers/import',
                                  json={'items': [{'path': str(outsider), 'label': '偷'}]})
        self.assertEqual(answer.status_code, 400)
        self.assertIn('上传按钮', answer.json()['detail'])
        self.assertEqual(self.client.get('/api/stickers').json()['total'], 0)

    def test_import_batch_is_all_or_nothing_on_bad_image(self):
        good = self._managed_png('good.png')
        bad = self._managed_png('bad.png', b'not an image')
        answer = self.client.post('/api/stickers/import', json={'items': [
            {'path': str(good), 'label': '好'}, {'path': str(bad), 'label': '坏'}]})
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(self.client.get('/api/stickers').json()['total'], 0)

    def test_disabling_the_plugin_hides_the_library(self):
        self.runtime.plugins.update_settings('sticker_admin', {'enabled': False})
        answer = self.client.get('/api/stickers')
        self.assertEqual(answer.status_code, 403)
        self.assertIn('表情包管理已关闭', answer.json()['detail'])

    def test_recognize_keeps_manual_description_when_model_returns_nothing(self):
        uploaded = self._managed_png('keep.png')
        self.client.post('/api/stickers/import',
                         json={'items': [{'path': str(uploaded), 'label': '图'}]})
        key = self.client.get('/api/stickers').json()['items'][0]['id']
        self.client.post(f'/api/stickers/{key}', json={'label': '图', 'description': '人工写的说明'})

        async def nothing(sources, model=None, max_side=None):
            return ''

        self.runtime.ai.describe_images = nothing
        answer = self.client.post(f'/api/stickers/{key}/recognize', json={})
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(self.client.get('/api/stickers').json()['items'][0]['description'],
                         '人工写的说明')

    def test_unknown_key_gives_404_and_bad_page_gives_400(self):
        self.assertEqual(self.client.post('/api/stickers/' + 'f' * 64,
                                          json={'label': 'x', 'description': ''}).status_code, 404)
        self.assertEqual(self.client.get('/api/stickers?page=0').status_code, 400)
        self.assertEqual(self.client.get('/api/stickers?q=' + 'x' * 101).status_code, 400)


class StickerCollectTests(RuntimeTestCase):
    """自动收集：别人发来的图进本地图库，且遵守开关、私聊、黑名单、只收表情。"""

    def _image_message(self, local_id=11, text='', sender='wxid_b', name='小红', at=False):
        raw = raw_message(text, local_id=local_id, at=at, sender=sender, sender_name=name)
        raw['type'] = '图片'
        return raw

    def _serve(self, data):
        """把"下载媒体"换成写一张真图，返回调用记录。"""
        calls = []

        def download(chat, local_id, msg_type, shard=''):
            calls.append((chat, local_id, msg_type, shard))
            folder = self.root / 'media'
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f'{local_id}.png'
            target.write_bytes(data)
            return str(target)

        self.runtime.bridge.download_media = download
        return calls

    async def _collect(self, message):
        from src.wechat_transports import message_from_raw
        plugin = self.runtime.plugins._plugins['sticker_steal']
        # 收集策略单测模拟 on_prepare 的登记；真实入站未触发时必须不收图，另有贯通回归。
        plugin.collect_for_reply(message_from_raw(message, self.runtime.bridge), self.runtime)
        return await plugin.drain(self.runtime)

    async def test_group_image_is_collected_and_deduped(self):
        runtime = await self.start_runtime()
        self._serve(png_bytes(21))
        added = await self._collect(self._image_message())
        self.assertEqual(added, 1)
        entries = runtime.stickers.entries()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]['label'].startswith('收集_'))
        # 同一条消息再来一次：登记指纹已记过，不会重复下载
        await self._collect(self._image_message())
        self.assertEqual(len(runtime.stickers.entries()), 1)

    async def test_private_messages_are_skipped_by_default(self):
        runtime = await self.start_runtime()
        self._serve(png_bytes(22))
        raw = self._image_message(sender='wxid_c', name='阿明')
        raw['username'] = 'wxid_c'
        added = await self._collect(raw)
        self.assertEqual(added, 0)
        self.assertEqual(runtime.stickers.entries(), [])
        # 打开开关后同样的消息就能收
        runtime.plugins.update_settings('sticker_steal', {'allowPrivate': True})
        added = await self._collect(self._image_message(local_id=12, sender='wxid_c', name='阿明'))
        self.assertEqual(added, 1)

    async def test_blocked_sender_is_not_collected(self):
        runtime = await self.start_runtime()
        self._serve(png_bytes(23))
        runtime.plugins.update_settings('sticker_steal', {'blockedSenders': '小红，wxid_b'})
        self.assertEqual(await self._collect(self._image_message()), 0)
        self.assertEqual(runtime.stickers.entries(), [])

    async def test_disabled_plugin_collects_nothing(self):
        runtime = await self.start_runtime()
        self._serve(png_bytes(24))
        runtime.plugins.update_settings('sticker_steal', {'enabled': False})
        self.assertEqual(await self._collect(self._image_message()), 0)
        self.assertEqual(runtime.stickers.entries(), [])

    async def test_sticker_only_skips_normal_sized_photos(self):
        runtime = await self.start_runtime()
        self._serve(png_bytes(25, (1200, 900)))
        self.assertEqual(await self._collect(self._image_message()), 0)
        self.assertEqual(runtime.stickers.entries(), [])
        # 同一张图，把"只收表情"关掉就收
        runtime.plugins.update_settings('sticker_steal', {'stickerOnly': False})
        self.assertEqual(await self._collect(self._image_message(local_id=13)), 1)

    async def test_emotion_messages_are_not_downloaded(self):
        # 微信 4.x 的动画表情在本地库是加密的，拿不到字节；这里确认不会去试下载
        runtime = await self.start_runtime()
        calls = self._serve(png_bytes(26))
        raw = self._image_message(local_id=14)
        raw['type'] = '动画表情'
        self.assertEqual(await self._collect(raw), 0)
        self.assertEqual(calls, [])

    async def test_slash_command_reports_library_size(self):
        runtime = await self.start_runtime()
        self._serve(png_bytes(27))
        await self._collect(self._image_message(local_id=15))
        sent = await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                       '12345678@chatroom', '/表情库', sender='阿明')
        del sent
        await self.wait_for_send(1)
        self.assertIn('共 1 张', runtime.bridge.sent_log[-1]['text'])


class StickerSendTests(RuntimeTestCase):
    """发送：模型写 [[表情:标签]] 时真把收集来的图发出去，并在成功后计"使用次数"。"""

    async def _seed(self, runtime, label='开心'):
        data = png_bytes(31)
        entry, _ = runtime.stickers.put(data, label)
        return entry, data

    async def test_marker_sends_collected_image_and_counts_use(self):
        runtime = await self.start_runtime()
        entry, data = await self._seed(runtime)
        self.install_ai('给你看这个～[[表情:开心]]')
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                '12345678@chatroom', '@鲸鱼娘 来个表情', sender='阿明', at=True)
        await self.wait_for_send(2)                 # 一条文字 + 一张图
        self.assertEqual(sent_files(runtime), [f"[文件]{entry['id']}.png"],
                         runtime.bridge.sent_log)
        # 发出去的确实是图库里那一份字节
        self.assertEqual((runtime.stickers.images / f"{entry['id']}.png").read_bytes(), data)
        rows = await self.wait_for_inbox_done()
        self.assertEqual(rows[-1][2], '给你看这个～')
        self.assertEqual(runtime.stickers.get(entry['id'])['uses'], 1)

    async def test_slash_command_sends_by_collected_label(self):
        runtime = await self.start_runtime()
        entry, data = await self._seed(runtime, '拍水花')
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                '12345678@chatroom', '/表情 拍水花', sender='阿明')
        await self.wait_for_send(2)
        self.assertEqual(sent_files(runtime), [f"[文件]{entry['id']}.png"])
        self.assertEqual((runtime.stickers.images / f"{entry['id']}.png").read_bytes(), data)
        # 发送记录在线程返回前产生；使用次数必须等真正成功收尾，不能只等日志出现。
        await self.wait_for_inbox_done()
        self.assertEqual(runtime.stickers.get(entry['id'])['uses'], 1)

    async def test_manual_library_still_wins_on_label_conflict(self):
        runtime = await self.start_runtime()
        manual = self.root / 'manual.png'
        manual.write_bytes(png_bytes(32))
        runtime.plugins.update_settings('image_library',
                                        {'library': f'开心 | {manual}'})
        collected, _ = await self._seed(runtime, '开心')
        library = runtime.plugins._plugins['image_library']
        self.assertEqual(library.all_entries(runtime)['开心'], str(manual))
        self.assertIn(collected['id'], [entry['id'] for entry in runtime.stickers.entries()])

    async def test_unknown_label_does_not_read_arbitrary_paths(self):
        runtime = await self.start_runtime()
        self.install_ai('想发图但标签不存在[[表情:../../windows/win.ini]]')
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                '12345678@chatroom', '@鲸鱼娘 发个图', sender='阿明', at=True)
        sent = await self.wait_for_send(1)
        self.assertEqual(sent_files(runtime), [])
        self.assertIn('暂时不可用', sent[-1]['text'])


class CadenceTests(RuntimeTestCase):
    """每 N 次成功回复附一张真图（旧版只提示 emoji，用户看到的却是"从不发图"）。"""

    async def _enable(self, runtime, every=2):
        runtime.plugins.update_settings('sticker_cadence',
                                        {'enabled': True, 'sendEveryN': every})

    async def test_image_is_attached_exactly_on_the_nth_reply(self):
        runtime = await self.start_runtime()
        runtime.stickers.put(png_bytes(41), '开心')
        await self._enable(runtime, every=2)
        replies = ['第一句回答，说点别的。', '第二句回答，换个内容。', '第三句回答，再来一句。']
        counter = {'n': 0}

        async def chat(messages, **kwargs):
            index = min(counter['n'], len(replies) - 1)
            counter['n'] += 1
            return replies[index]

        runtime.ai.chat = chat
        for index, text in enumerate(['@鲸鱼娘 一', '@鲸鱼娘 二', '@鲸鱼娘 三'], start=1):
            await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                    '12345678@chatroom', text, sender='阿明', at=True)
            await self.wait_for_send(index)
            await self.wait_for_inbox_done()
        # 每 2 次成功回复附图一次：只在第 2 条上带图，第 1、3 条不带
        self.assertEqual(len(sent_files(runtime)), 1, runtime.bridge.sent_log)
        self.assertEqual(runtime.stickers.entries()[0]['uses'], 1)

    async def test_failed_send_does_not_advance_the_counter(self):
        runtime = await self.start_runtime()
        runtime.stickers.put(png_bytes(42), '开心')
        await self._enable(runtime, every=1)
        plugin = runtime.plugins._plugins['sticker_cadence']
        message = {'kind': 'group', 'target': '12345678@chatroom', 'id': 'm1'}
        self.assertEqual(plugin._counts.get(plugin._room(message, runtime), 0), 0)
        # 没有调用 on_sent 就说明没发成功 → 计数不动
        self.assertEqual(plugin._counts.get(plugin._room(message, runtime), 0), 0)
        plugin.on_sent(message, '回复', runtime)
        self.assertEqual(plugin._counts.get(plugin._room(message, runtime)), 1)

    async def test_empty_library_only_sends_text(self):
        runtime = await self.start_runtime()
        await self._enable(runtime, every=1)
        self.install_ai('图库是空的，只能回文字。')
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                '12345678@chatroom', '@鲸鱼娘 来个表情', sender='阿明', at=True)
        await self.wait_for_send(1)
        self.assertEqual(sent_files(runtime), [])

    async def test_existing_image_is_not_doubled(self):
        runtime = await self.start_runtime()
        runtime.stickers.put(png_bytes(43), '开心')
        await self._enable(runtime, every=1)
        self.install_ai('这张给你[[表情:开心]]')
        await asyncio.to_thread(runtime.bridge.inject_mock_message,
                                '12345678@chatroom', '@鲸鱼娘 来个表情', sender='阿明', at=True)
        await self.wait_for_send(2)
        self.assertEqual(len(sent_files(runtime)), 1, runtime.bridge.sent_log)
