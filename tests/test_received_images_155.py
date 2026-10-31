"""真实图片字节的落地、容量清理与识图链路；不联网、不向微信发消息。"""
import asyncio
import base64
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from PIL import Image
from src.received_images import ReceivedImageStore
from src.plugins.manager import PluginManager


def png(index=0):
    output=io.BytesIO()
    Image.new('RGB',(8,8),(index%256,index//256,12)).save(output,'PNG')
    return output.getvalue()


class Storage155Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=ReceivedImageStore(self.root/'received')

    def test_51_deletes_oldest_25_but_retains_newest_and_external_data(self):
        paths=[]
        for i in range(50):
            result=self.store.put(png(i));paths.append(Path(result['path']))
            os.utime(paths[-1],(1000+i,1000+i))
            self.assertEqual(result['deleted'],0)
        unknown=self.store.root/'my-photo.png';unknown.write_bytes(png(500))
        external=self.root/'chat.sqlite';external.write_bytes(b'history')
        nested=self.store.root/'stickers';nested.mkdir();(nested/'original.png').write_bytes(png(501))
        result,raw=self.store.save_and_read(png(50))
        self.assertEqual((result['count'],result['deleted']),(26,25))
        self.assertEqual(raw,png(50))
        self.assertTrue(all(not p.exists() for p in paths[:25]))
        self.assertTrue(all(p.exists() for p in paths[25:]))
        self.assertEqual(external.read_bytes(),b'history');self.assertTrue(unknown.exists())
        self.assertTrue((nested/'original.png').exists())

    def test_duplicate_is_one_copy_and_restart_reads_it(self):
        first=self.store.put(png());second=self.store.put(png())
        self.assertEqual(first['path'],second['path']);self.assertEqual(second['count'],1)
        self.assertEqual(ReceivedImageStore(self.store.root).read(first['path']),png())

    def test_corrupt_oversized_pixels_and_external_reads_rejected(self):
        with self.assertRaises(ValueError):self.store.put(b'\x89PNG\r\n\x1a\nexample')
        output=io.BytesIO();Image.new('1',(4100,4100)).save(output,'PNG')
        with self.assertRaises(ValueError):self.store.put(output.getvalue())
        outside=self.root/('a'*64+'.png');outside.write_bytes(png())
        with self.assertRaises(ValueError):self.store.read(outside)

    def test_tampered_saved_file_not_used(self):
        result=self.store.put(png());Path(result['path']).write_bytes(png(1))
        with self.assertRaises(ValueError):self.store.read(result['path'])

    def test_unlink_failure_is_reported_and_next_save_retries(self):
        for i in range(50):self.store.put(png(i))
        real=Path.unlink
        def denied(path,*args,**kwargs):
            if path.suffix=='.png':raise PermissionError('in use')
            return real(path,*args,**kwargs)
        with patch.object(Path,'unlink',denied):result=self.store.put(png(50))
        self.assertEqual((result['count'],result['deleted'],result['cleanup_failed']),(51,0,25))
        result=self.store.put(png(51))
        self.assertEqual((result['count'],result['deleted']),(26,26))

    def test_symlink_never_read_or_deleted(self):
        outside=self.root/'outside.png';outside.write_bytes(png())
        link=self.store.root/('a'*64+'.png')
        try:link.symlink_to(outside)
        except OSError:self.skipTest('本机没有创建符号链接权限')
        self.assertEqual(self.store._managed(),[])
        with self.assertRaises(ValueError):self.store.read(link)
        self.assertTrue(outside.exists())

    def test_concurrent_persistence_returns_matching_saved_bytes(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(self.store.save_and_read,(png(i) for i in range(70))))
        for i,(_,raw) in enumerate(results):self.assertEqual(raw,png(i))
        self.assertLessEqual(len(self.store._managed()),50)


class Pipeline155Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.manager=PluginManager(self.root/'data/wechat-only')
        self.plugin=self.manager._plugins['image_understanding'];self.notes=[]
        self.source=self.root/'wechat-original.png';self.source.write_bytes(png())
        self.ai=SimpleNamespace(api_url='https://example.test/v1',model='vision',describe_images=AsyncMock(return_value='彩色方块'))
        self.download=AsyncMock(return_value={'ok':True,'path':str(self.source)})
        self.runtime=SimpleNamespace(root=self.root,mode='wechat',account=lambda:'self',note=self.notes.append,
                                     ai=self.ai,transport=SimpleNamespace(call=self.download))

    def message(self,**kw):
        return dict(kind='private',target='friend',media=[{'type':'image','local_id':1}],**kw)

    async def test_model_reads_persisted_file_not_download_path(self):
        async def describe(sources,**kw):
            paths=list((self.root/'data/received_images').glob('*.png'))
            self.assertEqual(len(paths),1)
            self.assertEqual(base64.b64decode(sources[0].split(',',1)[1]),paths[0].read_bytes())
            return '彩色方块'
        self.ai.describe_images.side_effect=describe
        message=self.message();await self.plugin.describe(message,self.runtime)
        self.assertEqual(message['vision_status'],'ok');self.assertTrue(self.source.exists())
        self.assertEqual(len(message['_image_storage']),1)
        self.assertTrue(any('当前 1 张' in n for n in self.notes))

    async def test_cdn_gif_original_saved_before_extracting_frames(self):
        output=io.BytesIO()
        Image.new('RGB',(8,8),'red').save(output,'GIF',save_all=True,append_images=[Image.new('RGB',(8,8),'blue')])
        data=output.getvalue()
        message=self.message();message['media']=[{'type':'emotion','url':'https://example.test/a.gif'}]
        with patch('src.sticker_library.fetch_image',AsyncMock(return_value=data)):
            await self.plugin.describe(message,self.runtime)
        self.download.assert_not_awaited()
        paths=list((self.root/'data/received_images').glob('*.gif'))
        self.assertEqual(len(paths),1);self.assertEqual(paths[0].read_bytes(),data)
        self.assertEqual(len(self.ai.describe_images.await_args.args[0]),2)

    async def test_library_original_also_persisted(self):
        raw=png();self.runtime.stickers=SimpleNamespace(matching_md5=lambda _:raw,record_description=lambda *_:None)
        message=self.message();message['media']=[{'type':'emotion','md5':'a'*32}]
        await self.plugin.describe(message,self.runtime)
        self.download.assert_not_awaited();self.ai.describe_images.assert_awaited_once()
        self.assertEqual(Path(message['_image_storage'][0]['path']).read_bytes(),raw)

    async def test_write_failure_does_not_call_vision(self):
        with patch.object(ReceivedImageStore,'save_and_read',side_effect=OSError('disk full')):
            message=self.message();await self.plugin.describe(message,self.runtime)
        self.ai.describe_images.assert_not_awaited();self.download.assert_awaited_once()
        self.assertIn('磁盘空间',message['vision_status']);self.assertNotIn('vision_description',message)

    async def test_corrupt_download_not_saved_or_sent_to_model(self):
        self.source.write_bytes(b'\x89PNG\r\n\x1a\nexample')
        message=self.message();await self.plugin.describe(message,self.runtime)
        self.ai.describe_images.assert_not_awaited();self.assertIn('损坏',message['vision_status'])

    async def test_cache_still_confirms_original_is_on_disk(self):
        first=self.message();await self.plugin.describe(first,self.runtime)
        Path(first['_image_storage'][0]['path']).unlink()
        second=self.message();await self.plugin.describe(second,self.runtime)
        self.assertTrue(second['vision_cache_hit']);self.assertEqual(self.ai.describe_images.await_count,1)
        self.assertTrue(Path(second['_image_storage'][0]['path']).exists())

    async def test_encrypted_without_original_never_fakes_download(self):
        message=self.message();message['media']=[{'type':'emotion','md5':'b'*32}]
        await self.plugin.describe(message,self.runtime)
        self.download.assert_not_awaited();self.ai.describe_images.assert_not_awaited()
        self.assertNotIn('_image_storage',message);self.assertIn('加密',message['vision_status'])


class Folder155Tests(unittest.TestCase):
    def test_dialog_exposes_exact_storage_folder(self):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PyQt6.QtWidgets import QApplication
        import wechat_gui as gui
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder,patch.object(gui,'ROOT',Path(folder)),patch.object(os,'startfile') as start:
            dialog=gui.PluginConfigDialog({'id':'image_understanding','name':'图片识别'})
            dialog.image_store_button.click();app.processEvents()
            start.assert_called_once_with(str(Path(folder)/'data/received_images'))
            self.assertTrue((Path(folder)/'data/received_images').is_dir());dialog.close()


from test_wechat_app_flow import RuntimeTestCase,raw_message

class Eligibility155Tests(RuntimeTestCase):
    async def test_unmentioned_group_image_does_not_download_or_recognize(self):
        bot=await self.start_runtime()
        bot.transport.call=AsyncMock();bot.ai.describe_images=AsyncMock()
        raw=raw_message('[图片]');raw['type']='图片'
        bot.ingest(raw);await asyncio.sleep(.2)
        # 契约更新（1.2.70）：群聊未点名的图片**允许随口回一句文字**，
        # 但仍然不下载原图、不做识图 —— 否则每个群图都要花流量和识图钱。
        bot.ai.describe_images.assert_not_awaited()
        self.assertFalse((self.root/'data/received_images').exists())
