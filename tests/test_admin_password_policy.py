"""验证密码格式边界，以及拒绝输入时不会改变已保存凭据。"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.admin_password import password_error
from src.admin_sessions import AdminSessions


class AdminPasswordPolicyTests(unittest.TestCase):
    def test_allowed_characters_and_boundaries(self):
        for value in ('1','a','鲸','鲸鱼娘Admin123','a'*128,'鲸'*128,'𠀀'):
            with self.subTest(value=value[:20]):
                self.assertEqual(password_error(value),'')

    def test_rejects_empty_long_special_and_non_string_inputs(self):
        for value in ('','a'*129,'a b',' a','a ','a_b','a-b','a!','a。','a\n','a\t','Ａ','①','😀','a\u200b',None,123,[]):
            with self.subTest(value=str(value)[:20]):
                self.assertIn('1–128',password_error(value))
                self.assertIn('特殊符号',password_error(value))

    def test_one_and_128_characters_persist_as_hashes_and_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            bot=SimpleNamespace(root=Path(temp))
            for value in ('7','鲸鱼娘123','a'*128):
                service=AdminSessions(bot)
                service.set_password(value)
                loaded=AdminSessions(bot)
                saved=loaded.credential
                digest=hashlib.pbkdf2_hmac('sha256',value.encode(),bytes.fromhex(saved['salt']),300000).hex()
                self.assertEqual(digest,saved['digest'])
                self.assertEqual(set(saved),{'salt','digest'})

    def test_invalid_change_preserves_file_credential_and_login(self):
        with tempfile.TemporaryDirectory() as temp:
            service=AdminSessions(SimpleNamespace(root=Path(temp)))
            service.set_password('1')
            before=service.path.read_bytes()
            credential=dict(service.credential)
            service.authenticated[('bot','owner')]=9999999999
            for value in ('bad!','has space','a'*129,''):
                with self.assertRaisesRegex(ValueError,'特殊符号'):
                    service.set_password(value)
                self.assertEqual(service.path.read_bytes(),before)
                self.assertEqual(service.credential,credential)
                self.assertIn(('bot','owner'),service.authenticated)
