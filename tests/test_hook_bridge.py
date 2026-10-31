"""协议桥只使用 httpx 假传输、假进程证据和假回执，不碰真实微信。"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from src.hook_bridge import (HardenedHookBridge, HOOK_VERSION, KNOWN_DLL_SHA256,
                             MAX_RESPONSE_BYTES, HookUnavailable, loopback_url)


ACCOUNT = 'wxid_robot'
TARGET = 'filehelper'
BODY = '只在假传输中发送'
APPROVED_TEST_HASH = 'b' * 64


class Journal:
    def __init__(self):
        self.sends = {}
        self.inbound = set()
        self.acks = []

    def begin_send(self, request_id, target, text):
        if request_id in self.sends:
            return False
        self.sends[request_id] = ['started']
        return True

    def finish_send(self, request_id, state, **kwargs):
        self.sends[request_id].append(state)

    def record_inbound(self, raw):
        key = raw['hook_msgid']
        if key in self.inbound:
            return False
        self.inbound.add(key)
        return True

    def ack_inbound(self, message_id):
        self.acks.append(message_id)


class Receipt:
    def __init__(self, **overrides):
        self.overrides = overrides
        self.prepares = []

    def prepare(self, account, target, text):
        self.prepares.append((account, target, text))
        return {'account': account, 'target': target, 'body_sha256': hashlib.sha256(text.encode()).hexdigest()}

    def verify(self, baseline):
        return dict(baseline, verified=True, account_matches=True, new_self_rows=1, server_id=123456, **self.overrides)


class HookBridgeTests(unittest.TestCase):
    def setUp(self):
        approval = patch('src.hook_identity.APPROVED_PROVIDER_SHA256', frozenset({APPROVED_TEST_HASH}))
        approval.start()
        self.addCleanup(approval.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.requests = []
        self.responses = {}
        self.account_path = str((self.root / 'account-data').absolute())
        self.settings = {'enabled': True, 'api_url': 'http://127.0.0.1:30001',
            'account': ACCOUNT, 'target': TARGET, 'account_path': self.account_path,
            'callback_url': 'http://127.0.0.1:8091/api/wechat/hook/callback/' + 'a'*64}
        self.snapshot = {'verified': True, 'pid': 50, 'create_time': 100., 'account': ACCOUNT,
            'account_path': self.account_path, 'version': HOOK_VERSION,
            'dll_sha256': APPROVED_TEST_HASH, 'loopback_listener': True}
        self.probe = Mock(side_effect=lambda *args: dict(self.snapshot))
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle), trust_env=False)
        self.journal = Journal()
        self.receipt = Receipt()
        self.bridge = self.make_bridge()

    def tearDown(self):
        self.bridge.disconnect()
        self.client.close()
        self.temp.cleanup()

    def handle(self, request):
        self.requests.append((request.url.path, json.loads(request.content)))
        custom = self.responses.get(request.url.path)
        if callable(custom):
            return custom(request)
        if isinstance(custom, httpx.Response):
            return custom
        if request.url.path == '/GetSelfProfile':
            return httpx.Response(200, json=custom or {'wxid': ACCOUNT})
        if request.url.path == '/set_callback':
            return httpx.Response(200, json=custom or {'ret': 0, 'callback': self.settings['callback_url']})
        return httpx.Response(200, json=custom or {'ret': 0})

    def make_bridge(self, **kwargs):
        values = dict(data_dir=self.root / 'data/wechat-only', settings=self.settings,
            client=self.client, identity_probe=self.probe, receipt_verifier=self.receipt,
            journal=self.journal)
        values.update(kwargs)
        return HardenedHookBridge(**values)

    def ready(self):
        self.assertTrue(self.bridge.connect()[0])
        self.bridge.resume_sends()

    def send_requests(self):
        return [row for row in self.requests if row[0] == '/SendTextMsg']

    def test_default_off_never_probes_or_touches_network(self):
        disabled = self.make_bridge(settings={})
        self.assertFalse(disabled.connect()[0])
        self.probe.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_url_rejects_remote_dns_credentials_redirect_paths_and_query(self):
        for value in ('http://localhost:30001', 'https://127.0.0.1:30001',
                      'http://example.com:30001', 'http://127.0.0.1:30001/x',
                      'http://user:pass@127.0.0.1:30001', 'http://127.0.0.1:30001?a=b',
                      'http://127.0.0.1', 'http://127.0.0.1:30001#x'):
            with self.subTest(value=value), self.assertRaises(HookUnavailable):
                loopback_url(value)

    def test_current_version_hash_and_loopback_listener_are_all_required(self):
        for key, value in [('verified', False), ('version', '4.1.15.56'), ('dll_sha256', 'a'*64),
                           ('loopback_listener', False), ('pid', 0), ('create_time', float('nan')),
                           ('account', 'wxid_other'), ('account_path', str(self.root))]:
            with self.subTest(key=key):
                self.snapshot[key] = value
                self.assertFalse(self.bridge.connect()[0])
                self.assertEqual(self.requests, [])
                self.snapshot.update({'verified': True, 'pid': 50, 'create_time': 100., 'account': ACCOUNT,
                    'account_path': self.account_path, 'version': HOOK_VERSION,
                    'dll_sha256': APPROVED_TEST_HASH, 'loopback_listener': True})

    def test_profile_mismatch_is_not_replaced_by_configured_account(self):
        self.responses['/GetSelfProfile'] = {'wxid': 'wxid_other'}
        self.assertFalse(self.bridge.connect()[0])
        self.assertFalse(self.bridge.online)
        self.assertNotIn('/set_callback', [row[0] for row in self.requests])

    def test_profile_redirect_is_not_followed(self):
        self.responses['/GetSelfProfile'] = httpx.Response(302, headers={'Location': 'http://example.com'})
        self.assertFalse(self.bridge.connect()[0])
        self.assertEqual(len(self.requests), 1)

    def test_response_size_bound_prevents_unlimited_json(self):
        self.responses['/GetSelfProfile'] = httpx.Response(200, content=b'x' * (MAX_RESPONSE_BYTES+1))
        self.assertFalse(self.bridge.connect()[0])
        self.assertNotIn('xxxx', self.bridge.last_error)

    def test_connect_does_not_register_callback_or_send(self):
        self.ready()
        self.assertEqual([row[0] for row in self.requests], ['/GetSelfProfile'])
        self.assertFalse(self.bridge._listening)
        self.assertEqual(self.bridge.backend, 'hook')

    def test_start_requires_confirmed_unguessable_callback_and_does_not_send(self):
        self.ready()
        callback = Mock()
        self.assertTrue(self.bridge.start_listening(callback)[0])
        self.assertTrue(self.bridge._listening)
        self.assertEqual(self.requests[-1], ('/set_callback', {'callback_url': self.settings['callback_url']}))
        self.assertEqual(self.send_requests(), [])
        self.responses['/set_callback'] = {'ret': 0, 'callback': 'http://127.0.0.1:8091/other'}
        self.assertFalse(self.bridge.start_listening(callback)[0])

    def test_stop_during_callback_registration_cannot_revive_listener(self):
        self.ready()
        def stopped(request):
            self.bridge.stop_listening()
            return httpx.Response(200, json={'ret': 0, 'callback': self.settings['callback_url']})
        self.responses['/set_callback'] = stopped
        self.assertFalse(self.bridge.start_listening(Mock())[0])
        self.assertFalse(self.bridge._listening)

    def test_restarted_listener_replaces_the_previous_host_callback(self):
        self.ready()
        old, current = Mock(), Mock()
        self.assertTrue(self.bridge.start_listening(old)[0])
        self.assertTrue(self.bridge.start_listening(current)[0])
        self.assertEqual(self.bridge._callbacks, [current])

    def test_missing_verifier_blocks_send_before_post(self):
        self.bridge = self.make_bridge(receipt_verifier=None)
        self.ready()
        self.assertEqual(self.bridge.inspect_send_environment()['waiting_code'], 'receipt_unavailable')
        result = self.bridge.send_text(TARGET, BODY)
        self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')
        self.assertEqual(self.send_requests(), [])

    def test_fixed_scope_and_file_methods_never_expand_target_or_media(self):
        self.ready()
        for result in (self.bridge.send_text('wxid_other', BODY),
                       self.bridge.send_files(TARGET, ['secret.png']),
                       self.bridge.send_text('weixin', BODY)):
            self.assertFalse(result[0])
            self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')
        self.assertEqual(self.send_requests(), [])
        self.assertEqual(self.bridge.inspect_send_environment('wxid_other')['waiting_code'], 'target_not_open')

    def test_verified_receipt_is_required_for_success_and_only_one_submission(self):
        self.ready()
        result = self.bridge.send_text(TARGET, BODY)
        self.assertTrue(result[0])
        self.assertEqual(result.diagnostic['submission_state'], 'confirmed')
        self.assertTrue(result.diagnostic['delivery_verified'])
        self.assertEqual(len(self.send_requests()), 1)
        self.assertEqual(list(self.journal.sends.values()), [['started', 'submitted', 'confirmed']])

    def test_late_verified_receipt_keeps_confirmation_without_reviving_cancelled_send_state(self):
        self.ready()
        original = self.receipt.verify
        def cancelled(baseline):
            self.bridge.cancel_pending_sends()
            return original(baseline)
        self.receipt.verify = cancelled
        result = self.bridge.send_text(TARGET, BODY)
        self.assertTrue(result[0])
        self.assertEqual(result.diagnostic['submission_state'], 'confirmed')
        self.assertTrue(result.diagnostic['delivery_verified'])
        self.assertFalse(result.diagnostic['ready'])
        self.assertFalse(self.bridge.send_environment['ready'])
        self.assertEqual(self.bridge.send_environment['waiting_code'], 'late_confirmation')
        self.assertTrue(self.bridge._cancel.is_set())
        self.assertEqual(list(self.journal.sends.values()), [['started', 'submitted', 'confirmed']])
        self.assertEqual(len(self.send_requests()), 1)

    def test_late_verified_receipt_keeps_timeout_hold_and_failure_counter(self):
        self.ready()
        original = self.receipt.verify
        def timed_out(baseline):
            self.bridge.mark_send_timeout('本轮已超时，不重发')
            return original(baseline)
        self.receipt.verify = timed_out
        result = self.bridge.send_text(TARGET, BODY)
        self.assertTrue(result[0])
        self.assertEqual(result.diagnostic['submission_state'], 'confirmed')
        self.assertTrue(result.diagnostic['delivery_verified'])
        self.assertFalse(result.diagnostic['ready'])
        self.assertFalse(self.bridge.send_environment['ready'])
        self.assertEqual(self.bridge.send_environment['waiting_code'], 'late_confirmation')
        self.assertTrue(self.bridge._cancel.is_set())
        self.assertEqual(self.bridge.consecutive_fail, 1)
        self.assertEqual(self.bridge.send_fail, 1)
        self.assertEqual(list(self.journal.sends.values()), [['started', 'submitted', 'confirmed']])
        self.assertEqual(len(self.send_requests()), 1)

    def test_ret_zero_with_missing_receipt_remains_uncertain_and_not_retried(self):
        self.ready()
        self.receipt.verify = Mock(return_value={'verified': False})
        result = self.bridge.send_text(TARGET, BODY)
        self.assertFalse(result[0])
        self.assertEqual(result.diagnostic['submission_state'], 'uncertain')
        self.assertFalse(result.diagnostic['delivery_verified'])
        self.assertEqual(len(self.send_requests()), 1)
        self.assertEqual(list(self.journal.sends.values()), [['started', 'submitted', 'uncertain']])

    def test_real_journal_confirms_receipt_and_keeps_only_body_hash(self):
        from src.hook_gateway import HookJournal
        journal = HookJournal(self.root / 'real-state.sqlite3', ACCOUNT)
        try:
            self.bridge = self.make_bridge(journal=journal)
            self.ready()
            result = self.bridge.send_text(TARGET, BODY)
            self.assertTrue(result[0])
            row = journal.get_send(result.diagnostic['request_id'])
            self.assertEqual(row['state'], 'confirmed')
            self.assertNotIn(BODY, str(row))
        finally:
            journal.close()

    def test_real_journal_marks_submitted_but_unverified_as_uncertain(self):
        from src.hook_gateway import HookJournal
        journal = HookJournal(self.root / 'uncertain-state.sqlite3', ACCOUNT)
        try:
            self.bridge = self.make_bridge(journal=journal)
            self.ready()
            self.receipt.verify = Mock(return_value={'verified': False})
            result = self.bridge.send_text(TARGET, BODY)
            self.assertFalse(result[0])
            self.assertEqual(journal.get_send(result.diagnostic['request_id'])['state'], 'uncertain')
            self.assertEqual(len(self.send_requests()), 1)
        finally:
            journal.close()

    def test_crlf_is_normalized_consistently_for_wire_and_receipt(self):
        self.ready()
        result = self.bridge.send_text(TARGET, '第一行\r\n第二行\r第三行')
        self.assertTrue(result[0])
        self.assertEqual(self.send_requests()[0][1]['msg'], '第一行\n第二行\n第三行')
        self.assertEqual(self.receipt.prepares[0][2], '第一行\n第二行\n第三行')

    def test_receipt_must_match_real_account_target_body_and_one_server_row(self):
        self.ready()
        original = self.receipt.verify
        for key, value in [('account', 'wxid_other'), ('target', 'wxid_other'), ('body_sha256', '0'*64),
                           ('account_matches', False), ('new_self_rows', 2), ('server_id', 0), ('server_id', True)]:
            def changed(baseline, key=key, value=value):
                result = original(baseline)
                result[key] = value
                return result
            self.receipt.verify = changed
            with self.subTest(key=key):
                self.assertFalse(self.bridge.send_text(TARGET, BODY)[0])
        self.assertEqual(len(self.send_requests()), 7)

    def test_timeout_after_post_is_uncertain_and_never_falls_back(self):
        self.ready()
        def timeout(request):
            raise httpx.ReadTimeout('private response body must not escape')
        self.responses['/SendTextMsg'] = timeout
        result = self.bridge.send_text(TARGET, BODY)
        self.assertEqual(result.diagnostic['submission_state'], 'uncertain')
        self.assertNotIn('private', result[1])
        self.assertNotIn(BODY, result[1])
        self.assertEqual(len(self.send_requests()), 1)

    def test_identity_change_during_baseline_aborts_before_submission(self):
        self.ready()
        original = self.receipt.prepare
        def prepare(*args):
            result = original(*args)
            self.snapshot['pid'] += 1
            return result
        self.receipt.prepare = prepare
        result = self.bridge.send_text(TARGET, BODY)
        self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')
        self.assertEqual(self.send_requests(), [])

    def test_not_submitted_failure_invalidates_previous_ready_status(self):
        self.ready()
        self.assertTrue(self.bridge.inspect_send_environment()['ready'])
        result = self.bridge.send_text('wxid_outside', BODY)
        self.assertFalse(result[0])
        self.assertFalse(self.bridge.send_environment['ready'])
        self.assertEqual(self.bridge.send_environment['waiting_code'], 'target_not_open')
        self.assertEqual(self.bridge.send_fail, 1)

    def test_transport_timeout_invalidates_status_and_cancels_old_generation(self):
        self.ready()
        self.assertTrue(self.bridge.inspect_send_environment()['ready'])
        generation = self.bridge._generation
        self.bridge.mark_send_timeout('超时，不重发')
        self.bridge.resume_sends()
        self.assertFalse(self.bridge._send_active(generation))
        self.assertFalse(self.bridge.send_environment['ready'])
        self.assertEqual(self.bridge.send_environment['submission_state'], 'uncertain')

    def test_cancel_during_baseline_aborts_before_journal_and_submission(self):
        self.ready()
        def prepare(*args):
            self.bridge.cancel_pending_sends()
            return {}
        self.receipt.prepare = prepare
        result = self.bridge.send_text(TARGET, BODY)
        self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')
        self.assertEqual(self.journal.sends, {})
        self.assertEqual(self.send_requests(), [])

    def test_cancel_then_resume_cannot_revive_a_previously_running_send(self):
        self.ready()
        def prepare(*args):
            self.bridge.cancel_pending_sends()
            self.bridge.resume_sends()
            return {}
        self.receipt.prepare = prepare
        result = self.bridge.send_text(TARGET, BODY)
        self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')
        self.assertEqual(self.journal.sends, {})
        self.assertEqual(self.send_requests(), [])

    def test_no_production_provider_blocks_before_any_profile_or_send_request(self):
        with patch('src.hook_identity.APPROVED_PROVIDER_SHA256', frozenset()):
            self.assertFalse(self.bridge.connect()[0])
        self.assertEqual(self.requests, [])

    def test_callback_persists_and_deduplicates_before_host_delivery(self):
        self.ready()
        callback = Mock()
        self.assertTrue(self.bridge.start_listening(callback)[0])
        raw = {'username': TARGET, 'hook_msgid': '123'}
        with patch('src.hook_gateway.normalize_callback', return_value=raw):
            self.assertTrue(self.bridge.ingest_raw(b'{}'))
            self.assertFalse(self.bridge.ingest_raw(b'{}'))
        callback.assert_called_once_with(raw)
        self.assertEqual(self.journal.acks, [])
        self.bridge.ack_inbound('123')
        self.assertEqual(self.journal.acks, ['123'])

    def test_pending_event_is_replayed_until_host_explicitly_acks_after_ingest(self):
        self.ready()
        raw = {'username': TARGET, 'hook_msgid': '125'}
        self.journal.pending_inbound = Mock(return_value=[raw])
        callback = Mock()
        self.assertTrue(self.bridge.start_listening(callback)[0])
        callback.assert_called_once_with(raw)
        self.assertEqual(self.journal.acks, [])
        self.bridge.replay_pending()
        self.assertEqual(callback.call_count, 2)
        self.bridge.ack_inbound('125')
        self.assertEqual(self.journal.acks, ['125'])

    def test_real_journal_and_receipt_cache_close_and_reopen_on_disconnect(self):
        from src.hook_gateway import HookJournal
        journal = HookJournal(self.root / 'disposable.sqlite3', ACCOUNT)
        self.bridge = self.make_bridge(journal=journal)
        self.receipt.close = Mock()
        self.ready()
        self.bridge.disconnect()
        self.assertTrue(journal._closed)
        self.receipt.close.assert_called_once()
        self.assertIsNone(self.bridge._journal)
        self.ready()
        self.assertEqual(self.bridge._journal.path, journal.path)

    def test_callback_disk_failure_stops_both_ingress_and_sending(self):
        self.ready()
        callback = Mock()
        self.assertTrue(self.bridge.start_listening(callback)[0])
        self.journal.record_inbound = Mock(side_effect=OSError('secret path'))
        with patch('src.hook_gateway.normalize_callback', return_value={'username': TARGET, 'hook_msgid': '124'}):
            self.assertFalse(self.bridge.ingest_raw(b'{}'))
        callback.assert_not_called()
        self.assertFalse(self.bridge.online)
        self.assertTrue(self.bridge._cancel.is_set())
        self.assertNotIn('secret path', self.bridge.last_error)

    def test_stop_during_ingress_probe_does_not_save_or_schedule_event(self):
        self.ready()
        callback = Mock()
        self.assertTrue(self.bridge.start_listening(callback)[0])
        original = self.probe.side_effect
        def stopped(*args):
            self.bridge.stop_receiving()
            return original(*args)
        self.probe.side_effect = stopped
        with patch('src.hook_gateway.normalize_callback', return_value={'username': TARGET, 'hook_msgid': '124'}):
            self.assertFalse(self.bridge.ingest_raw(b'{}'))
        self.assertEqual(self.journal.inbound, set())
        callback.assert_not_called()

    def test_security_notice_halts_sends_before_scheduling_host_callback(self):
        self.ready()
        states = []
        def callback(raw):
            states.append((self.bridge._hold(), self.bridge._cancel.is_set(), self.bridge.online))
        self.assertTrue(self.bridge.start_listening(callback)[0])
        raw = {'username': 'weixin', 'hook_msgid': '126', 'content': '为了你的账号安全，请重新登录'}
        with patch('src.hook_gateway.normalize_callback', return_value=raw):
            self.assertTrue(self.bridge.ingest_raw(b'{}'))
        self.assertEqual(states, [(True, True, False)])
        self.assertEqual(self.send_requests(), [])

    def test_disconnect_and_stop_never_call_any_wechat_shutdown_endpoint(self):
        self.ready()
        before = len(self.requests)
        self.bridge.stop_receiving()
        self.bridge.stop_listening()
        self.bridge.disconnect()
        self.assertEqual(len(self.requests), before)
        self.assertFalse(self.bridge.online)


if __name__ == '__main__':
    unittest.main()
