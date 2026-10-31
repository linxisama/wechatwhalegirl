"""atomic_text 在 os.replace 被占用时降级为就地写入。"""
import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.persistence import atomic_text


def _denied():
    error=PermissionError(errno.EACCES,'另一个程序正在使用此文件，进程无法访问。')
    error.winerror=32
    return error


class AtomicTextReplaceFallbackTest(unittest.TestCase):
    def setUp(self):
        self._sandbox=tempfile.TemporaryDirectory()
        self.addCleanup(self._sandbox.cleanup)
        self.root=Path(self._sandbox.name)
        self.path=self.root/'cfg.yaml'
        self.bak=Path(str(self.path)+'.bak')

    def _tmp_files(self):
        return sorted(p.name for p in self.root.glob('*.tmp'))

    def test_replace_ok_writes_text_and_keeps_backup(self):
        self.path.write_text('old\n',encoding='utf-8')
        atomic_text(self.path,'new\n')
        self.assertEqual('new\n',self.path.read_text(encoding='utf-8'))
        self.assertEqual('old\n',self.bak.read_text(encoding='utf-8'))
        self.assertEqual([],self._tmp_files())

    def test_permission_denied_rewrites_in_place_and_keeps_old_backup(self):
        self.path.write_text('old\n',encoding='utf-8')
        with patch('src.persistence.os.replace',side_effect=_denied()):
            atomic_text(self.path,'new\n')
        self.assertEqual('new\n',self.path.read_text(encoding='utf-8'))
        self.assertEqual('old\n',self.bak.read_text(encoding='utf-8'))
        self.assertEqual([],self._tmp_files())

    def test_permission_denied_logs_warning_with_path_and_winerror(self):
        self.path.write_text('old\n',encoding='utf-8')
        with patch('src.persistence.os.replace',side_effect=_denied()),patch('src.persistence.logger') as logger:
            atomic_text(self.path,'new\n')
        message=logger.warning.call_args[0][0]
        self.assertIn(str(self.path),message)
        self.assertIn('32',message)

    def test_other_oserror_propagates_and_leaves_no_tmp(self):
        self.path.write_text('old\n',encoding='utf-8')
        with patch('src.persistence.os.replace',side_effect=OSError(errno.EIO,'io error')):
            with self.assertRaises(OSError) as caught:
                atomic_text(self.path,'new\n')
        self.assertEqual(errno.EIO,caught.exception.errno)
        self.assertNotIsInstance(caught.exception,PermissionError)
        self.assertEqual('old\n',self.path.read_text(encoding='utf-8'))
        self.assertEqual([],self._tmp_files())

    def test_missing_target_is_created(self):
        atomic_text(self.path,'fresh\n')
        self.assertEqual('fresh\n',self.path.read_text(encoding='utf-8'))
        self.assertFalse(self.bak.exists())
        self.assertEqual([],self._tmp_files())

    def test_backup_false_writes_without_bak(self):
        self.path.write_text('old\n',encoding='utf-8')
        atomic_text(self.path,'new\n',backup=False)
        self.assertEqual('new\n',self.path.read_text(encoding='utf-8'))
        self.assertFalse(self.bak.exists())
        self.assertEqual([],self._tmp_files())

    def test_permission_denied_without_backup_still_rewrites(self):
        self.path.write_text('old\n',encoding='utf-8')
        with patch('src.persistence.os.replace',side_effect=_denied()):
            atomic_text(self.path,'new\n',backup=False)
        self.assertEqual('new\n',self.path.read_text(encoding='utf-8'))
        self.assertFalse(self.bak.exists())
        self.assertEqual([],self._tmp_files())


if __name__=='__main__':
    unittest.main()
