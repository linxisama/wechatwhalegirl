"""请求固定槽故障注入；仅临时文件和替身，不访问沙盒或微信。"""
import ctypes
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.isolated_wechat import request_slots as slots
from src.isolated_wechat.protocol import Pairing, ProtocolError, request_digest, signed, verify


class RequestMirrorPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'control').mkdir()
        self.path = self.root / 'control/request.json'
        self.path.write_bytes(b'pending')
        self.pair = Pairing({'version': 1, 'channel_id': 'a' * 32, 'secret': 'b' * 64}, self.root)
        self.payload = {'version': 1, 'channel_id': 'a' * 32, 'epoch': 'c' * 32,
                        'request_id': 'd' * 32, 'action': 'send_text',
                        'args': {'target': 'filehelper', 'text': '离线替身'},
                        'created': 999., 'deadline': 1005.}
        self.clock = SimpleNamespace(monotonic=lambda: 20., time=lambda: 1000.)
        self.patchers = [patch.object(slots, 'time', self.clock),
                         patch.object(slots, 'os', SimpleNamespace(name='nt')),
                         patch.object(slots, '_write_existing')]
        self.writer = [p.start() for p in self.patchers][-1]
        for p in self.patchers:
            self.addCleanup(p.stop)

    def publish(self, path=None, payload=None, **changes):
        payload = self.payload if payload is None else payload
        args = dict(epoch='c' * 32, digest=request_digest(self.payload), deadline=24.)
        args.update(changes)
        return slots.publish_request_mirror(self.path if path is None else path,
                                           self.pair, payload, **args)

    def test_fixed_slot_keeps_exact_signed_payload_and_single_write(self):
        self.assertEqual(self.publish(), self.path)
        self.writer.assert_called_once()
        args, kwargs = self.writer.call_args
        self.assertEqual(args[0], self.path)
        self.assertEqual(args[1], slots._bytes(signed(self.pair, self.payload)))
        self.assertEqual(verify(self.pair, json.loads(args[1])), self.payload)
        self.assertAlmostEqual(kwargs['deadline'], 20.05)
        self.assertEqual(self.path.read_bytes(), b'pending')

    def test_original_monotonic_budget_caps_acquisition_without_reset(self):
        self.publish(deadline=20.012)
        self.assertAlmostEqual(self.writer.call_args.kwargs['deadline'], 20.012)

    def test_original_signed_wall_deadline_caps_acquisition(self):
        self.payload['deadline'] = 1000.008
        self.publish()
        self.assertAlmostEqual(self.writer.call_args.kwargs['deadline'], 20.008)

    def test_validation_and_serialization_time_still_use_original_deadline(self):
        monotonic = iter((20.049,))
        self.clock.monotonic = lambda: next(monotonic)
        self.publish(deadline=20.05)
        self.assertAlmostEqual(self.writer.call_args.kwargs['deadline'], 20.05)

    def test_expired_original_monotonic_budget_never_writes(self):
        for deadline in (19.99, 20.):
            with self.subTest(deadline=deadline), self.assertRaises(TimeoutError):
                self.publish(deadline=deadline)
        self.writer.assert_not_called()

    def test_expired_signed_wall_deadline_never_writes(self):
        for deadline in (999.9, 1000.):
            payload = dict(self.payload, deadline=deadline)
            with self.subTest(deadline=deadline), self.assertRaises((ProtocolError, TimeoutError)):
                self.publish(payload=payload, digest=request_digest(payload))
        self.writer.assert_not_called()

    def test_wall_deadline_expiring_during_validation_never_writes(self):
        ticks = iter((1000., 1006.))
        self.clock.time = lambda: next(ticks)
        with self.assertRaises(TimeoutError):
            self.publish()
        self.writer.assert_not_called()

    def test_bad_epoch_request_id_action_and_args_never_write(self):
        for change in ({'epoch': 'e' * 32}, {'request_id': 'D' * 32},
                       {'action': 'execute_code'}, {'args': []}):
            payload = dict(self.payload, **change)
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                self.publish(payload=payload, digest=request_digest(payload))
        self.writer.assert_not_called()

    def test_changed_body_uuid_or_deadline_cannot_replace_original_digest(self):
        for change in ({'args': {'target': 'other', 'text': 'changed'}},
                       {'request_id': 'e' * 32}, {'deadline': 1006.}):
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                self.publish(payload=dict(self.payload, **change))
        self.writer.assert_not_called()

    def test_mismatched_pairing_and_unsigned_version_rejected(self):
        for change in ({'channel_id': 'e' * 32}, {'version': 2}, {'signature': 'f' * 64}):
            payload = dict(self.payload, **change)
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                self.publish(payload=payload, digest=request_digest(payload))
        self.writer.assert_not_called()

    def test_non_finite_boolean_or_invalid_captured_bindings_never_write(self):
        for changes in ({'deadline': True}, {'deadline': float('nan')},
                        {'deadline': float('inf')}, {'epoch': 'bad'},
                        {'digest': 'F' * 64}, {'digest': ''}, {'digest': None}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                self.publish(**changes)
        self.writer.assert_not_called()

    def test_only_exact_fixed_path_allowed_without_generic_writer_fallback(self):
        for relative in ('control/requests.json', 'control/request.json.extra',
                         'requests/' + 'd' * 32 + '.json', 'ocr/relay.json',
                         'control/../control/request.json'):
            with self.subTest(relative=relative), self.assertRaises(ProtocolError):
                self.publish(path=self.root / relative)
        self.writer.assert_not_called()

    def test_outside_pairing_rejected(self):
        with self.assertRaises(ProtocolError):
            self.publish(path=self.root.parent / 'request.json')
        self.writer.assert_not_called()

    def test_reparse_or_symlink_rejected_before_write(self):
        original = Path.lstat
        def linked(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.path:
                return SimpleNamespace(st_file_attributes=0x400, st_mode=info.st_mode)
            return info
        with patch.object(Path, 'lstat', linked), self.assertRaises(ProtocolError):
            self.publish()
        self.writer.assert_not_called()

    def test_missing_slot_is_not_created_or_fallen_back(self):
        self.path.unlink()
        self.writer.side_effect = FileNotFoundError('missing fixed slot')
        with self.assertRaises(FileNotFoundError):
            self.publish()
        self.writer.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_write_or_flush_or_close_error_propagates_without_second_write(self):
        for error in (ctypes.WinError(2), ctypes.WinError(5), OSError('partial write'),
                      OSError('flush failed'), OSError('close failed'), TimeoutError('late flush')):
            self.writer.reset_mock()
            self.writer.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                self.publish()
            self.writer.assert_called_once()
            self.assertEqual(self.path.read_bytes(), b'pending')

    def test_oversize_rejected_before_any_write(self):
        with patch.object(slots, 'MAX_DOCUMENT_BYTES', 32), self.assertRaises(ProtocolError):
            self.publish()
        self.writer.assert_not_called()

    def test_non_windows_fails_closed_without_recreating_or_replacing_slot(self):
        with patch.object(slots, 'os', SimpleNamespace(name='posix')), self.assertRaises(OSError):
            self.publish()
        self.writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'pending')


from src.isolated_wechat import host_bridge, protocol
from src.isolated_wechat.host_bridge import RpcError, SandboxBridge
from tests.test_isolated_transport1315 import ChannelCase


class RequestMirrorHostIntegrationTests(ChannelCase):
    def host_pair(self):
        service = self.service()
        service.heartbeat()
        host = SandboxBridge(self.root / 'host-data', self.channel)
        return service, host

    def test_host_canonical_precedes_mirror_with_identical_bytes_and_bindings(self):
        service, host = self.host_pair()
        publications = []
        original_publish = slots.publish_request_mirror
        def observe(path, pairing, payload, **bindings):
            canonical = pairing.root / 'requests' / (payload['request_id'] + '.json')
            self.assertTrue(canonical.is_file())
            publications.append((canonical.read_bytes(), dict(payload), dict(bindings)))
            return original_publish(path, pairing, payload, **bindings)
        def write_once(path, raw, *, deadline):
            self.assertEqual(raw, publications[0][0])
            Path(path).write_bytes(raw)  # 仅临时测试文件；生产使用互斥句柄。
            value = protocol.verify(self.pairing, json.loads(raw))
            protocol.write_document(self.channel / 'responses' / (value['request_id'] + '.json'),
                self.pairing, {'epoch': service.epoch, 'request_id': value['request_id'],
                'request_digest': request_digest(value), 'ok': True, 'value': {'ok': True}})
        with patch.object(host_bridge, 'publish_request_mirror', side_effect=observe) as publisher, \
             patch.object(slots, '_write_existing', side_effect=write_once) as writer:
            self.assertTrue(host._rpc('display_name', {'text': 'wxid_friend'})['ok'])
        publisher.assert_called_once()
        writer.assert_called_once()
        payload, bindings = publications[0][1:]
        self.assertEqual(bindings['epoch'], payload['epoch'])
        self.assertEqual(bindings['digest'], request_digest(payload))
        self.assertLessEqual(writer.call_args.kwargs['deadline'], bindings['deadline'])
        self.assertEqual(list((self.channel / 'requests').glob('*.json')), [])

    def test_partial_mirror_failure_cancels_once_and_never_reposts_send(self):
        _service, host = self.host_pair()
        writes = []
        original = protocol.write_document
        def record(path, pairing, payload, **kwargs):
            writes.append(Path(path))
            return original(path, pairing, payload, **kwargs)
        with patch.object(host_bridge, 'write_document', side_effect=record), \
             patch.object(slots, '_write_existing', side_effect=OSError('partial flush')) as writer, \
             self.assertRaises(RpcError) as failure:
            host._rpc('send_text', {'target': 'filehelper', 'text': '离线替身'}, timeout=1)
        writer.assert_called_once()
        self.assertEqual(failure.exception.diagnostic['submission_state'], 'uncertain')
        self.assertEqual(sum(path.parent.name == 'requests' for path in writes), 1)
        self.assertEqual(writes.count(self.channel / 'control/cancel.json'), 1)
        self.assertEqual(list((self.channel / 'requests').glob('*.json')), [])

    def test_changed_payload_after_canonical_is_rejected_without_mirror_write(self):
        _service, host = self.host_pair()
        original = protocol.write_document
        writes = []
        def mutate(path, pairing, payload, **kwargs):
            writes.append(Path(path))
            result = original(path, pairing, payload, **kwargs)
            if Path(path).parent.name == 'requests':
                payload['args']['text'] = '改变的离线替身'
            return result
        with patch.object(host_bridge, 'write_document', side_effect=mutate), \
             patch.object(slots, '_write_existing') as writer, self.assertRaises(RpcError) as failure:
            host._rpc('send_text', {'target': 'filehelper', 'text': '离线替身'}, timeout=1)
        writer.assert_not_called()
        self.assertEqual(failure.exception.diagnostic['submission_state'], 'uncertain')
        self.assertEqual(sum(path.parent.name == 'requests' for path in writes), 1)
        self.assertEqual(writes.count(self.channel / 'control/cancel.json'), 1)

    def test_host_original_deadline_includes_canonical_publication_cost(self):
        service, host = self.host_pair()
        clock = [20.]
        original = protocol.write_document
        bounds = []
        def canonical(path, pairing, payload, **kwargs):
            result = original(path, pairing, payload, **kwargs)
            if Path(path).parent.name == 'requests':
                clock[0] = 20.98  # 入口1秒预算只剩20ms，不能另起50ms。
            return result
        def write_once(path, raw, *, deadline):
            bounds.append(deadline)
            value = protocol.verify(self.pairing, json.loads(raw))
            original(self.channel / 'responses' / (value['request_id'] + '.json'), self.pairing,
                     {'epoch': service.epoch, 'request_id': value['request_id'],
                      'request_digest': request_digest(value), 'ok': True, 'value': {'ok': True}})
        with patch.object(host_bridge, 'write_document', side_effect=canonical), \
             patch.object(host_bridge.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(slots, '_write_existing', side_effect=write_once):
            self.assertTrue(host._rpc('display_name', {'text': 'wxid_friend'}, timeout=1)['ok'])
        self.assertEqual(bounds, [21.])
