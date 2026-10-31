"""协议读者必须允许Windows发布者原子替换；只运行自有离线文件和FakeNative。"""
import ctypes
import errno
import json
import os
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.isolated_wechat import protocol
from src.isolated_wechat.protocol import ProtocolError, read_document, write_document
from tests.test_isolated_transport1315 import ChannelCase


class ProtocolSharedReaderTests(ChannelCase):
    def test_reader_gets_complete_old_signature_while_writer_publishes_new_document(self):
        target = self.channel / 'service.json'
        write_document(target, self.pairing, {'epoch': 'a' * 32, 'value': '旧签名全文'})
        entered, release = threading.Event(), threading.Event()
        outcomes, errors, streams = [], [], []
        actual = protocol._open_shared_read
        class PausedReader:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def read(self, limit):
                entered.set()
                if not release.wait(2): raise TimeoutError('owned reader was not released')
                return self.stream.read(limit)
        def open_paused(path):
            stream = actual(path);streams.append(stream)
            return PausedReader(stream)
        def read_old():
            try: outcomes.append(read_document(target, self.pairing))
            except BaseException as error: errors.append(error)
        with patch.object(protocol, '_open_shared_read', side_effect=open_paused):
            reader = threading.Thread(target=read_old)
            reader.start()
            try:
                self.assertTrue(entered.wait(2))
                write_document(target, self.pairing, {'epoch': 'b' * 32, 'value': '新签名全文'})
            finally:
                release.set();reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes[0]['value'], '旧签名全文')
        self.assertEqual(outcomes[0]['epoch'], 'a' * 32)
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertEqual(read_document(target, self.pairing)['value'], '新签名全文')

    def test_held_protocol_reader_does_not_stop_live_guest_heartbeat(self):
        service = self.service();service.heartbeat()
        target = self.channel / 'service.json'
        before = read_document(target, self.pairing)['heartbeat']
        with protocol._open_shared_read(target) as reader:
            watcher = threading.Thread(target=service._watch)
            watcher.start()
            try:
                end = time.monotonic() + 2
                while not service._stop.is_set() and time.monotonic() < end:
                    if read_document(target, self.pairing)['heartbeat'] > before:
                        break
                    time.sleep(.01)
                self.assertFalse(service._stop.is_set())
                self.assertGreater(read_document(target, self.pairing)['heartbeat'], before)
                old = json.loads(reader.read().decode('utf-8'))
                self.assertEqual(protocol.verify(self.pairing, old)['heartbeat'], before)
            finally:
                service._stop.set();watcher.join(2)
        self.assertFalse(watcher.is_alive())
        self.assertEqual(service.bridge.cancelled, 0)

    def test_missing_file_preserves_file_not_found_without_retry(self):
        started = time.monotonic()
        with self.assertRaises(FileNotFoundError) as failure:
            read_document(self.channel / 'absent.json', self.pairing)
        self.assertEqual(failure.exception.errno, errno.ENOENT)
        self.assertLess(time.monotonic() - started, 2)

    def test_bounded_and_malformed_reads_close_stream_and_do_not_bypass_signature(self):
        target = self.channel / 'document.json'
        actual = protocol._open_shared_read
        streams = []
        def tracked(path):
            stream = actual(path);streams.append(stream);return stream
        with patch.object(protocol, '_open_shared_read', side_effect=tracked):
            target.write_bytes(b'x' * 100)
            with self.assertRaisesRegex(ProtocolError, '大小限制'):
                read_document(target, self.pairing, 10)
            target.write_bytes(b'{bad-json')
            with self.assertRaisesRegex(ProtocolError, '无法解析'):
                read_document(target, self.pairing)
            write_document(target, self.pairing, {'value': '原文'})
            raw = target.read_bytes().replace('原文'.encode(), '改文'.encode())
            target.write_bytes(raw)
            with self.assertRaisesRegex(ProtocolError, '签名不匹配'):
                read_document(target, self.pairing)
        self.assertEqual(len(streams), 3)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_non_windows_reader_keeps_normal_binary_open(self):
        sentinel = object()
        path = SimpleNamespace(open=lambda mode: sentinel if mode == 'rb' else None)
        with patch.object(protocol, 'os', SimpleNamespace(name='posix')):
            self.assertIs(protocol._open_shared_read(path), sentinel)

    def test_first_publish_and_repeated_replace_keep_complete_signed_documents(self):
        target = self.channel / '首次发布.json'
        self.assertFalse(target.exists())
        for sequence in range(5):
            write_document(target, self.pairing, {'sequence': sequence, 'value': '完整签名文档'})
            self.assertEqual(read_document(target, self.pairing)['sequence'], sequence)
        self.assertEqual(list(self.channel.glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows可变槽只读短重查')
    def test_mutable_slot_rechecks_path_then_validates_signature_after_transient_2_or_32(self):
        for relative in ('service.json', 'control/cancel.json', 'control/stop.json'):
            for code in (2, 32):
                with self.subTest(relative=relative, code=code):
                    target = self.channel / relative
                    payload = protocol.signed(self.pairing, {'value': '完整签名'})
                    with patch.object(protocol, '_read_json', side_effect=[ctypes.WinError(code), payload]) as reader, \
                         patch.object(protocol, 'checked_path', wraps=protocol.checked_path) as checker:
                        self.assertEqual(read_document(target, self.pairing)['value'], '完整签名')
                    self.assertEqual(reader.call_count, 2)
                    self.assertEqual(checker.call_count, 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows可变槽只读短重查')
    def test_missing_or_busy_mutable_slot_exhausts_short_read_budget_with_original_error(self):
        target = self.channel / 'service.json'
        for code in (2, 32):
            with self.subTest(code=code):
                started = time.monotonic()
                with patch.object(protocol, '_read_json', side_effect=ctypes.WinError(code)) as reader:
                    with self.assertRaises(OSError) as error:
                        read_document(target, self.pairing)
                self.assertEqual(error.exception.winerror, code)
                self.assertLess(time.monotonic() - started, .3)
                self.assertGreater(reader.call_count, 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows可变槽只读短重查')
    def test_immutable_files_and_real_access_denial_are_never_rechecked(self):
        for relative, code in (('responses/item.json', 2), ('events/item.json', 32),
                               ('consumed/item.json', 2), ('pairing.json', 2), ('service.json', 5)):
            with self.subTest(relative=relative, code=code):
                with patch.object(protocol, '_read_json', side_effect=ctypes.WinError(code)) as reader, \
                     patch.object(protocol.time, 'sleep', side_effect=AssertionError('must not wait')):
                    with self.assertRaises(OSError) as error:
                        read_document(self.channel / relative, self.pairing)
                self.assertEqual(error.exception.winerror, code)
                reader.assert_called_once()

    def test_success_or_invalid_signature_does_not_call_retry_clock(self):
        target = self.channel / 'service.json'
        write_document(target, self.pairing, {'value': '原始签名'})
        with patch.object(protocol.time, 'monotonic', side_effect=AssertionError('normal reads do not use retry clock')):
            self.assertEqual(read_document(target, self.pairing)['value'], '原始签名')
            target.write_bytes(target.read_bytes().replace('原始'.encode(), '修改'.encode()))
            with self.assertRaisesRegex(ProtocolError, '签名不匹配'):
                read_document(target, self.pairing)

    def test_exclusive_request_and_consumption_do_not_use_replacement_or_overwrite(self):
        target = self.channel / 'consumed' / ('a' * 32 + '.json')
        with patch.object(protocol, '_replace_shared_document', side_effect=AssertionError('exclusive cannot replace')):
            write_document(target, self.pairing, {'request_id': 'a' * 32, 'retry_allowed': False}, exclusive=True)
            with self.assertRaises(FileExistsError):
                write_document(target, self.pairing, {'retry_allowed': True}, exclusive=True)
        self.assertFalse(read_document(target, self.pairing)['retry_allowed'])

    @unittest.skipUnless(os.name == 'nt', '验证Windows失败不会被降级发布隐藏')
    def test_unsupported_atomic_replace_error_is_preserved_with_old_document(self):
        target = self.channel / 'service.json'
        write_document(target, self.pairing, {'value': '原始签名'})
        before = target.read_bytes()
        def unsupported(*args): ctypes.set_last_error(50);return False
        fake = SimpleNamespace(ReplaceFileW=unsupported)
        with patch.object(ctypes, 'WinDLL', return_value=fake), \
             patch.object(Path, 'replace', side_effect=AssertionError('unsupported filesystem cannot fallback')):
            with self.assertRaises(OSError) as failure:
                write_document(target, self.pairing, {'value': '不应提交的新值'})
        self.assertEqual(failure.exception.winerror, 50)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.channel.glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', '验证外部不共享DELETE的句柄继续按真实失败处理')
    def test_external_exclusive_reader_still_causes_failure_and_preserves_old_document(self):
        target = self.channel / 'service.json'
        write_document(target, self.pairing, {'value': '原始签名'})
        before = target.read_bytes()
        with target.open('rb'):
            with self.assertRaises(OSError):
                write_document(target, self.pairing, {'value': '被阻止的新值'})
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.channel.glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', '验证Windows native handle所有权')
    def test_crt_adoption_failure_closes_native_handle(self):
        import msvcrt
        from ctypes import wintypes
        target = self.channel / 'owned.json';target.write_bytes(b'{}')
        actual_dll = ctypes.WinDLL
        kernel = actual_dll('kernel32', use_last_error=True)
        native_create = kernel.CreateFileW
        native_create.argtypes = (wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                  wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
        native_create.restype = wintypes.HANDLE
        native_close = kernel.CloseHandle
        native_close.argtypes = (wintypes.HANDLE,);native_close.restype = wintypes.BOOL
        handles = []
        def create(*args):
            handle = native_create(*args);handles.append(handle);return handle
        fake = SimpleNamespace(CreateFileW=create, CloseHandle=lambda h: native_close(h))
        with patch.object(ctypes, 'WinDLL', return_value=fake), \
             patch.object(msvcrt, 'open_osfhandle', side_effect=OSError('owned fixture cannot adopt handle')):
            with self.assertRaisesRegex(OSError, 'cannot adopt'):
                protocol._open_shared_read(target)
        self.assertEqual(len(handles), 1)
        info = wintypes.DWORD()
        kernel.GetHandleInformation.argtypes = (wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD))
        self.assertFalse(kernel.GetHandleInformation(handles[0], ctypes.byref(info)))
        self.assertEqual(ctypes.get_last_error(), 6)

    @unittest.skipUnless(os.name == 'nt', '验证Windows CRT descriptor所有权')
    def test_fdopen_failure_closes_adopted_descriptor(self):
        import msvcrt
        target = self.channel / 'owned.json';target.write_bytes(b'{}')
        actual = msvcrt.open_osfhandle
        descriptors = []
        def adopt(*args):
            descriptor = actual(*args);descriptors.append(descriptor);return descriptor
        with patch.object(msvcrt, 'open_osfhandle', side_effect=adopt), \
             patch.object(os, 'fdopen', side_effect=OSError('owned fixture cannot create stream')):
            with self.assertRaisesRegex(OSError, 'cannot create stream'):
                protocol._open_shared_read(target)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError) as failure: os.fstat(descriptors[0])
        self.assertEqual(failure.exception.errno, errno.EBADF)


if __name__ == '__main__':
    unittest.main()
