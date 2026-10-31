"""外部微信协议与收据离线回归；不联网、不导入或加载第三方 DLL。"""
import hashlib
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.hook_gateway import (CallbackToken, HookJournal, HookJournalError, HookProtocolError,
                              MAX_CALLBACK_BYTES, normalize_callback)
from src.wechat_transports import message_from_raw

ACCOUNT = 'wxid_bound'
ROOM = '12345678@chatroom'


def callback(*, message_id=7000000000000000001, sender='wxid_alice', room='', text='你好', **extra):
    value = {'event_type': 1001, 'type': 1, 'msgid': message_id, 'sender': sender,
             'wxid': sender, 'roomid': room, 'content': text, 'timestamp': 1792902393}
    value.update(extra)
    return value


def native(**kwargs):
    return normalize_callback(callback(**kwargs), account=ACCOUNT, now=1792902400)


class HookParserTests(unittest.TestCase):
    def test_private_native_pipeline_preserves_original_message_and_sender(self):
        raw = native()
        bridge = SimpleNamespace(self_id=ACCOUNT, account_name='机器人', display_name=lambda name, *args: name)
        message = message_from_raw(raw, bridge)
        self.assertEqual(message['target'], 'wxid_alice')
        self.assertEqual(message['sender_id'], 'wxid_alice')
        self.assertEqual(message['local_id'], '7000000000000000001')
        self.assertEqual(message['created'], 1792902393)
        self.assertEqual(message['id'], 'hook-v1:wxid_bound:7000000000000000001')
        self.assertNotIn('server_id', raw)
        self.assertEqual(message['source_event'], '')

    def test_group_members_remain_distinct_without_cross_member_buffering(self):
        alice = native(room=ROOM, text='wxid_alice:\n第一句')
        bob = native(room=ROOM, sender='wxid_bob', message_id=7000000000000000002, text='第二句')
        self.assertEqual([alice['username'], bob['username']], [ROOM, ROOM])
        self.assertEqual([alice['sender_id'], bob['sender_id']], ['wxid_alice', 'wxid_bob'])
        self.assertEqual([alice['content'], bob['content']], ['第一句', '第二句'])
        self.assertNotEqual(alice['local_id'], bob['local_id'])

    def test_only_exact_sender_prefix_is_removed(self):
        value = native(room=ROOM, text='wxid_bob:\n这是转述内容')
        self.assertEqual(value['content'], 'wxid_bob:\n这是转述内容')

    def test_missing_or_non_positive_message_id_never_gets_synthetic_identity(self):
        for value in (None, 0, -1, True, '', 'wxid_123', '1.5', '9' * 25):
            with self.subTest(value=value), self.assertRaises(HookProtocolError):
                native(message_id=value)
        self.assertEqual(native(message_id='0000123')['hook_msgid'], '123')

    def test_missing_timestamp_is_not_replaced_with_current_time(self):
        for value in (None, True, 0, '1792902393', float('nan'), 1792902499):
            with self.subTest(value=value), self.assertRaises(HookProtocolError):
                native(timestamp=value)

    def test_unverified_callback_self_account_must_not_override_actual_binding(self):
        for field in ('self_wxid', 'account_wxid', 'own_wxid', 'self_id'):
            with self.subTest(field=field), self.assertRaisesRegex(HookProtocolError, '账号'):
                native(**{field: 'wxid_other'})
        self.assertEqual(native(self_wxid=ACCOUNT)['_hook_account'], ACCOUNT)

    def test_self_and_outgoing_messages_do_not_become_inbound(self):
        self.assertIsNone(native(sender=ACCOUNT))
        self.assertIsNone(native(is_send=True))

    def test_ambiguous_routes_fail_without_guessing_contact_from_nickname(self):
        for values in ({'sender': '', 'wxid': '', 'roomid': ROOM},
                       {'sender': 'wxid_alice', 'wxid': 'wxid_bob'},
                       {'roomid': '不是群号'}, {'sender': ROOM, 'roomid': ROOM},
                       {'roomid': '', 'sender': ROOM}):
            with self.subTest(values=values), self.assertRaises(HookProtocolError):
                native(**values)

    def test_official_safety_card_reaches_existing_hold_guard_only(self):
        value = native(sender='weixin', type=49, text='<msg>为了你的账号安全，请重新登录</msg>')
        from src.wechat_safety import security_notice
        self.assertTrue(security_notice(value))
        self.assertIsNone(native(sender='wxid_alice', type=49, text='<msg>普通卡片</msg>'))
        self.assertIsNone(native(sender='weixin', type=49, text='<msg>一般通知</msg>'))

    def test_images_unknown_events_and_non_integer_message_types_are_not_invented_as_text(self):
        self.assertIsNone(native(type=3, content='<img arbitrary-path="private"/>'))
        self.assertIsNone(native(type=True))
        self.assertIsNone(native(event_type=1002))

    def test_json_bytes_strict_utf8_duplicates_and_size_are_bounded(self):
        value = callback()
        encoded = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.assertEqual(normalize_callback(encoded, account=ACCOUNT)['username'], 'wxid_alice')
        for raw in (b'\xff', b'{}' * MAX_CALLBACK_BYTES,
                    b'{"event_type":1001,"event_type":1002}', b'[]'):
            with self.subTest(raw=raw[:30]), self.assertRaises(HookProtocolError):
                normalize_callback(raw, account=ACCOUNT)

    def test_body_and_stable_identifier_do_not_accept_invalid_input(self):
        for values in ({'content': ''}, {'content': '\x00abc'}, {'content': '字' * 12001},
                       {'sender': 'wxid_x\nforged'}, {'roomid': ['123@chatroom']}):
            with self.subTest(values=values), self.assertRaises(HookProtocolError):
                native(**values)


class HookCallbackTokenTests(unittest.TestCase):
    def test_loopback_and_exact_unguessable_rotatable_path(self):
        token = CallbackToken()
        self.assertRegex(token.path, r'^/api/wechat/hook/callback/[0-9a-f]{64}$')
        for peer in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
            self.assertTrue(token.accepts(token.path, peer))
        for peer in ('localhost', '192.168.1.2', '0.0.0.0', '::', '::1%abc', '', None):
            self.assertFalse(token.accepts(token.path, peer))
        old = token.path
        self.assertFalse(token.accepts(old + '?other=1', '127.0.0.1'))
        self.assertNotEqual(token.rotate(), old)
        self.assertFalse(token.accepts(old, '127.0.0.1'))
        self.assertTrue(token.accepts(token.path, '127.0.0.1'))

    def test_unicode_path_is_rejected_without_raising_from_constant_time_compare(self):
        self.assertFalse(CallbackToken().accepts('/api/wechat/hook/callback/中文', '127.0.0.1'))


class HookJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'gateway.sqlite3'
        self.journal = HookJournal(self.path, ACCOUNT)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def reopen(self):
        self.journal.close()
        self.journal = HookJournal(self.path, ACCOUNT)

    def test_inbound_survives_restart_is_deduplicated_and_acknowledged(self):
        value = native()
        self.assertTrue(self.journal.record_inbound(value))
        self.reopen()
        self.assertFalse(self.journal.record_inbound(value))
        self.assertEqual(self.journal.pending_inbound(), [value])
        self.journal.ack_inbound(value['hook_msgid'])
        self.reopen()
        self.assertEqual(self.journal.pending_inbound(), [])
        self.assertFalse(self.journal.record_inbound(value))

    def test_conflicting_message_id_does_not_overwrite_original(self):
        original = native()
        self.journal.record_inbound(original)
        changed = dict(original, content='另一个人的内容', sender_id='wxid_bob', sender_username='wxid_bob')
        with self.assertRaisesRegex(HookJournalError, '不同内容'):
            self.journal.record_inbound(changed)
        self.assertEqual(self.journal.pending_inbound(), [original])

    def test_old_target_backlog_does_not_block_new_target_and_security_notices(self):
        for index in range(110):
            self.journal.record_inbound(native(message_id=index + 1))
        selected = native(message_id=111, sender='wxid_selected')
        notice = native(message_id=112, sender='weixin', text='为了你的账号安全，请重新登录')
        self.journal.record_inbound(selected)
        self.journal.record_inbound(notice)
        self.assertEqual(self.journal.pending_inbound(limit=100, target='wxid_selected'), [selected, notice])
        self.assertEqual(len(self.journal.pending_inbound(limit=200)), 112)

    def test_shared_database_does_not_mix_accounts_or_recover_another_accounts_send(self):
        original = native()
        self.journal.record_inbound(original)
        self.journal.begin_send('attempt-1', 'filehelper', '只属于原账号')
        other = HookJournal(self.path, 'wxid_other')
        try:
            self.assertEqual(other.pending_inbound(), [])
            self.assertIsNone(other.get_send('attempt-1'))
            second = normalize_callback(callback(), account='wxid_other', now=1792902400)
            self.assertTrue(other.record_inbound(second))
            with self.assertRaises(HookProtocolError):
                other.record_inbound(original)
            self.assertEqual(self.journal.get_send('attempt-1')['state'], 'started')
        finally:
            other.close()

    def test_full_pending_queue_rolls_back_new_event_and_preserves_existing(self):
        with patch('src.hook_gateway.MAX_PENDING_EVENTS', 1):
            original = native()
            self.assertTrue(self.journal.record_inbound(original))
            with self.assertRaisesRegex(HookJournalError, '已满'):
                self.journal.record_inbound(native(message_id=42))
            self.assertEqual(self.journal.pending_inbound(limit=1), [original])
            self.assertFalse(self.journal.record_inbound(original))
        self.journal.ack_inbound(original['hook_msgid'])
        self.assertTrue(self.journal.record_inbound(native(message_id=42)))

    def test_concurrent_consumers_get_only_one_send_attempt_and_no_plaintext_in_receipt(self):
        barrier = threading.Barrier(8)
        def begin():
            barrier.wait()
            return self.journal.begin_send('same-attempt', 'filehelper', '不该出现在收据的正文')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: begin(), range(8)))
        self.assertEqual(results.count(True), 1)
        self.assertNotIn('不该出现在收据的正文', json.dumps(self.journal.get_send('same-attempt'), ensure_ascii=False))
        for target, text in (('wxid_other', '不该出现在收据的正文'), ('filehelper', '改过的正文')):
            with self.assertRaisesRegex(HookJournalError, '已被其他内容占用'):
                self.journal.begin_send('same-attempt', target, text)

    def test_crashed_inflight_send_is_uncertain_and_never_retried(self):
        self.assertTrue(self.journal.begin_send('attempt-1', 'filehelper', '原文'))
        self.reopen()
        self.assertEqual(self.journal.get_send('attempt-1')['state'], 'uncertain')
        self.assertFalse(self.journal.begin_send('attempt-1', 'filehelper', '原文'))
        with self.assertRaises(HookJournalError):
            self.journal.finish_send('attempt-1', 'submitted')

    def test_interface_ret_zero_is_only_submitted_and_requires_matching_server_evidence(self):
        self.journal.begin_send('attempt-1', 'filehelper', '原文')
        result = self.journal.finish_send('attempt-1', 'submitted')
        self.assertEqual(result['state'], 'submitted')
        self.assertIsNone(result['server_id'])
        receipt = {'account': ACCOUNT, 'target': 'filehelper',
                   'body_sha256': hashlib.sha256('原文'.encode()).hexdigest(),
                   'new_outgoing_record_verified': True, 'server_id': 777}
        for changes in ({'server_id': 0}, {'account': 'wxid_other'}, {'target': 'wxid_other'},
                        {'body_sha256': '0' * 64}, {'new_outgoing_record_verified': False}):
            with self.subTest(changes=changes), self.assertRaises(HookProtocolError):
                self.journal.finish_send('attempt-1', 'confirmed', receipt=dict(receipt, **changes))
        self.assertEqual(self.journal.finish_send('attempt-1', 'confirmed', receipt=receipt)['server_id'], '777')
        self.assertFalse(self.journal.begin_send('attempt-1', 'filehelper', '原文'))

    def test_not_submitted_does_not_enable_replay_or_fabricated_confirmation(self):
        self.journal.begin_send('attempt-1', 'filehelper', '原文')
        self.journal.finish_send('attempt-1', 'not_submitted')
        self.assertFalse(self.journal.begin_send('attempt-1', 'filehelper', '原文'))
        with self.assertRaises(HookJournalError):
            self.journal.finish_send('attempt-1', 'confirmed', receipt={})

    def test_unverified_submission_can_be_downgraded_without_reopening_attempt(self):
        self.journal.begin_send('attempt-1', 'filehelper', '原文')
        self.journal.finish_send('attempt-1', 'submitted')
        self.assertEqual(self.journal.finish_send('attempt-1', 'uncertain')['state'], 'uncertain')
        self.assertFalse(self.journal.begin_send('attempt-1', 'filehelper', '原文'))
        with self.assertRaises(HookJournalError):
            self.journal.finish_send('attempt-1', 'submitted')

    def test_parent_traversal_rejected_and_closed_journal_fails_clearly(self):
        with self.assertRaisesRegex(HookJournalError, '上级目录'):
            HookJournal(Path(self.temp.name) / '..' / 'outside.sqlite3', ACCOUNT)
        self.journal.close()
        with self.assertRaisesRegex(HookJournalError, '已关闭'):
            self.journal.pending_inbound()
