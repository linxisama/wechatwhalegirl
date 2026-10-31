"""共享目录RPC的离线验收；全部使用假微信，不创建窗口或发送真实消息。"""
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from src.runtime_identity import APP_VERSION
from src.send_outcome import SendOutcome
from src.isolated_wechat.protocol import (ProtocolError, live_service, load_pairing, read_document,
                                          request_digest, signed, validate_control, verify, write_document)
from src.isolated_wechat.guest_service import GuestService
from src.isolated_wechat.host_bridge import RpcError, SandboxBridge


class FakeRelay:
    def __init__(self, _channel):
        self.started = False
    def start(self): self.started = True
    def close(self): self.started = False


class FakeNative:
    def __init__(self, data_dir, **kwargs):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.online = False
        self.listening = False
        self.sends = []
        self.cancelled = 0
        self.resumed = 0
        self.configured_account = ''
        self.send_environment = {'ready': True}
        self.callback = None
    def status(self):
        return {'backend':'wechatauto','online':self.online,'self_id':'wxid_guest','account':'客体账号',
                'account_dir':'wxid_guest_abcd','db_dir':'guest-private','listening':self.listening,
                'send_environment':dict(self.send_environment),'error':''}
    def connect(self): self.online=True;return True,'读取已连接'
    def disconnect(self): self.online=False;self.listening=False
    def start_listening(self, callback): self.callback=callback;self.listening=True;return True,'监听已开始'
    def stop_listening(self): self.listening=False
    def stop_receiving(self): self.listening=False
    def cancel_pending_sends(self): self.cancelled+=1
    def resume_sends(self): self.resumed+=1
    def send_text(self, target, text, name, at):
        self.sends.append((target,text,name,at))
        return SendOutcome(True,'已提交',{'submission_state':'confirmed','send_key':True,'delivery_verified':False})
    def inspect_send_environment(self, who): return dict(self.send_environment, target_checked=who)
    def display_name(self, text): return {'wxid_friend':'朋友'}.get(text,text)
    def resolve_session(self,text): return 'wxid_friend' if text=='朋友' else text
    def member_names(self,text): return ['成员']


def wait_until(condition, seconds=2):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if condition(): return True
        time.sleep(.01)
    return False


class ChannelCase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.channel=self.root/'channel';self.channel.mkdir()
        (self.channel/'pairing.json').write_text(json.dumps({'version':1,'channel_id':uuid.uuid4().hex,
                                                            'secret':'12'*32}),'utf-8')
        self.pairing=load_pairing(self.channel)
        self._old_environment=os.environ.get('WHALE_ISOLATED_CHANNEL')
        self._services=[]
    def tearDown(self):
        for service,thread in self._services:
            service._stop.set();thread.join(2)
            self.assertFalse(thread.is_alive())
        if self._old_environment is None:os.environ.pop('WHALE_ISOLATED_CHANNEL',None)
        else:os.environ['WHALE_ISOLATED_CHANNEL']=self._old_environment
        self.temp.cleanup()
    def service(self):
        return GuestService(self.channel,self.root/'guest-profile',bridge_factory=FakeNative)
    def request(self, service, action, args=None, request_id=None, deadline=None):
        rid=request_id or uuid.uuid4().hex
        now=time.time()
        write_document(self.channel/'requests'/(rid+'.json'),self.pairing,{
            'epoch':service.epoch,'request_id':rid,'action':action,'args':args or {},
            'created':now,'deadline':deadline if deadline is not None else now+10})
        return rid


class ProtocolTests(ChannelCase):
    def test_signature_binds_payload_and_pairing(self):
        document=signed(self.pairing,{'epoch':'a'*32,'value':3})
        self.assertEqual(verify(self.pairing,document)['value'],3)
        document['value']=4
        with self.assertRaises(ProtocolError):verify(self.pairing,document)

    def test_expired_heartbeat_and_oversized_documents_are_rejected(self):
        write_document(self.channel/'service.json',self.pairing,{'epoch':'a'*32,'active':True,
            'heartbeat':time.time()-30,'app_version':APP_VERSION})
        with self.assertRaises(ProtocolError):live_service(self.pairing)
        with self.assertRaises(ProtocolError):read_document(self.channel/'service.json',self.pairing,10)

    def test_no_paths_outside_channel(self):
        with self.assertRaises(ProtocolError):write_document(self.root/'outside.json',self.pairing,{'value':1})
        self.assertFalse((self.root/'outside.json').exists())

    def test_parent_segments_cannot_escape_relative_boundary_check(self):
        with self.assertRaises(ProtocolError):
            write_document(self.channel/'control/../../outside.json', self.pairing, {'value':1})
        self.assertFalse((self.root/'outside.json').exists())

    def test_symbolic_link_is_never_followed(self):
        target=self.root/'outside.json';target.write_text('{}','utf-8')
        link=self.channel/'control/link.json'
        try:link.symlink_to(target)
        except (OSError,NotImplementedError):self.skipTest('本机没有创建测试链接的权限')
        with self.assertRaises(ProtocolError):read_document(link,self.pairing)
        with self.assertRaises(ProtocolError):write_document(link,self.pairing,{'value':1})
        self.assertEqual(target.read_text('utf-8'),'{}')


class GuestTests(ChannelCase):
    def test_control_slots_exist_before_watch_are_inert_and_accept_host_replacement(self):
        service=self.service()
        for action in ('cancel','stop'):
            value=read_document(self.channel/'control'/(action+'.json'),self.pairing)
            self.assertEqual(value['action'],'pending')
            self.assertFalse(validate_control(value,service.epoch,action))
        watcher=threading.Thread(target=service._watch,daemon=True)
        watcher.start()
        try:
            self.assertTrue(wait_until(lambda:(self.channel/'service.json').exists()))
            self.assertEqual(service.bridge.cancelled,0)
            self.assertFalse(service._stop.is_set())
            host=SandboxBridge(self.root/'host-data',self.channel)
            host.cancel_pending_sends()
            self.assertTrue(wait_until(lambda:service.bridge.cancelled==1))
            self.assertFalse(service._stop.is_set())
            host._control('stop')
            self.assertTrue(wait_until(service._stop.is_set))
            self.assertEqual(service.bridge.cancelled,2)
        finally:
            service._stop.set();watcher.join(2)
            self.assertFalse(watcher.is_alive())

    def test_preexisting_valid_controls_survive_constructor_and_are_executed(self):
        epoch='a'*32
        expected={}
        for action in ('cancel','stop'):
            now=time.time()
            path=self.channel/'control'/(action+'.json')
            write_document(path,self.pairing,{'epoch':epoch,'action':action,
                'request_id':uuid.uuid4().hex,'created':now,'deadline':now+30})
            expected[action]=path.read_bytes()
        with patch('src.isolated_wechat.guest_service.uuid.uuid4',return_value=Mock(hex=epoch)):
            service=self.service()
        for action in ('cancel','stop'):
            path=self.channel/'control'/(action+'.json')
            self.assertEqual(path.read_bytes(),expected[action])
            self.assertTrue(validate_control(read_document(path,self.pairing),service.epoch,action))
        watcher=threading.Thread(target=service._watch,daemon=True)
        watcher.start()
        try:
            self.assertTrue(wait_until(service._stop.is_set))
            self.assertEqual(service.bridge.cancelled,2)
        finally:
            service._stop.set();watcher.join(2)
            self.assertFalse(watcher.is_alive())

    def test_duplicate_consumed_send_never_executes_twice_even_if_response_lost(self):
        service=self.service()
        args={'target':'wxid_friend','text':'唯一正文','target_name':'朋友'}
        rid=self.request(service,'send_text',args)
        self.assertTrue(service.process_once())
        response=self.channel/'responses'/(rid+'.json')
        self.assertTrue(read_document(response,self.pairing)['value']['ok'])
        response.unlink()
        self.request(service,'send_text',args,request_id=rid)
        service.process_once()
        self.assertEqual(len(service.bridge.sends),1)
        duplicate=read_document(response,self.pairing)['value']
        self.assertFalse(duplicate['ok'])
        self.assertEqual(duplicate['diagnostic']['submission_state'],'uncertain')

    def test_stale_or_wrong_epoch_request_has_no_side_effect(self):
        service=self.service()
        self.request(service,'send_text',{'target':'wxid_friend','text':'不能发'},deadline=time.time()-1)
        self.assertFalse(service.process_once())
        self.assertEqual(service.bridge.sends,[])
        self.assertEqual(list((self.channel/'consumed').iterdir()),[])

    def test_old_signed_requests_are_removed_without_consumption(self):
        service=self.service()
        rid=self.request(service,'send_text',{'target':'wxid_friend','text':'过期正文'},deadline=time.time()-1)
        self.assertFalse(service.process_once())
        self.assertFalse((self.channel/'requests'/(rid+'.json')).exists())
        self.assertEqual(service.bridge.sends,[])

    def test_old_malformed_request_cannot_permanently_occupy_queue(self):
        service=self.service()
        path=self.channel/'requests'/(uuid.uuid4().hex+'.json')
        path.write_text('{unfinished','utf-8')
        self.assertFalse(service.process_once())
        self.assertTrue(path.exists())  # 不删可能仍在写入的新文档。
        os.utime(path,(time.time()-36,time.time()-36))
        self.assertFalse(service.process_once())
        self.assertFalse(path.exists())

    def test_events_persist_until_ack_and_keep_original_dictionary(self):
        service=self.service()
        raw={'username':'room@chatroom','content':'普通消息','sender_username':'wxid_friend',
             'local_id':8,'shard':'message_0','type':'文本'}
        service._on_message(raw)
        self.assertEqual(service._poll_events(0),[{'event_id':1,'message':raw}])
        self.assertEqual(len(list((self.channel/'events').glob('*.json'))),1)
        self.assertEqual(service._poll_events(1),[])
        self.assertEqual(list((self.channel/'events').glob('*.json')),[])

    def test_safety_notice_holds_guest_and_is_still_forwarded_to_host(self):
        service=self.service()
        raw={'username':'weixin','type':'文本','content':'为了你的账号安全，请重新登录'}
        service._on_message(raw)
        self.assertGreater(service.bridge.cancelled,0)
        hold=json.loads((self.root/'guest-profile/data/wechat_safety_hold.json').read_text('utf-8'))
        self.assertTrue(hold['active'])
        self.assertEqual(service._poll_events(0)[0]['message'],raw)
        self.assertFalse(service.dispatch('resume_sends',{})['ok'])

    def test_overflow_stops_receiving_instead_of_dropping_silently(self):
        service=self.service()
        with patch('src.isolated_wechat.guest_service.MAX_EVENTS',1):
            service._on_message({'username':'wxid_friend','content':'第一条'})
            service._on_message({'username':'wxid_friend','content':'第二条'})
        self.assertTrue(service._overflow)
        self.assertGreater(service.bridge.cancelled,0)
        self.assertFalse(service.dispatch('connect',{})['ok'])
        self.assertFalse(service.dispatch('start_listening',{})['ok'])
        self.assertFalse(service.dispatch('resume_sends',{})['ok'])
        outcome=service.dispatch('send_text',{'target':'wxid_friend','text':'不得发'})
        self.assertEqual(outcome['diagnostic']['submission_state'],'not_submitted')
        self.assertEqual(service.bridge.sends,[])

    def test_event_disk_error_stops_instead_of_advancing_sequence(self):
        service=self.service()
        with patch('src.isolated_wechat.guest_service.write_document',side_effect=OSError('disk full')):
            service._on_message({'username':'wxid_friend','content':'必须保留的原文'})
        self.assertTrue(service._overflow)
        self.assertEqual(service._sequence,0)
        self.assertGreater(service.bridge.cancelled,0)

    def test_consumption_precedes_send_and_contains_no_body(self):
        service=self.service()
        rid=self.request(service,'send_text',{'target':'wxid_friend','text':'不能写入消费记录的正文'})
        original=service.bridge.send_text
        def checked(*args):
            marker=self.channel/'consumed'/(rid+'.json')
            self.assertTrue(marker.exists())
            self.assertNotIn('不能写入消费记录的正文',marker.read_text('utf-8'))
            return original(*args)
        service.bridge.send_text=checked
        service.process_once()
        self.assertEqual(len(service.bridge.sends),1)


class HostIntegrationTests(ChannelCase):
    def start_pair(self):
        service=self.service()
        thread=threading.Thread(target=service.serve_forever,daemon=True)
        thread.start()
        self._services.append((service,thread))
        self.assertTrue(wait_until(lambda:(self.channel/'service.json').exists()))
        relay=patch('src.isolated_wechat.ocr_relay.HostOCRRelay',FakeRelay)
        relay.start();self.addCleanup(relay.stop)
        host=SandboxBridge(self.root/'host-data',self.channel)
        return service,host

    def test_connection_events_ack_names_and_submit_evidence(self):
        service,host=self.start_pair()
        self.assertTrue(host.connect()[0])
        self.assertEqual(host.backend,'sandbox')
        self.assertTrue(host.text_only)
        host.resume_sends()
        received=[]
        self.assertTrue(host.start_listening(received.append)[0])
        service.bridge.callback({'username':'wxid_friend','target_name':'朋友','content':'原文',
                                 'local_id':19,'shard':'message_0','type':'文本'})
        self.assertTrue(wait_until(lambda:len(received)==1))
        self.assertEqual(received[0]['content'],'原文')
        self.assertTrue(wait_until(lambda:not list((self.channel/'events').glob('*.json'))))
        result=host.send_text('wxid_friend','回复','朋友')
        self.assertTrue(result[0])
        self.assertEqual(result.diagnostic['submission_state'],'confirmed')
        self.assertEqual(len(service.bridge.sends),1)
        self.assertEqual(host.display_name('wxid_friend'),'朋友')
        self.assertEqual(host.resolve_session('朋友'),'wxid_friend')
        self.assertEqual(host.send_files('wxid_friend',['image.png']).diagnostic['submission_state'],'not_submitted')
        host.disconnect()
        self.assertIsNone(host._relay)

    def test_only_readonly_connect_wait_is_extended_and_send_remains_24_seconds(self):
        service=self.service();service.heartbeat()
        notes=[]
        host=SandboxBridge(self.root/'host-data',self.channel,note=notes.append)
        status={'online':True,'self_id':'wxid_guest','account':'客体账号'}
        with patch('src.isolated_wechat.ocr_relay.HostOCRRelay',FakeRelay), \
             patch.object(host,'_rpc',return_value={'ok':True,'status':status}) as rpc:
            self.assertTrue(host.connect()[0])
            rpc.assert_called_once_with('connect',host._settings(),timeout=85)
            rpc.reset_mock()
            self.assertTrue(host.send_text('wxid_friend','离线假发送')[0])
            self.assertEqual(rpc.call_args.args[0],'send_text')
            self.assertEqual(rpc.call_args.kwargs['timeout'],24)
        self.assertTrue(any('只读' in note and '85' in note for note in notes))
        host._relay.close()

    def test_late_readonly_response_keeps_29_second_request_start_deadline(self):
        service=self.service();service.heartbeat()
        host=SandboxBridge(self.root/'host-data',self.channel)
        requests=[]
        original=write_document
        def respond(path,pairing,payload,**kwargs):
            result=original(path,pairing,payload,**kwargs)
            if path.parent.name=='requests':
                requests.append(dict(payload))
                original(self.channel/'responses'/path.name,pairing,
                    {'epoch':service.epoch,'request_id':payload['request_id'],
                     'request_digest':request_digest(verify(pairing,signed(pairing,payload))),
                     'ok':True,'value':{'ok':True}})
            return result
        # 模拟同一epoch的51秒迟到只读响应，不真实等待或操作微信。
        with patch('src.isolated_wechat.host_bridge.write_document',side_effect=respond), \
             patch('src.isolated_wechat.host_bridge.time.monotonic',
                   side_effect=lambda:51.22 if requests else 0):
            self.assertTrue(host._rpc('connect',timeout=85)['ok'])
        self.assertEqual(len(requests),1)
        self.assertLessEqual(requests[0]['deadline']-requests[0]['created'],29)
        self.assertGreater(requests[0]['deadline']-requests[0]['created'],28)

    def test_readonly_inspection_wait_covers_inner_worker_without_changing_send_budget(self):
        host=SandboxBridge(self.root/'host-data',self.channel)
        with patch.object(host,'_rpc',return_value={'environment':{'ready':False}}) as rpc:
            self.assertFalse(host.inspect_send_environment('文件传输助手')['ready'])
            rpc.assert_called_once_with('inspect_environment',{'who':'文件传输助手'},timeout=35)

    def test_readonly_inspection_timeout_is_reported_once_without_automatic_repost(self):
        host=SandboxBridge(self.root/'host-data',self.channel)
        with patch.object(host,'_rpc',side_effect=RpcError('只读检查未在预算内完成')) as rpc:
            result=host.inspect_send_environment('文件传输助手')
        rpc.assert_called_once_with('inspect_environment',{'who':'文件传输助手'},timeout=35)
        self.assertFalse(result['ready'])
        self.assertEqual(result['waiting_code'],'isolated_unavailable')

    def test_name_resolution_executes_in_guest_account_without_host_contacts(self):
        service,host=self.start_pair()
        service.bridge._chat_name=Mock(return_value='客体唯一备注')
        self.assertTrue(host.connect()[0])
        with patch.object(host,'_contact_names',side_effect=AssertionError('不应读取宿主联系人')):
            self.assertEqual(host._chat_name('wxid_friend','旧缓存名'),'客体唯一备注')
        service.bridge._chat_name.assert_called_once_with('wxid_friend','旧缓存名')
        host.disconnect()

    def test_inbound_name_normalization_never_waits_for_rpc_even_when_busy(self):
        from src.wechat_transports import message_from_raw
        host=SandboxBridge(self.root/'host-data',self.channel)
        host.self_id='wxid_guest'
        host._rpc_lock.acquire()
        try:
            with patch.object(host,'_rpc',side_effect=AssertionError('同步入站不应访问RPC')):
                message=message_from_raw({'username':'room@chatroom','sender_username':'wxid_member',
                    'type':'文本','content':'鲸鱼娘你好','local_id':1},host,['鲸鱼娘'])
                self.assertEqual(message['target_name'],'room@chatroom')
                self.assertEqual(host.display_name('wxid_member','已知备用名'),'已知备用名')
                self.assertEqual(host.resolve_target_name('room@chatroom'),'unknown')
                host._cache_name('room@chatroom','缓存群名')
                self.assertEqual(host.resolve_target_name('room@chatroom'),'缓存群名')
        finally:
            host._rpc_lock.release()

    def test_event_labels_are_cached_before_callback_but_do_not_authorize_send_target(self):
        service,host=self.start_pair()
        service.bridge._chat_name=Mock(return_value='客体当前真实备注')
        self.assertTrue(host.connect()[0])
        seen=[]
        self.assertTrue(host.start_listening(lambda raw:seen.append(host.display_name(raw['username'])))[0])
        service._on_message({'username':'wxid_friend','target_name':'事件旧备注','type':'文本','content':'入站'})
        self.assertTrue(wait_until(lambda:len(seen)==1))
        self.assertEqual(seen,['事件旧备注'])
        self.assertEqual(host.resolve_target_name('wxid_friend'),'事件旧备注')
        self.assertEqual(host._chat_name('wxid_friend'),'客体当前真实备注')
        service.bridge._chat_name.assert_called_once_with('wxid_friend','')
        host.disconnect()

    def test_invalid_labels_are_ignored_and_account_change_clears_cached_names(self):
        host=SandboxBridge(self.root/'host-data',self.channel)
        for label in ('', '\n伪造行', 'x'*257, None):
            host._cache_name('room@chatroom',label)
        self.assertEqual(host.resolve_target_name('room@chatroom'),'unknown')
        host._apply_status({'self_id':'wxid_first','account':'第一个账号','online':True})
        host._cache_name('room@chatroom','第一个账号的群名')
        host._apply_status({'self_id':'wxid_second','account':'第二个账号','online':True})
        self.assertEqual(host.resolve_target_name('room@chatroom'),'unknown')
        self.assertEqual(host.display_name('wxid_second'),'第二个账号')

    def test_guest_connection_failure_counter_and_rejection_evidence_are_preserved(self):
        service,host=self.start_pair()
        original=service.bridge.status
        service.bridge.status=lambda:dict(original(),consecutive_fail=2)
        self.assertTrue(host.connect()[0])
        self.assertEqual(host.consecutive_fail,2)
        # 客体参数检查发生在调用微信之前；签名响应中的 not_submitted 不能丢失。
        result=host.send_text('wxid_friend','x'*12001)
        self.assertFalse(result[0])
        self.assertEqual(result.diagnostic['submission_state'],'not_submitted')
        self.assertEqual(host.consecutive_fail,2)
        self.assertEqual(service.bridge.sends,[])
        host.disconnect()

    def test_slow_inspection_does_not_disconnect_event_poll_waiting_on_rpc_lock(self):
        service,host=self.start_pair()
        self.assertTrue(host.connect()[0])
        started=threading.Event()
        def slow(who):
            started.set();time.sleep(3.3)
            return {'ready':True,'target_checked':who}
        service.bridge.inspect_send_environment=slow
        inspected=[]
        inspector=threading.Thread(target=lambda:inspected.append(host.inspect_send_environment('朋友')))
        inspector.start();self.assertTrue(started.wait(1))
        # 先启动客体监听，再启动宿主轮询，避免管理调用等待同一把锁。
        service.bridge.start_listening(service._on_message)
        received=[];host._callbacks.append(received.append)
        host._listening=True
        host._poll_stop=threading.Event()
        host._poll_thread=threading.Thread(target=host._poll_remote,args=(host._poll_stop,),daemon=True)
        host._poll_thread.start()
        inspector.join(5)
        self.assertFalse(inspector.is_alive())
        service._on_message({'username':'wxid_friend','content':'定位后仍可收到'})
        self.assertTrue(wait_until(lambda:len(received)==1))
        self.assertTrue(host.online)
        self.assertTrue(host._listening)
        host.disconnect()

    def test_missing_pairing_does_not_prevent_constructing_console_bridge(self):
        host=SandboxBridge(self.root/'host-data',self.root/'missing')
        self.assertEqual(host.backend,'sandbox')
        self.assertFalse(host.connect()[0])
        self.assertFalse(host.status()['online'])
        self.assertIsNone(host._wx)

    def test_cancel_control_interrupts_only_owned_send_and_heartbeat_keeps_running(self):
        service,host=self.start_pair()
        self.assertTrue(host.connect()[0])
        started,released=threading.Event(),threading.Event()
        outcomes=[]
        def blocked(*args):
            service.bridge.sends.append(args)
            started.set();released.wait(2)
            return SendOutcome(False,'取消后结果待核实',{'submission_state':'uncertain','send_key':True})
        def cancel():
            service.bridge.cancelled+=1
            released.set()
        service.bridge.send_text=blocked
        service.bridge.cancel_pending_sends=cancel
        caller=threading.Thread(target=lambda:outcomes.append(host.send_text('wxid_friend','单次正文')))
        caller.start()
        self.assertTrue(started.wait(1))
        self.assertTrue(live_service(self.pairing)['active'])
        host.cancel_pending_sends()
        caller.join(2)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(service.bridge.sends),1)
        self.assertEqual(outcomes[0].diagnostic['submission_state'],'uncertain')
        host.disconnect()

    def test_callback_failure_leaves_event_unacknowledged(self):
        service,host=self.start_pair()
        self.assertTrue(host.connect()[0])
        def broken(_raw):raise RuntimeError('host callback failed')
        self.assertTrue(host.start_listening(broken)[0])
        service.bridge.callback({'username':'wxid_friend','content':'必须保留的消息','type':'文本'})
        self.assertTrue(wait_until(lambda:not host.online))
        self.assertEqual(host._event_ack,0)
        self.assertEqual(len(list((self.channel/'events').glob('*.json'))),1)
        host.disconnect()

    def test_async_delivery_keeps_guest_event_until_explicit_host_ack(self):
        service,host=self.start_pair()
        try:
            self.assertTrue(host.connect()[0])
            seen=[]
            def deferred(raw):
                seen.append(raw)
                return False
            self.assertTrue(host.start_listening(deferred)[0])
            service._on_message({'username':'wxid_friend','content':'等待宿主入库','type':'文本'})
            self.assertTrue(wait_until(lambda:len(seen)==1))
            self.assertEqual(seen[0]['_sandbox_event_id'],1)
            # 再等两次poll，仍未确认时不能清guest文件或重复安排runtime。
            time.sleep(.6)
            self.assertEqual(len(seen),1)
            self.assertEqual(host._event_ack,0)
            self.assertEqual(len(list((self.channel/'events').glob('*.json'))),1)
            host.ack_inbound(seen[0]['_sandbox_event_id'])
            self.assertTrue(wait_until(lambda:not list((self.channel/'events').glob('*.json'))))
            self.assertEqual(json.loads(host._ack_file.read_text('utf-8'))['ack'],1)
        finally:
            host.disconnect()

    def test_stop_start_without_connect_replays_pending_only_to_current_callback(self):
        service,host=self.start_pair()
        try:
            connected = host.connect()
            self.assertTrue(connected[0], connected[1])
            old,new=[],[]
            def deferred(raw):
                old.append(raw)
                return False
            self.assertTrue(host.start_listening(deferred)[0])
            service._on_message({'username':'wxid_friend','content':'停止前尚未入库','type':'文本'})
            self.assertTrue(wait_until(lambda:len(old)==1))
            host.stop_listening()
            self.assertEqual(host._event_ack,0)
            self.assertTrue(host.start_listening(new.append)[0])
            self.assertTrue(wait_until(lambda:len(new)==1))
            self.assertEqual(len(old),1)
            self.assertEqual(new[0]['_sandbox_event_id'],1)
            self.assertTrue(wait_until(lambda:not list((self.channel/'events').glob('*.json'))))
            self.assertEqual(host._event_ack,1)
        finally:
            host.disconnect()

    def test_epoch_switch_never_reuses_old_connection_or_sends(self):
        service,host=self.start_pair()
        self.assertTrue(host.connect()[0])
        service.epoch=uuid.uuid4().hex;service.heartbeat()
        result=host.send_text('wxid_friend','不得在新会话补发')
        self.assertFalse(result[0])
        self.assertEqual(result.diagnostic['submission_state'],'not_submitted')
        self.assertEqual(service.bridge.sends,[])
        if host._relay:host._relay.close()

    def test_rpc_timeout_is_uncertain_only_after_send_request_was_posted(self):
        service=self.service();service.heartbeat()
        host=SandboxBridge(self.root/'host-data',self.channel)
        with self.assertRaises(RpcError) as failure:
            # 文件签名/路径核验会受并行打包IO影响，留出确实投递请求的时间。
            host._rpc('send_text',{'target':'wxid_friend','text':'正文'},timeout=.5)
        self.assertEqual(failure.exception.diagnostic['submission_state'],'uncertain')
        self.assertEqual(list((self.channel/'requests').glob('*.json')),[])
        cancel=read_document(self.channel/'control/cancel.json',self.pairing)
        self.assertEqual(cancel['action'],'cancel')

    def test_request_flush_error_is_uncertain_and_cancels_possible_guest_work(self):
        service=self.service();service.heartbeat()
        host=SandboxBridge(self.root/'host-data',self.channel)
        original=write_document
        def after_write(path,*args,**kwargs):
            original(path,*args,**kwargs)
            if path.parent.name=='requests':
                raise OSError('flush failed after guest-visible write')
        with patch('src.isolated_wechat.host_bridge.write_document',side_effect=after_write):
            with self.assertRaises(RpcError) as failure:
                host._rpc('send_text',{'target':'wxid_friend','text':'正文'},timeout=.2)
        self.assertEqual(failure.exception.diagnostic['submission_state'],'uncertain')
        self.assertEqual(list((self.channel/'requests').glob('*.json')),[])
        self.assertEqual(read_document(self.channel/'control/cancel.json',self.pairing)['action'],'cancel')


class HostAcknowledgementTests(ChannelCase):
    def setUp(self):
        super().setUp()
        service=self.service()
        self.host=SandboxBridge(self.root/'host-data',self.channel)
        self.host._pairing=self.pairing
        self.host._epoch=service.epoch
        self.host._load_ack()
        self.seen=[]
        def deferred(raw):
            self.seen.append(raw)
            return False
        self.host._callbacks=[deferred]
        self.stop=threading.Event()

    def receive(self, sequence, content='内容'):
        self.host._receive_event({'event_id':sequence,
            'message':{'username':'wxid_friend','type':'文本','content':content}},self.stop)

    def test_out_of_order_completion_only_advances_durable_contiguous_prefix(self):
        for sequence in (1,2,3):self.receive(sequence)
        self.host.ack_inbound(3)
        self.host.ack_inbound(2)
        self.assertEqual(self.host._event_ack,0)
        self.assertFalse(self.host._ack_file.exists())
        self.host.ack_inbound(1)
        self.assertEqual(self.host._event_ack,3)
        self.assertEqual(json.loads(self.host._ack_file.read_text('utf-8'))['ack'],3)
        self.assertEqual(self.host._event_pending,{})
        self.host.ack_inbound(3)  # 重复完成不回退水位。
        self.assertEqual(self.host._event_ack,3)

    def test_disk_failure_never_advances_memory_ack_or_discards_pending(self):
        self.receive(1)
        with patch.object(self.host,'_save_ack',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.host.ack_inbound(1)
        self.assertEqual(self.host._event_ack,0)
        self.assertIn(1,self.host._event_pending)
        self.assertEqual(self.host._event_completed,set())
        self.host.ack_inbound(1)
        self.assertEqual(self.host._event_ack,1)

    def test_restart_reloads_only_saved_prefix_and_replays_unacknowledged_event(self):
        self.receive(1)
        self.receive(2)
        self.host.ack_inbound(1)
        self.host._load_ack()
        self.assertEqual(self.host._event_ack,1)
        self.assertEqual(self.host._event_seen,1)
        self.assertEqual(self.host._event_pending,{})
        self.receive(2)
        self.assertEqual([raw['_sandbox_event_id'] for raw in self.seen],[1,2,2])

    def test_unknown_or_malformed_ack_cannot_skip_undelivered_events(self):
        self.receive(1)
        for value in (2,0,-1,True,'1',None):
            with self.subTest(value=value),self.assertRaises(ProtocolError):
                self.host.ack_inbound(value)
        self.assertEqual(self.host._event_ack,0)

    def test_duplicate_pending_is_not_redelivered_and_conflicting_body_stops(self):
        self.receive(1)
        self.receive(1)
        self.assertEqual(len(self.seen),1)
        with self.assertRaises(ProtocolError):self.receive(1,'同id不同内容')
        with self.assertRaises(ProtocolError):self.receive(3)
        self.assertEqual(self.host._event_ack,0)

    def test_early_async_ack_waits_for_all_synchronous_callbacks_to_finish(self):
        observed=[]
        def asynchronous(raw):
            self.host.ack_inbound(raw['_sandbox_event_id'])
            observed.append(self.host._event_ack)
            return False
        def synchronous(raw):
            observed.append(self.host._event_ack)
        self.host._callbacks=[asynchronous,synchronous]
        self.receive(1)
        self.assertEqual(observed,[0,0])
        self.assertEqual(self.host._event_ack,1)

    def test_later_callback_failure_cannot_publish_an_early_ack(self):
        def asynchronous(raw):
            self.host.ack_inbound(raw['_sandbox_event_id'])
            return False
        def failed(raw):
            raise OSError('同步接收尚未持久完成')
        self.host._callbacks=[asynchronous,failed]
        with self.assertRaises(OSError):self.receive(1)
        self.assertEqual(self.host._event_ack,0)
        self.assertFalse(self.host._ack_file.exists())
        self.assertIn(1,self.host._event_pending)


if __name__=='__main__':unittest.main()
