"""管理员所有者不应阻断同一实际用户；只检查自建临时目录和内存描述符。"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import private_directory as private
from src import sandbox_manager as manager
from tests import test_sandbox_manager1315 as fixtures


class PrivateDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_directory_and_content_are_never_reused_or_changed(self):
        path = self.parent / 'existing'
        path.mkdir()
        content = path / 'old-record'
        content.write_bytes(b'keep evidence')
        before = path.stat()
        with self.assertRaises(FileExistsError):
            private.create_private_directory(path)
        self.assertEqual(content.read_bytes(), b'keep evidence')
        self.assertEqual(path.stat().st_mode, before.st_mode)

    def test_missing_parent_is_not_created(self):
        path = self.parent / 'missing' / 'private'
        with self.assertRaises(FileNotFoundError):
            private.create_private_directory(path)
        self.assertFalse(path.parent.exists())

    def test_existing_file_is_not_replaced(self):
        path = self.parent / 'existing-file'
        path.write_bytes(b'keep file')
        with self.assertRaises(FileExistsError):
            private.create_private_directory(path)
        self.assertEqual(path.read_bytes(), b'keep file')

    def test_reparse_ancestor_refused_before_native_creation(self):
        info = type('ReparseInfo', (), {'st_file_attributes': 0x400})()
        with patch.object(Path, 'lstat', return_value=info), \
                patch.object(private, '_create_windows_directory') as native:
            with self.assertRaisesRegex(OSError, '链接'):
                private.create_private_directory(self.parent / 'private')
        native.assert_not_called()

    def test_descriptor_rejects_non_sid_data(self):
        for invalid in (None, '', 'BA', 'OW', 'S-1-5-21-1)(A;;FA;;;WD', 'S-1-5-21\n'):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                private._directory_sddl(invalid)

    @unittest.skipUnless(os.name != 'nt', 'POSIX 权限只在非 Windows 核验')
    def test_posix_directory_is_private(self):
        path = private.create_private_directory(self.parent / 'private')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    @unittest.skipUnless(os.name == 'nt', '需要 Windows 安全描述符')
    def test_new_directory_has_protected_explicit_actual_user_dacl_and_inheritance(self):
        import win32api
        import win32con
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        expected = {win32security.ConvertSidToStringSid(user), 'S-1-5-18', 'S-1-5-32-544'}
        path = private.create_private_directory(self.parent / 'private')
        descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
        self.assertTrue(descriptor.GetSecurityDescriptorControl()[0] & 0x1000)  # DACL_PROTECTED
        acl = descriptor.GetSecurityDescriptorDacl()
        self.assertIsNotNone(acl)
        self.assertEqual(acl.GetAceCount(), 3)
        actual = set()
        for index in range(acl.GetAceCount()):
            (kind, flags), mask, sid = acl.GetAce(index)
            self.assertEqual(kind, win32security.ACCESS_ALLOWED_ACE_TYPE)
            self.assertEqual(flags, 3)  # OBJECT_INHERIT | CONTAINER_INHERIT
            self.assertEqual(mask, 0x1F01FF)  # FILE_ALL_ACCESS
            actual.add(win32security.ConvertSidToStringSid(sid))
        self.assertEqual(actual, expected)
        child = path / 'record.json'
        child.write_text('no secrets', encoding='utf-8')
        inherited = win32security.GetFileSecurity(str(child), win32security.DACL_SECURITY_INFORMATION)
        child_acl = inherited.GetSecurityDescriptorDacl()
        self.assertEqual({win32security.ConvertSidToStringSid(child_acl.GetAce(i)[2])
                          for i in range(child_acl.GetAceCount())}, expected)

    @unittest.skipUnless(os.name == 'nt', '需要 Windows AccessCheck')
    def test_admin_owner_explicit_user_allows_filtered_user_but_owner_rights_does_not(self):
        import ctypes
        from ctypes import wintypes
        import win32api
        import win32con
        import win32security

        class GenericMapping(ctypes.Structure):
            _fields_ = [(name, wintypes.DWORD) for name in ('read', 'write', 'execute', 'all')]

        api = ctypes.WinDLL('advapi32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD))
        api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        api.AccessCheck.argtypes = (ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD,
            ctypes.POINTER(GenericMapping), ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.BOOL))
        api.AccessCheck.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel.LocalFree.restype = ctypes.c_void_p
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                               win32con.TOKEN_QUERY | win32con.TOKEN_DUPLICATE)
        restricted = impersonation = None
        try:
            user = win32security.ConvertSidToStringSid(
                win32security.GetTokenInformation(token, win32security.TokenUser)[0])
            admin = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
            # 内存令牌中禁用管理员组，确保测试提权运行时仍模拟沙盒共享进程的过滤令牌。
            restricted = win32security.CreateRestrictedToken(token, 1, [(admin, 0)], [], [])
            impersonation = win32security.DuplicateToken(restricted, win32security.SecurityImpersonation)

            def allowed(sddl):
                descriptor = ctypes.c_void_p()
                self.assertTrue(api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    sddl, 1, ctypes.byref(descriptor), None))
                try:
                    mapping = GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
                    privileges = ctypes.create_string_buffer(4096)
                    size, granted, status = wintypes.DWORD(4096), wintypes.DWORD(), wintypes.BOOL()
                    self.assertTrue(api.AccessCheck(descriptor, int(impersonation), 1,
                        ctypes.byref(mapping), privileges, ctypes.byref(size), ctypes.byref(granted),
                        ctypes.byref(status)))
                    return bool(status.value)
                finally:
                    kernel.LocalFree(descriptor)

            old = 'O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)'
            self.assertFalse(allowed(old))
            self.assertTrue(allowed('O:BAG:BA' + private._directory_sddl(user)))
        finally:
            if impersonation is not None:
                impersonation.Close()
            if restricted is not None:
                restricted.Close()
            token.Close()

    @unittest.skipUnless(os.name == 'nt', '需要 Windows 创建权限核验')
    def test_readback_failure_returns_no_usable_directory_or_pairing_data(self):
        path = self.parent / 'private'
        with patch.object(private, '_verify_windows_directory', side_effect=OSError('ACL not applied')):
            with self.assertRaisesRegex(OSError, 'ACL not applied'):
                private.create_private_directory(path)
        # 不回退继承式创建，也不先写入秘密再检查权限；留下本次空目录供诊断。
        self.assertTrue(path.is_dir())
        self.assertEqual(list(path.iterdir()), [])


class SandboxPrivateDirectoryIntegrationTests(unittest.TestCase):
    setUp = fixtures.SandboxManagerTests.setUp
    tearDown = fixtures.SandboxManagerTests.tearDown
    fake_cli = fixtures.SandboxManagerTests.fake_cli
    prepare = fixtures.SandboxManagerTests.prepare

    def test_program_staging_and_pairing_both_use_atomic_private_creation(self):
        with patch.object(manager, 'create_private_directory', wraps=private.create_private_directory) as create:
            result = self.prepare()
        paths = [call.args[0] for call in create.call_args_list]
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].name.startswith('.preparing-'))
        self.assertEqual(paths[1], Path(result['channel_dir']))
        self.assertEqual(paths[0].parent.parent.name, 'isolated_wechat_programs')

    @unittest.skipUnless(os.name == 'nt', '需要 Windows 复制后的实际 ACL')
    def test_copytree_copy2_and_stage_rename_keep_only_actual_user_system_admin(self):
        import win32api
        import win32con
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            user = win32security.ConvertSidToStringSid(
                win32security.GetTokenInformation(token, win32security.TokenUser)[0])
        finally:
            token.Close()
        expected = {user, 'S-1-5-18', 'S-1-5-32-544'}
        result = self.prepare()
        program = Path(next(c for c in self.commands if c[1] == 'share')[
            next(c for c in self.commands if c[1] == 'share').index('--host-path') + 1])
        self.assertFalse(program.name.startswith('.preparing-'))
        channel = Path(result['channel_dir'])
        for path in (program, *program.rglob('*'), channel, *channel.rglob('*')):
            with self.subTest(path=path.relative_to(self.root)):
                descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
                acl = descriptor.GetSecurityDescriptorDacl()
                self.assertIsNotNone(acl)
                self.assertEqual(acl.GetAceCount(), 3)
                self.assertEqual({win32security.ConvertSidToStringSid(acl.GetAce(i)[2])
                                  for i in range(acl.GetAceCount())}, expected)
                for index in range(acl.GetAceCount()):
                    self.assertEqual(acl.GetAce(index)[0][0], win32security.ACCESS_ALLOWED_ACE_TYPE)
                    self.assertEqual(acl.GetAce(index)[1], 0x1F01FF)
                if path in (program, channel):
                    self.assertTrue(descriptor.GetSecurityDescriptorControl()[0] & 0x1000)


if __name__ == '__main__':
    unittest.main()
