# -*- coding: utf-8 -*-
"""1.3.81 机器体检（--machine-check）的判据与报告格式。"""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import machine_check as mc


def fake_site(statuses=('ok',) * 9, summary='9 项探测全部正常'):
    keys = list(mc.ITEM_TITLES)
    items = []
    for key, status in zip(keys, statuses):
        items.append({'key': key, 'status': status, 'detail': f'{key} 明细', 'data': {}})
    return {'ok': all(s == 'ok' for s in statuses), 'summary': summary, 'items': items,
            'platform': {'port': 8091, 'frozen': True, 'os': 'Windows-11', 'python': '3.12.9'}}


def fake_install(writable=True, fallback=False, residue=0):
    return {
        'data_root': {'primary': 'C:/data', 'active': 'C:/data', 'writable': writable,
                      'atomic_replace': True, 'fallback_used': fallback, 'reason': ''},
        'marker': {'exists': True, 'version': '1.3.81', 'installed_at': '2026-11-02 10:00:00',
                   'install_dir': 'C:/app', 'note': ''},
        'rollback': {'available': False, 'install_dir': 'C:/app', 'note': '没有上一版备份'},
        'residue': {'count': residue, 'message': f'旧版本残留 {residue} 项' if residue else '没有残留'},
    }


class VerdictTests(unittest.TestCase):
    def test_all_ok(self):
        report = mc.collect(site=fake_site(), install=fake_install())
        self.assertEqual(report['verdict'], 'ok')
        self.assertTrue(report['ok'])

    def test_warn_item(self):
        statuses = ('ok',) * 8 + ('warn',)
        report = mc.collect(site=fake_site(statuses), install=fake_install())
        self.assertEqual(report['verdict'], 'warn')
        self.assertTrue(report['ok'])

    def test_unknown_item(self):
        statuses = ('ok',) * 8 + ('unknown',)
        report = mc.collect(site=fake_site(statuses), install=fake_install())
        self.assertEqual(report['verdict'], 'unknown')
        self.assertFalse(report['ok'])

    def test_blocked_beats_unknown(self):
        statuses = ('ok',) * 8 + ('unknown',)
        report = mc.collect(site=fake_site(statuses), install=fake_install(writable=False))
        self.assertEqual(report['verdict'], 'blocked')
        self.assertFalse(report['ok'])

    def test_unconfirmed_data_root_is_not_blocked(self):
        install = fake_install()
        install['data_root']['writable'] = None
        report = mc.collect(site=fake_site(), install=install)
        self.assertEqual(report['verdict'], 'ok')


class SuggestionTests(unittest.TestCase):
    def test_warn_detail_listed(self):
        statuses = ('ok',) * 8 + ('warn',)
        report = mc.collect(site=fake_site(statuses), install=fake_install())
        joined = ' '.join(report['suggestions'])
        self.assertIn(mc.ITEM_TITLES['data_root'], joined)
        self.assertIn('data_root 明细', joined)

    def test_clean_report_says_nothing_to_do(self):
        report = mc.collect(site=fake_site(), install=fake_install())
        self.assertEqual(report['suggestions'], ['没有需要处理的事项'])

    def test_residue_is_listed(self):
        report = mc.collect(site=fake_site(), install=fake_install(residue=3))
        self.assertTrue(any('旧版本残留 3 项' in text for text in report['suggestions']))

    def test_fallback_listed(self):
        install = fake_install(fallback=True)
        install['data_root']['reason'] = '主目录改名被占用'
        report = mc.collect(site=fake_site(), install=install)
        self.assertTrue(any('备用目录' in text for text in report['suggestions']))


class RenderTests(unittest.TestCase):
    def test_sections_present(self):
        report = mc.collect(site=fake_site(), install=fake_install())
        text = mc.render(report)
        for section in ('[九项自检]', '[数据目录]', '[安装]', '[旧版本残留]', '[需要你做的事]', '[原始数据]'):
            self.assertIn(section, text)
        self.assertIn('结论：ok', text)
        self.assertIn(mc.VERDICTS['ok'], text)

    def test_verdict_text_for_unknown(self):
        statuses = ('unknown',) + ('ok',) * 8
        report = mc.collect(site=fake_site(statuses), install=fake_install())
        self.assertIn(mc.VERDICTS['unknown'], mc.render(report))

    def test_render_survives_odd_values(self):
        report = mc.collect(site=fake_site(), install=fake_install())
        report['platform']['executable'] = pathlib.Path('C:/app/x.exe')
        report['data_root']['reason'] = ValueError('boom')
        text = mc.render(report)
        self.assertIn('[原始数据]', text)
        json.loads(text.split('[原始数据]', 1)[1])

    def test_home_is_redacted(self):
        home = str(pathlib.Path.home())
        report = mc.collect(site=fake_site(), install=fake_install())
        report['platform']['executable'] = home + r'\AppData\Local\Programs\x.exe'
        text = mc.render(report)
        self.assertNotIn(home, text)
        self.assertIn('~', text)


class WriteTests(unittest.TestCase):
    def test_write_report_bom_crlf(self):
        report = mc.collect(site=fake_site(), install=fake_install())
        with tempfile.TemporaryDirectory() as folder:
            target = pathlib.Path(folder) / 'sub' / 'machine-check.txt'
            written = mc.write_report(report, target)
            raw = written.read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(raw.count(b'\n') - raw.count(b'\r\n'), 0)

    def test_main_writes_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            target = pathlib.Path(folder) / 'check.txt'
            with mock.patch.object(mc, 'collect', return_value=mc.collect(site=fake_site(), install=fake_install())):
                code = mc.main([str(target)])
            self.assertEqual(code, mc.EXIT_OK)
            self.assertTrue(target.is_file())

    def test_main_reports_write_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            blocker = pathlib.Path(folder) / 'blocker'
            blocker.write_text('x', encoding='utf-8')
            with mock.patch.object(mc, 'collect', return_value=mc.collect(site=fake_site(), install=fake_install())):
                code = mc.main([str(blocker / 'check.txt')])
        self.assertEqual(code, mc.EXIT_WRITE_FAILED)

    def test_main_defaults_to_cwd(self):
        report = mc.collect(site=fake_site(), install=fake_install())
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                with mock.patch.object(mc, 'collect', return_value=report):
                    code = mc.main([])
                self.assertEqual(code, mc.EXIT_OK)
                self.assertTrue((pathlib.Path(folder) / mc.REPORT_NAME).is_file())
            finally:
                os.chdir(old)


class DegradeTests(unittest.TestCase):
    def test_site_probe_crash_degrades(self):
        with mock.patch.object(mc.site_profile, 'collect', side_effect=RuntimeError('boom')), \
             mock.patch.object(mc.install_guard, 'resolve_data_root', return_value=fake_install()['data_root']), \
             mock.patch.object(mc.install_guard, 'read_install_marker', return_value=fake_install()['marker']), \
             mock.patch.object(mc.install_guard, 'rollback_state', return_value=fake_install()['rollback']), \
             mock.patch.object(mc.install_guard, 'residue_inventory', return_value=fake_install()['residue']):
            report = mc.collect()
        self.assertEqual(report['verdict'], 'unknown')
        self.assertIn('探测出错', report['site']['summary'])
        self.assertTrue(mc.render(report))

    def test_install_guard_crash_degrades(self):
        with mock.patch.object(mc.site_profile, 'collect', return_value=fake_site()), \
             mock.patch.object(mc.install_guard, 'resolve_data_root', side_effect=OSError('nope')), \
             mock.patch.object(mc.install_guard, 'read_install_marker', side_effect=OSError('nope')), \
             mock.patch.object(mc.install_guard, 'rollback_state', side_effect=OSError('nope')), \
             mock.patch.object(mc.install_guard, 'residue_inventory', side_effect=OSError('nope')):
            report = mc.collect()
        self.assertIn('探测出错', report['data_root']['reason'])
        self.assertEqual(report['verdict'], 'ok')

    def test_collect_never_raises_on_garbage_guard(self):
        report = mc.collect(site=fake_site(), install={'data_root': 'oops', 'marker': 5, 'residue': None})
        self.assertIn(report['verdict'], ('ok', 'warn', 'blocked', 'unknown'))


if __name__ == '__main__':
    unittest.main()
