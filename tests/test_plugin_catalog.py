"""插件分层清单：基础内置 + 市场包，必须和真实插件一一对应。"""
import tempfile
import unittest
from pathlib import Path

from src.plugin_catalog import (BASIC_PLUGINS, MARKET_DESCRIPTIONS, MARKET_PLUGINS,
                                is_basic, is_market, market_spec, validate)
from src.plugins.manager import PluginManager


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.real = set(self.manager._plugins)

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_real_plugin_is_classified(self):
        self.assertEqual(validate(self.real), [])

    def test_two_lists_do_not_overlap(self):
        self.assertFalse(set(BASIC_PLUGINS) & set(MARKET_PLUGINS))

    def test_expected_split_sizes(self):
        self.assertEqual(len(BASIC_PLUGINS), 25)
        self.assertEqual(len(MARKET_PLUGINS), 8)

    def test_core_plugins_are_all_basic(self):
        from src.plugin_policy import CORE_PLUGINS
        for plugin_id in CORE_PLUGINS:
            self.assertTrue(is_basic(plugin_id), plugin_id)

    def test_retired_plugins_are_not_in_the_market(self):
        from src.plugin_policy import RETIRED_PLUGINS
        for plugin_id in RETIRED_PLUGINS:
            self.assertFalse(is_market(plugin_id), plugin_id)
            self.assertFalse(is_basic(plugin_id), plugin_id)

    def test_sticker_chain_is_built_in(self):
        """配图/表情链路必须内置：分层时一度划进市场包，用户装完就"不发表情了"。"""
        for plugin_id in ('sticker_admin', 'sticker_memory', 'sticker_cadence', 'image_library'):
            self.assertTrue(is_basic(plugin_id), plugin_id)

    def test_every_market_plugin_has_a_description(self):
        for plugin_id in MARKET_PLUGINS:
            self.assertTrue(MARKET_DESCRIPTIONS.get(plugin_id), plugin_id)
            self.assertEqual(market_spec(plugin_id)['id'], plugin_id)

    def test_market_spec_rejects_basic_plugin(self):
        with self.assertRaises(KeyError):
            market_spec(BASIC_PLUGINS[0])

    def test_unknown_plugin_is_reported(self):
        problems = validate(self.real | {'made_up_plugin'})
        self.assertTrue(any('没归类' in problem for problem in problems))


if __name__ == '__main__':
    unittest.main()
