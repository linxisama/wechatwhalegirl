"""视频抽帧识别的离线回归测试。

视频和动图走同一条视觉通路：先把视频落到本机，再按时间等间隔抽 N 帧交给视觉模型。
这里锁住三件事：
1. 抽帧是确定性的（第 i 帧取 (i+0.5)/N * duration），同一段视频每次结果一致；
2. 帧数听 videoFrames，并且和图片共享同一条 3 画面上限；
3. 解码库缺失 / 文件坏 / 超限都只降级，绝不抛异常，其它画面照常识别。

全程离线：transport 与 ai 都是桩，视频夹具用 PyAV 现场合成，不发任何网络请求。
"""

import base64
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import av
from PIL import Image

from src.plugins.manager import PluginManager
from src.plugins.sticker_admin import StickerAdmin
from src.video_frames import MAX_VIDEO_SECONDS, NO_DECODER, frames_for_video

_IMAGE_BUFFER = io.BytesIO()
Image.new('RGB', (8, 8), 'blue').save(_IMAGE_BUFFER, 'PNG')
PNG_BYTES = _IMAGE_BUFFER.getvalue()


def _write_video(path, frames=10, rate=5, size=(64, 48)):
    """合成一段无损测试视频：第 i 帧整幅灰度值 i*20，像素值就能反推抽的是第几帧。"""
    container = av.open(str(path), 'w')
    try:
        stream = container.add_stream('rawvideo', rate=rate)
        stream.width, stream.height = size
        stream.pix_fmt = 'rgb24'
        for index in range(frames):
            picture = Image.new('RGB', size, (index * 20,) * 3)
            for packet in stream.encode(av.VideoFrame.from_image(picture)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    return str(path)


def _level_of(data_url):
    """data URL 转回首通道灰度值（合成视频里就等于帧序号*20）。"""
    raw = base64.b64decode(data_url.split(',', 1)[1])
    return Image.open(io.BytesIO(raw)).convert('RGB').getpixel((5, 5))[0]


class VideoFrameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_frames_follow_even_time_offsets(self):
        path = _write_video(self.folder / 'sample.avi')
        # 10 帧 @5fps = 2.0 秒：3 帧落在 0.33/1.0/1.67 秒，即第 2/5/9 帧（灰度 40/100/180）。
        frames, note = frames_for_video(path, 3, 512)
        self.assertEqual(note, '')
        self.assertEqual([_level_of(item) for item in frames], [40, 100, 180])
        # 帧数听 want；want=1 只要一张；超过 3 的取值按上限夹住。
        self.assertEqual(len(frames_for_video(path, 1, 512)[0]), 1)
        self.assertEqual(len(frames_for_video(path, 9, 512)[0]), 3)

    def test_bad_files_report_readable_reasons(self):
        self.assertEqual(frames_for_video(str(self.folder / 'nope.mp4'), 3, 512),
                         ([], '视频文件不存在'))
        broken = self.folder / 'broken.mp4'
        broken.write_bytes(b'not a video at all' * 100)
        self.assertEqual(frames_for_video(str(broken), 3, 512), ([], '视频文件无法解码'))
        empty = self.folder / 'empty.mp4'
        empty.write_bytes(b'')
        self.assertEqual(frames_for_video(str(empty), 3, 512), ([], '视频文件是空的'))

    def test_missing_decoder_only_degrades(self):
        path = _write_video(self.folder / 'sample.avi')
        saved = sys.modules.get('av')
        sys.modules['av'] = None
        try:
            self.assertEqual(frames_for_video(path, 3, 512), ([], NO_DECODER))
        finally:
            if saved is None:
                sys.modules.pop('av', None)
            else:
                sys.modules['av'] = saved

    def test_oversize_and_overlong_videos_are_refused(self):
        path = _write_video(self.folder / 'sample.avi')
        with mock.patch('src.video_frames.MAX_VIDEO_BYTES', 1):
            self.assertIn('MB', frames_for_video(path, 3, 512)[1])
        with mock.patch('src.video_frames.MAX_VIDEO_SECONDS', 0.5):
            reason = frames_for_video(path, 3, 512)[1]
        # 时长上限读的是模块常量（默认 600 秒），这里只锁"超限就拒绝且说明含秒数"
        self.assertGreater(MAX_VIDEO_SECONDS, 0)
        self.assertTrue(reason.startswith('视频超过'))
        self.assertIn('秒', reason)


class VideoVisionPipelineTests(unittest.IsolatedAsyncioTestCase):
    """插件层：视频进同一条视觉通路，帧数与图片共享 3 画面上限。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.folder = Path(self.tmp.name)
        self.manager = PluginManager(self.folder / 'state')
        self.notes = []
        self.ai = SimpleNamespace(api_url='https://api.deepseek.com/v1/chat/completions',
                                  model='deepseek-chat',
                                  describe_images=AsyncMock(return_value='海边的日落'))
        self.paths = {'image': self.png_file(), 'video': _write_video(self.folder / 'clip.avi')}
        self.download = AsyncMock(side_effect=self.download_media)
        self.runtime = SimpleNamespace(ai=self.ai, mode='wechat', account=lambda: '鲸鱼娘',
                                       note=self.notes.append,
                                       transport=SimpleNamespace(call=self.download))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def download_media(self, action, args):
        return {'ok': True, 'path': self.paths.get(args.get('msg_type'), '')}

    def png_file(self):
        target = self.folder / 'a.png'
        target.write_bytes(PNG_BYTES)
        return str(target)

    def set_frames(self, value):
        self.manager._settings.setdefault('sticker_admin', {})['videoFrames'] = value

    def message(self, *kinds):
        media = [{'type': kind, 'local_id': index + 1, 'shard': 'message_0'}
                 for index, kind in enumerate(kinds)]
        return {'kind': 'group', 'target': '12345678@chatroom', 'text': '[视频]', 'media': media}

    async def test_video_message_sends_video_frames_per_setting(self):
        schema = StickerAdmin.META.config_schema['videoFrames']
        self.assertEqual((schema.type, schema.min, schema.max), ('int', 1, 3))
        self.assertEqual(StickerAdmin.META.default_settings['videoFrames'], 3)
        self.assertEqual(StickerAdmin.META.default_settings['gifFrames'], 2)
        for want in (1, 3):
            with self.subTest(videoFrames=want):
                self.set_frames(want)
                self.ai.describe_images.reset_mock()
                self.notes.clear()
                message = self.message('video')
                await self.manager.describe_images(message, self.runtime)
                self.assertEqual(message['vision_status'], 'ok')
                screens = self.ai.describe_images.await_args.args[0]
                self.assertEqual(len(screens), want)
                self.assertTrue(all(item.startswith('data:image/png') for item in screens))
                self.assertTrue(any('视频抽了' in note for note in self.notes))
                self.assertIn('海边的日落', message['vision_description'])

    async def test_image_and_video_share_the_three_screen_budget(self):
        self.set_frames(3)
        message = self.message('image', 'video')
        await self.manager.describe_images(message, self.runtime)
        screens = self.ai.describe_images.await_args.args[0]
        self.assertEqual(len(screens), 3)          # 1 张图 + 视频只剩 2 个名额
        self.assertLessEqual(len(screens), 3)

    async def test_unreadable_video_keeps_the_image_recognized(self):
        self.set_frames(3)
        self.paths['video'] = str(self.folder / 'missing.mp4')
        message = self.message('image', 'video')
        await self.manager.describe_images(message, self.runtime)
        self.assertEqual(len(self.ai.describe_images.await_args.args[0]), 1)
        self.assertIn('海边的日落', message['vision_description'])
        self.assertTrue(message['vision_status'].startswith('部分缺失'))
        self.assertIn('视频', message['vision_status'])
        self.assertTrue(any('视频' in note for note in self.notes))

    async def test_broken_video_and_missing_decoder_still_recognize_the_image(self):
        self.set_frames(3)
        broken = self.folder / 'broken.mp4'
        broken.write_bytes(b'not a video at all' * 100)
        self.paths['video'] = str(broken)
        message = self.message('image', 'video')
        await self.manager.describe_images(message, self.runtime)
        self.assertIn('视频文件无法解码', message['vision_status'])
        self.assertEqual(len(self.ai.describe_images.await_args.args[0]), 1)
        # 冻结版少了 av 也只能降级：图片照常识别，状态里写清缺组件。
        self.ai.describe_images.reset_mock()
        self.paths['video'] = _write_video(self.folder / 'clip2.avi')
        saved = sys.modules.get('av')
        sys.modules['av'] = None
        try:
            message = self.message('image', 'video')
            await self.manager.describe_images(message, self.runtime)
        finally:
            if saved is None:
                sys.modules.pop('av', None)
            else:
                sys.modules['av'] = saved
        self.assertIn(NO_DECODER, message['vision_status'])
        self.assertEqual(len(self.ai.describe_images.await_args.args[0]), 1)
        self.assertIn('海边的日落', message['vision_description'])


if __name__ == '__main__':
    unittest.main()
