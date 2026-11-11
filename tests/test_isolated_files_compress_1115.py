"""隔离通道大图压缩的离线验收：假微信与临时目录，不发送真实消息。

1.3.113 把「太大」当结论直接拒绝；1.3.115 起先在宿主按预算压一版再判。
这里锁住六件事：预算怎么算、压出来的确实是 JPEG、小图一个字节都不动、
动图与非图片照旧拒绝、整批每张都让出份额、缺 Pillow 时退回原判据。
"""
import base64
import os
import sys
import tempfile
import unittest
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from src.isolated_wechat import host_bridge
from src.isolated_wechat.host_bridge import (_compressed_jpeg, _files_payload, _shrink_limit)
from src.isolated_wechat.protocol import MAX_SEND_FILE_BYTES, SEND_FILES_BUDGET
from tests.test_isolated_transport1315 import ChannelCase


@lru_cache(maxsize=8)
def noise_image(side=650, mode="RGB"):
    """随机像素：JPEG/PNG 都压不小，才是「大图」该有的样子。"""
    from PIL import Image
    channels = len(mode)
    return Image.frombytes(mode, (side, side), os.urandom(side * side * channels))


@lru_cache(maxsize=8)
def noise_png(side=650, mode="RGB", **options):
    output = BytesIO()
    noise_image(side, mode).save(output, "PNG", **options)
    return output.getvalue()


def noise_gif(side=80):
    output = BytesIO()
    noise_image(side).convert("P", palette=0).save(output, "GIF")
    return output.getvalue()


class CompressLimitTests(unittest.TestCase):
    def test_single_file_keeps_the_file_cap(self):
        self.assertEqual(_shrink_limit(1), MAX_SEND_FILE_BYTES)
        self.assertEqual(_shrink_limit(0), MAX_SEND_FILE_BYTES)

    def test_batch_shares_the_budget_evenly(self):
        self.assertEqual(_shrink_limit(3), min(MAX_SEND_FILE_BYTES, SEND_FILES_BUDGET // 3))
        self.assertLessEqual(_shrink_limit(6) * 6, SEND_FILES_BUDGET)


class CompressedPayloadTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))

    def write(self, name, data):
        path = self.folder / name
        path.write_bytes(data)
        return str(path)

    def big_png(self):
        data = noise_png()
        self.assertGreater(len(data), MAX_SEND_FILE_BYTES)
        return data

    def test_big_photo_is_compressed_into_the_budget(self):
        data = self.big_png()
        source = self.write("照片.png", data)
        result = _files_payload([source], True)
        self.assertTrue(result["ok"], result.get("message"))
        item = result["files"][0]
        self.assertEqual(item["name"], "照片.jpg")
        self.assertTrue(item["image"])
        sent = base64.b64decode(item["data"], validate=True)
        self.assertLessEqual(len(sent), MAX_SEND_FILE_BYTES)
        self.assertLess(len(sent), len(data))
        self.assertTrue(sent.startswith(b"\xff\xd8"))
        self.assertEqual(Path(source).read_bytes(), data)

    def test_compressed_bytes_really_open_as_jpeg(self):
        from PIL import Image
        blob = _compressed_jpeg(self.big_png(), 400 * 1024)
        self.assertTrue(blob)
        self.assertLessEqual(len(blob), 400 * 1024)
        with Image.open(BytesIO(blob)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertLessEqual(max(image.size), 1600)

    def test_transparent_png_lands_on_white_not_black(self):
        from PIL import Image
        alpha = Image.new("L", (400, 400), 0)
        transparent = noise_image(400, "RGB")
        transparent.putalpha(alpha)
        output = BytesIO()
        transparent.save(output, "PNG")
        source = self.write("透明.png", output.getvalue())
        with patch.object(host_bridge, "MAX_SEND_FILE_BYTES", 20000), \
             patch.object(host_bridge, "SEND_FILES_BUDGET", 20000):
            result = _files_payload([source], True)
        self.assertTrue(result["ok"], result.get("message"))
        sent = base64.b64decode(result["files"][0]["data"], validate=True)
        with Image.open(BytesIO(sent)) as image:
            corner = image.convert("RGB").getpixel((0, 0))
        self.assertGreaterEqual(min(corner), 240, corner)

    def test_small_image_is_sent_byte_for_byte(self):
        data = noise_png(60)
        result = _files_payload([self.write("小.png", data)], True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["files"][0]["name"], "小.png")
        self.assertEqual(base64.b64decode(result["files"][0]["data"], validate=True), data)

    def test_animation_is_still_refused(self):
        gif = noise_gif()
        source = self.write("动图.gif", gif)
        with patch.object(host_bridge, "MAX_SEND_FILE_BYTES", 2000), \
             patch.object(host_bridge, "SEND_FILES_BUDGET", 2000):
            result = _files_payload([source], True)
        self.assertFalse(result["ok"])
        self.assertIn("动图.gif", result["message"])

    def test_plain_file_over_limit_is_still_refused(self):
        source = self.write("压缩包.bin", b"0" * (MAX_SEND_FILE_BYTES + 1))
        result = _files_payload([source], False)
        self.assertFalse(result["ok"])
        self.assertIn("1MB", result["message"])

    def test_corrupt_image_falls_back_to_the_refusal(self):
        source = self.write("坏图.png", b"\x89PNG\r\n\x1a\n" + b"garbage" * 200000)
        result = _files_payload([source], True)
        self.assertFalse(result["ok"])
        self.assertIn("本次未发送", result["message"])

    def test_batch_of_big_photos_fits_the_budget(self):
        paths = [self.write("图%d.png" % index, self.big_png()) for index in range(3)]
        result = _files_payload(paths, True)
        self.assertTrue(result["ok"], result.get("message"))
        sizes = [len(base64.b64decode(item["data"], validate=True)) for item in result["files"]]
        self.assertEqual(len(sizes), 3)
        self.assertLessEqual(max(sizes), SEND_FILES_BUDGET // 3)
        self.assertLessEqual(sum(sizes), SEND_FILES_BUDGET)
        self.assertTrue(all(item["name"].endswith(".jpg") for item in result["files"]))

    def test_missing_pillow_falls_back_to_the_old_refusal(self):
        source = self.write("照片.png", self.big_png())
        with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None, "PIL.ImageOps": None}):
            result = _files_payload([source], True)
        self.assertFalse(result["ok"])
        self.assertIn("1MB", result["message"])


class HostCompressedSendTests(ChannelCase):
    def test_send_files_carries_the_compressed_bytes(self):
        from src.isolated_wechat.host_bridge import SandboxBridge
        service = self.service()
        service.heartbeat()
        host = SandboxBridge(self.root / "host-data", self.channel)
        host.online = True
        host._control = Mock()
        path = self.root / "大照片.png"
        path.write_bytes(noise_png())
        with patch.object(host, "_rpc", return_value={"ok": True, "message": "已提交",
                                                     "status": {"online": True}}) as rpc:
            outcome = host.send_files("wxid_friend", [str(path)], "朋友", True)
        self.assertTrue(outcome[0])
        args = rpc.call_args.args[1]
        item = args["files"][0]
        sent = base64.b64decode(item["data"], validate=True)
        self.assertTrue(item["name"].endswith(".jpg"))
        self.assertLessEqual(len(sent), MAX_SEND_FILE_BYTES)
        self.assertTrue(args["as_image"])


if __name__ == "__main__":
    unittest.main()
