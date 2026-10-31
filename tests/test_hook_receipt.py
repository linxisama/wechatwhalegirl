"""回执判断使用假快照，完全不读取真实微信或进程内存。"""
import copy
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hook_receipt import NativeHookReceipt, ReceiptUnavailable, _committed_wal


ACCOUNT = 'wxid_receipt_owner'


def row(local=1, seq=10, server=100, sender=ACCOUNT, text='旧正文'):
    return {'shard': 'message__message_0.db', 'local_id': local, 'sort_seq': seq,
            'server_id': server, 'sender': sender, 'text': text, 'type': 1}


class Provider:
    def __init__(self):
        self.rows = [row()]
        self.fresh = True
        self.calls = []
        self.closed = False

    def snapshot(self, target, watermark):
        self.calls.append((target, watermark))
        return {'fresh': self.fresh, 'rows': copy.deepcopy(self.rows)}

    def close(self):
        self.closed = True


class HookReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / (ACCOUNT + '_abcd')
        self.path.mkdir()
        self.identity = {'verified': True, 'account': ACCOUNT, 'account_path': str(self.path),
                         'pid': 123, 'create_time': 1234.5}
        self.provider = Provider()
        self.receipt = NativeHookReceipt(self.path, ACCOUNT, lambda: dict(self.identity),
            db_factory=lambda _owner: self.provider, work_dir=Path(self.temp.name) / 'cache', poll_seconds=.25)

    def tearDown(self):
        self.receipt.close()
        self.temp.cleanup()

    def prepare(self, text='本轮正文'):
        return self.receipt.prepare(ACCOUNT, 'filehelper', text)

    def test_unique_new_self_server_record_confirms_with_exact_metadata(self):
        token = self.prepare()
        self.provider.rows.append(row(local=2, seq=11, server=222, text='本轮正文'))
        result = self.receipt.verify(token)
        self.assertTrue(result['verified'])
        self.assertTrue(result['account_matches'])
        self.assertEqual((result['new_self_rows'], result['server_id']), (1, 222))
        self.assertEqual(result['target'], 'filehelper')
        self.assertEqual(result['body_sha256'], hashlib.sha256('本轮正文'.encode()).hexdigest())
        self.assertFalse(result['recipient_read_verified'])
        self.assertNotIn('本轮正文', repr(result))

    def test_existing_row_server_id_update_does_not_count_as_new_send(self):
        self.provider.rows = [row(server=0, text='本轮正文')]
        token = self.prepare()
        self.provider.rows[0]['server_id'] = 333
        self.assertFalse(self.receipt.verify(token)['verified'])

    def test_incoming_and_wrong_body_or_wrong_type_do_not_confirm(self):
        for changed in (row(local=2, seq=11, sender='wxid_other', text='本轮正文'),
                        row(local=3, seq=12, text='本轮正文 '),
                        dict(row(local=4, seq=13, text='本轮正文'), type=3)):
            with self.subTest(changed=changed['local_id']):
                self.provider.rows = [row()]
                token = self.prepare()
                self.provider.rows.append(changed)
                self.assertFalse(self.receipt.verify(token)['verified'])

    def test_duplicate_exact_self_rows_are_uncertain(self):
        token = self.prepare()
        self.provider.rows += [row(local=2, seq=11, text='本轮正文'), row(local=3, seq=12, text='本轮正文')]
        result = self.receipt.verify(token)
        self.assertFalse(result['verified'])
        self.assertEqual(result['new_self_rows'], 2)

    def test_nonzero_server_id_is_required(self):
        token = self.prepare()
        self.provider.rows.append(row(local=2, seq=11, server=0, text='本轮正文'))
        self.assertFalse(self.receipt.verify(token)['verified'])

    def test_changed_process_or_account_refuses_before_db_and_after_send(self):
        token = self.prepare()
        count = len(self.provider.calls)
        self.identity['create_time'] += 1
        self.assertFalse(self.receipt.verify(token)['verified'])
        self.assertEqual(len(self.provider.calls), count)
        with self.assertRaises(ReceiptUnavailable):
            self.prepare()

    def test_closed_receipt_rebinds_on_next_connection_without_reusing_old_baseline(self):
        old = self.prepare()
        self.receipt.close()
        self.identity['create_time'] += 1
        self.assertFalse(self.receipt.verify(old)['verified'])
        token = self.prepare()
        self.provider.rows.append(row(local=2, seq=11, text='本轮正文'))
        self.assertTrue(self.receipt.verify(token)['verified'])

    def test_unverified_identity_and_stale_snapshot_refuse_prepare(self):
        self.identity['verified'] = False
        with self.assertRaises(ReceiptUnavailable):
            self.prepare()
        self.identity['verified'] = True
        self.provider.fresh = False
        with self.assertRaises(ReceiptUnavailable):
            self.prepare()

    def test_tokens_single_use_and_expire(self):
        token = self.prepare()
        self.receipt._tokens[token['id']]['created'] = time.monotonic() - 31
        self.assertFalse(self.receipt.verify(token)['verified'])
        self.assertFalse(self.receipt.verify(token)['verified'])

    def test_new_row_at_same_sort_sequence_is_detected_by_row_identity(self):
        token = self.prepare()
        self.provider.rows.append(row(local=2, seq=10, text='本轮正文'))
        self.assertTrue(self.receipt.verify(token)['verified'])

    def test_only_line_endings_are_normalized(self):
        token = self.prepare('第一行\r\n第二行')
        self.provider.rows.append(row(local=2, seq=11, text='第一行\n第二行'))
        self.assertTrue(self.receipt.verify(token)['verified'])

    def test_prepare_requires_two_stable_snapshots(self):
        original = self.provider.snapshot
        def changing(*args):
            result = original(*args)
            self.provider.rows.append(row(local=10, seq=20))
            return result
        with patch.object(self.provider, 'snapshot', changing):
            with self.assertRaisesRegex(ReceiptUnavailable, '稳定'):
                self.prepare()

    def test_truncated_baseline_does_not_mistake_old_same_sequence_row_for_new_send(self):
        old = [row(local=i, seq=10) for i in range(1, 101)]
        hidden = row(local=101, seq=10, text='本轮正文')
        self.provider.rows = old + [hidden]
        def truncated(target, watermark):
            return {'fresh': True, 'rows': copy.deepcopy(old if watermark is None else self.provider.rows)}
        with patch.object(self.provider, 'snapshot', truncated):
            token = self.prepare()
            self.assertFalse(self.receipt.verify(token)['verified'])

    def test_cache_cannot_be_inside_wechat_account(self):
        with self.assertRaises(ReceiptUnavailable):
            NativeHookReceipt(self.path, ACCOUNT, lambda: self.identity, work_dir=self.path / 'cache')


class WALBoundaryTests(unittest.TestCase):
    def test_rejects_partial_uncommitted_and_old_generation_without_merging(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'message.db-wal'
            header = (0x377F0682).to_bytes(4, 'big') + bytes(4) + (4096).to_bytes(4, 'big') + bytes(4) + b'abcdefgh' + bytes(8)
            frame = (1).to_bytes(4, 'big') + (1).to_bytes(4, 'big') + b'abcdefgh' + bytes(8) + bytes(4096)
            path.write_bytes(header + frame)
            _committed_wal(path)
            self.assertEqual(path.read_bytes(), header + frame)
            for raw in (header + frame[:-1], header + frame[:4] + bytes(4) + frame[8:],
                        header + frame[:8] + b'old_salt' + frame[16:]):
                path.write_bytes(raw)
                with self.assertRaises(ReceiptUnavailable):
                    _committed_wal(path)
