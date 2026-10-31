"""识图清晰度：送模型的长边可配置（默认 2048），动图抽帧同尺寸，GIF 上限放宽到 12MB。"""
import base64
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from src.image_input import prepare_vision_source
from src.plugins.manager import PluginManager
from src.sticker_library import frames_for_vision


def _png(width, height, color='white'):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), color).save(buffer, 'PNG')
    return buffer.getvalue()


def _size_of(data_url):
    return Image.open(io.BytesIO(base64.b64decode(data_url.split(',', 1)[1]))).size


class VisionSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.plugin = self.manager._plugins['image_understanding']

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_side_is_2048_so_screenshots_stay_sharp(self):
        self.assertEqual(self.plugin.get('visionMaxSide'), 2048)
        self.assertEqual(self.plugin._max_side(), 2048)

    def test_large_side_keeps_full_resolution(self):
        source = 'data:image/png;base64,' + base64.b64encode(_png(1600, 1200)).decode()
        # 2048 上限下 1600 宽的图不再被缩小
        self.assertEqual(_size_of(prepare_vision_source(source, 2048)), (1600, 1200))
        # 1024 上限下才缩
        self.assertEqual(_size_of(prepare_vision_source(source, 1024)), (1024, 768))

    def test_frames_use_the_same_side(self):
        frames_img = []
        for index in range(3):
            buffer = io.BytesIO()
            Image.new('RGB', (1200, 900), ('red', 'green', 'blue')[index]).save(buffer, 'PNG')
            frames_img.append(Image.open(io.BytesIO(buffer.getvalue())))
        gif = io.BytesIO()
        frames_img[0].save(gif, 'GIF', save_all=True, append_images=frames_img[1:], duration=200, loop=0)
        small = frames_for_vision(gif.getvalue(), 2, 512)
        big = frames_for_vision(gif.getvalue(), 2, 2048)
        self.assertEqual(len(small), 2)
        self.assertLessEqual(max(_size_of(item)[0] for item in small), 512)
        self.assertGreater(max(_size_of(item)[0] for item in big), 512)

    def test_out_of_range_values_are_refused_and_stale_values_are_clamped(self):
        for bad in (99999, 1, -5):
            with self.assertRaises(ValueError):
                self.manager.update_settings('image_understanding', {'visionMaxSide': bad})
        # 老配置里可能躺着越界值：读取时仍要夹住，不能让 0/99999 直接送模型
        self.manager._settings['image_understanding']['visionMaxSide'] = 99999
        self.assertEqual(self.plugin._max_side(), 4096)
        self.manager._settings['image_understanding']['visionMaxSide'] = 0
        self.assertEqual(self.plugin._max_side(), 2048)

    def test_animated_images_are_not_dropped_by_the_old_6mb_cap(self):
        from src.plugins.image_understanding import MAX_IMAGE_BYTES
        self.assertGreater(MAX_IMAGE_BYTES, 6 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
