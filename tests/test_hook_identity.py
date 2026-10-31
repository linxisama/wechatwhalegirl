import unittest
from unittest.mock import patch
from src.hook_identity import HOOK_DLL_SHA256, HOOK_VERSION, loopback_api, validate_snapshot

APPROVED_TEST_HASH = 'a' * 64


class HookIdentityTests(unittest.TestCase):
    def setUp(self):
        approval = patch('src.hook_identity.APPROVED_PROVIDER_SHA256', frozenset({APPROVED_TEST_HASH}))
        approval.start()
        self.addCleanup(approval.stop)

    def snapshot(self, **changes):
        value = dict(version=HOOK_VERSION, dll_sha256=APPROVED_TEST_HASH, dll_loaded=True,
            api_port_owned=True, pid=123, process_create_time=123.0, account='wxid_sample',
            open_accounts=['wxid_sample_ab12'], loopback_listener=True)
        value.update(changes)
        return value

    def test_only_explicit_local_api_address(self):
        self.assertEqual(loopback_api('http://127.0.0.1:30001'), ('127.0.0.1', 30001))
        for value in ('http://localhost:30001', 'http://0.0.0.0:30001', 'http://example.com:30001',
                      'http://127.0.0.1', 'http://user@127.0.0.1:30001',
                      'http://127.0.0.1:30001/SendTextMsg', 'http://127.0.0.1:30001?token=x',
                      '\nhttp://127.0.0.1:30001', 'http://127.0.0.1:30001\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                loopback_api(value)

    def test_every_positive_evidence_required(self):
        self.assertTrue(validate_snapshot(self.snapshot(), 'wxid_sample')['ready'])
        for change in (dict(version='4.1.15.56'), dict(dll_sha256='x'), dict(dll_loaded=False),
            dict(api_port_owned=False), dict(pid=0), dict(loopback_listener=False),
            dict(account='wxid_other'), dict(open_accounts=[]),
            dict(open_accounts=['wxid_sample_ab12', 'wxid_other_ab12']), dict(process_create_time=None),
            dict(process_create_time=float('nan')), dict(process_create_time=float('inf')),
            dict(process_create_time=-1), dict(process_create_time=True), dict(open_accounts=[None])):
            with self.subTest(change=change):
                self.assertFalse(validate_snapshot(self.snapshot(**change), 'wxid_sample')['ready'])

    def test_wildcard_listener_has_specific_rejection(self):
        result = validate_snapshot(self.snapshot(loopback_listener=False), 'wxid_sample')
        self.assertEqual(result['waiting_code'], 'unsafe_listener')

    def test_original_hash_is_never_approved_even_if_registered_by_mistake(self):
        with patch('src.hook_identity.APPROVED_PROVIDER_SHA256', frozenset({HOOK_DLL_SHA256})):
            result = validate_snapshot(self.snapshot(dll_sha256=HOOK_DLL_SHA256), 'wxid_sample')
        self.assertFalse(result['ready'])
        self.assertEqual(result['waiting_code'], 'provider_unapproved')

    def test_production_has_no_approved_provider_and_empty_or_invalid_evidence_refuses(self):
        with patch('src.hook_identity.APPROVED_PROVIDER_SHA256', frozenset()):
            self.assertEqual(validate_snapshot(self.snapshot(), 'wxid_sample')['waiting_code'], 'provider_unapproved')
        self.assertFalse(validate_snapshot(None, 'wxid_sample')['ready'])
