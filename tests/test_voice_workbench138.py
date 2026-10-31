"""语音工作台只用已安装的所选模型；转写失败不猜测、不下载。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

from src.asr_service import ASRService
from src.plugins.manager import PluginManager


class Voice138(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.plugin=PluginManager(self.root/'plugins')._plugins['speech_to_text_wechat']
        self.runtime=SimpleNamespace(root=self.root,config=None)

    def tearDown(self):self.tmp.cleanup()

    async def test_missing_selected_model_does_not_download_or_use_other_model(self):
        directory=self.root/'data/models/faster-whisper/tiny'
        directory.mkdir(parents=True);(directory/'model.bin').write_bytes(b'x')
        voice=self.root/'speech.wav';voice.write_bytes(b'RIFF')
        service=self.plugin._service_for(self.runtime)
        with patch.object(service,'transcribe',new=AsyncMock()) as transcribe:
            with self.assertRaisesRegex(ValueError,'尚未安装'):
                await self.plugin.transcribe_file(voice,self.runtime)
            transcribe.assert_not_called()
        self.assertEqual(service.cache_dir,self.root/'data/voice_inbox')
        self.assertFalse(self.plugin._prefetching)

    async def test_local_transcription_enabled_empty_and_format_errors(self):
        service=self.plugin._service_for(self.runtime)
        model=service.model_dir/'base';model.mkdir(parents=True);(model/'model.bin').write_bytes(b'x')
        voice=self.root/'speech.wav';voice.write_bytes(b'RIFF')
        with patch.object(service,'transcribe',new=AsyncMock(return_value=SimpleNamespace(text='  明天继续整理  '))):
            self.assertEqual(await self.plugin.transcribe_file(voice,self.runtime),'明天继续整理')
        with patch.object(service,'transcribe',new=AsyncMock(return_value=SimpleNamespace(text=''))):
            with self.assertRaisesRegex(ValueError,'有效语音'):await self.plugin.transcribe_file(voice,self.runtime)
        self.plugin._manager.update_settings('speech_to_text_wechat',{'enabled':False})
        with self.assertRaisesRegex(ValueError,'关闭'):await self.plugin.transcribe_file(voice,self.runtime)

    async def test_named_model_layouts_and_auto_language(self):
        service=self.plugin._service_for(self.runtime)
        model=service.model_dir/'models--Systran--faster-whisper-base/snapshots/abc'
        model.mkdir(parents=True);(model/'model.bin').write_bytes(b'x')
        self.assertEqual(service.local_model_path(),model)
        service.language='auto'
        engine=Mock();engine.transcribe.return_value=([SimpleNamespace(text='好的')],SimpleNamespace(language='zh',duration=1))
        voice=self.root/'speech.wav';voice.write_bytes(b'RIFF')
        with patch.object(service,'_ensure_model',return_value=engine):
            self.assertEqual(service._transcribe_sync(voice),'好的')
        self.assertIsNone(engine.transcribe.call_args.kwargs['language'])

    async def test_removed_directory_cancels_late_prefetch_before_download(self):
        import shutil
        service=self.plugin._service_for(self.runtime)
        shutil.rmtree(service.model_dir)
        with self.assertRaisesRegex(RuntimeError,'已移除'):service._ensure_model()


if __name__=='__main__':unittest.main()
