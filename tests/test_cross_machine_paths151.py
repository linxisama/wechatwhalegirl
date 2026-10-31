"""1.3.51 批3：跨机器路径差异自测（中文 / 空格 / #& / 超长 / 盘符根）。

全部离线：目录一律建在 tempfile 下（盘符根那条只用 .tmp- 前缀的临时目录，测完立刻删），
不读写真实数据根、不启端口、不碰真实微信，也不改任何既有语义。
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import compat_advice as advice
from src import install_guard as guard
from src import persistence

MASK = str(Path.home())


def long_path(base):
    """拼一条总长超过 180 个字符的路径。"""
    parts = ['很长的目录 名字#1']
    parts += ['第%d层数据目录_' % index + 'x' * 24 for index in range(1, 4)]
    return Path(base).joinpath(*parts)


def shapes(base):
    """三种由 tempfile 兜住的路径形态；盘符根由 drive_root() 单独造。"""
    return {
        'chinese_space': Path(base) / '微信 机器人 数据',
        'hash_amp': Path(base) / 'a#b&c 配置',
        'long': long_path(base),
    }


def profile_fixture():
    return {
        'ok': True,
        'generated_at': '2026-10-31T10:00:00',
        'platform': {
            'app_version': '1.3.50',
            'port': 8091,
            'frozen': False,
            'executable': '~/WhaleGirlWeChat.exe',
            'python': '3.12.10',
            'os': 'Windows-11',
            'machine': 'AMD64',
        },
        'items': [],
    }


class PathCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)

    def drive_root(self):
        """在盘符根下造一个 .tmp- 前缀的临时目录，测完立刻删；造不出来就返回 None。"""
        root = Path(self.base.drive + os.sep)
        try:
            folder = Path(tempfile.mkdtemp(prefix='.tmp-wgc151-', dir=str(root)))
        except OSError:
            return None
        self.addCleanup(lambda: shutil.rmtree(str(folder), ignore_errors=True))
        return folder

    def all_shapes(self):
        found = shapes(self.base)
        drive = self.drive_root()
        if drive is not None:
            found['drive_root'] = drive
        return found


class PersistencePathTests(PathCase):
    def test_shape_paths_are_really_odd(self):
        found = shapes(self.base)
        self.assertIn(' ', str(found['chinese_space']))
        self.assertIn('#', str(found['hash_amp']))
        self.assertIn('&', str(found['hash_amp']))
        self.assertGreater(len(str(found['long'])), 180)
        found['long'].mkdir(parents=True)
        self.assertTrue(found['long'].is_dir())

    def test_atomic_text_round_trip_with_backup_on_every_shape(self):
        for name, folder in sorted(self.all_shapes().items()):
            with self.subTest(shape=name):
                target = folder / 'config.wechat.yaml'
                folder.mkdir(parents=True, exist_ok=True)
                persistence.atomic_text(target, 'v1 #& 中文')
                self.assertEqual(target.read_text(encoding='utf-8'), 'v1 #& 中文')
                persistence.atomic_text(target, 'v2')
                self.assertEqual(target.read_text(encoding='utf-8'), 'v2')
                backup = target.with_suffix(target.suffix + '.bak')
                self.assertTrue(backup.is_file(), name + ' 没有生成 .bak 备份')
                self.assertEqual(backup.read_text(encoding='utf-8'), 'v1 #& 中文')

    def test_probe_directory_is_writable_on_every_shape(self):
        for name, folder in sorted(self.all_shapes().items()):
            with self.subTest(shape=name):
                folder.mkdir(parents=True, exist_ok=True)
                probe = persistence.probe_directory(folder)
                self.assertTrue(probe['writable'], name + ' 探测说不可写：' + probe['reason'])
                self.assertTrue(probe['atomic_replace'], name + ' 探测说改名被挡：' + probe['reason'])
                self.assertEqual(probe['reason'], '')
                self.assertEqual(probe['path'], str(folder))
                self.assertGreaterEqual(probe['elapsed_ms'], 0)
                self.assertEqual([p.name for p in folder.glob('.write-probe-*')], [])

    def test_atomic_text_degrades_when_rename_is_blocked(self):
        folder = shapes(self.base)['chinese_space']
        folder.mkdir(parents=True)
        target = folder / 'config.yaml'
        persistence.atomic_text(target, 'v1')
        with patch.object(persistence.os, 'replace', side_effect=PermissionError(13, 'in use')):
            persistence.atomic_text(target, 'v2')
        self.assertEqual(target.read_text(encoding='utf-8'), 'v2')

    def test_probe_directory_degrades_without_failing(self):
        folder = shapes(self.base)['hash_amp']
        folder.mkdir(parents=True)
        with patch.object(persistence.os, 'replace', side_effect=PermissionError(13, 'in use')):
            probe = persistence.probe_directory(folder)
        self.assertTrue(probe['writable'])
        self.assertFalse(probe['atomic_replace'])
        self.assertIn('就地重写', probe['reason'])
        self.assertEqual([p.name for p in folder.glob('.write-probe-*')], [])


class InstallGuardPathTests(PathCase):
    def test_resolve_data_root_accepts_chinese_space_root(self):
        root = shapes(self.base)['chinese_space']
        root.mkdir(parents=True)
        decision = guard.resolve_data_root(root)
        self.assertEqual(decision['active'], str(root))
        self.assertEqual(decision['primary'], str(root))
        self.assertTrue(decision['writable'], '中文+空格数据根被判不可写：' + decision['reason'])
        self.assertTrue(decision['atomic_replace'])
        self.assertFalse(decision['fallback_used'])
        self.assertEqual(decision['reason'], '')

    def test_resolve_data_root_accepts_long_path_root(self):
        root = shapes(self.base)['long']
        root.mkdir(parents=True)
        self.assertGreater(len(str(root)), 180)
        decision = guard.resolve_data_root(root)
        self.assertEqual(decision['active'], str(root))
        self.assertTrue(decision['writable'], '超长路径数据根被判不可写：' + decision['reason'])
        self.assertFalse(decision['fallback_used'])

    def test_resolve_data_root_accepts_drive_root(self):
        root = self.drive_root()
        if root is None:
            self.skipTest('盘符根不可写，跳过这一形态')
        decision = guard.resolve_data_root(root)
        self.assertEqual(decision['active'], str(root))
        self.assertTrue(decision['writable'])

    def test_upgrade_self_check_on_chinese_space_root(self):
        root = shapes(self.base)['chinese_space']
        root.mkdir(parents=True)
        (root / 'config.wechat.yaml').write_text('a: 1', encoding='utf-8')
        result = guard.upgrade_self_check(root)
        self.assertEqual(result['root'], str(root))
        self.assertTrue(result['ok'], str(result['problems']))
        self.assertEqual(result['items']['config']['status'], 'ok')
        self.assertEqual(result['items']['config']['value'], 1)
        self.assertTrue(result['message'])

    def test_channel_inventory_on_hash_amp_root(self):
        root = shapes(self.base)['hash_amp']
        root.mkdir(parents=True)
        result = guard.channel_inventory(root)
        # result['root'] 是通道目录（数据根/data/isolated_wechat），不是数据根本身
        self.assertTrue(result['root'].startswith(str(root)), result['root'])
        self.assertTrue(result['root'].endswith('isolated_wechat'))
        self.assertFalse(result['exists'])
        self.assertEqual(result['verified'], [])
        self.assertEqual(result['unverified'], [])
        self.assertEqual(result['ignored'], [])
        self.assertTrue(result['message'])


class RedactionTests(PathCase):
    def test_evidence_masks_home(self):
        evidence = advice._evidence({'root': MASK, 'mixed': MASK + os.sep + '微信 数据#&'})
        dumped = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn(MASK, dumped)
        self.assertNotIn(Path.home().name, dumped)
        self.assertIn('~', dumped)

    def test_collect_advice_on_odd_root_masks_username(self):
        root = shapes(self.base)['chinese_space']
        root.mkdir(parents=True)
        (root / 'config.wechat.yaml.bak-20260101').write_text('old', encoding='utf-8')
        result = advice.collect_advice(
            root,
            profile=profile_fixture(),
            dsh=None,
            data_root=guard.resolve_data_root(root),
            self_check=guard.upgrade_self_check(root),
            channels=guard.channel_inventory(root),
        )
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(MASK, dumped)
        self.assertNotIn(Path.home().name, dumped)
        keys = [item['key'] for item in result['items']]
        self.assertIn('leftover_residue', keys)
        self.assertEqual(len(keys), 9)


class LeftoverResidueTests(PathCase):
    def test_single_level_scan_counts_kinds_and_never_deletes(self):
        root = shapes(self.base)['chinese_space']
        root.mkdir(parents=True)
        (root / 'config.wechat.yaml.bak-20260101').write_text('a', encoding='utf-8')
        (root / 'messages.sqlite3.bak').write_text('bb', encoding='utf-8')
        nested = root / '.tmp-old'
        nested.mkdir()
        (nested / 'payload.tmp').write_text('c', encoding='utf-8')
        channel = root / 'isolated_wechat'
        channel.mkdir()
        (channel / 'stray.bak').write_text('d', encoding='utf-8')
        deep = root / 'data'
        deep.mkdir()
        (deep / 'hidden.bak').write_text('e', encoding='utf-8')
        result = guard.residue_inventory(root)
        self.assertTrue(result['scanned'])
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['bytes'], 4)
        self.assertEqual(result['by_kind'], {'旧配置备份': 1, '备份': 1, '临时文件': 1})
        self.assertEqual(result['limit'], 200)
        self.assertFalse(result['truncated'])
        for keep in ('config.wechat.yaml.bak-20260101', 'messages.sqlite3.bak', '.tmp-old'):
            self.assertTrue((root / keep).exists(), '只统计不删，' + keep + ' 不该消失')

    def test_more_than_limit_reports_at_least(self):
        root = shapes(self.base)['hash_amp']
        root.mkdir(parents=True)
        for index in range(205):
            (root / ('old%d.bak' % index)).write_text('x', encoding='utf-8')
        result = guard.residue_inventory(root)
        self.assertEqual(result['count'], 200)
        self.assertTrue(result['truncated'])
        self.assertIn('至少 200', result['message'])
        self.assertEqual(len(list(root.glob('*.bak'))), 205)

    def test_missing_and_none_roots_degrade(self):
        missing = guard.residue_inventory(self.base / 'nope')
        self.assertFalse(missing['scanned'])
        self.assertEqual(missing['count'], 0)
        self.assertIn('还不存在', missing['message'])
        nothing = guard.residue_inventory(None)
        self.assertFalse(nothing['scanned'])
        self.assertEqual(nothing['count'], 0)
        self.assertTrue(nothing['message'])

    def test_clean_root_adds_no_advice_item(self):
        root = shapes(self.base)['long']
        root.mkdir(parents=True)
        (root / 'config.wechat.yaml').write_text('a: 1', encoding='utf-8')
        result = advice.collect_advice(
            root,
            profile=profile_fixture(),
            dsh=None,
            data_root=guard.resolve_data_root(root),
            self_check=guard.upgrade_self_check(root),
            channels=guard.channel_inventory(root),
        )
        keys = [item['key'] for item in result['items']]
        self.assertNotIn('leftover_residue', keys)
        self.assertEqual(len(keys), 8)


if __name__ == '__main__':
    unittest.main()
