"""冻结版只带基础插件：spec 的排除清单与 catalog 必须一致。"""
import re
import unittest
from pathlib import Path

from src.plugin_catalog import BASIC_PLUGINS, MARKET_PLUGINS

SPEC = Path(__file__).resolve().parent.parent / 'wechat_bot.spec'


class FreezeLayeringTests(unittest.TestCase):
    def setUp(self):
        self.text = SPEC.read_text('utf-8')

    def test_spec_reads_the_catalog(self):
        self.assertIn('from src.plugin_catalog import MARKET_PLUGINS', self.text)

    def test_market_modules_are_excluded(self):
        self.assertIn('MARKET_EXCLUDES', self.text)
        self.assertIn('*sorted(MARKET_EXCLUDES)', self.text)

    def test_basic_modules_are_not_excluded_from_hiddenimports(self):
        # hiddenimports 只排除 RETIRED 与 MARKET_EXCLUDES，基础插件不会被剔掉
        line = next(l for l in self.text.splitlines() if 'collect_submodules("src")' in l)
        self.assertNotIn('BASIC', line)

    def test_escape_hatch_exists(self):
        self.assertIn('WHALE_KEEP_ALL_PLUGINS', self.text)


class CatalogShapeTests(unittest.TestCase):
    def test_basic_and_market_are_python_identifiers(self):
        for name in BASIC_PLUGINS + MARKET_PLUGINS:
            self.assertRegex(name, r'^[a-z][a-z0-9_]*$')


if __name__ == '__main__':
    unittest.main()
