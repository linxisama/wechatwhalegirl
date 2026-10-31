"""批量导入插件：预检不写盘、批量只重载一次、加载失败要回滚。"""
import tempfile
import unittest
from pathlib import Path

from src.plugin_batch import expand_paths, install, preview
from src.plugin_installer import InstallError
from src.plugins.manager import PluginManager

GOOD = '''
from . import PluginBase, PluginMeta, FieldSchema

class Plugin(PluginBase):
    META = PluginMeta(id="{pid}", name="{name}", description="测试用",
                      config_schema={{"enabled": FieldSchema(type="bool", label="启用")}},
                      default_settings={{"enabled": False}})
'''

BROKEN_RUNTIME = '''
from . import PluginBase, PluginMeta

class Plugin(PluginBase):
    META = PluginMeta(id="{pid}", name="{name}", description="导入即炸")
    raise RuntimeError("boom")
'''


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / 'incoming'
        self.source.mkdir()
        self.manager = PluginManager(root / 'plugins')
        self.plugins_dir = Path(self.manager.custom_plugins_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, body, pid=None, label='演示'):
        path = self.source / name
        path.write_text(body.format(pid=pid or name.replace('.py', ''), name=label), 'utf-8')
        return path

    def test_expand_scans_folders_one_level(self):
        self.write('a.py', GOOD, 'batch_a')
        (self.source / 'nested').mkdir()
        (self.source / 'nested' / 'b.py').write_text('x', 'utf-8')
        found = expand_paths([str(self.source)])
        self.assertEqual([p.name for p in found], ['a.py'])

    def test_expand_deduplicates(self):
        path = self.write('a.py', GOOD, 'batch_a')
        self.assertEqual(len(expand_paths([str(path), str(path)])), 1)

    def test_preview_does_not_write(self):
        self.write('a.py', GOOD, 'batch_a')
        result = preview([str(self.source)], self.plugins_dir)
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['plugin_id'], 'batch_a')
        self.assertFalse((self.plugins_dir / 'batch_a.py').exists())

    def test_preview_flags_conflicts(self):
        self.write('a.py', GOOD, 'batch_a')
        (self.plugins_dir / 'batch_a.py').write_text('old', 'utf-8')
        result = preview([str(self.source)], self.plugins_dir)
        self.assertEqual(result['conflicts'], ['batch_a'])

    def test_preview_reports_dangerous_code(self):
        (self.source / 'bad.py').write_text(
            'import os\nos.system("calc")\nfrom . import PluginBase, PluginMeta\n'
            'class Plugin(PluginBase):\n    META = PluginMeta(id="bad", name="坏", description="d")\n', 'utf-8')
        result = preview([str(self.source)], self.plugins_dir)
        self.assertFalse(result['ok'])
        self.assertTrue(result['items'][0]['problems'])

    def test_install_good_plugin(self):
        self.write('a.py', GOOD, 'batch_a', '批量A')
        result = install(self.manager, [str(self.source)])
        self.assertEqual(result['installed'], 1)
        self.assertIn('batch_a', self.manager._plugins)

    def test_install_enables_when_asked(self):
        self.write('a.py', GOOD, 'batch_a')
        result = install(self.manager, [str(self.source)], enable=True)
        self.assertTrue(result['results'][0].get('enabled'))
        self.assertTrue(self.manager._plugins['batch_a'].get('enabled'))

    def test_broken_plugin_is_rejected_before_it_reaches_disk(self):
        self.write('bad.py', BROKEN_RUNTIME, 'batch_broken')
        result = install(self.manager, [str(self.source)])
        self.assertEqual(result['installed'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertFalse(result['results'][0]['ok'])
        self.assertTrue(result['results'][0]['error'])
        self.assertEqual(result['rolled_back'], [])
        self.assertFalse((self.plugins_dir / 'batch_broken.py').exists())
        self.assertNotIn('batch_broken', self.manager._plugins)

    def test_overwrite_restores_previous_file_on_failure(self):
        (self.plugins_dir / 'batch_broken.py').write_text('# original', 'utf-8')
        self.write('bad.py', BROKEN_RUNTIME, 'batch_broken')
        install(self.manager, [str(self.source)], overwrite=True)
        self.assertEqual((self.plugins_dir / 'batch_broken.py').read_text('utf-8'), '# original')

    def test_empty_input_is_rejected(self):
        with self.assertRaises(InstallError):
            install(self.manager, [])


if __name__ == '__main__':
    unittest.main()
