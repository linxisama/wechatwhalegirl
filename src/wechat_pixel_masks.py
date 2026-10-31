"""Exact same-frame RGB masks; no capture, OCR, cached evidence or input."""
from __future__ import annotations

from contextlib import ExitStack
from PIL import ImageChops


def mark_mask(rgb, *, maximum=180, minimum=180):
    """Exact byte-channel predicate; no image or previous evidence is retained."""
    with ExitStack() as stack:
        channels = [stack.enter_context(channel) for channel in rgb.split()]
        rg_max = stack.enter_context(ImageChops.lighter(channels[0], channels[1]))
        high = stack.enter_context(ImageChops.lighter(rg_max, channels[2]))
        rg_min = stack.enter_context(ImageChops.darker(channels[0], channels[1]))
        low = stack.enter_context(ImageChops.darker(rg_min, channels[2]))
        spread = stack.enter_context(ImageChops.subtract(high, low))
        dark = stack.enter_context(high.point([255 if v < maximum else 0 for v in range(256)]))
        colored = stack.enter_context(spread.point([255 if v > 45 else 0 for v in range(256)]))
        low_dark = stack.enter_context(low.point([255 if v < minimum else 0 for v in range(256)]))
        saturated_dark = stack.enter_context(ImageChops.darker(colored, low_dark))
        return ImageChops.lighter(dark, saturated_dark)
