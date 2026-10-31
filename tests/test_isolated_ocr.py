"""签名像素中继的离线往返；OCR替身、临时目录，无桌面输入。"""
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image

from src.runtime_identity import APP_VERSION
from src.isolated_wechat.protocol import ProtocolError, load_pairing, write_document
from src.isolated_wechat.ocr_relay import GuestOCR, HostOCRRelay, dimensions, line_from_words


def word(text,x,y=5,width=10,height=20):
    return SimpleNamespace(text=text,bounding_rect=SimpleNamespace(x=x,y=y,width=width,height=height))


class OCRLineGeometryTests(unittest.TestCase):
    def test_densely_spaced_chinese_words_do_not_inherit_line_text_spaces(self):
        # WinRT的line.text可能是“文 件 传 输 助 手”，词框才是间距证据。
        words=[word(character,5+index*11) for index,character in enumerate('文件传输助手')]
        self.assertEqual(line_from_words(words,100,40),['文件传输助手',5,5,65,20])

    def test_real_large_gap_and_spaces_inside_a_word_are_preserved(self):
        self.assertEqual(line_from_words([word('Test',5,width=30),word('A',43)],100,40)[0],'Test A')
        self.assertEqual(line_from_words([word('Test A',5,width=50)],100,40)[0],'Test A')
        self.assertEqual(line_from_words([word('Test',5,width=30),word('A',36)],100,40)[0],'TestA')

    def test_horizontal_order_uses_word_geometry_and_fractional_bounds_expand_outwards(self):
        result=line_from_words([word('件',16.2,y=5.2),word('文',5.2,y=5.2)],100,40)
        self.assertEqual(result,['文件',5,5,22,21])

    def test_invalid_overlapping_or_out_of_image_word_rejects_the_entire_line(self):
        malformed=[word('坏',-1),word('坏',95),word('坏',20,y=-1),word('坏',20,y=35),
                   word('坏',20,width=0),word('坏',20,height=-1),word('坏',float('nan')),
                   word('坏',20,width=float('inf')),word('坏',True),word('坏\n行',20),
                   word('',20),word(' ',20),word('坏',14),None]
        for bad in malformed:
            with self.subTest(bad=bad):
                self.assertIsNone(line_from_words([word('文',5),bad],100,40))

    def test_two_separate_line_results_are_not_expanded_into_per_word_rows(self):
        lines=[[word('发',5),word('送',16)],[word('发',45),word('送',56)]]
        rows=[line_from_words(words,100,40) for words in lines]
        self.assertEqual(len(rows),2)
        self.assertEqual([row[0] for row in rows],['发送','发送'])


class IsolatedOCRTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        (self.root/'pairing.json').write_text(json.dumps({'version':1,'channel_id':uuid.uuid4().hex,
            'secret':'ab'*32}),'utf-8')
        self.pairing=load_pairing(self.root)
        self.epoch=uuid.uuid4().hex
        write_document(self.root/'service.json',self.pairing,{'epoch':self.epoch,'active':True,
            'app_version':APP_VERSION,'heartbeat':time.time()})

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_checks_identity_and_removes_temporary_pixels(self):
        with patch('src.isolated_wechat.ocr_relay.LocalOCR.available',return_value=True), \
             patch('src.isolated_wechat.ocr_relay.recognize',AsyncMock(return_value=[['发送',1,1,8,8]])):
            relay=HostOCRRelay(self.root).start()
            try:
                guest=GuestOCR(self.root,_guest_check=False)
                until=time.monotonic()+2
                while not guest.available() and time.monotonic()<until:time.sleep(.01)
                with Image.new('RGB',(20,20),'white') as image:
                    self.assertEqual(guest.recognize(image),[('发送',1,1,8,8)])
                self.assertFalse(list((self.root/'ocr').glob('*.bgra')))
                self.assertFalse(list((self.root/'ocr').glob('*.request.json')))
                self.assertFalse(list((self.root/'ocr').glob('*.result.json')))
            finally:relay.close()

    def test_stale_or_wrong_epoch_relay_is_not_available(self):
        guest=GuestOCR(self.root,_guest_check=False)
        for epoch,stamp in ((uuid.uuid4().hex,time.time()),(self.epoch,time.time()-10)):
            write_document(self.root/'ocr/relay.json',self.pairing,{'epoch':epoch,'heartbeat':stamp,
                'app_version':APP_VERSION,'available':True})
            self.assertFalse(guest.available())

    def test_invalid_dimensions_are_rejected_before_ocr(self):
        for width,height in ((True,10),(0,10),(4097,1),(4096,4096)):
            with self.subTest(width=width,height=height),self.assertRaises(ProtocolError):
                dimensions(width,height)

    def test_untrusted_pixel_path_never_reaches_engine(self):
        relay=HostOCRRelay(self.root)
        identifier=uuid.uuid4().hex
        request=self.root/'ocr'/(identifier+'.request.json')
        write_document(request,self.pairing,{'epoch':self.epoch,'request_id':identifier,
            'app_version':APP_VERSION,'width':2,'height':2,'deadline':time.time()+5,
            'pixel_file':'../../private.txt','pixel_sha256':'0'*64})
        with patch('src.isolated_wechat.ocr_relay.recognize',AsyncMock()) as engine:
            relay._one(request,self.epoch,True)
        engine.assert_not_called()

    def test_guest_guard_is_used_by_default(self):
        with patch.object(GuestOCR,'_require_guest',side_effect=ProtocolError('guest required')):
            with self.assertRaises(ProtocolError):GuestOCR(self.root)
