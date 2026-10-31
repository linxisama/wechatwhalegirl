"""首次启动套用内置浅色背景；用户设过/清除过就不动。"""
import tempfile
import unittest
from pathlib import Path

from src.gui.config import apply_default_background, load_gui_config, save_gui_config


class DefaultBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bundle = root / 'bundle'
        (self.bundle / 'assets' / 'backgrounds').mkdir(parents=True)
        (self.bundle / 'assets' / 'backgrounds' / 'mio_light.png').write_bytes(b'\x89PNG fake')
        self.data = root / 'data'

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_install_gets_the_bundled_background(self):
        config = apply_default_background({'background_image': ''}, self.bundle, self.data)
        target = Path(config['background_image'])
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b'\x89PNG fake')
        self.assertTrue(str(target).startswith(str(self.data)))

    def test_user_background_is_never_overwritten(self):
        config = apply_default_background({'background_image': 'C:/mine.png'}, self.bundle, self.data)
        self.assertEqual(config['background_image'], 'C:/mine.png')
        self.assertFalse((self.data / 'backgrounds' / 'mio_light.png').exists())

    def test_opt_out_is_respected(self):
        config = apply_default_background({'background_image': '', 'default_background': False},
                                          self.bundle, self.data)
        self.assertEqual(config['background_image'], '')
        self.assertFalse((self.data / 'backgrounds' / 'mio_light.png').exists())

    def test_missing_asset_is_not_fatal(self):
        empty = Path(self.tmp.name) / 'empty'
        empty.mkdir()
        config = apply_default_background({'background_image': ''}, empty, self.data)
        self.assertEqual(config['background_image'], '')

    def test_flag_survives_round_trip(self):
        path = Path(self.tmp.name) / 'gui.json'
        save_gui_config(path, {'background_image': '', 'default_background': False})
        self.assertFalse(load_gui_config(path)['default_background'])

    def test_default_is_on(self):
        path = Path(self.tmp.name) / 'gui2.json'
        save_gui_config(path, {'background_image': ''})
        self.assertTrue(load_gui_config(path)['default_background'])


if __name__ == '__main__':
    unittest.main()
