"""Fake Win32/clock only, never opens a real target or accesses Sandbox."""
import ctypes
from ctypes import wintypes
import unittest
from unittest.mock import patch

from src.isolated_wechat import ocr_slots as proto
from src.isolated_wechat.protocol import ProtocolError

class Clock:
    def __init__(self):self.t=0.;self.sleeps=[]
    def monotonic(self):return self.t
    def sleep(self,value):self.sleeps.append(value);self.t+=value

class Kernel:
    def __init__(self,clock):
        self.clock=clock;self.calls=[];self.errors=[];self.error=0
        self.fail=None;self.short=False;self.slow_flush=False
    def CreateFileW(self,*args):
        self.calls.append(('open',args))
        if self.errors:
            self.error=self.errors.pop(0)
            if self.error:return proto._INVALID_HANDLE_VALUE
        return 87
    def SetFilePointerEx(self,*args):
        self.calls.append(('seek',args))
        if self.fail=='seek':self.error=5;return False
        return True
    def WriteFile(self,handle,data,size,written,unused):
        self.calls.append(('write',(handle,ctypes.string_at(data,size),size)))
        if self.fail=='write':self.error=5;return False
        ctypes.cast(written,ctypes.POINTER(wintypes.DWORD)).contents.value=size-1 if self.short else size
        return True
    def SetEndOfFile(self,*args):
        self.calls.append(('truncate',args))
        if self.fail=='truncate':self.error=5;return False
        return True
    def FlushFileBuffers(self,*args):
        self.calls.append(('flush',args))
        if self.slow_flush:self.clock.t=1.01
        if self.fail=='flush':self.error=5;return False
        return True
    def CloseHandle(self,*args):self.calls.append(('close',args));return True

class ExistingSlotWriteTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.kernel=Kernel(self.clock)
        self.patches=[patch.object(proto,'time',self.clock),patch.object(proto,'_kernel',return_value=self.kernel),
            patch.object(proto.ctypes,'get_last_error',side_effect=lambda:self.kernel.error)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
    def run_write(self,**kwargs):return proto._write_existing('synthetic-slot',b'{"n":1}',deadline=kwargs.pop('deadline',1),**kwargs)
    def names(self):return [name for name,args in self.kernel.calls]
    def test_uses_share_zero_open_existing_and_exact_single_write_order(self):
        self.run_write()
        self.assertEqual(self.names(),['open','seek','write','truncate','flush','close'])
        args=self.kernel.calls[0][1]
        self.assertEqual(args[1:5],(0x40000000,0,None,3))
        self.assertEqual(self.kernel.calls[2][1][1],b'{"n":1}')
    def test_sharing_conflicts_only_retry_acquire_then_write_once(self):
        self.kernel.errors=[32,32,0]
        self.run_write()
        self.assertEqual(self.names(),['open','open','open','seek','write','truncate','flush','close'])
        self.assertEqual(self.clock.sleeps,[.005,.005])
    def test_permission_error_is_immediate_no_write_or_close(self):
        self.kernel.errors=[5]
        with self.assertRaises(OSError) as caught:self.run_write()
        self.assertEqual(caught.exception.winerror,5)
        self.assertEqual(self.names(),['open']);self.assertFalse(self.clock.sleeps)
    def test_missing_slot_is_immediate_and_never_created(self):
        self.kernel.errors=[2]
        with self.assertRaises(OSError) as caught:self.run_write()
        self.assertEqual(caught.exception.winerror,2)
        self.assertEqual(self.names(),['open'])
    def test_permanent_sharing_conflict_stops_at_original_deadline(self):
        self.kernel.errors=[32]*100
        with self.assertRaises(TimeoutError):self.run_write(deadline=.012)
        self.assertAlmostEqual(self.clock.t,.012)
        self.assertTrue(all(name=='open' for name in self.names()))
        self.assertEqual(len(self.kernel.calls),3)
    def test_write_failure_closes_handle_without_retry_or_truncate(self):
        self.kernel.fail='write'
        with self.assertRaises(OSError):self.run_write()
        self.assertEqual(self.names(),['open','seek','write','close'])
    def test_short_write_closes_and_is_uncertain_without_second_write(self):
        self.kernel.short=True
        with self.assertRaises(OSError):self.run_write()
        self.assertEqual(self.names(),['open','seek','write','close'])
    def test_seek_truncate_or_flush_failure_never_leaks_handle(self):
        for failure in ('seek','truncate','flush'):
            with self.subTest(failure=failure):
                self.kernel.calls.clear();self.kernel.fail=failure
                with self.assertRaises(OSError):self.run_write()
                self.assertEqual(self.names()[-1],'close')
                self.assertEqual(self.names().count('close'),1)
                self.assertLessEqual(self.names().count('write'),1)
    def test_late_flush_is_rejected_while_handle_is_closed(self):
        self.kernel.slow_flush=True
        with self.assertRaises(TimeoutError):self.run_write()
        self.assertEqual(self.names()[-1],'close')
    def test_invalid_arguments_fail_before_open(self):
        for deadline,data in ((True,b'x'),(float('nan'),b'x'),(1,b''),(1,b'x'*(4*1024*1024+1))):
            with self.subTest(deadline=deadline,length=len(data)):
                with self.assertRaises(ProtocolError):proto._write_existing('synthetic',data,deadline=deadline)
        self.assertFalse(self.kernel.calls)

    def test_close_failure_is_not_reported_as_success_or_rewritten(self):
        with patch.object(self.kernel,'CloseHandle',return_value=False) as close:
            with self.assertRaises(OSError):self.run_write()
        close.assert_called_once_with(87)
        self.assertEqual(self.names().count('write'),1)



"""Synthetic path/pairing tests, using fake writers and no Sandbox or OCR."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from src.isolated_wechat import ocr_slots as publish
from src.isolated_wechat.protocol import Pairing,ProtocolError,verify
import json

class OCRPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.folder=self.root/'ocr';self.folder.mkdir()
        self.pair=Pairing({'version':1,'channel_id':'a'*32,'secret':'b'*64},self.root)
        self.clock=SimpleNamespace(monotonic=lambda:20.,time=lambda:1000.)
        self.payload={'epoch':'c'*32,'request_id':'d'*32,'ok':True,'rows':[]}
        self.patches=[patch.object(publish,'time',self.clock),patch.object(publish,'_write_existing'),
                      patch.object(publish,'write_document')]
        self.pm,self.writer,self.original=[p.start() for p in self.patches]
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def existing(self,name):
        path=self.folder/name;path.write_bytes(b'pending');return path
    def test_existing_exact_relay_preserves_signed_bytes_and_50ms_bound(self):
        path=self.existing('relay.json')
        self.assertEqual(publish.publish_ocr_document(path,self.pair,self.payload),path)
        self.writer.assert_called_once();self.original.assert_not_called()
        args,kw=self.writer.call_args
        self.assertEqual(args[0],path)
        self.assertEqual(verify(self.pair,json.loads(args[1])),dict(self.payload,version=1,channel_id='a'*32))
        self.assertAlmostEqual(kw['deadline'],20.05)
    def test_result_rechecks_original_metadata_and_keeps_remaining_budget(self):
        path=self.existing('d'*32+'.result.json')
        job={'request_id':'d'*32,'epoch':'c'*32,'deadline':1000.04}
        with patch.object(publish,'read_document',return_value=job) as read:
            publish.publish_ocr_document(path,self.pair,self.payload)
        read.assert_called_once_with(path.with_name('d'*32+'.request.json'),self.pair,8192)
        self.assertAlmostEqual(self.writer.call_args.kwargs['deadline'],20.04)
        self.original.assert_not_called()
    def test_result_acquisition_never_exceeds_100ms(self):
        path=self.existing('d'*32+'.result.json')
        with patch.object(publish,'read_document',return_value={'request_id':'d'*32,'epoch':'c'*32,'deadline':1006.}):
            publish.publish_ocr_document(path,self.pair,self.payload)
        self.assertAlmostEqual(self.writer.call_args.kwargs['deadline'],20.1)
    def test_expired_or_changed_request_never_publishes(self):
        path=self.existing('d'*32+'.result.json')
        for change in ({'deadline':999.},{'deadline':1008.},{'deadline':float('nan')},{'deadline':True},
                       {'epoch':'e'*32},{'request_id':'e'*32}):
            job={'request_id':'d'*32,'epoch':'c'*32,'deadline':1006.};job.update(change)
            with self.subTest(change=change),patch.object(publish,'read_document',return_value=job):
                with self.assertRaises((TimeoutError,ProtocolError)):publish.publish_ocr_document(path,self.pair,self.payload)
        self.writer.assert_not_called();self.original.assert_not_called()
    def test_signature_or_read_failure_does_not_fallback(self):
        path=self.existing('d'*32+'.result.json')
        for error in (ProtocolError('bad signature'),FileNotFoundError('request disappeared')):
            with self.subTest(error=type(error).__name__),patch.object(publish,'read_document',side_effect=error):
                with self.assertRaises(type(error)):publish.publish_ocr_document(path,self.pair,self.payload)
        self.writer.assert_not_called();self.original.assert_not_called()
    def test_exclusive_pending_and_first_capability_create_once(self):
        first=self.folder/'relay.json'
        publish.publish_ocr_document(first,self.pair,self.payload)
        self.original.assert_called_once_with(first,self.pair,self.payload,exclusive=True)
        self.original.reset_mock()
        pending=self.existing('d'*32+'.result.json')
        publish.publish_ocr_document(pending,self.pair,self.payload,exclusive=True)
        self.original.assert_called_once_with(pending,self.pair,self.payload,exclusive=True)
        self.writer.assert_not_called()
    def test_non_ocr_or_near_miss_paths_keep_original_publication(self):
        for relative in ('service.json','control/request.json','responses/'+'d'*32+'.result.json',
                         'ocr/relay.json.backup','ocr/'+'D'*32+'.result.json','ocr/'+'d'*31+'.result.json'):
            path=self.root/relative
            with self.subTest(relative=relative):
                self.original.reset_mock()
                publish.publish_ocr_document(path,self.pair,self.payload)
                self.original.assert_called_once_with(path,self.pair,self.payload,exclusive=False)
        self.writer.assert_not_called()
    def test_existing_write_error_preserves_name_and_does_not_fallback(self):
        path=self.existing('relay.json');self.writer.side_effect=OSError('locked')
        with self.assertRaises(OSError):publish.publish_ocr_document(path,self.pair,self.payload)
        self.writer.assert_called_once();self.original.assert_not_called()
        self.assertEqual(path.read_bytes(),b'pending')
    def test_oversized_document_rejected_before_write(self):
        path=self.existing('relay.json')
        with patch.object(publish,'MAX_DOCUMENT_BYTES',1):
            with self.assertRaises(ProtocolError):publish.publish_ocr_document(path,self.pair,self.payload)
        self.writer.assert_not_called();self.original.assert_not_called()

    def test_missing_result_is_not_created_or_fallen_back(self):
        path=self.folder/('d'*32+'.result.json')
        with self.assertRaises(FileNotFoundError):publish.publish_ocr_document(path,self.pair,self.payload)
        self.writer.assert_not_called();self.original.assert_not_called()
        self.assertFalse(path.exists())

    def test_payload_request_id_must_match_result_slot_before_any_write(self):
        path=self.existing('d'*32+'.result.json')
        for payload in (dict(self.payload,request_id='e'*32),None):
            with self.subTest(payload=payload),patch.object(publish,'read_document') as read:
                with self.assertRaises(ProtocolError):publish.publish_ocr_document(path,self.pair,payload)
                read.assert_not_called()
        self.writer.assert_not_called();self.original.assert_not_called()

    def test_close_preserves_signed_heartbeat_but_original_expiry_still_rejects(self):
        import time
        from src.isolated_wechat import ocr_relay,protocol
        from src.runtime_identity import APP_VERSION
        (self.root/'pairing.json').write_text(json.dumps(dict(self.pair)),'utf-8')
        stamp=time.time()
        protocol.write_document(self.root/'service.json',self.pair,
            {'epoch':'c'*32,'active':True,'heartbeat':stamp,'app_version':APP_VERSION})
        path=self.folder/'relay.json'
        protocol.write_document(path,self.pair,
            {'epoch':'c'*32,'available':True,'heartbeat':stamp,'app_version':APP_VERSION})
        relay=ocr_relay.HostOCRRelay(self.root)
        guest=ocr_relay.GuestOCR(self.root,_guest_check=False)
        before=path.read_bytes()
        self.assertTrue(guest.available())
        relay.close()
        self.assertEqual(path.read_bytes(),before)
        # 客体代理仍活着，只让保留下来的宿主心跳过期，不能借代理心跳续活 OCR。
        protocol.write_document(self.root/'service.json',self.pair,
            {'epoch':'c'*32,'active':True,'heartbeat':stamp+9,'app_version':APP_VERSION})
        with patch.object(ocr_relay.time,'time',return_value=stamp+9):
            self.assertFalse(guest.available())


