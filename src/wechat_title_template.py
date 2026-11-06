"""标题栏会话名的模板比对：把候选名渲染成位图，与真机墨迹算形状 IoU。

微信 4.x 没有控件树，发送前只能靠 OCR 读标题栏会话名；但 18px 中文标题上
本地 OCR 与 winsdk OCR 都会把「李清浩」读成 '子/ 月 / 口' 之类的邻形字，
换放大/二值化变体只是换一种错法（33 个变体实测票数 7 比 7）。

本模块提供独立的第二证据源：名字的**形状**。纯函数、无副作用——不读窗口、
不碰 OCR、不写文件，模块 import 时不加载字体。
"""
from __future__ import annotations

import functools

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = r'C:\Windows\Fonts\msyh.ttc'
TEMPLATE_PX = 32          # 渲染模板用的字号
INK_THRESHOLD = 160       # 灰度 < 该值算墨迹
WIDTH_RATIOS = (0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15)
OFFSETS = (-2, -1, 0, 1, 2)
ASPECT_TOLERANCE = 0.10
MIN_SCORE = 0.50          # 接受阈值
MIN_MARGIN = 0.10         # 与次名的领先幅度阈值
# 渲染前粗筛额外放宽的量：font.getbbox 的盒子与紧致墨迹盒子的宽高比最大相对差
# 0.103（5600 个真名实测），放宽 0.20 保证精确剪枝留下的名字不会被粗筛提前砍掉。
PREFILTER_MARGIN = 0.20


@functools.lru_cache(maxsize=8)
def _font(font_path, size):
    """按需加载字体：模块 import 时不能构造字体对象，缓存避免每次渲染都读 TTC。"""
    return ImageFont.truetype(font_path, size)


def _gray(image):
    """统一成二维 uint8 灰度数组；PIL 图与 ndarray 都收。

    两条入口都走 PIL 的 convert('L')：自己算 ITU-R 601 权重会和 PIL 差一两个
    灰度级，同一块墨迹可能一边算墨、一边算底，得分就对不上。
    """
    if isinstance(image, Image.Image):
        return np.asarray(image.convert('L'))
    if isinstance(image, (str, bytes)):
        # 传路径进来通常是调用方搞错了：本模块只比对内存里的图，不做隐式读盘。
        raise TypeError('ink_image 需要 PIL 图像或 ndarray，收到的是路径；请先自行打开图像')
    array = np.asarray(image)
    if array.dtype == np.bool_:
        array = array.astype(np.uint8) * 255
    elif array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    return np.asarray(Image.fromarray(array).convert('L'))


def _aspect(box):
    """从 (left, top, right, bottom) 取宽高比；盒子不成立时返回 None。"""
    if not box or len(box) != 4:
        return None
    width = int(box[2]) - int(box[0])
    height = int(box[3]) - int(box[1])
    if width <= 0 or height <= 0:
        return None
    return width / height


@functools.lru_cache(maxsize=None)
def _quick_aspect(text, font_path=FONT_PATH, size=TEMPLATE_PX):
    """不渲染、只用 font.getbbox 估的宽高比；拿不到盒子时返回 None（表示不剪）。

    font_path/size 参与缓存键，换了字号或字体的调用不会读到按默认值算出的估值。
    """
    return _aspect(_font(font_path, size).getbbox(text))


def _to_pil(mask):
    """bool 掩膜 → 0/255 灰度图；LANCZOS 不能直接吃 mode '1'。"""
    return Image.fromarray(np.asarray(mask, dtype=bool).astype(np.uint8) * 255)


def ink_box(image):
    """返回 (mask: np.ndarray[bool], (left, top, right, bottom))；无墨迹返回 (None, None)。"""
    gray = _gray(image)
    mask = gray < INK_THRESHOLD
    if not mask.any():
        return None, None
    rows, cols = np.where(mask)
    left, top = int(cols.min()), int(rows.min())
    right, bottom = int(cols.max()) + 1, int(rows.max()) + 1
    return mask[top:bottom, left:right], (left, top, right, bottom)


@functools.lru_cache(maxsize=None)
def render_template(text, font_path=FONT_PATH, size=TEMPLATE_PX):
    """渲染一个候选名，返回 (mask, (left, top, right, bottom)) 或 None（渲染不出墨迹）。

    None 是必须的返回值而不是异常：池里真有渲染不出一个墨迹像素的名字（纯空格），
    不显式挡掉就会在取 min/max 时抛
    'zero-size array to reduction operation minimum which has no identity'。
    结果进 lru_cache：5600 个名字全池渲染约 1.2s，第二次必须近乎免费。
    """
    text = text if isinstance(text, str) else str(text)
    font = _font(font_path, size)
    # 画布留足两侧与上下留白：汉字笔画会超出 em 框，画布窄了会被截断，
    # 截断后的形状和真机墨迹对不上。
    width = max(int(round(font.getlength(text))) + 2 * size + 4, size * 3)
    canvas = Image.new('L', (width, size * 3), 255)
    ImageDraw.Draw(canvas).text((size, size), text, fill=0, font=font)
    # 先取整张画布的布尔掩膜再求边界，比 point(LUT).getbbox() 再 crop 少一次
    # 全画布映射与一次裁剪（5600 名实测 0.83s 对 0.90s），结果逐像素相同。
    full = np.asarray(canvas) < INK_THRESHOLD
    rows = np.flatnonzero(full.any(axis=1))
    if rows.size == 0:
        return None
    cols = np.flatnonzero(full.any(axis=0))
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    left, right = int(cols[0]), int(cols[-1]) + 1
    mask = full[top:bottom, left:right]
    # 缓存对象会被所有调用方共享，锁死写权限，避免谁顺手改一格污染整池结果。
    mask.flags.writeable = False
    return mask, (left, top, right, bottom)


def score_against(ink_mask, ink_size, template, *, width_ratios=WIDTH_RATIOS,
                  offsets=OFFSETS, tolerance=ASPECT_TOLERANCE):
    """单个候选与真机墨迹的 IoU（0..1）。

    ink_size 是 ink_box() 的盒子，只参与宽高比剪枝；真正拿去重采样与求交并集的
    尺寸以掩膜数组为准——交并集就在那个数组上算，用别的尺寸缩放会整片错位。
    """
    if ink_mask is None or template is None:
        return 0.0
    ink = np.asarray(ink_mask, dtype=bool)
    if ink.ndim != 2 or ink.size == 0:
        return 0.0
    template_mask, template_box = template
    shape = np.asarray(template_mask, dtype=bool)
    if shape.ndim != 2 or shape.size == 0:
        return 0.0
    ink_h, ink_w = ink.shape
    template_h, template_w = shape.shape
    ink_aspect = _aspect(ink_size) or ink_w / ink_h
    template_aspect = _aspect(template_box) or template_w / template_h
    # I4 宽高比剪枝：整体比例差太多的候选不可能靠横向缩放救回来，
    # 先砍掉能省下 35 次重采样（全池绝大多数死在这一步）。
    if abs(template_aspect - ink_aspect) / ink_aspect > tolerance:
        return 0.0
    ink_count = int(ink.sum())
    if not ink_count:
        return 0.0
    pil_template = _to_pil(shape)
    best = 0.0
    for ratio in width_ratios:
        target_w = max(1, int(round(ink_w * ratio)))
        scaled = np.asarray(pil_template.resize((target_w, ink_h), Image.LANCZOS)) > 128
        template_count = int(scaled.sum())
        start = (ink_w - target_w) // 2
        for offset in offsets:
            left = start + offset
            low, high = max(0, left), min(ink_w, left + target_w)
            if high <= low:
                continue
            overlap = int(np.count_nonzero(ink[:, low:high] & scaled[:, low - left:high - left]))
            union = ink_count + template_count - overlap
            if union:
                score = overlap / union
                if score > best:
                    best = score
    return best


def _score_all(ink_mask, ink_size, names, tolerance):
    """按 names 顺序逐名打分；渲染不出墨迹的名字记 0.0，与剪枝掉的一致。

    渲染前先用 font.getbbox 粗筛：渲染一个名字约 200us，getbbox 只要 31us，
    而全池里真正能过宽高比剪枝的只有两成左右——先把明显不像的挡在渲染之前，
    省下的正是最贵的那一步（5600 名实测 2.2s → 1.8s）。粗筛只会多留不会多砍：
    阈值取 tolerance + PREFILTER_MARGIN，永远比精确剪枝宽，所以精确剪枝留下的
    名字必然也过粗筛（tests 里有一条用例专门盯这个超集关系）。
    """
    ink_aspect = _aspect(ink_size)
    slack = tolerance + PREFILTER_MARGIN
    scores = []
    for name in names:
        text = name if isinstance(name, str) else str(name)
        quick = _quick_aspect(text)
        if quick is not None and ink_aspect is not None:
            if abs(quick - ink_aspect) / ink_aspect > slack:
                scores.append((0.0, text))
                continue
        template = render_template(text)
        scores.append((0.0 if template is None else
                       score_against(ink_mask, ink_size, template, tolerance=tolerance), text))
    return scores


def rank(ink_image, names, *, tolerance=ASPECT_TOLERANCE):
    """返回 [(score, name), ...] 按分数降序；names 为空或墨迹为空时返回 []。

    同分保持入参顺序（sort 是稳定的），调用方拿 top1/top2 时不会因为并列而随机。
    """
    candidates = [name if isinstance(name, str) else str(name) for name in (names or ())]
    if not candidates:
        return []
    ink_mask, ink_size = ink_box(ink_image)
    if ink_mask is None:
        return []
    scores = _score_all(ink_mask, ink_size, candidates, tolerance)
    scores.sort(key=lambda item: item[0], reverse=True)
    return scores


def judge(ink_image, mine, pool):
    """裁决：返回 dict(accepted=bool, mine=float, best_rival=float, rival=str, margin=float, reason=str)。

    accepted 为 True 当且仅当 mine 在池中的得分 >= MIN_SCORE 且 mine - best_rival >= MIN_MARGIN。
    best_rival 取池里「除 mine 之外」的最高分（池里没有 mine 时 mine=0.0 且 accepted=False）。
    reason 取 'accepted' / 'below_threshold' / 'margin_too_small' / 'not_in_pool' / 'no_ink'。

    两个阈值同时不满足时报 'below_threshold'：分都不够，先修的是模板本身。
    """
    mine_text = mine if isinstance(mine, str) else str(mine)
    ink_mask, ink_size = ink_box(ink_image)
    if ink_mask is None:
        return dict(accepted=False, mine=0.0, best_rival=0.0, rival='', margin=0.0, reason='no_ink')
    pool_names = [name if isinstance(name, str) else str(name) for name in (pool or ())]
    mine_score = 0.0
    best_rival, rival = 0.0, ''
    in_pool = False
    for score, name in _score_all(ink_mask, ink_size, pool_names, ASPECT_TOLERANCE):
        if name == mine_text:
            in_pool = True
            mine_score = max(mine_score, score)
        elif score > best_rival:
            best_rival, rival = score, name
    if not in_pool:
        return dict(accepted=False, mine=0.0, best_rival=best_rival, rival=rival,
                    margin=0.0 - best_rival, reason='not_in_pool')
    margin = mine_score - best_rival
    if mine_score < MIN_SCORE:
        reason = 'below_threshold'
    elif margin < MIN_MARGIN:
        reason = 'margin_too_small'
    else:
        reason = 'accepted'
    return dict(accepted=reason == 'accepted', mine=mine_score, best_rival=best_rival,
                rival=rival, margin=margin, reason=reason)
