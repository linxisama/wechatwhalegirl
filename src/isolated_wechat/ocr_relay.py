"""隔离桌面的本机 OCR：仅交换签名的小区域像素，不执行输入或保存识别正文。"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import threading
import time
import uuid
from pathlib import Path

from ..runtime_identity import APP_VERSION
from .protocol import (ProtocolError, checked_path, live_service, load_pairing,
                       read_document, valid_epoch, write_document)
from .ocr_slots import publish_ocr_document

MAX_PIXELS_BYTES = 16 * 1024 * 1024
MAX_DIMENSION = 4096


def dimensions(width, height):
    if (type(width) is not int or type(height) is not int or
            not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION or
            width * height * 4 > MAX_PIXELS_BYTES):
        raise ProtocolError('OCR 区域超过限制')
    return width, height


def line_from_words(words, width, height, *, angle=None):
    """按真实词框重组一条 OCR 行；不用引擎自动插入的 line.text 空格。"""
    dimensions(width, height)
    try:
        if angle is None:
            angle = 0.0
        if type(angle) not in (int, float) or not math.isfinite(angle):
            return None
        angle = (angle + 180) % 360 - 180
        if abs(angle) > 15:
            return None  # 当前通道只识别水平窗口标签，旋转过大的证据不可沿用。
        words = list(words)
        if not 1 <= len(words) <= 512:
            return None
        measured = []
        for word in words:
            text = word.text
            if (not isinstance(text, str) or not text.strip() or text != text.strip() or len(text) > 512
                    or any(character in text for character in '\r\n\x00')):
                return None
            rect = word.bounding_rect
            values = (rect.x, rect.y, rect.width, rect.height)
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
                return None
            x, y, w, h = values
            if not (0 <= x < x + w <= width and 0 <= y < y + h <= height):
                return None
            # 只处理框之间的分词；词自身的空格不得删除，边缘空格含义不明则拒绝整行。
            measured.append((text, x, y, w, h))
        measured.sort(key=lambda row: row[1])
        parts, previous = [], None
        for text, x, y, w, h in measured:
            if previous is not None:
                _, old_x, old_y, old_w, old_h = previous
                gap = x - old_x - old_w
                if (x <= old_x or x + w <= old_x + old_w
                        or min(y + h, old_y + old_h) <= max(y, old_y)):
                    return None
                # WinRT 放大识别的字框会略向外扩（实测 54px 字框重叠 3px）。
                # 只容纳双边界仍依次递增、且相对字高和字宽都很小的外扩；
                # 不能把窄框、大量重叠或包含关系猜成可信文字顺序。
                if gap < 0 and (-gap > max(2, min(h, old_h) * .08)
                                or -gap > min(w, old_w) * .08):
                    return None
                if gap >= max(3, min(h, old_h) * .25):
                    parts.append(' ')
            parts.append(text)
            previous = (text, x, y, w, h)
        label = ''.join(parts)
        if not label or len(label) > 512:
            return None
        # WinRT 的词框位于校正后的坐标系。按官方约定绕图像中心顺时针
        # 转回原图，不能直接用未旋转的框二次裁剪标题，否则会裁掉字形。
        radians = math.radians(angle)
        cosine, sine = math.cos(radians), math.sin(radians)
        corners = []
        for _, x, y, w, h in measured:
            for a, b in ((x,y), (x+w,y), (x,y+h), (x+w,y+h)):
                dx, dy = a - width / 2, b - height / 2
                corners.append((width / 2 + cosine * dx - sine * dy,
                                height / 2 + sine * dx + cosine * dy))
        if any(not -1e-6 <= x <= width+1e-6 or not -1e-6 <= y <= height+1e-6 for x,y in corners):
            return None
        left = max(0, math.floor(min(x for x,y in corners) + 1e-7))
        top = max(0, math.floor(min(y for x,y in corners) + 1e-7))
        right = min(width, math.ceil(max(x for x,y in corners) - 1e-7))
        bottom = min(height, math.ceil(max(y for x,y in corners) - 1e-7))
        return [label, left, top, right - left, bottom - top]
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


async def recognize(pixels, width, height):
    """WinRT 全内存识别；完成状态轮询保证 watchdog 可以取消。"""
    dimensions(width, height)
    if len(pixels) != width * height * 4:
        raise ProtocolError('OCR 像素长度不匹配')
    from winsdk.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage.streams import DataWriter
    engine = None
    for language in OcrEngine.available_recognizer_languages or []:
        if str(language.language_tag).lower().startswith('zh'):
            engine = OcrEngine.try_create_from_language(language)
            if engine is not None:
                break
    if engine is None:
        engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None or max(width, height) > OcrEngine.max_image_dimension:
        raise ProtocolError('本机 OCR 语言不可用')
    writer, bitmap, operation = DataWriter(), None, None
    try:
        writer.write_bytes(pixels)
        bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(),
            BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.IGNORE)
        operation = engine.recognize_async(bitmap)
        while int(operation.status) == 0:
            await asyncio.sleep(.01)
        recognized = operation.get_results()
        rows = []
        for line in recognized.lines or []:
            row = line_from_words(line.words or [], width, height,
                                  angle=getattr(recognized, 'text_angle', None))
            if row is not None:
                rows.append(row)
        return rows[:128]
    finally:
        if operation is not None and int(operation.status) == 0:
            operation.cancel()
        if bitmap is not None:
            bitmap.close()
        writer.close()


class LocalOCR:
    @staticmethod
    def available():
        try:
            from winsdk.windows.media.ocr import OcrEngine
            return bool(OcrEngine.available_recognizer_languages)
        except Exception:
            return False

    @staticmethod
    def recognize(image):
        dimensions(image.width, image.height)
        with image.convert('RGBA') as rgba:
            pixels = rgba.tobytes('raw', 'BGRA')
        return asyncio.run(asyncio.wait_for(recognize(pixels, image.width, image.height), 5))


def _folder(pairing):
    folder = checked_path(pairing.root / 'ocr', pairing)
    folder.mkdir(exist_ok=True)
    return checked_path(folder, pairing)


def _clean(path, pairing):
    try:
        checked_path(path, pairing).unlink(missing_ok=True)
    except (OSError, ProtocolError):
        pass


class HostOCRRelay:
    def __init__(self, channel):
        self.pairing = load_pairing(channel)
        self.folder = _folder(self.pairing)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._serve, daemon=True, name='sandbox-ocr')
            self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=6)
        # 删除会再次污染 Sandbox 的缺名缓存。保留最后一份签名心跳，
        # 原八秒新鲜度仍会将离线中继判为不可用；没有中继就不会产生 OCR 回执。

    def _serve(self):
        available = LocalOCR.available()
        while not self._stop.is_set():
            try:
                service = live_service(self.pairing)
                epoch = service['epoch']
                publish_ocr_document(self.folder / 'relay.json', self.pairing,
                    {'epoch':epoch, 'app_version':APP_VERSION, 'heartbeat':time.time(), 'available':available})
                for request in list(self.folder.glob('*.request.json'))[:8]:
                    if self._stop.is_set():
                        break
                    self._one(request, epoch, available)
            except (OSError, ProtocolError):
                pass  # 缺失客体仅是离线；不切前台、不重启微信。
            self._stop.wait(.04)

    def _one(self, request, epoch, available):
        identifier = request.name.removesuffix('.request.json')
        if not valid_epoch(identifier):
            return
        pixels_path = self.folder / (identifier + '.bgra')
        response_path = self.folder / (identifier + '.result.json')
        response = {'request_id':identifier, 'epoch':epoch, 'ok':False, 'rows':[]}
        try:
            job = read_document(request, self.pairing, 8192)
            now = time.time()
            if (not available or job.get('epoch') != epoch or job.get('request_id') != identifier
                    or job.get('pixel_file') != pixels_path.name or job.get('app_version') != APP_VERSION
                    or type(job.get('deadline')) not in (int, float)
                    or not now <= job['deadline'] <= now + 7):
                raise ProtocolError('OCR 请求过期或配对不符')
            width, height = dimensions(job.get('width'), job.get('height'))
            with checked_path(pixels_path, self.pairing).open('rb') as stream:
                pixels = stream.read(MAX_PIXELS_BYTES + 1)
            if (len(pixels) != width * height * 4 or
                    hashlib.sha256(pixels).hexdigest() != job.get('pixel_sha256')):
                raise ProtocolError('OCR 像素校验失败')
            remaining = min(5, job['deadline']-time.time())
            if remaining <= 0:
                raise ProtocolError('OCR 请求已过期')
            rows = asyncio.run(asyncio.wait_for(recognize(pixels, width, height), remaining))
            if not self._stop.is_set() and time.time() <= job['deadline']:
                response.update(ok=True, rows=rows)
        except Exception:
            pass  # 不能把底层异常正文或像素写入日志。
        finally:
            try:
                publish_ocr_document(response_path, self.pairing, response)
            except (OSError, ProtocolError):
                pass
            _clean(request, self.pairing)
            _clean(pixels_path, self.pairing)


class GuestOCR:
    def __init__(self, channel, *, _guest_check=True):
        if _guest_check:
            self._require_guest()
        self.pairing = load_pairing(channel)
        self.folder = _folder(self.pairing)

    @staticmethod
    def _require_guest():
        if os.name != 'nt':
            raise ProtocolError('隔离 OCR 仅支持 Windows 沙盒')
        import ctypes
        from ctypes import wintypes
        name, size = ctypes.create_unicode_buffer(512), wintypes.DWORD(512)
        if (not ctypes.windll.advapi32.GetUserNameW(name, ctypes.byref(size))
                or name.value != 'WDAGUtilityAccount'):
            raise ProtocolError('隔离 OCR 仅允许沙盒客体用户')

    def _ready(self):
        service = live_service(self.pairing)
        relay = read_document(self.folder/'relay.json', self.pairing, 8192)
        if (relay.get('epoch') != service['epoch'] or relay.get('app_version') != APP_VERSION
                or relay.get('available') is not True or type(relay.get('heartbeat')) not in (int, float)
                or not -2 <= time.time()-relay['heartbeat'] <= 8):
            raise ProtocolError('本机 OCR 中继未就绪')
        return service['epoch']

    def available(self):
        try:
            self._ready()
            return True
        except (OSError, ProtocolError):
            return False

    def recognize(self, image):
        epoch = self._ready()
        width, height = dimensions(image.width, image.height)
        identifier = uuid.uuid4().hex
        pixels_path = checked_path(self.folder/(identifier+'.bgra'), self.pairing)
        request = self.folder/(identifier+'.request.json')
        response = self.folder/(identifier+'.result.json')
        try:
            with image.convert('RGBA') as rgba:
                pixels = rgba.tobytes('raw', 'BGRA')
            with pixels_path.open('xb') as stream:
                stream.write(pixels)
            # 预建响应槽避免共享目录对不存在文件的数秒负缓存。
            write_document(response, self.pairing,
                {'epoch':epoch, 'request_id':identifier, 'pending':True}, exclusive=True)
            read_deadline = time.monotonic()+6
            deadline = time.time()+6
            write_document(request, self.pairing, {'epoch':epoch, 'request_id':identifier,
                'app_version':APP_VERSION, 'width':width, 'height':height,
                'pixel_file':pixels_path.name, 'pixel_sha256':hashlib.sha256(pixels).hexdigest(),
                'deadline':deadline}, exclusive=True)
            while time.monotonic() < read_deadline:
                # pending→结果的替换可短暂缺名/共享冲突；同次只读恢复仍受原六秒
                # 预算限制，不能因50ms恢复窗而延长请求或重新投递OCR。
                reply = read_document(response, self.pairing, 256*1024,
                                      retry_deadline=read_deadline)
                if reply.get('epoch') != epoch or reply.get('request_id') != identifier:
                    raise ProtocolError('OCR 回执身份不符')
                if reply.get('pending') is not True:
                    if reply.get('ok') is not True:
                        raise ProtocolError('本机 OCR 请求未成功')
                    rows = reply.get('rows')
                    if not isinstance(rows, list) or len(rows)>128:
                        raise ProtocolError('OCR 回执格式无效')
                    for row in rows:
                        if (not isinstance(row, list) or len(row)!=5 or not isinstance(row[0], str)
                                or len(row[0])>512 or any(type(v) is not int for v in row[1:])
                                or min(row[1:3])<0 or min(row[3:])<=0
                                or row[1]+row[3]>width or row[2]+row[4]>height):
                            raise ProtocolError('OCR 位置数据无效')
                    return [tuple(row) for row in rows]
                time.sleep(min(.025, max(0, read_deadline-time.monotonic())))
            raise TimeoutError('本机 OCR 请求超时')
        finally:
            for path in (pixels_path, request, response):
                _clean(path, self.pairing)
