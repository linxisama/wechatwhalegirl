"""Pure offline race tests against staged copies. No API/client/UI/send access."""
import importlib.util
import ast
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

WORK=Path(__file__).resolve().parent
ROOT=WORK.parent
sys.path.insert(0,str(ROOT))
from src.wechat_bridge import WeChatBridge

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module

from src import wechat_bridge as base
from src.isolated_wechat import host_bridge as hostmod

class Alive:
    def is_alive(self): return True

def status_payload():
    return {'online':True,'listening':True,'self_id':'wxid_fixture_current','account':'fixture',
            'send_environment':{'ready':True},'error':''}

def message():
    return {'event_id':1,'message':{'username':'wxid_fixture','target_name':'fixture',
            'sender_username':'wxid_sender','sender_name':'fixture sender','content':'offline fixture'}}

def wait_until(predicate):
    end=time.monotonic()+2
    while time.monotonic()<end:
        if predicate(): return
        time.sleep(.002)
    raise AssertionError('offline race did not reach expected point')

class ListenerStagedTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='listener-staged-only-')
        self.addCleanup(self.folder.cleanup)
        self.patcher=patch.object(WeChatBridge,'status',base.WeChatBridge.status)
        self.patcher.start();self.addCleanup(self.patcher.stop)
        self.host=hostmod.SandboxBridge(Path(self.folder.name)/'data',Path(self.folder.name)/'absent-channel')
        self.host._service=Mock(return_value=(None,{'status':{}}))
        self.host._pairing={'channel_id':'0'*32}
        self.host._epoch='1'*32
        self.host.online=True;self.host._listening=True
        self.host._receive_generation=7
        self.host._poll_stop=threading.Event()
        self.host.note=Mock()
        self.workers=[]
        self.addCleanup(self.cleanup_workers)

    def cleanup_workers(self):
        self.host._poll_stop.set()
        for thread,stop,release in self.workers:
            stop.set();release.set();thread.join(2)
            self.assertFalse(thread.is_alive())

    def blocked(self,result=None,error=None):
        entered,release=threading.Event(),threading.Event()
        stop=self.host._poll_stop
        def rpc(action,args,timeout):
            self.assertEqual(action,'poll')
            entered.set()
            if not release.wait(2): raise AssertionError('test release missing')
            if error: raise error
            return result
        self.host._rpc=rpc
        thread=threading.Thread(target=self.host._poll_remote,args=(stop,7),daemon=True)
        self.host._poll_thread=thread
        self.workers.append((thread,stop,release))
        thread.start();self.assertTrue(entered.wait(2))
        return thread,stop,release

    def replace_source(self,*,online,listening,error):
        with self.host._event_lock:
            self.host._poll_stop.set()
            self.host._poll_stop=threading.Event()
            self.host._receive_generation+=1
            self.host._poll_thread=Alive()
            self.host.online=online;self.host._listening=listening;self.host.last_error=error
            self.host.self_id='wxid_new';self.host.account_name='new'
            self.host.send_environment={'ready':False,'reason':'new generation'}

    def test_old_success_cannot_restore_new_failure_or_mutate_identity(self):
        thread,_,release=self.blocked({'status':status_payload(),'events':[message()]})
        self.host._callbacks=[Mock()]
        self.replace_source(online=False,listening=False,error='new listener failed')
        release.set();thread.join(2)
        self.assertFalse(self.host.online)
        self.assertEqual(self.host.last_error,'new listener failed')
        self.assertEqual(self.host.self_id,'wxid_new')
        self.assertEqual(self.host.send_environment,{'ready':False,'reason':'new generation'})
        self.host._callbacks[0].assert_not_called()
        self.assertEqual((self.host._event_ack,self.host._event_seen,self.host.recv_count),(0,0,0))
        self.assertEqual(self.host.status()['read_state'],'disconnected')

    def test_old_failure_cannot_clobber_new_working_listener(self):
        thread,_,release=self.blocked(error=hostmod.RpcError('old failure'))
        self.replace_source(online=True,listening=True,error='new current notice')
        release.set();thread.join(2)
        self.assertTrue(self.host.online)
        self.assertTrue(self.host._listening)
        self.assertEqual(self.host.last_error,'new current notice')
        self.assertTrue(self.host.status()['listening'])
        self.host.note.assert_not_called()

    def test_stop_while_rpc_inflight_drops_late_events_and_status(self):
        thread,_,release=self.blocked({'status':status_payload(),'events':[message()]})
        callback=Mock();self.host._callbacks=[callback]
        self.host.last_error='kept until next explicit connect'
        self.host.stop_receiving();release.set();thread.join(2)
        callback.assert_not_called()
        self.assertEqual(self.host.last_error,'kept until next explicit connect')
        self.assertEqual((self.host._event_ack,self.host._event_seen,self.host.recv_count),(0,0,0))
        self.assertFalse(self.host.status()['listening'])
        self.assertEqual(self.host.status()['read_state'],'connected')

    def test_current_rpc_failure_stops_health_and_retains_error(self):
        thread,_,release=self.blocked(error=hostmod.RpcError('current read timeout'))
        release.set();thread.join(2)
        value=self.host.status()
        self.assertEqual((value['online'],value['listening'],value['read_state']),(False,False,'disconnected'))
        self.assertEqual(value['error'],'current read timeout')
        self.assertFalse(self.host._listening)
        self.host.note.assert_called_once()

    def test_current_signature_failure_stops_health(self):
        thread,_,release=self.blocked(error=hostmod.ProtocolError('signature invalid'))
        release.set();thread.join(2)
        self.assertFalse(self.host.status()['online'])
        self.assertFalse(self.host.status()['listening'])
        self.assertEqual(self.host.status()['error'],'signature invalid')

    def test_callback_failure_preserves_pending_without_ack_or_count(self):
        thread,_,release=self.blocked({'status':status_payload(),'events':[message()]})
        self.host._callbacks=[Mock(side_effect=RuntimeError('offline consumer failed'))]
        release.set();thread.join(2)
        self.assertFalse(self.host.online)
        self.assertEqual(self.host._event_ack,0)
        self.assertEqual(self.host.recv_count,0)
        self.assertIn(1,self.host._event_pending)
        self.assertIn('RuntimeError',self.host.last_error)

    def test_dead_thread_reference_is_not_listening_in_base_or_sandbox(self):
        self.host._poll_thread=threading.Thread(target=lambda:None)
        self.host._poll_thread.start();self.host._poll_thread.join(1)
        self.assertFalse(self.host.status()['listening'])
        self.assertEqual(self.host.status()['read_state'],'connected')
        self.assertFalse(base.WeChatBridge.status(self.host)['listening'])

    def test_live_thread_with_stop_set_does_not_claim_receiving(self):
        self.host._poll_thread=Alive();self.host._poll_stop.set()
        value=self.host.status()
        self.assertFalse(value['listening'])
        self.assertEqual(value['read_state'],'connected')

    def test_valid_current_poll_delivers_once_and_persists_one_ack(self):
        thread,stop,release=self.blocked({'status':status_payload(),'events':[message()]})
        received=[];self.host._callbacks=[received.append]
        apply=self.host._apply_status
        lock_observations=[]
        def checked_apply(value):
            lock_observations.append(self.host._event_lock._is_owned())
            return apply(value)
        self.host._apply_status=checked_apply
        release.set();wait_until(lambda:self.host._event_ack==1)
        self.assertEqual(len(received),1)
        self.assertEqual((self.host.recv_count,self.host._event_seen,self.host._event_ack),(1,1,1))
        value=self.host.status()
        self.assertTrue(value['listening']);self.assertEqual(value['read_state'],'listening')
        persisted=json.loads(self.host._ack_file.read_text('utf-8'))
        self.assertEqual(persisted['ack'],1)
        self.assertTrue(lock_observations and all(lock_observations))
        self.host.stop_receiving();thread.join(2)
        self.assertFalse(self.host.status()['listening'])

    def test_generation_change_alone_blocks_old_finally(self):
        thread,_,release=self.blocked({'status':status_payload(),'events':[]})
        with self.host._event_lock:
            self.host._receive_generation+=1
            self.host._listening=True
            self.host.last_error='new generation retained'
        release.set();thread.join(2)
        self.assertTrue(self.host._listening)
        self.assertEqual(self.host.last_error,'new generation retained')

    def test_replaced_worker_source_cannot_apply_same_event_or_generation(self):
        thread,_,release=self.blocked({'status':status_payload(),'events':[]})
        with self.host._event_lock:
            self.host._poll_thread=Alive()
            self.host.last_error='new worker owns same generation'
            self.host.self_id='wxid_new_worker'
        release.set();thread.join(2)
        self.assertEqual(self.host.last_error,'new worker owns same generation')
        self.assertEqual(self.host.self_id,'wxid_new_worker')
        self.assertTrue(self.host._listening)

    def test_restart_replaces_dead_reference_without_already_running_lie(self):
        old_stop=self.host._poll_stop
        old_thread=threading.Thread(target=lambda:None);old_thread.start();old_thread.join(1)
        self.host._poll_thread=old_thread
        self.host._rpc=Mock(return_value={'ok':True})
        entered=threading.Event()
        def worker(stop,generation): entered.set();stop.wait(2)
        self.host._poll_remote=worker
        ok,_=self.host.start_listening(lambda _:None)
        self.assertTrue(ok);self.assertTrue(entered.wait(1))
        self.assertTrue(old_stop.is_set())
        self.assertIsNot(self.host._poll_thread,old_thread)
        self.host._rpc.assert_called_once_with('start_listening',self.host._settings(),timeout=5)
        self.host.stop_receiving();self.host._poll_thread.join(2)

class UiListenerStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree=ast.parse((ROOT/'wechat_gui.py').read_text('utf-8'))
        console=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ConsoleTab')
        method=copy.deepcopy(next(n for n in console.body if isinstance(n,ast.FunctionDef) and n.name=='refresh'))
        cut=next(i for i,n in enumerate(method.body) if isinstance(n,ast.Assign)
                 and any(isinstance(t,ast.Name) and t.id=='is_mock' for t in n.targets))
        method.body=method.body[:cut]
        module=ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[]))
        namespace={'time':time}
        exec(compile(module,'staged-ConsoleTab.refresh-receive-section','exec'),namespace)
        cls.refresh=staticmethod(namespace['refresh'])

    def render(self,*,running=True,listening=False,connecting=False,safety=False,backend='sandbox'):
        view=Mock(spec=['card_running','card_conn','btn_start','btn_test_wechat'])
        data={'running':running,'connecting':connecting,'connection':{
            'backend':backend,'online':True,'listening':listening,'account':'fixture',
            'received':0,'sent':0,'admin':True,'send_environment':{'ready':True},
            'error':'current receiving failure' if not listening else ''},'safety':{'active':safety}}
        self.refresh(view,data)
        return view

    def test_running_without_receiving_has_explicit_current_warning(self):
        view=self.render()
        self.assertEqual(view.card_running.update.call_args.args[0],'服务运行 · 接收已停止')
        self.assertFalse(view.card_running.update.call_args.kwargs['ok'])
        self.assertIn('current receiving failure',view.card_running.update.call_args.kwargs['detail'])
        self.assertFalse(view.card_conn.update.call_args.kwargs['ok'])
        self.assertIn('当前会话就绪',view.card_conn.update.call_args.args[0])
        self.assertIn('接收已停止',view.card_conn.update.call_args.kwargs['detail'])

    def test_current_healthy_receiving_retains_green_state(self):
        view=self.render(listening=True)
        self.assertEqual(view.card_running.update.call_args.args[0],'运行中')
        self.assertTrue(view.card_running.update.call_args.kwargs['ok'])
        self.assertTrue(view.card_conn.update.call_args.kwargs['ok'])

    def test_intentionally_stopped_is_not_reported_as_runtime_receive_error(self):
        view=self.render(running=False)
        self.assertEqual(view.card_running.update.call_args.args[0],'已停止')
        self.assertNotIn('接收已停止',view.card_conn.update.call_args.kwargs['detail'])

    def test_connecting_keeps_preparing_label(self):
        view=self.render(connecting=True)
        self.assertEqual(view.card_running.update.call_args.args[0],'正在连接微信…')
        self.assertIsNone(view.card_running.update.call_args.kwargs['ok'])

    def test_security_pause_remains_final_overriding_state(self):
        view=self.render(safety=True)
        self.assertEqual(view.card_running.update.call_args.args[0],'账号安全暂停')
        self.assertEqual(view.card_conn.update.call_args.args[0],'安全暂停 · 不连接微信')
        view.btn_start.setEnabled.assert_called_once_with(False)

    def test_native_running_with_dead_receiver_is_also_warning(self):
        view=self.render(backend='wechatauto')
        self.assertEqual(view.card_running.update.call_args.args[0],'服务运行 · 接收已停止')
        self.assertFalse(view.card_running.update.call_args.kwargs['ok'])
        self.assertFalse(view.card_conn.update.call_args.kwargs['ok'])
        self.assertIn('接收已停止',view.card_conn.update.call_args.kwargs['detail'])

    def test_native_healthy_receiver_keeps_green_state(self):
        view=self.render(backend='wechatauto',listening=True)
        self.assertEqual(view.card_running.update.call_args.args[0],'运行中')
        self.assertTrue(view.card_running.update.call_args.kwargs['ok'])
        self.assertTrue(view.card_conn.update.call_args.kwargs['ok'])

if __name__=='__main__': unittest.main(verbosity=2)
