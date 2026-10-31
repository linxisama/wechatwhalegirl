"""固定请求镜像的离线失效测试；只使用假微信，不操作窗口或外发消息。"""
import ctypes
import json
import os
import time
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from src.isolated_wechat import protocol
from src.isolated_wechat.guest_service import GuestService
from src.isolated_wechat.host_bridge import RpcError, SandboxBridge
from tests.test_isolated_transport1315 import ChannelCase


class MirrorCase(ChannelCase):
    def payload(self, service, action='resume_sends', args=None, **changes):
        now = time.time()
        result = dict(epoch=service.epoch, request_id=uuid.uuid4().hex,
                      action=action, args={} if args is None else args,
                      created=now, deadline=now + 10)
        result.update(changes)
        return result

    def publish(self, value, *, mirror=True, canonical=False):
        if canonical:
            protocol.write_document(self.channel / 'requests' / (value['request_id'] + '.json'),
                                    self.pairing, value, exclusive=True)
        if mirror:
            protocol.write_document(self.channel / 'control/request.json', self.pairing, value)
        return value['request_id']

    def response(self, request_id):
        return protocol.read_document(self.channel / 'responses' / (request_id + '.json'), self.pairing)

    def digest(self, payload):
        return protocol.request_digest(protocol.verify(self.pairing, protocol.signed(self.pairing, payload)))

    def process(self, service):
        # 协议/落盘失败可以由调用层停止代理，也可以在本轮拒绝；两者都不能重做业务。
        try:
            return service.process_once()
        except (OSError, protocol.ProtocolError):
            return False


class MirrorGuestTests(MirrorCase):
    def test_pending_slot_exists_before_first_advertisement_and_is_inert(self):
        service = self.service()
        pending = protocol.read_document(self.channel / 'control/request.json', self.pairing)
        self.assertEqual(pending['epoch'], service.epoch)
        self.assertEqual(pending['action'], 'pending')
        self.assertFalse(self.process(service))
        self.assertEqual(service.bridge.resumed, 0)
        service.heartbeat()
        self.assertIn('request_mirror_v1', protocol.live_service(self.pairing)['capabilities'])

    def test_complete_signed_mirror_works_while_original_uuid_is_invisible(self):
        service = self.service()
        value = self.payload(service)
        rid = self.publish(value)
        self.assertFalse((self.channel / 'requests' / (rid + '.json')).exists())
        self.assertTrue(self.process(service))
        self.assertEqual(service.bridge.resumed, 1)
        self.assertTrue(self.response(rid)['ok'])
        self.assertEqual(self.response(rid)['request_digest'], self.digest(value))

    def test_stale_slot_and_later_uuid_never_repeat_management_action(self):
        service = self.service()
        value = self.payload(service)
        rid = self.publish(value)
        self.process(service)
        first_response = (self.channel / 'responses' / (rid + '.json')).read_bytes()
        self.assertFalse((self.channel / 'consumed' / (rid + '.json')).exists())
        self.publish(value, mirror=False, canonical=True)
        for _ in range(3):
            self.process(service)
        self.assertEqual(service.bridge.resumed, 1)
        self.assertEqual((self.channel / 'responses' / (rid + '.json')).read_bytes(), first_response)

    def test_successfully_acknowledged_response_is_not_recreated_by_stale_mirror(self):
        service = self.service()
        dispatch = Mock(wraps=service.dispatch)
        service.dispatch = dispatch
        for action, args in (('resume_sends', {}), ('display_name', {'text': 'wxid_friend'}),
                             ('send_text', {'target': 'wxid_friend', 'text': '离线替身'})):
            with self.subTest(action=action):
                rid = self.publish(self.payload(service, action, args))
                self.assertTrue(self.process(service))
                response_path = self.channel / 'responses' / (rid + '.json')
                self.assertTrue(self.response(rid)['ok'])
                response_path.unlink()  # 宿主已读取确认并正常清理。
                before = dispatch.call_count
                self.process(service)
                self.process(service)
                self.assertEqual(dispatch.call_count, before)
                self.assertFalse(response_path.exists(), '每轮 poll 不应留下重建的孤儿回执')

    def test_same_uuid_and_mirror_are_one_logical_send_and_marker_binds_digest(self):
        service = self.service()
        value = self.payload(service, 'send_text', {'target': 'wxid_friend', 'text': '离线替身正文'})
        rid = self.publish(value, canonical=True)
        self.process(service)
        self.process(service)
        self.assertEqual(len(service.bridge.sends), 1)
        marker = protocol.read_document(self.channel / 'consumed' / (rid + '.json'), self.pairing)
        self.assertEqual(marker['request_digest'], self.digest(value))
        self.assertNotIn('离线替身正文', json.dumps(marker, ensure_ascii=False))

    def test_visible_conflicting_canonical_and_mirror_are_rejected_before_dispatch(self):
        service = self.service()
        value = self.payload(service)
        self.publish(value, mirror=False, canonical=True)
        self.publish(dict(value, args={'conflicting': True}))
        self.process(service)
        self.process(service)
        self.assertEqual(service.bridge.resumed, 0)

    def test_conflicting_late_uuid_does_not_replace_response_or_delete_conflicting_source(self):
        service = self.service()
        value = self.payload(service)
        rid = self.publish(value)
        self.process(service)
        response_path = self.channel / 'responses' / (rid + '.json')
        expected_response = response_path.read_bytes()
        conflicting = dict(value, args={'different': True})
        self.publish(conflicting, mirror=False, canonical=True)
        canonical_path = self.channel / 'requests' / (rid + '.json')
        expected_canonical = canonical_path.read_bytes()
        self.process(service)
        self.process(service)
        self.assertEqual(service.bridge.resumed, 1)
        self.assertEqual(response_path.read_bytes(), expected_response)
        self.assertEqual(canonical_path.read_bytes(), expected_canonical)

    def test_request_mutated_during_dispatch_is_not_removed_as_original(self):
        service = self.service()
        value = self.payload(service)
        rid = self.publish(value, canonical=True)
        canonical_path = self.channel / 'requests' / (rid + '.json')
        original = service.dispatch
        def mutate(action, args):
            protocol.write_document(canonical_path, self.pairing, dict(value, args={'replaced': True}))
            return original(action, args)
        service.dispatch = mutate
        self.process(service)
        self.assertEqual(service.bridge.resumed, 1)
        self.assertTrue(canonical_path.exists())
        self.assertEqual(protocol.read_document(canonical_path, self.pairing)['args'], {'replaced': True})

    def test_invalid_signature_never_dispatches(self):
        service = self.service()
        value = self.payload(service)
        envelope = protocol.signed(self.pairing, value)
        envelope['args'] = {'changed_without_signing': True}
        (self.channel / 'control/request.json').write_text(json.dumps(envelope), 'utf-8')
        self.process(service)
        self.assertEqual(service.bridge.resumed, 0)
        self.assertEqual(list((self.channel / 'consumed').iterdir()), [])

    def test_wrong_epoch_and_expired_mirror_never_dispatch(self):
        service = self.service()
        for changes in ({'epoch': uuid.uuid4().hex}, {'deadline': time.time() - 1}):
            with self.subTest(changes=changes):
                self.publish(self.payload(service, **changes))
                self.process(service)
        self.assertEqual(service.bridge.resumed, 0)
        self.assertEqual(list((self.channel / 'consumed').iterdir()), [])

    def test_restart_gets_new_epoch_and_cannot_replay_previous_slot(self):
        first = self.service()
        value = self.payload(first)
        self.publish(value)
        self.process(first)
        second = self.service()
        self.assertNotEqual(first.epoch, second.epoch)
        self.process(second)
        self.assertEqual(second.bridge.resumed, 0)

    def test_stop_set_before_discovery_prevents_mirror_dispatch(self):
        service = self.service()
        self.publish(self.payload(service))
        service._stop.set()
        self.process(service)
        self.assertEqual(service.bridge.resumed, 0)

    def test_response_failure_after_management_action_does_not_reexecute_stale_slot(self):
        service = self.service()
        value = self.payload(service)
        rid = self.publish(value)
        original = protocol.write_document
        def fail_response(path, *args, **kwargs):
            if Path(path).parent.name == 'responses':
                raise OSError('offline simulated response disk failure')
            return original(path, *args, **kwargs)
        with patch('src.isolated_wechat.guest_service.write_document', side_effect=fail_response):
            self.process(service)
        self.assertEqual(service.bridge.resumed, 1)
        self.process(service)
        self.publish(value, mirror=False, canonical=True)
        self.process(service)
        self.assertEqual(service.bridge.resumed, 1)
        response_path = self.channel / 'responses' / (rid + '.json')
        if response_path.exists():
            response = self.response(rid)
            self.assertEqual(response['request_digest'], self.digest(value))

    def test_consumed_marker_digest_conflict_never_executes_or_returns_success(self):
        service = self.service()
        value = self.payload(service, 'send_text', {'target': 'wxid_friend', 'text': '替身'})
        rid = self.publish(value)
        protocol.write_document(self.channel / 'consumed' / (rid + '.json'), self.pairing,
            {'epoch': service.epoch, 'request_id': rid, 'action': 'send_text',
             'consumed': time.time(), 'retry_allowed': False, 'request_digest': 'a' * 64}, exclusive=True)
        self.process(service)
        self.assertEqual(service.bridge.sends, [])
        response_path = self.channel / 'responses' / (rid + '.json')
        if response_path.exists():
            response = self.response(rid)
            self.assertFalse(response['ok'])
            self.assertNotEqual(response['request_digest'], self.digest(value))

    def test_clock_rollback_does_not_revalidate_expired_unseen_request(self):
        service = self.service()
        base = time.time()
        value = self.payload(service, created=base, deadline=base + 2)
        with patch('src.isolated_wechat.guest_service.time.time', return_value=base + 4):
            self.publish(value)
            self.process(service)
        with patch('src.isolated_wechat.guest_service.time.time', return_value=base + 1):
            self.publish(value)
            self.process(service)
        self.assertEqual(service.bridge.resumed, 0)

    def test_expiry_between_consumption_and_dispatch_prevents_action(self):
        service = self.service()
        base = time.time()
        value = self.payload(service, created=base, deadline=base + 1)
        self.publish(value)
        current = [base]
        original = protocol.write_document
        def expire_on_consume(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if Path(path).parent.name == 'consumed':
                current[0] = base + 2
            return result
        with patch('src.isolated_wechat.guest_service.time.time', side_effect=lambda: current[0]), \
             patch('src.isolated_wechat.guest_service.write_document', side_effect=expire_on_consume):
            self.process(service)
        self.assertEqual(service.bridge.resumed, 0)

    def test_seen_capacity_rejects_new_identity_without_evicting_active_identity(self):
        service = self.service()
        base = time.time() + .1
        first = self.payload(service, created=base, deadline=base + 20)
        second = self.payload(service, created=base, deadline=base + 20)
        with patch('src.isolated_wechat.guest_service.MAX_REQUESTS_SEEN', 1), \
             patch('src.isolated_wechat.guest_service.time.time', return_value=base), \
             patch('src.isolated_wechat.guest_service.time.monotonic', return_value=1000):
            self.publish(first)
            self.process(service)
            self.publish(second)
            self.process(service)
            self.publish(first)
            self.process(service)
        self.assertEqual(service.bridge.resumed, 1)

    def test_wall_expiry_alone_does_not_reclaim_identity_before_monotonic_grace(self):
        service = self.service()
        base = time.time() + .1
        wall, monotonic = [base], [1000.0]
        first = self.payload(service, created=base, deadline=base + 2)
        with patch('src.isolated_wechat.guest_service.MAX_REQUESTS_SEEN', 1), \
             patch('src.isolated_wechat.guest_service.time.time', side_effect=lambda: wall[0]), \
             patch('src.isolated_wechat.guest_service.time.monotonic', side_effect=lambda: monotonic[0]):
            self.publish(first)
            self.process(service)
            wall[0], monotonic[0] = base + 3, 1001.0
            self.publish(self.payload(service))
            self.process(service)
            self.assertEqual(service.bridge.resumed, 1)
            wall[0], monotonic[0] = base + 4, 1036.0
            self.publish(self.payload(service))
            self.process(service)
            self.assertEqual(service.bridge.resumed, 2)
            # 已回收身份也不能随墙钟倒退而重新成为有效请求。
            wall[0], monotonic[0] = base + 1, 1037.0
            self.publish(first)
            self.process(service)
        self.assertEqual(service.bridge.resumed, 2)

    def test_monotonic_grace_alone_does_not_evict_unexpired_identity(self):
        service = self.service()
        base = time.time() + .1
        wall, monotonic = [base], [1000.0]
        with patch('src.isolated_wechat.guest_service.MAX_REQUESTS_SEEN', 1), \
             patch('src.isolated_wechat.guest_service.time.time', side_effect=lambda: wall[0]), \
             patch('src.isolated_wechat.guest_service.time.monotonic', side_effect=lambda: monotonic[0]):
            self.publish(self.payload(service, created=base, deadline=base + 29))
            self.process(service)
            wall[0], monotonic[0] = base + 1, 1036.0
            self.publish(self.payload(service))
            self.process(service)
        self.assertEqual(service.bridge.resumed, 1)


class MirrorHostTests(MirrorCase):
    def host_publications(self, publisher):
        # 原断言仍观测 canonical + mirror 两次逻辑发布；固定槽已独立采用
        # 专用 writer，故障注入夹具须拦截它的新入口，不调用真实 Win32。
        stack = ExitStack()
        stack.enter_context(patch('src.isolated_wechat.host_bridge.write_document',
                                  side_effect=publisher))
        stack.enter_context(patch('src.isolated_wechat.host_bridge.publish_request_mirror',
            side_effect=lambda path, pairing, payload, **bindings: publisher(path, pairing, payload)))
        return stack

    def host_pair(self, *, mirror=True):
        service = self.service()
        service.heartbeat()
        if not mirror:
            status = protocol.live_service(self.pairing)
            status['capabilities'] = ['current_chat_text']
            protocol.write_document(self.channel / 'service.json', self.pairing, status)
        return service, SandboxBridge(self.root / 'host-data', self.channel)

    def test_host_posts_one_original_and_exactly_same_mirror_and_checks_bound_response(self):
        service, host = self.host_pair()
        posts = []
        original = protocol.write_document
        def answer(path, pairing, payload, **kwargs):
            result = original(path, pairing, payload, **kwargs)
            if Path(path).parent.name == 'requests' or Path(path) == self.channel / 'control/request.json':
                posts.append((Path(path), dict(payload), kwargs.get('exclusive', False)))
            if Path(path) == self.channel / 'control/request.json':
                original(self.channel / 'responses' / (payload['request_id'] + '.json'), pairing,
                    {'epoch': service.epoch, 'request_id': payload['request_id'], 'ok': True,
                     'request_digest': self.digest(payload), 'value': {'ok': True, 'value': '已核验'}})
            return result
        with self.host_publications(answer):
            self.assertEqual(host._rpc('display_name', {'text': 'wxid_friend'})['value'], '已核验')
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][1], posts[1][1])
        self.assertTrue(posts[0][2])
        self.assertFalse(posts[1][2])

    def test_wrong_response_digest_is_rejected_even_with_valid_signature_and_identity(self):
        service, host = self.host_pair()
        original = protocol.write_document
        def answer(path, pairing, payload, **kwargs):
            result = original(path, pairing, payload, **kwargs)
            if Path(path).parent.name == 'requests':
                original(self.channel / 'responses' / (payload['request_id'] + '.json'), pairing,
                    {'epoch': service.epoch, 'request_id': payload['request_id'], 'ok': True,
                     'request_digest': 'f' * 64, 'value': {'ok': True, 'value': '错误摘要'}})
            return result
        with self.host_publications(answer), \
             self.assertRaises(RpcError):
            host._rpc('display_name', {'text': 'wxid_friend'})

    def test_legacy_capability_uses_only_original_without_requiring_mirror_digest(self):
        service, host = self.host_pair(mirror=False)
        posts = []
        original = protocol.write_document
        def answer(path, pairing, payload, **kwargs):
            result = original(path, pairing, payload, **kwargs)
            posts.append(Path(path))
            if Path(path).parent.name == 'requests':
                original(self.channel / 'responses' / (payload['request_id'] + '.json'), pairing,
                    {'epoch': service.epoch, 'request_id': payload['request_id'], 'ok': True,
                     'value': {'ok': True}})
            return result
        with self.host_publications(answer):
            self.assertTrue(host._rpc('display_name', {'text': 'wxid_friend'})['ok'])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].parent.name, 'requests')

    def test_slow_publication_cannot_restart_original_monotonic_budget(self):
        _service, host = self.host_pair()
        original = protocol.write_document
        clock = [0.0]
        posts = []
        def slow(path, pairing, payload, **kwargs):
            result = original(path, pairing, payload, **kwargs)
            posts.append(Path(path))
            if Path(path).parent.name == 'requests':
                clock[0] = 2.0  # 发布已超过入口的一秒总预算。
            return result
        with self.host_publications(slow), \
             patch('src.isolated_wechat.host_bridge.time.monotonic', side_effect=lambda: clock[0]), \
             patch('src.isolated_wechat.host_bridge.time.sleep', side_effect=AssertionError('不得重启超时等待')), \
             self.assertRaises(RpcError):
            host._rpc('display_name', {'text': 'wxid_friend'}, timeout=1)
        self.assertEqual(sum(path.parent.name == 'requests' for path in posts), 1)
        self.assertLessEqual(sum(path == self.channel / 'control/request.json' for path in posts), 1)

    def test_response_identity_recheck_cannot_return_success_after_original_deadline(self):
        service, host = self.host_pair()
        original_write = protocol.write_document
        original_service = host._service
        clock = [0.0]
        calls = [0]
        def slow_identity_recheck():
            value = original_service()
            calls[0] += 1
            if calls[0] == 2:
                clock[0] = 2.0
            return value
        def answer(path, pairing, payload, **kwargs):
            result = original_write(path, pairing, payload, **kwargs)
            if Path(path) == self.channel / 'control/request.json':
                original_write(self.channel / 'responses' / (payload['request_id'] + '.json'), pairing,
                    {'epoch': service.epoch, 'request_id': payload['request_id'], 'ok': True,
                     'request_digest': self.digest(payload), 'value': {'ok': True}})
            return result
        with self.host_publications(answer), \
             patch.object(host, '_service', side_effect=slow_identity_recheck), \
             patch('src.isolated_wechat.host_bridge.time.monotonic', side_effect=lambda: clock[0]), \
             self.assertRaises(RpcError):
            host._rpc('display_name', {'text': 'wxid_friend'}, timeout=1)

    def test_waiting_for_publication_lock_cannot_post_after_original_deadline(self):
        _service, host = self.host_pair()
        clock = [0.0]
        class DelayedLock:
            def __enter__(self):
                clock[0] = 2.0
                return self
            def __exit__(self, *_args):
                return False
        host._lock = DelayedLock()
        with patch('src.isolated_wechat.host_bridge.time.monotonic', side_effect=lambda: clock[0]), \
             patch('src.isolated_wechat.host_bridge.write_document') as writer, \
             self.assertRaises(RpcError) as failure:
            host._rpc('display_name', {'text': 'wxid_friend'}, timeout=1)
        self.assertEqual(failure.exception.diagnostic['submission_state'], 'not_submitted')
        writer.assert_not_called()

    def test_host_cleanup_preserves_signed_conflicting_request_and_response(self):
        service, host = self.host_pair()
        original = protocol.write_document
        conflicts = {}
        def replace_during_processing(path, pairing, payload, **kwargs):
            result = original(path, pairing, payload, **kwargs)
            if Path(path) == self.channel / 'control/request.json':
                changed = dict(payload, args={'text': '同一身份的冲突内容'})
                canonical = self.channel / 'requests' / (payload['request_id'] + '.json')
                response = self.channel / 'responses' / (payload['request_id'] + '.json')
                original(canonical, pairing, changed)
                original(response, pairing,
                    {'epoch': service.epoch, 'request_id': payload['request_id'], 'ok': True,
                     'request_digest': self.digest(changed), 'value': {'ok': True}})
                conflicts.update(canonical=canonical, response=response,
                                 canonical_bytes=canonical.read_bytes(), response_bytes=response.read_bytes())
            return result
        with self.host_publications(replace_during_processing), \
             self.assertRaises(RpcError):
            host._rpc('display_name', {'text': 'wxid_friend'}, timeout=1)
        self.assertTrue(conflicts)
        self.assertEqual(conflicts['canonical'].read_bytes(), conflicts['canonical_bytes'])
        self.assertEqual(conflicts['response'].read_bytes(), conflicts['response_bytes'])

    def test_mirror_publication_failure_does_not_repost_possible_send(self):
        _service, host = self.host_pair()
        original = protocol.write_document
        posts = []
        def fail_mirror(path, pairing, payload, **kwargs):
            posts.append(Path(path))
            if Path(path) == self.channel / 'control/request.json':
                raise OSError('offline mirror replacement outcome unknown')
            return original(path, pairing, payload, **kwargs)
        with self.host_publications(fail_mirror), \
             self.assertRaises(RpcError) as failure:
            host._rpc('send_text', {'target': 'wxid_friend', 'text': '假发送'}, timeout=1)
        self.assertEqual(failure.exception.diagnostic['submission_state'], 'uncertain')
        self.assertEqual(sum(path.parent.name == 'requests' for path in posts), 1)
        self.assertEqual(sum(path == self.channel / 'control/request.json' for path in posts), 1)
        self.assertIn(self.channel / 'control/cancel.json', posts)


class RequestDigestTests(MirrorCase):
    def test_digest_is_stable_for_key_order_and_binds_full_verified_payload(self):
        service = self.service()
        value = protocol.verify(self.pairing, protocol.signed(self.pairing, self.payload(service)))
        expected = protocol.request_digest(value)
        self.assertEqual(expected, protocol.request_digest(dict(reversed(list(value.items())))))
        self.assertEqual(len(expected), 64)
        for key, replacement in (('channel_id', 'f' * 32), ('epoch', 'e' * 32),
                                 ('args', {'changed': True}), ('deadline', value['deadline'] + 1),
                                 ('request_id', uuid.uuid4().hex)):
            with self.subTest(key=key):
                self.assertNotEqual(expected, protocol.request_digest(dict(value, **{key: replacement})))


class MirrorReadRecoveryTests(MirrorCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows固定槽短读恢复')
    def test_only_exact_request_slot_recovers_transient_missing_or_sharing_error(self):
        for code in (2, 32):
            with self.subTest(code=code):
                payload = protocol.signed(self.pairing, {'value': '完整签名'})
                with patch.object(protocol, '_read_json', side_effect=[ctypes.WinError(code), payload]) as reader, \
                     patch.object(protocol, 'checked_path', wraps=protocol.checked_path) as checked:
                    result = protocol.read_document(self.channel / 'control/request.json', self.pairing)
                self.assertEqual(result['value'], '完整签名')
                self.assertEqual(reader.call_count, 2)
                self.assertEqual(checked.call_count, 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows固定槽短读恢复')
    def test_immutable_uuid_and_similar_slot_names_never_get_read_retry(self):
        for relative in ('requests/' + uuid.uuid4().hex + '.json',
                         'control/requests.json', 'control/request.json.extra'):
            for code in (2, 32):
                with self.subTest(relative=relative, code=code):
                    with patch.object(protocol, '_read_json', side_effect=ctypes.WinError(code)) as reader, \
                         patch.object(protocol.time, 'sleep', side_effect=AssertionError('immutable cannot wait')):
                        with self.assertRaises(OSError) as failure:
                            protocol.read_document(self.channel / relative, self.pairing)
                    self.assertEqual(failure.exception.winerror, code)
                    reader.assert_called_once()
