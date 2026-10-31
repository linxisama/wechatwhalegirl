# -*- coding: utf-8 -*-
"""发送键的像素闸门：唯一绿色圆角块 + 块内两字白簇（OCR 只做加分确认）。

为什么不再以 OCR 为准：真机（125% 缩放、1006x596 渲染区）把 send_region 放大 4x/8x、
NEAREST/LANCZOS、反相、掩膜都读不出绿底白字的「发送」，而同一个 OCR 在标题区 4x
NEAREST 能正常读出会话名——引擎是好的，前提「两份并排必须读出『发送』」在本机不成立。
所以改成形状结构取证，OCR 降级为不阻塞的加分确认。

四条证据同时成立才返回按钮（任一不成立都返回空列表，调用方 fail-closed 不点）：
    1) 扫描窗内恰好 1 个绿色块（尺寸/填充率/长宽比过滤）；
    2) 该块严格落在扫描窗内部（贴边说明被裁，露出部分不足以证明是完整按钮）；
    3) 块内缩区的白像素 4-连通簇恰好 2 个「主簇」，其余都是碎片；
    4) 两枚主簇的高度、基线、间距、整体跨度、上下留白、墨密度都在真机实测比例内。
"""
from PIL import Image

# 工具栏分隔线之下才是 send_region 的上边，而饱和绿的发送键可以压着分隔线：真机实测
# 按钮实占 y=549..574、send_region 上边是 550，顶行被裁掉。上探几行才能拿到完整按钮；
# 上探后仍然贴边，说明还被别的东西裁着，直接判失败。
SCAN_TOP_MARGIN = 6
# 硬二值化阈值：真机实测 150 才能把「发」「送」分成两簇（80 会连成一整块，120 碎片变多）。
# 与标题 OCR 用的同一条实测阈值保持一致。
GLYPH_THRESHOLD = 150
SEND_LABEL = '发送'
# 绿色块过滤（沿用真机实测值：48x25、填充率 0.838、长宽比 1.92）。
MIN_BLOCK_W, MAX_BLOCK_W = 30, 120
MIN_BLOCK_H, MAX_BLOCK_H = 15, 55
MIN_BLOCK_FILL = .55
MIN_BLOCK_RATIO, MAX_BLOCK_RATIO = 1.2, 4.5


def _window(frame, region):
    """send_region 上探 SCAN_TOP_MARGIN 行，得到真正用于找按钮的扫描窗。"""
    try:
        x0, y0, x1, y1 = (int(value) for value in region)
    except (TypeError, ValueError):
        return None
    y0 -= SCAN_TOP_MARGIN
    if not (0 <= x0 < x1 <= frame.width and 0 <= y0 < y1 <= frame.height):
        return None
    return (x0, y0, x1, y1)


def _green_blocks(crop):
    """4-连通求绿色块，返回 [(left, top, right, bottom, fill)]。"""
    pixels = crop.load()
    remaining = {(x, y) for y in range(crop.height) for x in range(crop.width)
                 if pixels[x, y][0] < 100 and pixels[x, y][1] > 140 and pixels[x, y][2] < 180}
    blocks = []
    while remaining:
        seed = remaining.pop()
        points = [seed]
        stack = [seed]
        while stack:
            x, y = stack.pop()
            for near in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if near in remaining:
                    remaining.remove(near)
                    stack.append(near)
                    points.append(near)
        left, top = min(x for x, y in points), min(y for x, y in points)
        right, bottom = max(x for x, y in points) + 1, max(y for x, y in points) + 1
        width, height = right - left, bottom - top
        if (MIN_BLOCK_W <= width <= MAX_BLOCK_W and MIN_BLOCK_H <= height <= MAX_BLOCK_H
                and MIN_BLOCK_RATIO <= width / float(height) <= MAX_BLOCK_RATIO
                and len(points) >= width * height * MIN_BLOCK_FILL):
            blocks.append((left, top, right, bottom, len(points) / float(width * height)))
    return blocks


def _clusters(ink):
    """白像素 4-连通分簇；两字之间没有空列，只能按簇切，不能按列投影切。"""
    remaining = set(ink)
    pieces = []
    while remaining:
        seed = remaining.pop()
        pixels = [seed]
        stack = [seed]
        while stack:
            x, y = stack.pop()
            for near in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if near in remaining:
                    remaining.remove(near)
                    stack.append(near)
                    pixels.append(near)
        xs = [x for x, _ in pixels]
        ys = [y for _, y in pixels]
        pieces.append({'size': len(pixels), 'left': min(xs), 'top': min(ys),
                       'width': max(xs) - min(xs) + 1, 'height': max(ys) - min(ys) + 1})
    return pieces


def _ocr_bonus(inset_w, inset_h, ink, recognize):
    """加分确认：把观测到的白像素排成两份并排喂给 OCR。读不出/读错都不影响结论。"""
    try:
        mask = Image.new('L', (inset_w, inset_h), 255)
        mask.putdata([0 if (x, y) in ink else 255
                      for y in range(inset_h) for x in range(inset_w)])
        strip = Image.new('L', ((mask.width + 10) * 2 + 10, mask.height + 20), 255)
        for index in range(2):
            strip.paste(mask, (10 + index * (mask.width + 10), 10))
        with strip.resize((strip.width * 2, strip.height * 2), Image.Resampling.BICUBIC) as enlarged:
            rows = recognize(enlarged)
        mask.close()
        strip.close()
        if len(rows) != 2:
            return 'other'
        joined = ''.join(str(row[0]).replace(' ', '') for row in sorted(rows, key=lambda row: row[1]))
        return 'send' if joined == SEND_LABEL else 'other'
    except Exception:
        # OCR 只是加分项：引擎读不出、抛异常都不改变「结构证据成立」的结论。
        return 'error'


def explain_send_button(frame, region, recognize=None):
    """返回完整证据字典（ok/reason/button/glyphs/ink/ocr），供调用方写诊断日志。"""
    result = {'ok': False, 'reason': 'no_evidence', 'button': None, 'glyphs': [],
              'ink': 0, 'ocr': 'skipped', 'green_blocks': 0, 'scan_window': None}
    window = _window(frame, region)
    if window is None:
        result['reason'] = 'region_out_of_frame'
        return result
    result['scan_window'] = list(window)
    with frame.crop(window).convert('RGB') as crop:
        blocks = _green_blocks(crop)
        result['green_blocks'] = len(blocks)
        if len(blocks) != 1:
            result['reason'] = 'no_green_block' if not blocks else 'multiple_green_blocks'
            return result
        left, top, right, bottom, fill = blocks[0]
        result['block'] = [window[0] + left, window[1] + top, right - left, bottom - top]
        result['fill'] = round(fill, 3)
        if left < 1 or top < 1 or right > crop.width - 1 or bottom > crop.height - 1:
            result['reason'] = 'green_block_clipped'
            return result
        width, height = right - left, bottom - top
        inset_x = max(4, round(width * .16))
        inset_y = max(2, round(height * .12))
        inset_w, inset_h = width - 2 * inset_x, height - 2 * inset_y
        if inset_w < 8 or inset_h < 6:
            result['reason'] = 'glyph_inset_too_small'
            return result
        with crop.crop((left + inset_x, top + inset_y, right - inset_x, bottom - inset_y)) as glyphs:
            data = list(glyphs.getdata())
    ink = {(index % inset_w, index // inset_w) for index, pixel in enumerate(data)
           if min(pixel) > GLYPH_THRESHOLD}
    pieces = _clusters(ink)
    ink_total = sum(piece['size'] for piece in pieces)
    result['ink'] = ink_total
    if not ink_total:
        # 禁用态是灰底灰字：这里兜底，绝不把「没有字形」当成按钮可用。
        result['reason'] = 'no_glyph_ink'
        return result
    floor = max(12, int(round(ink_total * .15)))
    majors = sorted((piece for piece in pieces if piece['size'] >= floor),
                    key=lambda piece: piece['left'])
    result['glyphs'] = majors
    if len(majors) != 2:
        result['reason'] = 'glyphs_not_two'
        return result
    first, second = majors
    hmax = max(first['height'], second['height'])
    gap = second['left'] - (first['left'] + first['width'])
    span = second['left'] + second['width'] - first['left']
    if (first['left'] + first['width'] > second['left'] + 1
            or gap > max(2, round(hmax * .5))
            or min(first['width'], second['width']) < round(hmax * .35)
            or abs(first['height'] - second['height']) > max(2, round(hmax * .3))
            or abs(first['top'] - second['top']) > max(2, round(hmax * .35))
            or not .35 * inset_w <= span <= .95 * inset_w):
        result['reason'] = 'glyph_geometry_invalid'
        return result
    if first['size'] + second['size'] < .55 * ink_total:
        result['reason'] = 'glyph_noise_dominated'
        return result
    ink_top = min(first['top'], second['top'])
    ink_bottom = max(first['top'] + first['height'], second['top'] + second['height'])
    density = (first['size'] + second['size']) / float(inset_w * inset_h)
    if (abs(ink_top - (inset_h - ink_bottom)) > max(3, round(inset_h * .35))
            or not .06 <= density <= .45):
        result['reason'] = 'glyph_centering_invalid'
        return result
    if callable(recognize):
        result['ocr'] = _ocr_bonus(inset_w, inset_h, ink, recognize)
    result.update(ok=True, reason='unique_green_block_with_two_glyph_clusters',
                  button=[window[0] + left, window[1] + top, width, height])
    return result


def locate_send_button(frame, region, recognize=None):
    """唯一按钮的 (label, x, y, w, h) 列表；x/y 是绿色块自身左上角（帧坐标）。"""
    evidence = explain_send_button(frame, region, recognize)
    if not evidence.get('ok'):
        return []
    return [tuple([SEND_LABEL] + list(evidence['button']))]
