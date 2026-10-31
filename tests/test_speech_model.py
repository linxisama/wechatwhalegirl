"""语音识别：模型路径、幂等预取、失败提示。

背景（用户反馈"好多次语音识别在群里面和私聊都会失效"）：安装包不带 141MB 的 whisper 模型，
而 `asr.model_dir` 是相对路径 —— 打包后它解析到**安装目录**，模型既不在那儿也写不进去。
于是每次语音都要现场下载，那几分钟里上层早就判失败了。
"""

import asyncio
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from src.asr_service import ASRService
from src.plugins.manager import PluginManager


class _Runtime:
    def __init__(self, root):
        self.root = Path(root)
        self.config = None
        self.notes = []

    def note(self, text):
        self.notes.append(text)


def write_silence(path, seconds=1.0, rate=16000):
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack('<h', 0) * int(rate * seconds))
    return path


class ModelPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PluginManager(self.root / 'plugins')
        self.plugin = self.manager._plugins['speech_to_text_wechat']

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_path_is_anchored_to_the_writable_data_root(self):
        resolved = Path(self.plugin._model_dir(_Runtime(self.root)))
        self.assertTrue(str(resolved).startswith(str(self.root)), resolved)
        self.assertTrue(str(resolved).endswith('faster-whisper'))

    def test_model_ready_only_when_a_snapshot_exists(self):
        runtime = _Runtime(self.root)
        self.assertFalse(self.plugin._model_ready(runtime))
        snapshot = Path(self.plugin._model_dir(runtime)) / 'models--Systran--faster-whisper-base'
        snapshot.mkdir(parents=True)
        self.assertFalse(self.plugin._model_ready(runtime))
        (snapshot / 'model.bin').write_bytes(b'x')
        self.assertTrue(self.plugin._model_ready(runtime))


class PrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_use_skips_and_prefetches_in_background(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            manager = PluginManager(Path(tmp.name) / 'plugins')
            plugin = manager._plugins['speech_to_text_wechat']
            runtime = _Runtime(tmp.name)
            self.assertFalse(await plugin._ensure_model(runtime))
            self.assertTrue(plugin._prefetching)
            self.assertIn('后台下载', runtime.notes[0])
        finally:
            tmp.cleanup()

    async def test_repeated_calls_do_not_start_a_second_download(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            manager = PluginManager(Path(tmp.name) / 'plugins')
            plugin = manager._plugins['speech_to_text_wechat']
            runtime = _Runtime(tmp.name)
            await plugin._ensure_model(runtime)
            await plugin._ensure_model(runtime)
            # 只数"启动预取"那条；等待期的提示也含"后台下载"，不能算成第二次下载
            self.assertEqual(sum(note.startswith('首次使用语音识别') for note in runtime.notes), 1)
            self.assertIn('正在后台下载', runtime.notes[-1])
        finally:
            tmp.cleanup()


class ServiceSmokeTests(unittest.IsolatedAsyncioTestCase):
    """真实加载一次模型跑 PCM（用本机已下好的模型；没下就跳过）。"""

    async def test_transcribe_silence_does_not_crash(self):
        model_dir = Path(r'C:\Users\16522\AppData\Local\WhaleGirlWeChat\data\models\faster-whisper')
        if not model_dir.is_dir() or not any(model_dir.rglob('model.bin')):
            self.skipTest('本机没有 whisper 模型，跳过真实加载')
        tmp = tempfile.mkdtemp(prefix='asr-smoke-')
        audio = write_silence(Path(tmp) / 'silence.wav')
        service = ASRService(model='base', model_dir=str(model_dir), language='zh', compute_type='int8')
        await service.start()
        try:
            with self.assertRaises(Exception):
                # 纯静音没有语音内容，服务层应当以 RuntimeError 收尾（而不是崩溃或挂住）
                await service.transcribe(str(audio))
        finally:
            await service.close()


if __name__ == '__main__':
    unittest.main()
