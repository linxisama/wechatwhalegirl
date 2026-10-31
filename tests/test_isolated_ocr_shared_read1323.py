"""OCR 可变槽发布期间的只读恢复；不访问桌面，不重投请求。"""
import ctypes
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.isolated_wechat import ocr_relay, protocol
from src.runtime_identity import APP_VERSION


@unittest.skipUnless(os.name == 'nt', 'Windows 共享映射的短暂替换错误')
class OCRSharedSlotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / 'pairing.json').write_text(json.dumps({
            'version': 1, 'channel_id': uuid.uuid4().hex, 'secret': 'ab' * 32}), 'utf-8')
        self.pairing = protocol.load_pairing(self.root)
        (self.root / 'ocr').mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_only_exact_ocr_heartbeat_and_uuid_result_recover_transient_replace_errors(self):
        for relative in ('ocr/relay.json', 'ocr/' + 'a' * 32 + '.result.json'):
            for code in (2, 32):
                with self.subTest(relative=relative, winerror=code):
                    envelope = protocol.signed(self.pairing, {'verified': True})
                    with patch.object(protocol, '_read_json', side_effect=[ctypes.WinError(code), envelope]) as read, \
                         patch.object(protocol, 'checked_path', wraps=protocol.checked_path) as checked:
                        self.assertTrue(protocol.read_document(self.root / relative, self.pairing)['verified'])
                    self.assertEqual(read.call_count, 2)
                    self.assertEqual(checked.call_count, 2)

    def test_other_ocr_names_and_immutable_documents_fail_without_wait(self):
        names = ('ocr/relay.json.backup', 'relay.json', 'ocr/wrong.result.json',
                 'ocr/' + 'A' * 32 + '.result.json', 'ocr/' + 'a' * 31 + '.result.json',
                 'ocr/' + 'a' * 32 + '.request.json', 'ocr/' + 'a' * 32 + '.bgra',
                 'responses/' + 'a' * 32 + '.result.json', 'events/' + 'a' * 32 + '.json',
                 'consumed/' + 'a' * 32 + '.json', 'pairing.json')
        for name in names:
            with self.subTest(name=name), \
                 patch.object(protocol, '_read_json', side_effect=ctypes.WinError(32)) as read, \
                 patch.object(protocol.time, 'sleep', side_effect=AssertionError('unexpected retry')):
                with self.assertRaises(OSError):
                    protocol.read_document(self.root / name, self.pairing)
                read.assert_called_once()

    def test_real_access_denial_or_other_io_errors_are_not_retried_for_ocr(self):
        for code in (3, 5, 50):
            with self.subTest(winerror=code), \
                 patch.object(protocol, '_read_json', side_effect=ctypes.WinError(code)) as read, \
                 patch.object(protocol.time, 'sleep', side_effect=AssertionError('unexpected retry')):
                with self.assertRaises(OSError):
                    protocol.read_document(self.root / 'ocr/relay.json', self.pairing)
                read.assert_called_once()

    def test_transient_recovery_still_rejects_invalid_signature(self):
        forged = protocol.signed(self.pairing, {'value': 'original'})
        forged['value'] = 'changed'
        with patch.object(protocol, '_read_json', side_effect=[ctypes.WinError(32), forged]) as read:
            with self.assertRaisesRegex(protocol.ProtocolError, '签名不匹配'):
                protocol.read_document(self.root / 'ocr/relay.json', self.pairing)
        self.assertEqual(read.call_count, 2)

    def test_parse_error_is_not_retried(self):
        with patch.object(protocol, '_read_json', side_effect=protocol.ProtocolError('无法解析')) as read, \
             patch.object(protocol.time, 'sleep', side_effect=AssertionError('unexpected retry')):
            with self.assertRaisesRegex(protocol.ProtocolError, '无法解析'):
                protocol.read_document(self.root / 'ocr/relay.json', self.pairing)
        read.assert_called_once()

    def test_expired_caller_deadline_does_not_start_a_retry(self):
        with patch.object(protocol, '_read_json', side_effect=ctypes.WinError(32)) as read, \
             patch.object(protocol.time, 'monotonic', return_value=100), \
             patch.object(protocol.time, 'sleep', side_effect=AssertionError('expired read cannot wait')):
            with self.assertRaises(OSError):
                protocol.read_document(self.root / 'ocr/relay.json', self.pairing, retry_deadline=100)
        read.assert_called_once()

    def test_caller_deadline_caps_the_short_recovery_budget(self):
        clock = [100.0]
        sleeps = []
        def nap(delay):
            sleeps.append(delay)
            clock[0] += delay
        with patch.object(protocol, '_read_json', side_effect=ctypes.WinError(2)) as read, \
             patch.object(protocol.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(protocol.time, 'sleep', side_effect=nap):
            with self.assertRaises(OSError):
                protocol.read_document(self.root / 'ocr/relay.json', self.pairing, retry_deadline=100.007)
        self.assertAlmostEqual(sum(sleeps), .007)
        self.assertEqual(read.call_count, 2)

    def test_later_caller_deadline_cannot_expand_fifty_millisecond_read_limit(self):
        clock = [100.0]
        def nap(delay):
            clock[0] += delay
        with patch.object(protocol, '_read_json', side_effect=ctypes.WinError(32)), \
             patch.object(protocol.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(protocol.time, 'sleep', side_effect=nap):
            with self.assertRaises(OSError):
                protocol.read_document(self.root / 'ocr/relay.json', self.pairing, retry_deadline=999)
        self.assertAlmostEqual(clock[0] - 100, .05)

    def test_invalid_caller_deadline_is_rejected_before_read(self):
        for deadline in (True, '10', float('nan'), float('inf')):
            with self.subTest(deadline=deadline), patch.object(protocol, '_read_json') as read:
                with self.assertRaises(protocol.ProtocolError):
                    protocol.read_document(self.root / 'ocr/relay.json', self.pairing, retry_deadline=deadline)
                read.assert_not_called()

    def test_guest_recovers_pending_result_without_second_ocr_request(self):
        epoch = uuid.uuid4().hex
        protocol.write_document(self.root / 'service.json', self.pairing, {
            'epoch': epoch, 'active': True, 'heartbeat': time.time(), 'app_version': APP_VERSION})
        protocol.write_document(self.root / 'ocr/relay.json', self.pairing, {
            'epoch': epoch, 'available': True, 'heartbeat': time.time(), 'app_version': APP_VERSION})
        guest = ocr_relay.GuestOCR(self.root, _guest_check=False)
        actual_read, actual_write = protocol._read_json, ocr_relay.write_document
        failed, requests, read_limits, result_reads = [], [], [], []
        def read(path, limit):
            if Path(path).name.endswith('.result.json'):
                result_reads.append(path)
                if not failed:
                    failed.append(path)
                    raise ctypes.WinError(32)
                if len(result_reads) == 2:
                    return protocol.signed(self.pairing, {'epoch': epoch,
                        'request_id': Path(path).name.removesuffix('.result.json'), 'pending': True})
            return actual_read(path, limit)
        def publish(path, pairing, payload, exclusive=False):
            result = actual_write(path, pairing, payload, exclusive=exclusive)
            if Path(path).name.endswith('.request.json'):
                requests.append(dict(payload))
                reply = Path(path).with_name(Path(path).name.removesuffix('.request.json') + '.result.json')
                actual_write(reply, pairing, {'epoch': epoch, 'request_id': payload['request_id'],
                                             'ok': True, 'rows': [['文件传输助手', 1, 1, 8, 8]]})
            return result
        actual_read_document = ocr_relay.read_document
        def inspect_deadline(path, pairing, max_bytes=protocol.MAX_DOCUMENT_BYTES, **kwargs):
            if Path(path).name.endswith('.result.json'):
                read_limits.append(kwargs.get('retry_deadline'))
            return actual_read_document(path, pairing, max_bytes, **kwargs)
        with patch.object(protocol, '_read_json', side_effect=read), \
             patch.object(ocr_relay, 'write_document', side_effect=publish), \
             patch.object(ocr_relay, 'read_document', side_effect=inspect_deadline):
            with Image.new('RGB', (20, 20), 'white') as frame:
                self.assertEqual(guest.recognize(frame), [('文件传输助手', 1, 1, 8, 8)])
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(failed), 1)
        self.assertTrue(read_limits)
        self.assertTrue(all(type(limit) in (int, float) for limit in read_limits))
        self.assertEqual(len(read_limits), 2)
        self.assertEqual(len(set(read_limits)), 1)
        self.assertLessEqual(max(read_limits), time.monotonic() + 6)
        self.assertFalse(list((self.root / 'ocr').glob('*.request.json')))
        self.assertFalse(list((self.root / 'ocr').glob('*.result.json')))
        self.assertFalse(list((self.root / 'ocr').glob('*.bgra')))
