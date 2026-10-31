"""市场包生成脚本：每个市场插件一个 zip，且都能通过预检。"""
import json
import tempfile
import zipfile
import unittest
from pathlib import Path

from src.plugin_catalog import MARKET_PLUGINS
from tools.build_plugin_market import build


class MarketBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / 'market'
        cls.result = build(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_build_succeeds(self):
        self.assertEqual(self.result, 0)

    def test_one_zip_per_market_plugin(self):
        zips = sorted(self.out.glob('*.zip'))
        self.assertEqual(len(zips), len(MARKET_PLUGINS))

    def test_each_zip_has_manifest_entry_and_readme(self):
        for archive in self.out.glob('*.zip'):
            with zipfile.ZipFile(archive) as zf:
                names = set(zf.namelist())
                self.assertIn('plugin.json', names, archive.name)
                self.assertIn('使用说明.md', names, archive.name)
                manifest = json.loads(zf.read('plugin.json').decode('utf-8'))
            self.assertEqual(manifest['format'], 'whale-wechat-plugin')
            self.assertTrue(manifest['market'])
            self.assertIn(manifest['id'], MARKET_PLUGINS)
            self.assertTrue(manifest['description'])

    def test_index_readme_lists_every_plugin(self):
        index = (self.out / 'README.md').read_text('utf-8')
        for plugin_id in MARKET_PLUGINS:
            self.assertIn(plugin_id, index)


if __name__ == '__main__':
    unittest.main()
