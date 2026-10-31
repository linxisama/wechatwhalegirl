"""Fresh poll proves guest liveness; no RPC, DB, UI or actual sends."""
import tempfile
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

from src.isolated_wechat.host_bridge import SandboxBridge


class GuestReceiveHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.host=SandboxBridge(Path(self.temp.name)/'data',Path(self.temp.name)/'absent')
        self.host.online=self.host._listening=True;self.host._receive_generation=7
        self.host._pairing={'channel_id':'0'*32};self.host._epoch='1'*32
        self.host._service=Mock(return_value=(None,{'status':{}}));self.host.note=Mock()
        self.threads=[];self.addCleanup(self.close)

    def close(self):
        self.host._poll_stop.set()
        for thread,stop,release in self.threads:
            stop.set();release.set();thread.join(2);self.assertFalse(thread.is_alive())

    def event(self):
        return {'event_id':1,'message':{'username':'wxid_fixture_contact','sender_username':'wxid_fixture_contact',
            'target_name':'fixture','sender_name':'fixture','content':'offline fixture'}}

    def start(self,status,events=(),before_return=None):
        entered,release=threading.Event(),threading.Event();second=threading.Event()
        stop=self.host._poll_stop;calls=[]
        def rpc(action,args,timeout):
            calls.append(1)
            if len(calls)==1:
                entered.set();self.assertTrue(release.wait(2))
                if before_return:before_return()
                return {'status':status,'events':list(events)}
            second.set();stop.wait(2);return {'status':status,'events':[]}
        self.host._rpc=rpc
        thread=threading.Thread(target=self.host._poll_remote,args=(stop,7),daemon=True)
        self.host._poll_thread=thread;self.threads.append((thread,stop,release))
        thread.start();self.assertTrue(entered.wait(2));release.set()
        return thread,stop,second

    def status(self,listening=True):
        return {'online':True,'listening':listening,'self_id':'wxid_fixture_self','account':'fixture','send_environment':{}}

    def test_guest_false_stops_host_even_if_database_online(self):
        thread,_,_=self.start(self.status(False));thread.join(2)
        self.assertFalse(thread.is_alive());self.assertFalse(self.host.is_receiving())
        self.assertFalse(self.host._listening);self.assertFalse(self.host.online)
        self.assertIn('客体消息监听已停止',self.host.last_error);self.host.note.assert_called_once()

    def test_missing_or_non_boolean_guest_status_fails_closed(self):
        for status in ({'online':True},self.status(None),self.status(1),self.status('true'),None,[]):
            with self.subTest(status=status):
                self.host._poll_stop=threading.Event();self.host._receive_generation=7
                self.host.online=self.host._listening=True
                thread,_,_=self.start(status);thread.join(2)
                self.assertFalse(thread.is_alive());self.assertFalse(self.host.is_receiving())
                self.assertIn('状态未核验',self.host.last_error)

    def test_sync_event_is_received_and_acked_once_before_false_health_stops(self):
        seen=[];self.host._callbacks=[seen.append]
        thread,_,_=self.start(self.status(False),[self.event()]);thread.join(2)
        self.assertEqual(len(seen),1)
        self.assertEqual((self.host._event_seen,self.host._event_ack,self.host.recv_count),(1,1,1))
        self.assertFalse(self.host.is_receiving())

    def test_unknown_status_keeps_identity_and_does_not_accept_or_ack_events(self):
        self.host.self_id='wxid_old_bound_self';self.host.account_name='old bound fixture'
        seen=[];self.host._callbacks=[seen.append]
        thread,_,_=self.start({'online':True,'self_id':'wxid_unverified'},[self.event()]);thread.join(2)
        self.assertFalse(thread.is_alive());self.assertFalse(self.host.online)
        self.assertEqual(self.host.self_id,'wxid_old_bound_self')
        self.assertEqual(self.host.account_name,'old bound fixture')
        self.assertEqual(seen,[])
        self.assertEqual((self.host._event_seen,self.host._event_ack,self.host.recv_count),(0,0,0))
        self.assertEqual(self.host._event_pending,{})
        self.assertIn('状态未核验',self.host.last_error)

    def test_async_event_pending_is_preserved_without_ack_before_false_stop(self):
        seen=[]
        def callback(raw):seen.append(raw);return False
        self.host._callbacks=[callback]
        thread,_,_=self.start(self.status(False),[self.event()]);thread.join(2)
        self.assertEqual(len(seen),1)
        self.assertEqual((self.host._event_seen,self.host._event_ack,self.host.recv_count),(1,0,1))
        self.assertIn(1,self.host._event_pending)
        self.assertFalse(self.host.is_receiving())

    def test_true_poll_stays_healthy_and_does_not_use_connect_cached_false(self):
        self.host._remote_status={'online':True,'listening':False}
        thread,stop,second=self.start(self.status(True));self.assertTrue(second.wait(2))
        self.assertTrue(thread.is_alive());self.assertTrue(self.host.is_receiving())
        self.assertIs(self.host._remote_status['listening'],True)
        self.host.note.assert_not_called();stop.set();thread.join(2)

    def test_old_false_poll_cannot_clobber_new_source(self):
        replacement=object()
        def take_over():
            with self.host._event_lock:
                self.host._poll_stop.set();self.host._poll_stop=threading.Event()
                self.host._receive_generation=8;self.host._poll_thread=replacement
                self.host.online=self.host._listening=True;self.host.last_error='new source'
        thread,_,_=self.start(self.status(False),before_return=take_over);thread.join(2)
        self.assertTrue(self.host.online);self.assertTrue(self.host._listening)
        self.assertEqual(self.host.last_error,'new source');self.host.note.assert_not_called()

    def test_callback_takeover_skips_old_false_after_batch_and_does_not_ack_new_source(self):
        def callback(raw):
            with self.host._event_lock:
                self.host._poll_stop.set();self.host._poll_stop=threading.Event()
                self.host._receive_generation=8;self.host._poll_thread=object()
                self.host.online=self.host._listening=True;self.host.last_error='new callback source'
        self.host._callbacks=[callback]
        thread,_,_=self.start(self.status(False),[self.event()]);thread.join(2)
        self.assertTrue(self.host.online);self.assertTrue(self.host._listening)
        self.assertEqual(self.host.last_error,'new callback source');self.host.note.assert_not_called()
        self.assertEqual(self.host._event_ack,0)


if __name__=='__main__':unittest.main()
