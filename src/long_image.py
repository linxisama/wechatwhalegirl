"""长截图按阅读顺序切片；限制解码面积和请求数量，避免阻塞聊天。"""
import base64
import math


def plan_tiles(width, height, maximum=3):
    if width < 1 or height < 1 or width * height > 32_000_000:
        raise ValueError('图片尺寸超限')
    maximum = max(2, min(3, int(maximum)))
    scale = min(1, 1200 / width)
    crop, overlap = math.floor(1800 / scale), math.ceil(120 / scale)
    result, top = [], 0
    while top < height and len(result) < maximum:
        size = min(crop, height - top)
        result.append((top, size))
        if top + size >= height:
            break
        top += crop - overlap
    return result, result[-1][0] + result[-1][1] < height


def split_long_image(source, maximum=3):
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PyQt6.QtGui import QImageReader

    raw = base64.b64decode(source.split(',', 1)[1], validate=True)
    buf = QBuffer()
    buf.setData(QByteArray(raw))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buf)
    size = reader.size()
    width, height = size.width(), size.height()
    plan, partial = plan_tiles(width, height, maximum)
    if height <= 2048 or height / width < 3:
        return None
    picture = reader.read()
    if picture.isNull():
        raise ValueError('长图无法解码')
    parts, total = [], 0
    for top, height in plan:
        tile = picture.copy(0, top, width, height)
        if width > 1200:
            tile = tile.scaledToWidth(1200, Qt.TransformationMode.SmoothTransformation)
        out = QBuffer()
        out.open(QIODevice.OpenModeFlag.WriteOnly)
        if not tile.save(out, 'JPEG', 85):
            raise ValueError('长图分段编码失败')
        data = bytes(out.data())
        total += len(data)
        if total > 6 * 1024 * 1024:
            partial = True
            break
        parts.append('data:image/jpeg;base64,' + base64.b64encode(data).decode('ascii'))
    if not parts:
        raise ValueError('长图分段体积超限')
    return parts, partial
