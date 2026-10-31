"""客体侧视频抽帧通道的离线回归测试。

沙盒后端下不了媒体（host_bridge.download_media 是 fail-closed 空桩），视频帧只能由
隔离客人端就地解码、随回执送回宿主。这里锁住四件事：
1. 客人端动作只在参数合法时动手，非法参数一律 fail-closed，绝不抛异常；
2. 宿主信封把任何异常与超时都翻成中文失败，绝不让插件崩；
3. Transport.call 只在通道真有 video_frames 能力时才转发；
4. 插件三级取画面顺序固定为 直链 -> 客体抽帧 -> 本机下载，任一层成功即停。

全程离线：bridge 与 transport 都是桩。本端装没装 PyAV 都能跑（装了就用真视频解码）。
"""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

try:  # 解码库是可选依赖：没有它 src/video_frames.py 只会降级成「本端缺少视频解码组件」。
    import av
except Exception:  # pragma: no cover - 取决于本端是否装了 PyAV
    av = None

from src.isolated_wechat.guest_service import GuestService
from src.isolated_wechat.host_bridge import RpcBusy, SandboxBridge, VIDEO_FRAME_TIMEOUT
from src.isolated_wechat.protocol import ACTIONS
from src.plugins.image_understanding import ImageUnderstanding
from src.wechat_transports import WeChatTransport

FRAME = 'data:image/png;base64,AAAA'
INVALID = {'ok': False, 'message': '隔离视频抽帧参数无效', 'frames': []}
NO_MEDIA = {'ok': False, 'message': '视频没有落到本机，未抽帧', 'frames': []}
RPC_FAILED = {'ok': False, 'message': '隔离后台视频抽帧失败或超时', 'frames': []}


def _write_video(path, frames=10, rate=5, size=(64, 48)):
    """合成一段无损测试视频（与 tests/test_video_frames.py 同一套夹具写法）。"""
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


class FakeBridge:
    """客人端的微信桥：只记录 download_media 的实参，绝不碰真机。"""

    def __init__(self, path='', error=None):
        self.calls = []
        self.path = path
        self.error = error

    def download_media(self, chat, local_id, msg_type, shard=''):
        self.calls.append((chat, local_id, msg_type, shard))
        if self.error is not None:
            raise self.error
        return self.path


def _guest(bridge):
    """绕开配对目录：只构造 dispatch 用得到的两个属性。"""
    service = object.__new__(GuestService)
    service.bridge = bridge
    service._overflow = False
    return service


class FakeTransport:
    """插件侧的通道桩：记录动作顺序，按动作回放预设结果。"""

    def __init__(self, frames=(), path=''):
        self.calls, self.params = [], []
        self.frames, self.path = list(frames), path

    async def call(self, action, params=None):
        self.calls.append(action)
        self.params.append(dict(params or {}))
        if action == 'video_frames':
            return {'ok': bool(self.frames), 'frames': list(self.frames), 'message': ''}
        if action == 'download_media':
            return {'ok': bool(self.path), 'path': self.path, 'message': ''}
        return {'ok': False, 'message': '不支持的通道动作：' + str(action)}


class GuestVideoFramesTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)

    def _clip(self, blob=b'clip-bytes'):
        clip = self.folder / 'clip.bin'
        clip.write_bytes(blob)
        return str(clip)

    def test_action_registered(self):
        # 不在 ACTIONS 里，客人端 validate_request 会直接丢请求，宿主只能干等到超时。
        self.assertIn('video_frames', ACTIONS)

    def test_returns_three_frames(self):
        clip = self._clip()
        frames = [FRAME] * 3
        bridge = FakeBridge(path=clip)
        with mock.patch('src.video_frames.frames_for_video', return_value=(frames, '')) as fake:
            result = _guest(bridge).dispatch('video_frames', {
                'chat': 'wxid_g@chatroom', 'local_id': 9, 'shard': 'message_0'})
        self.assertEqual(bridge.calls, [('wxid_g@chatroom', 9, 'video', 'message_0')])
        self.assertEqual(result, {'ok': True, 'frames': frames, 'message': ''})
        self.assertEqual(fake.call_args.args[0], clip)
        self.assertEqual(fake.call_args.kwargs, {'want': 3, 'max_side': 768})
        if av is not None:  # 装了 PyAV 才跑真解码，确保不是只有桩能过。
            real = _write_video(self.folder / 'clip.avi')
            decoded = _guest(FakeBridge(path=real)).dispatch('video_frames', {
                'chat': 'wxid_g@chatroom', 'local_id': 9, 'shard': 'message_0'})
            self.assertTrue(decoded['ok'])
            self.assertEqual(len(decoded['frames']), 3)
            self.assertTrue(decoded['frames'][0].startswith('data:image/'))

    def test_rejects_bad_args(self):
        bridge = FakeBridge(path='')
        service = _guest(bridge)
        for args in ({'chat': '', 'local_id': 9},
                     {'chat': 'x' * 257, 'local_id': 9},
                     {'chat': None, 'local_id': 9},
                     {'chat': 'wxid_g@chatroom'},
                     {'chat': 'wxid_g@chatroom', 'local_id': None},
                     {'chat': 'wxid_g@chatroom', 'local_id': True},
                     {'chat': 'wxid_g@chatroom', 'local_id': 9, 'shard': 'x' * 257},
                     {'chat': 'wxid_g@chatroom', 'local_id': 9, 'shard': 5},
                     {'chat': 'wxid_g@chatroom', 'local_id': 9, 'want': 0},
                     {'chat': 'wxid_g@chatroom', 'local_id': 9, 'want': '2'},
                     {'chat': 'wxid_g@chatroom', 'local_id': 9, 'max_side': 200}):
            with self.subTest(args=args):
                self.assertEqual(service.dispatch('video_frames', args), INVALID)
        self.assertEqual(bridge.calls, [])

    def test_fail_closed_on_internal_errors(self):
        ghost = str(self.folder / 'not-here.mp4')
        clip = self._clip()
        for bridge in (FakeBridge(error=RuntimeError('下载器炸了')),
                       FakeBridge(path=''), FakeBridge(path=ghost)):
            with self.subTest(case=str(bridge.path) + str(bridge.error)):
                self.assertEqual(_guest(bridge).dispatch(
                    'video_frames', {'chat': 'wxid_g@chatroom', 'local_id': 9}), NO_MEDIA)
        with mock.patch('src.video_frames.frames_for_video', side_effect=ImportError('没有解码组件')):
            result = _guest(FakeBridge(path=clip)).dispatch(
                'video_frames', {'chat': 'wxid_g@chatroom', 'local_id': 9})
        self.assertEqual(result, {'ok': False, 'message': '视频抽帧失败', 'frames': []})

    def test_reports_decode_reason(self):
        broken = self.folder / 'broken.mp4'
        broken.write_bytes(b'not-a-video')
        result = _guest(FakeBridge(path=str(broken))).dispatch(
            'video_frames', {'chat': 'wxid_g@chatroom', 'local_id': 9})
        self.assertFalse(result['ok'])
        self.assertEqual(result['frames'], [])
        self.assertIn('视频', result['message'])

    def test_trims_frames_over_payload_budget(self):
        clip = self._clip()
        heavy = ['data:image/png;base64,' + 'A' * 2_000_000] * 3
        with mock.patch('src.video_frames.frames_for_video', return_value=(heavy, '')):
            result = _guest(FakeBridge(path=clip)).dispatch(
                'video_frames', {'chat': 'wxid_g@chatroom', 'local_id': 9})
        # 回执超过 4MB 会写不出去、打死整条代理循环，所以要按预算裁帧。
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['frames']), 1)


class HostBridgeVideoFramesTest(unittest.TestCase):
    def test_envelope(self):
        self.assertEqual(VIDEO_FRAME_TIMEOUT, 20.0)
        bridge = object.__new__(SandboxBridge)
        with mock.patch.object(SandboxBridge, '_rpc', side_effect=RpcBusy('通道正忙')):
            self.assertEqual(bridge.video_frames('wxid_g@chatroom', 9, 'message_0'), RPC_FAILED)
        with mock.patch.object(SandboxBridge, '_rpc', side_effect=TimeoutError('超时')):
            self.assertEqual(bridge.video_frames('wxid_g@chatroom', 9), RPC_FAILED)
        rpc = mock.Mock(return_value={'ok': True, 'frames': [FRAME], 'message': ''})
        with mock.patch.object(SandboxBridge, '_rpc', rpc):
            self.assertEqual(bridge.video_frames('wxid_g@chatroom', 9, 'message_0'),
                             {'ok': True, 'frames': [FRAME], 'message': ''})
        rpc.assert_called_once_with('video_frames',
                                    {'chat': 'wxid_g@chatroom', 'local_id': '9',
                                     'shard': 'message_0', 'want': 3, 'max_side': 768},
                                    timeout=VIDEO_FRAME_TIMEOUT)
        with mock.patch.object(SandboxBridge, '_rpc', return_value={'ok': True, 'frames': []}):
            empty = bridge.video_frames('wxid_g@chatroom', 9)
        self.assertFalse(empty['ok'])
        self.assertEqual(empty['frames'], [])
        self.assertTrue(empty['message'])


class WeChatTransportCallTest(unittest.TestCase):
    def test_forwards_when_supported(self):
        class Bridge:
            def __init__(self):
                self.calls = []

            def video_frames(self, chat, local_id, shard='', want=3, max_side=768):
                self.calls.append((chat, local_id, shard, want, max_side))
                return {'ok': True, 'frames': [FRAME], 'message': ''}

        bridge = Bridge()
        result = asyncio.run(WeChatTransport.call(SimpleNamespace(bridge=bridge), 'video_frames', {
            'chat': 'wxid_g@chatroom', 'local_id': 9, 'shard': 'message_0',
            'want': 2, 'max_side': 768}))
        self.assertEqual(result['frames'], [FRAME])
        self.assertEqual(bridge.calls, [('wxid_g@chatroom', 9, 'message_0', 2, 768)])

    def test_refuses_without_capability(self):
        result = asyncio.run(WeChatTransport.call(SimpleNamespace(bridge=SimpleNamespace()),
                                            'video_frames', {'chat': 'wxid_g@chatroom'}))
        self.assertEqual(result, {'ok': False, 'message': '当前通道不支持视频抽帧', 'frames': []})


class PluginFallbackTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.plugin = object.__new__(ImageUnderstanding)

    def _run(self, transport):
        message = {'target': 'wxid_g@chatroom', 'local_id': 9, 'shard': 'message_0'}
        item = {'local_id': 9, 'shard': 'message_0'}
        runtime = SimpleNamespace(transport=transport)
        return asyncio.run(self.plugin._video_screens(runtime, message, item, 2, 768))

    def test_three_levels_in_order(self):
        # 第一层成功：只调客体侧动作，不再碰本机下载。
        transport = FakeTransport(frames=[FRAME])
        self.assertEqual(self._run(transport), ([FRAME], ''))
        self.assertEqual(transport.calls, ['video_frames'])
        self.assertEqual(transport.params[0]['want'], 2)
        self.assertEqual(transport.params[0]['max_side'], 768)
        self.assertEqual(transport.params[0]['chat'], 'wxid_g@chatroom')
        # 第二层失败：回退本机 download_media，顺序与旧文案都要保持。
        transport = FakeTransport(frames=[])
        self.assertEqual(self._run(transport), ([], '没能落到本机，未抽帧'))
        self.assertEqual(transport.calls, ['video_frames', 'download_media'])
        # 第二层失败但本机拿得到文件：第三层本地抽帧兜底。
        clip = self.folder / 'clip.mp4'
        clip.write_bytes(b'clip-bytes')
        transport = FakeTransport(frames=[], path=str(clip))
        with mock.patch('src.video_frames.frames_for_video',
                        return_value=([FRAME, FRAME], '')) as fake:
            self.assertEqual(self._run(transport), ([FRAME, FRAME], ''))
        self.assertEqual(transport.calls, ['video_frames', 'download_media'])
        self.assertEqual(fake.call_args.args[1:], (2, 768))


if __name__ == '__main__':
    unittest.main()
