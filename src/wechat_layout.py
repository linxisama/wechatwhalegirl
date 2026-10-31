"""Pure-Pillow layout evidence. No capture, OCR, files, UI calls or text output.

inspect_layout(image) returns detected long edges and closed input-frame candidates.
Its box is populated only for one unambiguous candidate with all four pixel edges.
draft_pixels(image, box) samples strictly inside that rectangle, never y0+200.
All rectangles use image-relative, right/bottom-exclusive pixel coordinates.
"""
from __future__ import annotations

from .wechat_pixel_masks import mark_mask


def _light_edge(first, second):
    return (min(sum(first), sum(second)) >= 420
            and max(max(first) - min(first), max(second) - min(second)) <= 45
            and 3 <= max(abs(a - b) for a, b in zip(first, second)) <= 125)


def _edge_at(before, center, after):
    # 同色三点的所有通道差都为 0，必低于原边线阈值 3；白底不必重复生成器运算。
    if before == center == after:
        return False
    # A thin outline can have the same fill on both sides. Include its center
    # rather than looking only for a color transition across the whole border.
    return (_light_edge(before, after) or _light_edge(before, center)
            or _light_edge(center, after))


def _runs(points, step=2, gap=6):
    if not points:
        return []
    groups, current = [], [points[0]]
    for point in points[1:]:
        if point - current[-1] <= gap:
            current.append(point)
        else:
            groups.append(current)
            current = [point]
    groups.append(current)
    return [(group[0], group[-1] + step, min(1.0, len(group) * step / (group[-1] - group[0] + step)))
            for group in groups]


def _overlap(first, second):
    return max(0, min(first[1], second[1]) - max(first[0], second[0])) / max(
        1, min(first[1] - first[0], second[1] - second[0]))


def _cluster_horizontal(rows):
    result = []
    for row in rows:
        match = next((old for old in reversed(result[-20:]) if row["y"] - old["y"] <= 4
                      and _overlap((row["x0"], row["x1"]), (old["x0"], old["x1"])) >= .85), None)
        if match is None:
            result.append(dict(row, y_min=row["y"], y_max=row["y"]))
        else:
            match["y_max"] = row["y"]
            if (row["x1"] - row["x0"]) * row["support"] > (match["x1"] - match["x0"]) * match["support"]:
                match.update(row)
    return result


def _header_edges(pixels, width, height):
    """测量标题下方贯通到右边缘的横线，保留真实左端点供分隔线交叉核验。"""
    edges = []
    for y in range(max(24, min(40, height // 20)), min(185, height // 4, height - 3)):
        points = [x for x in range(3, width - 3, 2)
                  if _edge_at(pixels[x, y - 2], pixels[x, y], pixels[x, y + 2])]
        for x0, x1, support in _runs(points):
            if (x1 - x0 >= width * .35 and width - x1 <= 12 and support >= .92):
                edges.append({'y': y, 'x0': x0, 'x1': x1, 'support': round(support, 3)})
    return _cluster_horizontal(edges)


def _filter_input_candidates(candidates, separators, width, height, header_edges=()):
    """Keep pixel-closed frames in the bottom of the measured right pane only."""
    strong = sorted((row for row in separators if row["support"] >= .70),
                    key=lambda row: row["support"], reverse=True)
    if len(strong) > 1 and strong[0]["support"] - strong[1]["support"] < .12:
        # 滚动条与面板边界可能都有很强的竖线，不能只因分数略高便择一。
        # 只有恰好一条与已实测的右侧标题横线左端相接，才消除这份歧义。
        anchored = [row for row in strong
                    if any(abs(row['x'] - edge['x0']) <= 6 for edge in header_edges)]
        strong = anchored if len(anchored) == 1 else []
    if not strong:
        return [], None, [{"box": row["box"], "reason": "pane_divider_unconfirmed"}
                          for row in candidates]
    divider = strong[0]
    pane_width = width - divider["x"]
    inset = max(4, round(width * .004))
    maximum_margin = max(28, round(pane_width * .10))
    kept, rejected = [], []
    for candidate in candidates:
        x0, y0, x1, y1 = candidate["box"]
        if x0 < divider["x"] + inset:
            reason = "left_of_chat_pane"
        elif y0 < height * .50 or y1 < height * .85:
            reason = "outside_bottom_input_area"
        elif x1 - x0 < pane_width * .60:
            reason = "too_narrow_for_chat_pane"
        elif x0 - divider["x"] > maximum_margin or width - x1 > maximum_margin:
            reason = "frame_not_aligned_to_chat_pane"
        else:
            kept.append(candidate)
            continue
        rejected.append({"box": candidate["box"], "reason": reason})
    return kept, divider, rejected


def inspect_layout(image):
    """Return geometry/evidence only; unavailable or ambiguous frame => box=None."""
    rgb = image.convert("RGB")
    try:
        width, height = rgb.size
        answer = {"size": [width, height], "box": None, "pixel_confirmed": False,
                  "horizontal_edges": [], "vertical_separators": [], "candidates": [],
                  "reason": "no_closed_frame"}
        if width < 180 or height < 160:
            answer["reason"] = "image_too_small"
            return answer
        pixels = rgb.load()
        minimum_width = max(100, int(width * .18))
        horizontal = []
        for y in range(max(3, int(height * .35)), height - 3):
            points = [x for x in range(max(3, int(width * .10)), width - 3, 2)
                      if _edge_at(pixels[x, y - 2], pixels[x, y], pixels[x, y + 2])]
            for x0, x1, support in _runs(points):
                if x1 - x0 >= minimum_width and support >= .75:
                    horizontal.append({"y": y, "x0": x0, "x1": x1, "support": round(support, 3)})
        horizontal = _cluster_horizontal(horizontal)
        answer["horizontal_edges"] = horizontal[-80:]

        y0, y1 = int(height * .15), int(height * .85)
        separators = []
        # 细分隔线可能只有一个物理像素；按 2px 步长同时采 x±2 会完全
        # 跳过另一奇偶性的边线。窗口缩小或客户区偏移 1px 不能抹掉证据。
        for x in range(max(3, int(width * .10)), max(4, int(width * .60))):
            count = sum(_edge_at(pixels[x - 2, y], pixels[x, y], pixels[x + 2, y])
                        for y in range(y0, y1, 3))
            support = count / max(1, len(range(y0, y1, 3)))
            if support >= .45:
                separators.append({"x": x, "y0": y0, "y1": y1, "support": round(support, 3)})
        for row in sorted(separators, key=lambda item: item["support"], reverse=True):
            if all(abs(row["x"] - old["x"]) > 6 for old in answer["vertical_separators"]):
                answer["vertical_separators"].append(row)
                if len(answer["vertical_separators"]) == 12:
                    break

        def side(expected_x, top, bottom, left):
            # Rounded-corner horizontal edges begin inside the true vertical sides.
            search = range(max(3, expected_x - (24 if left else 6)),
                           min(width - 3, expected_x + (6 if left else 24)) + 1)
            sample_y = range(top + min(12, (bottom - top) // 5),
                             bottom - min(12, (bottom - top) // 5), 2)
            choices = []
            for x in search:
                support = sum(_edge_at(pixels[x - 2, y], pixels[x, y], pixels[x + 2, y])
                              for y in sample_y) / max(1, len(sample_y))
                choices.append((support, x))
            return max(choices, default=(0, expected_x))

        candidates = []
        for index, top in enumerate(horizontal):
            for bottom in horizontal[index + 1:]:
                top_y, bottom_y = top["y"], bottom["y"]
                if not 30 <= bottom_y - top_y <= height * .42:
                    continue
                if _overlap((top["x0"], top["x1"]), (bottom["x0"], bottom["x1"])) < .88:
                    continue
                if max(abs(top["x0"] - bottom["x0"]), abs(top["x1"] - bottom["x1"])) > 24:
                    continue
                left_support, left_x = side(min(top["x0"], bottom["x0"]), top_y, bottom_y, True)
                right_support, right_x = side(max(top["x1"], bottom["x1"]), top_y, bottom_y, False)
                if left_support < .65 or right_support < .65 or right_x - left_x < minimum_width:
                    continue
                # Require a wide text region; tall message-area panels are diagnostics only.
                if (right_x - left_x) / max(1, bottom_y - top_y) < 1.5:
                    continue
                box = [left_x, top_y, right_x + 1, bottom_y + 1]
                evidence = {"box": box, "top_support": top["support"], "bottom_support": bottom["support"],
                            "left_support": round(left_support, 3), "right_support": round(right_support, 3)}
                # Adjacent antialiasing edges describe the same physical frame.
                same = next((old for old in candidates if max(abs(a - b) for a, b in zip(box, old["box"])) <= 6), None)
                if same is None:
                    candidates.append(evidence)
        answer["closed_frame_candidates"] = candidates[:20]
        answer["closed_frame_candidate_count"] = len(candidates)
        strong = sorted((row for row in answer['vertical_separators'] if row['support'] >= .70),
                        key=lambda row: row['support'], reverse=True)
        header_edges = (_header_edges(pixels, width, height)
                        if len(strong) > 1 and strong[0]['support'] - strong[1]['support'] < .12 else [])
        answer['pane_header_edges'] = header_edges
        candidates, divider, rejected = _filter_input_candidates(
            candidates, answer["vertical_separators"], width, height, header_edges)
        answer["pane_divider"] = divider
        answer["rejected_frames"] = rejected[:20]
        answer["candidates"] = candidates[:20]
        answer["candidate_count"] = len(candidates)
        if len(candidates) == 1:
            answer.update(box=candidates[0]["box"], pixel_confirmed=True,
                          reason="one_closed_frame_in_bottom_right_chat_pane",
                          includes_bottom_toolbar=True)
        elif candidates:
            answer["reason"] = "ambiguous_closed_frames_in_bottom_right_chat_pane"
        elif divider is None:
            answer["reason"] = "pane_divider_unconfirmed"
        else:
            answer["reason"] = "no_closed_frame_in_bottom_right_chat_pane"
        return answer
    finally:
        rgb.close()


def draft_pixels(image, box):
    """Return dark-pixel evidence wholly within a detected box; no OCR/text reading."""
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        return {"checked": False, "reason": "invalid_box"}
    width, height = image.size
    x0, y0, x1, y1 = map(int, box)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        return {"checked": False, "reason": "box_out_of_image"}
    inset_x, inset_y = max(6, min(15, (x1 - x0) // 25)), max(4, min(10, (y1 - y0) // 10))
    inside = (x0 + inset_x, y0 + inset_y, x1 - inset_x, y1 - inset_y)
    if inside[2] <= inside[0] or inside[3] <= inside[1]:
        return {"checked": False, "reason": "box_too_small"}
    with image.crop(inside).convert("RGB") as rgb:
        pixels = rgb.load()
        count = sum(sum(pixels[x, y]) < 450 for y in range(0, rgb.height, 2) for x in range(0, rgb.width, 2))
        sample_count = len(range(0, rgb.width, 2)) * len(range(0, rgb.height, 2))
    return {"checked": True, "sample_rect": list(inside), "dark_samples": count,
            "samples": sample_count, "has_dark_pixels": count > 20,
            "meaning": "pixel_evidence_only_not_text_readback"}


def inspect_composer(image):
    """输入边框与工具栏均有正向像素证据，才返回正文区域。"""
    from .wechat_toolbar import inspect_toolbar
    layout = inspect_layout(image)
    if not layout.get('pixel_confirmed'):
        return {'confirmed': False, 'reason': layout['reason'], 'layout': layout}
    toolbar = inspect_toolbar(image, layout['box'])
    if not toolbar.get('confirmed'):
        return {'confirmed': False, 'reason': toolbar['reason'], 'layout': layout, 'toolbar': toolbar}
    box = layout['box']
    result = {'confirmed': True, 'layout': layout, 'toolbar': toolbar,
              'pane_left': layout['pane_divider']['x'], 'draft_box': toolbar['draft_text_rect'],
              'send_region': [max(box[0], box[2] - 160), toolbar['toolbar_top'], box[2], box[3]]}
    control = composer_control_pixels(image, result['draft_box'], result)
    if control:
        result['draft_control'] = {'kind': 'verified_expand_glyph_exact', 'pixel_count': len(control)}
    return result


def _draft_mark(color):
    return max(color) < 205 or (max(color) - min(color) > 45 and min(color) < 210)


def composer_control_pixels(image, box, composer):
    """只识别已实测展开按钮的完整形状；返回实际点，不豁免按钮矩形。"""
    if (not isinstance(composer, dict) or composer.get('confirmed') is not True
            or not isinstance(box, (list, tuple)) or len(box) != 4
            or any(type(v) is not int for v in box)):
        return frozenset()
    layout, toolbar = composer.get('layout'), composer.get('toolbar')
    if (not isinstance(layout, dict) or layout.get('pixel_confirmed') is not True
            or layout.get('candidate_count') != 1 or not isinstance(toolbar, dict)
            or toolbar.get('confirmed') is not True):
        return frozenset()
    frame_box = layout.get('box')
    draft_box, toolbar_box = composer.get('draft_box'), toolbar.get('draft_text_rect')
    if (not isinstance(frame_box, (list, tuple)) or len(frame_box) != 4
            or any(type(v) is not int for v in frame_box)
            or not isinstance(draft_box, (list, tuple)) or list(draft_box) != list(box)
            or not isinstance(toolbar_box, (list, tuple)) or list(toolbar_box) != list(box)):
        return frozenset()
    fx0, fy0, fx1, fy1 = frame_box
    dx0, dy0, dx1, dy1 = box
    if not (0 <= fx0 < dx0 < dx1 < fx1 <= image.width
            and 0 <= fy0 < dy0 < dy1 < fy1 <= image.height and dy0 == fy0 + 6):
        return frozenset()
    # 4.1.8 客体不同实际像素尺寸下，分别验证的 25px/19px 灰色角箭头。
    # 完整形状与实测框边偏移逐套匹配，不进行缩放、平移或相似度放宽。
    templates = (
        (15, 5, ((0, 3), (0, 5), (1, 6), (4, 7), (5, 7), (5, 8), (6, 8), (6, 8))),
        (15, 2, ((0, 3), (0, 5), (3, 6), (4, 6), (5, 7), (5, 7), (5, 7))),
        (25, 3, ((0, 3), (0, 5), (1, 6), (4, 7), (5, 7), (5, 8), (6, 8), (6, 8))),
    )
    matched = []
    with image.convert('RGB') as rgb:
        px = rgb.load()
        for right_offset, top_offset, runs in templates:
            left, top = fx1 - right_offset, fy0 + top_offset
            expected = frozenset((left + x, top + y) for y, (start, end) in enumerate(runs)
                                 for x in range(start, end))
            guard = (left - 2, top - 2, left + max(end for _, end in runs) + 2,
                     top + len(runs) + 2)
            if not (fx0 < guard[0] < guard[2] < fx1 and fy0 <= guard[1] < guard[3] < fy1):
                continue
            observed = frozenset((x, y) for y in range(guard[1], guard[3])
                                 for x in range(guard[0], guard[2]) if _draft_mark(px[x, y]))
            if observed == expected and all(max(px[x, y]) - min(px[x, y]) <= 8 for x, y in expected):
                matched.append(expected)
    return matched[0] if len(matched) == 1 else frozenset()


def draft_state(image, box, *, composer=None):
    """正文全像素判定；边缘单个标点、孤点或彩色表情仍不能忽略。"""
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        raise ValueError('未确认正文区域')
    x0, y0, x1, y1 = map(int, box)
    if not (0 <= x0 < x1 <= image.width and 0 <= y0 < y1 <= image.height):
        raise ValueError('正文区域超出图像')
    # 原正文区域不裁边，只移除原完整模板已核验的控件像素。
    with image.crop((x0, y0, x1, y1)).convert('RGB') as crop, mark_mask(crop, maximum=205, minimum=210) as mask:
        control = composer_control_pixels(image, box, composer)
        for x, y in control:
            if x0 <= x < x1 and y0 <= y < y1:
                mask.putpixel((x - x0, y - y0), 0)
        bounds = mask.getbbox()
        if bounds is None:
            return 'empty'
        left, top, right, bottom = bounds
        width, height = right - left, bottom - top
        # 统计整张掩码，包括所有不连通孤点，不能仅统计最大的字形。
        count = mask.histogram()[255]
    # 这里只标为插入符候选；上层仍需观察真实闪灭，不能直接视为空草稿。
    caret = (width <= min(3, max(2, height // 7)) and 8 <= height <= 30
             and left <= 20 and top <= 25 and count >= height * .75)
    return 'caret_like' if caret else 'text'


def draft_has_text(image, box):
    # 单帧不能分辨竖线草稿与光标，必须保守视为非空；调用方可等待纯只读的闪烁证据。
    return draft_state(image, box) != 'empty'


def _vertical_header_rows(pixels, height, strong_x):
    """只沿已由全高证实的竖线带测量顶部连续性，不从横线长短猜聊天区域。"""
    return [(y, sum(_edge_at(pixels[x - 2, y], pixels[x, y], pixels[x + 2, y])
                    for x in strong_x) / len(strong_x))
            for y in range(3, min(185, height // 4, height - 3))]


def _crossing_header(pixels, height, strong_x, edges):
    """同高搜索栏可能并入标题横线；仅凭独立的 caption 起点及竖线交叉解歧。"""
    rows = _vertical_header_rows(pixels, height, strong_x)
    runs = [run for run in _runs([y for y, support in rows if support >= .70], step=1, gap=3)
            if run[1] - run[0] >= 24 and run[2] >= .85]
    if not runs:
        return None
    origin = runs[0][0]
    band_left = min(strong_x)
    # 必须真的看到面板竖线从一条更早的 caption 横线开始。竖线直通图像顶部、
    # caption 起点不明等情况保持拒绝，不能任取“第二条长线”当聊天标题下沿。
    before = [support for y, support in rows if origin - 30 <= y < origin - 6]
    if len(before) < 12 or sum(before) / len(before) > .25:
        return None
    captions = [edge for edge in edges if edge['x0'] < band_left - 6
                and edge['y_min'] - 6 <= origin <= edge['y_max'] + 6]
    if len(captions) != 1:
        return None
    caption = captions[0]
    candidates = []
    for edge in edges:
        if (edge['x0'] >= band_left - 6 or edge['y_min'] - caption['y_max'] < 20):
            continue
        above = [support for y, support in rows if edge['y_min'] - 22 <= y < edge['y_min'] - 5]
        below = [support for y, support in rows if edge['y_max'] + 5 < y <= edge['y_max'] + 22]
        if (min(len(above), len(below)) >= 12 and sum(above) / len(above) >= .70
                and sum(below) / len(below) >= .70):
            candidates.append(edge)
    return candidates[0] if len(candidates) == 1 else None


def title_region(image, pane_left):
    """标题下沿必须与面板竖线相接；横跨全窗的系统标题栏边线不是聊天标题。"""
    width, height = image.size
    if not 0 < pane_left < width - 100:
        return None
    with image.convert('RGB') as rgb:
        pixels = rgb.load()
        # 邻近的面板边线/滚动条会让横线左端出现几像素的短延伸。使用同一
        # 全高取样中实际存在的强竖线带；不把固定的 x 容差扩大到任意区域。
        sample_y = range(int(height * .15), int(height * .85), 3)
        strong_x = [x for x in range(max(3, pane_left - 24), min(width - 3, pane_left + 25))
                    if sum(_edge_at(pixels[x - 2, y], pixels[x, y], pixels[x + 2, y])
                           for y in sample_y) / max(1, len(sample_y)) >= .70]
        if not strong_x or max(strong_x) - min(strong_x) > 24:
            return None
        band_left, band_right = min(strong_x), max(strong_x)
        edges = _header_edges(pixels, width, height)
        candidates = [edge for edge in edges if band_left - 6 <= edge['x0'] <= band_right + 6]
        if not candidates:
            crossing = _crossing_header(pixels, height, strong_x, edges)
            if crossing is not None:
                candidates = [crossing]
        if len(candidates) != 1:
            return None
        bottom = candidates[0]['y_min']
        # 若有全窗标题栏的下沿，正文 OCR 从其后开始，不让系统窗口标题
        # 参与“最上方会话名”的选择。不存在这条独立证据时从窗口顶端读取。
        chrome = [edge for edge in edges
                  if edge['x0'] < band_left - 6 and edge['y_max'] < bottom - 4]
        top = max((edge['y_max'] + 1 for edge in chrome), default=0)
        if bottom - top < 20:
            return None
    # 窗口最小化/关闭按钮处于右侧，不能参与标题行选择。
    return (pane_left + 6, top, pane_left + int((width - pane_left) * .60), bottom)
