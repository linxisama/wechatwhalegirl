"""1.3.51：数据目录可写探测与回退、升级保数据自检、隔离通道残留盘点与显式清理。

全部离线：不启动控制台、不监听端口、不碰真实微信，也不往真实数据目录写东西；
所有目录都是 tempfile 临时目录，用完即弃。
"""
import errno
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import HTTPException

from src import install_guard as guard

VERIFIED = 'a' * 32
BROKEN = '5d78451fff6f4f3b987e6b84525b983e'
OTHER = '8ed4ba6a574a4aac807b06cc82986983'


def write_pairing(channel, channel_id):
    channel.mkdir(parents=True, exist_ok=True)
    pairing = {'version': 1, 'channel_id': channel_id, 'secret': '12' * 32}
    (channel / 'pairing.json').write_text(json.dumps(pairing), encoding='utf-8')


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'profile'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_writable_root_does_not_fall_back(self):
        result = guard.resolve_data_root(self.root)
        self.assertFalse(result['fallback_used'])
        self.assertEqual(result['active'], str(self.root))
        self.assertTrue(result['writable'])
        self.assertTrue(result['atomic_replace'])

    def test_unwritable_root_falls_back_and_keeps_existing_data(self):
        config = self.root / 'config.wechat.yaml'
        config.write_text('wechat: {}\n', encoding='utf-8')
        history = self.root / 'data' / 'wechat-only' / 'messages.sqlite3'
        history.parent.mkdir(parents=True)
        history.write_bytes(b'existing history bytes')
        chosen = guard.fallback_dirs(self.root)[0]

        def probe(path):
            path = Path(path)
            if path == self.root:
                return {'path': str(path), 'writable': False, 'atomic_replace': False,
                        'reason': '数据目录写不进去（WinError 5）：拒绝访问'}
            if path == chosen:
                return {'path': str(path), 'writable': True, 'atomic_replace': True, 'reason': ''}
            return {'path': str(path), 'writable': False, 'atomic_replace': False,
                    'reason': '拒绝访问'}

        result = guard.resolve_data_root(self.root, probe=probe)
        self.assertTrue(result['fallback_used'])
        self.assertEqual(result['active'], str(chosen))
        self.assertIn('没有被移动或删除', result['reason'])
        self.assertIn(str(self.root), result['reason'])
        # 回退只是换一处写；用户已有数据必须原地不动，连内容都不能变。
        self.assertEqual(config.read_text(encoding='utf-8'), 'wechat: {}\n')
        self.assertEqual(history.read_bytes(), b'existing history bytes')

    def test_no_writable_candidate_keeps_primary(self):
        def probe(path):
            return {'writable': False, 'atomic_replace': False, 'reason': '写不进去'}

        result = guard.resolve_data_root(self.root, probe=probe)
        self.assertFalse(result['fallback_used'])
        self.assertEqual(result['active'], str(self.root))
        self.assertIn('备用目录也写不进去', result['reason'])

    def test_probe_timeout_never_switches_directory(self):
        def slow(path):
            time.sleep(0.3)
            return {'writable': False, 'atomic_replace': False, 'reason': '写不进去'}

        result = guard.resolve_data_root(self.root, probe=slow, seconds=0.05)
        self.assertIsNone(result['writable'])
        self.assertFalse(result['fallback_used'])
        self.assertEqual(result['active'], str(self.root))
        self.assertIn('未切换目录', result['reason'])

    def test_probe_error_never_switches_directory(self):
        def boom(path):
            raise RuntimeError('探测自己炸了')

        result = guard.resolve_data_root(self.root, probe=boom)
        self.assertIsNone(result['writable'])
        self.assertFalse(result['fallback_used'])
        self.assertEqual(result['active'], str(self.root))
        self.assertIn('未切换目录', result['reason'])

    def test_fallback_dirs_exclude_primary_and_first_is_predictable(self):
        candidates = guard.fallback_dirs(self.root)
        self.assertEqual(len(candidates), 2)
        self.assertNotIn(self.root, candidates)
        self.assertEqual(candidates[0], self.root.parent / (self.root.name + '-fallback'))
        self.assertEqual(candidates[1].name, 'WhaleGirlWeChat')


class SelfCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'profile'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_self_check_reports_counts_of_user_data(self):
        (self.root / 'config.wechat.yaml').write_text(
            'ai:\n  model: x\nwechat:\n  listen: true\n', encoding='utf-8')
        store = self.root / 'data' / 'wechat-only'
        store.mkdir(parents=True)
        connection = sqlite3.connect(store / 'messages.sqlite3')
        connection.execute('CREATE TABLE conversation_log (id INTEGER PRIMARY KEY, body TEXT)')
        connection.executemany('INSERT INTO conversation_log (body) VALUES (?)',
                               [('a',), ('b',), ('c',)])
        connection.commit()
        connection.close()
        (store / 'plugin_settings.json').write_text(json.dumps({'a': 1, 'b': 2}), encoding='utf-8')
        (self.root / 'styles').mkdir()
        (self.root / 'styles' / 'one.yaml').write_text('name: one\n', encoding='utf-8')
        (self.root / 'styles' / 'two.yaml').write_text('name: two\n', encoding='utf-8')
        (self.root / 'styles' / 'custom').mkdir()
        (self.root / 'styles' / 'custom' / 'three.yaml').write_text('name: three\n', encoding='utf-8')
        channels = self.root / 'data' / 'isolated_wechat'
        channels.mkdir(parents=True)
        write_pairing(channels / VERIFIED, VERIFIED)
        (channels / BROKEN).mkdir()

        result = guard.upgrade_self_check(self.root)
        self.assertTrue(result['ok'], result['problems'])
        self.assertEqual(result['items']['config']['value'], 2)
        self.assertEqual(result['items']['history']['value'], 3)
        self.assertEqual(result['items']['plugin_settings']['value'], 2)
        self.assertEqual(result['items']['personas']['value'], 3)
        self.assertEqual(result['items']['isolated_channels']['value'], 1)
        self.assertEqual(result['items']['isolated_channels']['unverified'], 1)
        self.assertIn('升级数据自检', result['message'])
        self.assertIn('历史 3 条', result['message'])

    def test_missing_files_are_reported_not_raised(self):
        result = guard.upgrade_self_check(self.root)
        self.assertTrue(result['ok'])
        for name in ('config', 'history', 'plugin_settings', 'personas'):
            self.assertEqual(result['items'][name]['status'], 'missing')
            self.assertTrue(result['items'][name]['message'])
        self.assertTrue(result['notes'])
        # 空目录是全新安装的正常形态：既不抛错，也不能被当成「坏了」。
        self.assertEqual(result['problems'], [])

    def test_broken_config_is_reported(self):
        (self.root / 'config.wechat.yaml').write_text('ai: [unclosed\n', encoding='utf-8')
        result = guard.upgrade_self_check(self.root)
        self.assertFalse(result['ok'])
        self.assertEqual(result['items']['config']['status'], 'broken')
        self.assertTrue(result['problems'])
        self.assertIn('config', result['problems'][0])

    def test_broken_history_database_is_reported(self):
        store = self.root / 'data' / 'wechat-only'
        store.mkdir(parents=True)
        (store / 'messages.sqlite3').write_bytes(b'not a sqlite database at all' * 64)
        result = guard.upgrade_self_check(self.root)
        self.assertFalse(result['ok'])
        self.assertEqual(result['items']['history']['status'], 'broken')
        self.assertTrue(result['items']['history']['message'])

    def test_history_without_table_counts_zero_rows(self):
        store = self.root / 'data' / 'wechat-only'
        store.mkdir(parents=True)
        sqlite3.connect(store / 'messages.sqlite3').close()
        result = guard.upgrade_self_check(self.root)
        self.assertEqual(result['items']['history']['status'], 'ok')
        self.assertEqual(result['items']['history']['value'], 0)

    def test_self_check_never_writes_into_the_data_root(self):
        self.test_self_check_reports_counts_of_user_data()
        before = sorted(path.relative_to(self.root).as_posix()
                        for path in self.root.rglob('*'))
        guard.upgrade_self_check(self.root)
        after = sorted(path.relative_to(self.root).as_posix()
                       for path in self.root.rglob('*'))
        self.assertEqual(before, after)


class ChannelInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'profile'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def make_channels(self):
        channels = self.root / 'data' / 'isolated_wechat'
        channels.mkdir(parents=True)
        write_pairing(channels / VERIFIED, VERIFIED)
        (channels / BROKEN).mkdir()
        (channels / 'notes.txt').write_text('x', encoding='utf-8')
        return channels

    def test_inventory_separates_verified_unverified_and_ignored(self):
        self.make_channels()
        report = guard.channel_inventory(self.root)
        self.assertTrue(report['exists'])
        self.assertEqual([row['channel_id'] for row in report['verified']], [VERIFIED])
        self.assertEqual([row['channel_id'] for row in report['unverified']], [BROKEN])
        self.assertTrue(report['unverified'][0]['reason'])
        self.assertIn('清理残留通道', report['unverified'][0]['suggestion'])
        self.assertEqual([row['name'] for row in report['ignored']], ['notes.txt'])
        self.assertIn('核验不过 1 个', report['message'])

    def test_missing_channel_root_is_not_an_error(self):
        report = guard.channel_inventory(self.root)
        self.assertFalse(report['exists'])
        self.assertEqual(report['unverified'], [])
        self.assertIn('还没有隔离通道目录', report['message'])

    def test_remove_deletes_only_the_unverified_channel(self):
        channels = self.make_channels()
        result = guard.remove_unverified_channel(self.root, BROKEN, BROKEN)
        self.assertTrue(result['success'])
        self.assertEqual(result['channel_id'], BROKEN)
        self.assertFalse((channels / BROKEN).exists())
        self.assertTrue((channels / VERIFIED / 'pairing.json').is_file())
        self.assertTrue((channels / 'notes.txt').is_file())
        self.assertEqual(result['remaining_unverified'], 0)

    def test_remove_requires_the_channel_id_again(self):
        self.make_channels()
        with self.assertRaises(HTTPException) as caught:
            guard.remove_unverified_channel(self.root, BROKEN, VERIFIED)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn('二次确认', caught.exception.detail)

    def test_remove_refuses_a_verifiable_channel(self):
        channels = self.make_channels()
        with self.assertRaises(HTTPException) as caught:
            guard.remove_unverified_channel(self.root, VERIFIED, VERIFIED)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn('拒绝删除', caught.exception.detail)
        self.assertTrue((channels / VERIFIED).is_dir())

    def test_remove_reports_permission_error_in_chinese(self):
        channels = self.make_channels()

        def deny(path):
            error = PermissionError(errno.EACCES, '拒绝访问')
            error.winerror = 5
            raise error

        with self.assertRaises(HTTPException) as caught:
            guard.remove_unverified_channel(self.root, BROKEN, BROKEN, remove=deny)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn('管理员', caught.exception.detail)
        self.assertIn('WinError 5', caught.exception.detail)
        self.assertTrue((channels / BROKEN).is_dir())

    def test_remove_rejects_invalid_or_missing_channel(self):
        self.make_channels()
        with self.assertRaises(HTTPException) as invalid:
            guard.remove_unverified_channel(self.root, 'zzz', 'zzz')
        self.assertEqual(invalid.exception.status_code, 400)
        with self.assertRaises(HTTPException) as missing:
            guard.remove_unverified_channel(self.root, OTHER, OTHER)
        self.assertEqual(missing.exception.status_code, 404)


if __name__ == '__main__':
    unittest.main()
