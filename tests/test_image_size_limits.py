"""图片上限必须统一：12MB 的图不能在"保存阶段"被 6MB 的旧限制拒掉。

交接指南 3.1 的复现项：插件允许 12MB，但 ReceivedImageStore 依赖的
sticker_library 仍是 6MB，于是 6~12MB 的图"取图通过、保存失败"，表现为识别失败。
"""
import io
import unittest

from PIL import Image

from src.plugins import image_understanding
from src.sticker_library import MAX_BYTES, inspect_image
from src.received_images import MAX_BYTES as STORE_MAX


def png_bytes(side=800):
    """生成一张合法 PNG；用随机噪声让体积可控（纯色会被压得很小）。"""
    import os
    image = Image.frombytes('RGB', (side, side), os.urandom(side * side * 3))
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


class LimitTests(unittest.TestCase):
    def test_single_source_of_truth(self):
        self.assertEqual(MAX_BYTES, image_understanding.MAX_IMAGE_BYTES)
        self.assertEqual(MAX_BYTES, STORE_MAX)
        self.assertEqual(MAX_BYTES, 12 * 1024 * 1024)

    def test_7mb_image_is_accepted(self):
        data = png_bytes(1900)
        self.assertGreater(len(data), 6 * 1024 * 1024, '样本没到 7MB，测试无意义')
        self.assertLess(len(data), MAX_BYTES)
        info = inspect_image(data)          # 以前这里会抛"图片超过 6MB"
        self.assertTrue(info.get('ext'))

    def test_oversize_image_is_still_rejected(self):
        with self.assertRaises(ValueError):
            inspect_image(b'\x00' * (MAX_BYTES + 1))

    def test_pixel_limit_still_enforced(self):
        # 小字节但超大像素（例如 20000×20000 的纯色 PNG）仍要拦住
        image = Image.new('RGB', (5000, 4000), 'white')
        buffer = io.BytesIO()
        image.save(buffer, 'PNG')
        data = buffer.getvalue()
        if len(data) > MAX_BYTES:
            self.skipTest('样本超过字节上限，跳过像素维度用例')
        with self.assertRaises(ValueError):
            inspect_image(data)


if __name__ == '__main__':
    unittest.main()
