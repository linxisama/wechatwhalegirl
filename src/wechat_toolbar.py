"""Pure-Pillow, conservative bottom-toolbar geometry from an existing image.

No screenshot, files, OCR, user input, or chat-content interpretation. All boxes
are image-relative and right/bottom exclusive. This is temporary pixel evidence,
not a semantic UIA identification of the toolbar.
"""
from __future__ import annotations


def _joined(first, second, gap=3):
    a, b = first["box"], second["box"]
    return (max(a[0], b[0]) <= min(a[2], b[2]) + gap
            and max(a[1], b[1]) <= min(a[3], b[3]) + gap)


def _merge(parts):
    # Icons can contain separate dots, eyes or a handle. Merge near pieces before
    # testing size. A contiguous text word becomes wide and is then rejected.
    groups = []
    for part in parts:
        current = dict(part)
        changed = True
        while changed:
            changed = False
            for index, old in enumerate(groups):
                if _joined(current, old):
                    a, b = current["box"], old["box"]
                    current = {"box": [min(a[0], b[0]), min(a[1], b[1]),
                                       max(a[2], b[2]), max(a[3], b[3])],
                               "pixels": current["pixels"] + old["pixels"]}
                    groups.pop(index)
                    changed = True
                    break
        groups.append(current)
    return groups


def inspect_toolbar(image, box):
    """Confirm one bottom icon row or return no draft rectangle.

    Requires >=3 independently spaced, roughly square dark icons, aligned in y,
    near the frame's left and bottom edges, with a clean horizontal separator
    above them. Upper text and arbitrary proportional crop fallbacks are excluded.
    """
    answer = {"confirmed": False, "toolbar_top": None, "draft_text_rect": None,
              "reason": "unconfirmed", "icons": [], "candidates": [],
              "minimum_independent_icons": 3, "meaning": "pixel_geometry_only"}
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or any(type(value) is not int for value in box)):
        return dict(answer, reason="invalid_box")
    x0, y0, x1, y1 = box
    width, height = image.size
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        return dict(answer, reason="box_out_of_image")
    box_width, box_height = x1 - x0, y1 - y0
    if box_width < 180 or box_height < 64 or max(width, height) > 4096:
        return dict(answer, reason="box_too_small_or_image_too_large")
    inset = max(5, min(10, box_width // 100))
    # Only the bottom region may establish a toolbar; never scan upper draft text
    # as icon evidence. A frame too short to retain a text region is rejected.
    left, right = x0 + inset, x1 - inset
    top = max(y0 + box_height // 2, y1 - 64)
    bottom = y1 - 5
    if bottom - top < 18:
        return dict(answer, reason="bottom_band_too_short")
    band_width, band_height = right - left, bottom - top
    answer["scan_band"] = [left, top, right, bottom]
    with image.crop((left, top, right, bottom)).convert("RGB") as rgb:
        pixels = rgb.load()
        mask = bytearray(band_width * band_height)
        row_counts = [0] * band_height
        for y in range(band_height):
            for x in range(band_width):
                color = pixels[x, y]
                if max(color) <= 180 and max(color) - min(color) <= 65:
                    mask[y * band_width + x] = 1
                    row_counts[y] += 1
    dark_count = sum(row_counts)
    answer["dark_pixels_in_band"] = dark_count
    if dark_count < 60:
        return dict(answer, reason="too_few_dark_pixels")
    if dark_count > band_width * band_height * .20:
        return dict(answer, reason="bottom_band_too_dense")

    visited = bytearray(len(mask))
    components = []
    for start in range(len(mask)):
        if not mask[start] or visited[start]:
            continue
        visited[start] = 1
        stack, count = [start], 0
        sx, sy = start % band_width, start // band_width
        min_x = max_x = sx
        min_y = max_y = sy
        while stack:
            current = stack.pop()
            x, y = current % band_width, current // band_width
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            count += 1
            for ny in range(max(0, y - 1), min(band_height, y + 2)):
                for nx in range(max(0, x - 1), min(band_width, x + 2)):
                    neighbor = ny * band_width + nx
                    if mask[neighbor] and not visited[neighbor]:
                        visited[neighbor] = 1
                        stack.append(neighbor)
        if count >= 3:
            components.append({"box": [left + min_x, top + min_y,
                                        left + max_x + 1, top + max_y + 1], "pixels": count})
        if len(components) > 200:
            return dict(answer, reason="too_many_bottom_components")
    merged = _merge(components)
    icons = []
    for component in merged:
        a, b, c, d = component["box"]
        w, h = c - a, d - b
        density = component["pixels"] / (w * h)
        if (10 <= w <= 36 and 10 <= h <= 36 and .65 <= w / h <= 1.65
                and .08 <= density <= .75 and 6 <= y1 - d <= 30
                and a >= x0 + inset and c <= x1 - inset):
            icons.append(dict(component, center=[(a + c) / 2, (b + d) / 2],
                              density=round(density, 3)))
    answer["eligible_icon_count"] = len(icons)
    if len(icons) < 3:
        return dict(answer, reason="fewer_than_three_independent_icons")

    rows = []
    for anchor in sorted(icons, key=lambda item: item["center"][1]):
        row = sorted((item for item in icons
                      if abs(item["center"][1] - anchor["center"][1]) <= 4.5),
                     key=lambda item: item["box"][0])
        # Only the left toolbar cluster establishes geometry. A send-button word
        # at the far right cannot complete a short or ambiguous icon cluster.
        row = [item for item in row if item["center"][0] <= x0 + box_width * .65]
        if len(row) < 3 or row[0]["box"][0] - x0 > 48:
            continue
        accepted = [row[0]]
        for item in row[1:]:
            previous = accepted[-1]
            gap = item["box"][0] - previous["box"][2]
            center_gap = item["center"][0] - previous["center"][0]
            if 8 <= gap <= 60 and 24 <= center_gap <= 88:
                accepted.append(item)
            elif gap > 60:
                break
        if len(accepted) < 3:
            continue
        centers = [item["center"][0] for item in accepted]
        gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        if max(gaps) / min(gaps) > 1.45 or centers[-1] - centers[0] < 64:
            continue
        tops = [item["box"][1] for item in accepted]
        bottoms = [item["box"][3] for item in accepted]
        if max(tops) - min(tops) > 8 or max(bottoms) - min(bottoms) > 8:
            continue
        icon_top, icon_bottom = min(tops), max(bottoms)
        if icon_top <= top + 1 or not 6 <= y1 - icon_bottom <= 30:
            continue
        # Locate two entirely blank rows immediately above the icon band. This
        # evidence determines the cut; no assumed toolbar height is subtracted.
        separator = None
        for candidate in range(icon_top - 3, max(top + 1, icon_top - 10), -1):
            local = candidate - top
            if local >= 0 and local + 1 < band_height and row_counts[local] == row_counts[local + 1] == 0:
                separator = candidate
                break
        if separator is None or separator - (y0 + 6) < 22:
            continue
        candidate = {"toolbar_top": separator, "icon_band": [icon_top, icon_bottom],
            "icons": accepted, "center_gaps": gaps, "blank_separator_rows": [separator, separator + 1]}
        if not any(abs(old["toolbar_top"] - separator) <= 4 for old in rows):
            rows.append(candidate)
    answer["candidates"] = rows
    if len(rows) != 1:
        return dict(answer, reason="ambiguous_icon_rows" if rows else "aligned_bottom_icon_row_unconfirmed")
    candidate = rows[0]
    draft_rect = [x0 + inset, y0 + 6, x1 - inset, candidate["toolbar_top"]]
    return dict(answer, confirmed=True, reason="one_spaced_bottom_icon_row_with_blank_separator",
        toolbar_top=candidate["toolbar_top"], draft_text_rect=draft_rect, icons=candidate["icons"],
        icon_band=candidate["icon_band"], blank_separator_rows=candidate["blank_separator_rows"],
        center_gaps=candidate["center_gaps"])


find_toolbar = inspect_toolbar
