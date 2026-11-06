"""绑定窗口内的只读像素定位；构造和能力探测不激活、不校准、不落截图。"""
from __future__ import annotations

from .wechat_pixel_masks import mark_mask

import ctypes
import hashlib
import logging
import math
import os
import time

from .layout_hints import explain_composer
from .wechat_capture import grab_render, grab_screen
from .wechat_layout import (composer_control_pixels, draft_state, inspect_composer,
                            title_region, title_region_fallback)
from .wechat_ocr_labels import locate_send_button


# 定位失败的那一句话既贴到面板也落到日志：用户能照着修，远程也能从日志看出该修哪里。
# 走模块级 logger 复用宿主日志，不新开文件句柄。
_log = logging.getLogger(__name__)


# 标题栏 OCR 的固定变体表：同一块实测墨迹，每种放大倍率 × 预处理各识别一次。
# 真机 1264x748 / 125% 缩放下标题字高只有 17-18px：BICUBIC/LANCZOS 会把相邻笔画
# 糊成一团并漏掉字尾，只有"像素忠实放大（NEAREST）+ 硬二值化"能在多个倍率下读全整行。
# 这张表是按真机两种会话名（5 字「鲸鱼娘二号」、6 字「文件传输助手」）各扫一遍
# scale{2,3,4,6,8,10} × threshold{None,120,150,180} 网格定下来的：两张帧都全对的
# 只剩 (4,150)(6,150)(8,150)，倍率必须互不相同，否则同一种错读会被复制三份；
# 不做硬二值化（None）与过狠的 180 在两张帧上都有错读，(2,*) 与 (10,*) 至少一张帧错字。
# 单个变体仍可能把两个字并成一个、或把「助」读成「曲」——这类错字照样能通过完整性
# 检查（它只抓漏字），所以三份结果必须完全一致才算读到整行。
_TITLE_PASSES = ((4, 150), (6, 150), (8, 150))
# 中等字号（墨迹高 16-24px，如隔离沙盒 1118x809 帧与本机 1266x749 帧）和上表的旧帧不是
# 同一类像素：旧表在该帧上三个变体一行都读不出，发送闸门于是永远 title_unverified。
# 1118x809 沙盒帧上扫了 scale × threshold × padding 共 136 组、resample × scale ×
# threshold 共 48 组，只有"NEAREST 2 倍 + 硬阈值 150"和"LANCZOS 2/3 倍 + 不二值化"三组
# 能完整读出真值；NEAREST 不二值化、LANCZOS 加阈值都是 0 行，NEAREST 2.2 倍在阈值
# 140/150/160 下读成错字（'西莲 祟'）。重采样方式因此必须写进表里，不能只记倍率与阈值。
# 每项是 (放大倍率, 阈值或 None 表示不二值化, 白边, 'nearest' 或 'smooth')。
#
# 主族只放"平滑放大 + 不二值化"：2026-10-31 本机 1266x749 / 125% 缩放的 18px 帧
# （会话名「西莲寺紫」）复测时，带硬阈值 150 的两个变体都读成"西莲 累"——错字照样能
# 通过只抓漏字的完整性检查，整族于是被判不一致，真机发送恒 title_unverified。同一帧上
# 平滑不二值化在 1.5/1.75/2/2.25/2.5 倍都能读出真值，故选互不相同且都留余量的
# 1.75/2/2.5 三份；2 倍与 3 倍平滑在 1118x809 沙盒帧上也读得全，两族帧都能覆盖。
_MEDIUM_TITLE_PASSES = ((1.75, None, 0, 'smooth'), (2, None, 0, 'smooth'),
                        (2.5, None, 0, 'smooth'))
# 回退族保留沙盒帧上实测过的原始三变体（含一份硬阈值 150 的 NEAREST）：主族在本机帧
# 全过，沙盒帧若主族意外失手，这族仍是那份 136+48 组网格里唯一读得全的组合。仍然是
# 实测表，不按目标名挑候选，也不比较期望会话名。
_MEDIUM_TITLE_FALLBACK_PASSES = ((2, 150, 0, 'nearest'), (2, None, 0, 'smooth'),
                                 (3, None, 0, 'smooth'))
# 第三族（二值化 + 白边）只服务更早那台真机的 18px 标题条（tests/fixtures/
# filehelper-title-18px.png 那一类像素）：那一族三份都读得全，而本机帧上三份全错或全空。
# 保留它是因为它带一份独立证据——三族互不相交地覆盖三种真实像素来源，整族作废才换下一族。
_MEDIUM_TITLE_BINARIZED_PASSES = ((2.5, 150, 32, 'nearest'), (3, 150, 64, 'nearest'),
                                   (4, 150, 0, 'nearest'))
# 一次标题读取的总预算（秒）：两族最多六个变体，单次 LocalOCR 上限 5 秒，排满会顶到十几秒，
# 把发送链路既有的 18/24/25 秒时限挤掉。超预算当场放弃，绝不返回没核实完的标题。
TITLE_READ_BUDGET = 8.0
# 小字号档（墨迹高 8-13px）：微信 4.1.15.13 起标题条缩到 12px，既不在 16-24 的中等字号
# 区间、也不是旧表的 18px 帧，于是中表三族与旧表在这版微信上全族读不出（真机回包
# 2026-11-02：title 空 → 发送恒 refuse）。本表在回包帧上逐变体实测（真 LocalOCR）：
# (2,140)(2,150)(3,150) 三份都完整读出真值；而 (2.5,140)(2.5,150)(2.5,160)(2.5,170)
# (3,160)(2.5,190) 读成错字、(2,190) 甚至 0 行 —— 错字照样能过只抓漏字的完整性检查，
# 所以仍要求族内三份逐字一致，主族只放落在三个不同倍率/阈值上的实测组合。
_SMALL_TITLE_PASSES = ((2, 140, 8, 'nearest'), (2, 150, 8, 'nearest'),
                       (3, 150, 8, 'nearest'))
# 回退族换重采样方式（三份都带 8px 白边）：同一帧上 (3,140) NEAREST、(4,150) NEAREST、
# (2.5,None) LANCZOS 也都完整读出真值。与主族不共享任何一组参数，主族的像素前提不成立
# 时才轮到它，同样要求三份逐字一致。
_SMALL_TITLE_FALLBACK_PASSES = ((3, 140, 8, 'nearest'), (4, 150, 8, 'nearest'),
                                (2.5, None, 8, 'smooth'))
# 边界档（墨迹高 14-17px）：真机 1920x1440 / 100% 下标题墨迹正好 16px，正压在中等字号
# （16-24px）的下沿，medium 三族整族都读不出。16px 帧上唯一读得全的实测线索是
# 「2 倍放大 + 8px 白边」：NEAREST 2 倍阈值 160、2 倍平滑不二值化、NEAREST 2.5 倍
# 阈值 150 各读出一份，族内三份仍要求逐字一致；18px 及以上行为保持一字不变。
_BOUNDARY_TITLE_PASSES = ((2, 160, 8, 'nearest'), (2, None, 8, 'smooth'),
                          (2.5, 150, 8, 'nearest'))


# 48 变体基础网格：真机上不同窗口尺寸的标题条像素落在不同变体表里，单帧内能读全的
# 变体很少且互不相同，"族内三份逐字一致"于是永远凑不齐（同一台机器 1918x1088 帧墨迹高
# 18 走 medium 档、最大化 1920x1079 帧墨迹高 12 走 small 档，两档各自族内都凑不齐，
# 发送于是恒 title_unverified → 不发）。四张真实帧（本机 full 帧、本机 a 帧、
# filehelper 18px 夹具、415 标题条夹具）上这张网格全部收敛到正确标题；砍掉 1.5/1.75
# （32 组）或减到 24/12 组时"a 帧"一票都投不出来，所以三档倍率全留。超宽网格（400 组）
# 四帧全对但耗时 9.22-12.57s，超过 TITLE_READ_BUDGET 已否决，不要再放大网格。
# 顺序：scale 外层、threshold、padding、resample 内层。
_TITLE_BASE_GRID = tuple((scale, threshold, padding, resample)
                         for scale in (1.5, 1.75, 2.0, 2.5, 3.0, 4.0)
                         for threshold in (None, 150)
                         for padding in (0, 8)
                         for resample in ('smooth', 'nearest'))


def _pass_variants(passes):
    """旧式表没有重采样字段：缺省与历史一致，仍是像素忠实放大 + 硬阈值。"""
    return tuple(entry if len(entry) == 4 else (*entry, 'nearest') for entry in passes)


def _title_pass_families(passes):
    """按信任等级排好的候选族：中等字号档主族在前、回退族在后，旧式表仍只有一族。"""
    if passes is _MEDIUM_TITLE_PASSES:
        return (('medium', _pass_variants(_MEDIUM_TITLE_PASSES)),
                ('medium_fallback', _pass_variants(_MEDIUM_TITLE_FALLBACK_PASSES)),
                ('medium_binarized', _pass_variants(_MEDIUM_TITLE_BINARIZED_PASSES)))
    if passes is _SMALL_TITLE_PASSES:
        return (('small', _pass_variants(_SMALL_TITLE_PASSES)),
                ('small_fallback', _pass_variants(_SMALL_TITLE_FALLBACK_PASSES)))
    if passes is _BOUNDARY_TITLE_PASSES:
        # 第一族是新档的实测三份，第二族保留旧表三份：边界档读不出时仍有老帧证据兜底。
        return (('boundary', _pass_variants(_BOUNDARY_TITLE_PASSES)),
                ('legacy', _pass_variants(tuple((s, t, 0) for s, t in _TITLE_PASSES))))
    return (('legacy', _pass_variants(passes)),)


def _title_candidates(passes):
    """本次读取要依次尝试的候选：实测 profile 族在前，48 组基础网格兜底，去重保序。

    profile 表是按帧实测出来的优证据，先跑能在多数帧上几百毫秒内收敛；基础网格兜住
    "profile 表凑不齐"的那些帧。全程不接收、不比较期望会话名，所以网格不是因为"更接近
    目标"而胜出，只是前面那些候选没投票出来。
    """
    candidates = []
    for _family, variants in _title_pass_families(passes):
        for variant in variants:
            if variant not in candidates:
                candidates.append(variant)
    for variant in _TITLE_BASE_GRID:
        if variant not in candidates:
            candidates.append(variant)
    return tuple(candidates)


def _title_read_budget():
    """总预算可由环境变量覆盖；非法值与非正值一律退回常量，不允许变成无上限。"""
    try:
        value = float(os.environ.get('WHALE_TITLE_READ_BUDGET', ''))
    except ValueError:
        return TITLE_READ_BUDGET
    return value if value > 0 else TITLE_READ_BUDGET


def _title_passes(frame, ink):
    # 按同一帧的全部墨迹高度选固定表，不接收期望会话名。
    with frame.crop(ink).convert('RGB') as crop, mark_mask(crop) as mask:
        box = mask.getbbox()
    height = box[3] - box[1] if box else 0
    # 18-24px 仍是中等字号档；14-17px 走新的边界档（真机 16px 就在这一段）；
    # 8-13px 是小字号档（4.1.15.13 的 12px 标题条）；其余（含 25px 以上）仍是旧表。
    if 18 <= height <= 24:
        return _MEDIUM_TITLE_PASSES, height
    if 14 <= height <= 17:
        return _BOUNDARY_TITLE_PASSES, height
    if 8 <= height <= 13:
        return _SMALL_TITLE_PASSES, height
    return tuple((s, t, 0) for s, t in _TITLE_PASSES), height


def _title_words(rows):
    """保留 OCR 原有空格，分开的词也不能无条件拼成另一个会话名。"""
    ordered = sorted(rows, key=lambda row: row[1])
    parts = []
    previous = None
    for label, x, y, width, height in ordered:
        text = str(label).strip()
        if not text or any(character in text for character in '\r\n\x00'):
            return ''
        if previous is not None:
            _, old_x, _, old_width, old_height = previous
            gap = x - old_x - old_width
            if gap < -2:
                return ''  # 重叠词框不能证明文字先后关系。
            # 远端元素（未读角标、工具按钮）在像素上和标题同高同基线，只有间距能分开：
            # 真机「极客先锋社招新群（77）」右侧的未读角标被读成独立框后拼成了尾部 ' 0'。
            # 超过这个间距就不是同一个标题了，它和它之后的框一律丢弃。
            if gap >= max(12, 2 * min(height, old_height)):
                break
            if gap >= max(3, min(height, old_height) * .25):
                parts.append(' ')
        parts.append(text)
        previous = (text, x, y, width, height)
    return ''.join(parts)


def _complete_title_pixels(frame, region, rows):
    """完整扫描识别框外墨迹，漏前缀、后缀或标题越界均拒绝。"""
    if not rows:
        return False
    left = min(row[1] for row in rows)
    right = max(row[1] + row[3] for row in rows)
    top = max(region[1], min(row[2] for row in rows) - 2)
    bottom = min(region[3], max(row[2] + row[4] for row in rows) + 2)
    if left <= region[0] + 1 or right >= region[2] - 2 or top >= bottom:
        return False
    # 只查原标题区域；外侧工具按钮不能算成漏读的会话文字。
    tail_end = region[2]
    strips = ((region[0], top, max(region[0], left - 3), bottom),
              (min(tail_end, right + 3), top, tail_end, bottom))
    for box in strips:
        if box[0] >= box[2]:
            continue
        with frame.crop(box).convert('RGB') as crop, mark_mask(crop) as mask:
            if mask.getbbox() is not None:
                return False
    return True


def _title_ink_region(frame, region):
    """全像素墨迹外包框等价替代坐标集合；不挑选匹配目标的局部。"""
    x0, y0, x1, y1 = map(int, region)
    with frame.crop(region).convert('RGB') as crop, mark_mask(crop) as mask:
        box = mask.getbbox()
    if box is None:
        return region
    left, top, right, bottom = box
    return (max(x0, x0 + left - 10), max(y0, y0 + top - 8),
            min(x1, x0 + right + 10), min(y1, y0 + bottom + 8))


def ocr_provider():
    from .isolated_wechat.ocr_relay import GuestOCR, LocalOCR
    channel = os.environ.get('WHALE_ISOLATED_CHANNEL', '')
    # 存在隔离标记却配对失效时必须拒绝，不能悄悄改走其他桌面或 OCR 通道。
    return GuestOCR(channel) if channel else LocalOCR()


def readonly_gui_class(base):
    """先定义禁止的基类入口，再构造实例，避免 __init__ 自动校准抢前台。"""
    class PixelGUI(base):
        def __init__(self, *, hwnd, provider):
            if not hwnd:
                raise RuntimeError('未绑定微信窗口')
            self._pixel_ocr = provider
            self._pixel_composer = None
            self._pixel_geometry = None
            self._pixel_composer_fingerprint = None
            # 本帧"正文框外全部像素"的证据，以及绑在它上面的标题条几何：指纹相同就说明
            # 标题那一带的像素逐字节没变，重扫整窗标题区域（真机一次约几十毫秒）是白花。
            self._pixel_frame_proof = None
            self._pixel_title_region = None
            self._pixel_title_region_key = None
            self._pixel_title_region_source = None
            super().__init__(hwnd=hwnd, calibrate=False)

        def _load_layout(self):
            # 不读取历史比例布局，更不能因文件不存在而触发基类自动校准。
            return True

        def _update_layout(self):
            # 基类每次刷新坐标都会重算比例框；已核验的像素区域不得被它覆盖。
            result = super()._update_layout()
            if self._pixel_composer is not None:
                self.right_pane_left = int(self._pixel_composer['pane_left'])
                self.send_button_region = tuple(self._pixel_composer['send_region'])
            return result

        def calibrate_layout(self, *args, **kwargs):
            return False

        def _get_uia(self, *args, **kwargs):
            return None

        def _wake_accessibility(self, *args, **kwargs):
            return False

        def _hot_activate_accessibility(self, *args, **kwargs):
            return False

        def ocr_available(self):
            return bool(self._pixel_ocr.available())

        def _grab_screen(self, box=None):
            # 遮挡无关：整窗 PrintWindow 位图裁渲染区（box 传进来是屏幕坐标）。
            # 自绘不可用（返回 0 / 抛异常 / 窗口尺寸异常 / box 越出渲染区）时才回退
            # 屏幕抓帧——屏幕抓帧在窗口被遮挡时抓到的是遮挡者，只能当兜底。
            frame = grab_render(int(self.main_hwnd), (self.origin_x, self.origin_y),
                                (self.render_w, self.render_h), box)
            return frame if frame is not None else grab_screen(box)

        def _pixel_frame(self):
            self._update_render_rect()
            user = ctypes.windll.user32
            pid = ctypes.c_ulong()
            user.GetWindowThreadProcessId(ctypes.c_void_p(int(self.main_hwnd)), ctypes.byref(pid))
            if (not user.IsWindow(ctypes.c_void_p(int(self.main_hwnd)))
                    or user.IsIconic(ctypes.c_void_p(int(self.main_hwnd)))
                    or not self.pid or int(pid.value) != int(self.pid)):
                raise RuntimeError('绑定的微信窗口不可用')
            geometry = (int(self.origin_x), int(self.origin_y), int(self.render_w), int(self.render_h))
            if not (180 <= geometry[2] <= 4096 and 160 <= geometry[3] <= 4096):
                raise RuntimeError('窗口尺寸不受支持')
            frame = self._grab_screen(tuple(self.render_rect))
            self._update_render_rect()
            current = (int(self.origin_x), int(self.origin_y), int(self.render_w), int(self.render_h))
            if current != geometry or frame.size != geometry[2:]:
                frame.close()
                raise RuntimeError('截图期间窗口位置或尺寸改变')
            return frame, geometry

        def _inspect_composer(self, frame, geometry):
            # 正文会随输入/光标改变；仅在其外的全部实际像素逐字节相同、
            # 窗口几何相同时复用已完整核验的定位。任意外部像素变化都重新全检。
            if self._pixel_composer is not None and geometry == self._pixel_geometry:
                fingerprint = self._composer_fingerprint(frame, self._pixel_composer['draft_box'])
                if fingerprint == self._pixel_composer_fingerprint:
                    # 命中：本帧正文框外像素与当初全检那一帧逐字节相同，同一份证据也可以
                    # 用来证明标题条区域没动过，供 _title_region_cached 复用。
                    self._pixel_frame_proof = (tuple(geometry), fingerprint)
                    return self._pixel_composer
            info = inspect_composer(frame)
            if not info.get('confirmed'):
                # 原因码（pane_divider_unconfirmed 等）必须带出去：只报一句
                # 「没有唯一像素证据」，用户和日志都无从判断该修哪里。
                # 前缀必须逐字保留（既有测试与日志检索依赖它），诊断追加在 ｜ 之后。
                detail = '输入框或工具栏没有唯一像素证据（%s）｜%s' % (
                    info.get('reason') or '未给出原因码', explain_composer(info))
                _log.warning(detail)
                raise RuntimeError(detail)
            self._pixel_composer, self._pixel_geometry = info, geometry
            self._pixel_composer_fingerprint = self._composer_fingerprint(frame, info['draft_box'])
            self._pixel_frame_proof = (tuple(geometry), self._pixel_composer_fingerprint)
            self.right_pane_left = int(info['pane_left'])
            self.send_button_region = tuple(info['send_region'])
            return info

        @staticmethod
        def _composer_fingerprint(frame, box):
            if (not isinstance(box, (tuple, list)) or len(box) != 4
                    or any(type(v) is not int for v in box)
                    or not 0 <= box[0] < box[2] <= frame.width
                    or not 0 <= box[1] < box[3] <= frame.height):
                raise RuntimeError('定位缓存的正文区域无效')
            with frame.convert('RGB') as outside:
                outside.paste((255, 255, 255), tuple(box))
                digest = hashlib.sha256(outside.tobytes()).digest()
            return tuple(frame.size), tuple(box), digest

        def _title_region_cached(self, frame, info):
            """本帧标题条区域；正文框外像素与本帧已证的指纹相同就直接复用上次解析。

            为什么可以复用：标题条几何只由面板竖线与标题下沿横线决定，两条线都在正文框
            之外，而 _composer_fingerprint 已经把"正文框外全部像素 + 窗口矩形"折进 sha256；
            指纹相同即意味着标题条像素逐字节未变，扫出来的区域必然一样。窗口移动/缩放、
            分隔线变化、标题换字都会改指纹，这里随即重扫。没有唯一分隔线时先问
            title_region_fallback（无分隔线版式），它也给不出证据才返回 None，调用方该报错还报错。
            """
            proof = self._pixel_frame_proof
            key = (proof, int(info['pane_left']))
            if proof is not None and key == self._pixel_title_region_key:
                return self._pixel_title_region
            region = title_region(frame, info['pane_left'])
            source = 'divider'
            if region is None:
                # 无分隔线版式（微信 4.1.15.13 起标题栏与正文同为纯白）：没有与面板竖线
                # 相接的横线可证，退回「面板竖线起点 + 面板内最靠上的一段标题墨迹」。
                # 只换定位证据，不放松闸门：带偏就只会读到别的字，随后照旧拒绝发送。
                region = title_region_fallback(frame, info['pane_left'],
                                               bottom_limit=int(info['draft_box'][1]) - 4)
                source = 'header_ink'
            self._pixel_title_region, self._pixel_title_region_key = region, key
            self._pixel_title_region_source = source
            return region

        def readonly_ready(self):
            frame, geometry = self._pixel_frame()
            with frame:
                info = self._inspect_composer(frame, geometry)
                if self._title_region_cached(frame, info) is None:
                    raise RuntimeError('未确认聊天标题栏区域：既没有标题分隔线，也没有可证的标题行')
            return True

        def get_input_box(self):
            frame, geometry = self._pixel_frame()
            with frame:
                info = self._inspect_composer(frame, geometry)
            return tuple(info['draft_box'])

        def refresh_composer(self):
            """粘贴后强制重做一次输入框/工具栏像素核验，返回重新测到的正文区域。

            正文变多时输入框会变高、工具栏随之下移，沿用粘贴前的区域或缓存会点到错的
            位置，所以这里先把缓存全部作废，再整帧重新核验。
            """
            self._pixel_composer = None
            self._pixel_geometry = None
            self._pixel_composer_fingerprint = None
            self._pixel_frame_proof = None
            frame, geometry = self._pixel_frame()
            with frame:
                info = self._inspect_composer(frame, geometry)
                box = tuple(info['draft_box'])
            return box

        def _input_box_has_text(self, box=None):
            if (self._pixel_composer is None or tuple(box or ()) != tuple(self._pixel_composer['draft_box'])):
                raise RuntimeError('正文区域未经像素核验')
            # 首行竖线与光标单帧不可区分。只在其确实闪灭后才确认空白，静态标点仍拒绝。
            expected_geometry = self._pixel_geometry
            expected_frame = tuple(self._pixel_composer['layout']['box'])
            deadline = None
            for _ in range(13):
                frame, geometry = self._pixel_frame()
                with frame:
                    if geometry != expected_geometry:
                        raise RuntimeError('核验草稿时窗口位置改变')
                    info = self._inspect_composer(frame, geometry)
                    if (tuple(info['draft_box']) != tuple(box)
                            or tuple(info['layout']['box']) != expected_frame):
                        raise RuntimeError('核验草稿时输入框布局改变')
                    state = draft_state(frame, box, composer=info)
                if state != 'caret_like':
                    return state != 'empty'
                if deadline is None:
                    # 完整像素布局复核本身可能超过一秒，不能先耗尽闪烁观察时间。
                    deadline = time.monotonic() + 1.1
                if time.monotonic() >= deadline:
                    break
                time.sleep(.1)
            return True

        def probe_composer(self, *, want_ink=False):
            """一次抓帧内同时给出正文框、草稿判定与标题条指纹，供发送链同一处校验复用。

            为什么合并：真机一次 PrintWindow 抓帧要几百毫秒，而"粘贴前：正文框在哪 +
            草稿是不是空 + 目标会话有没有变"原先各抓一帧。这里只减少抓帧次数，不合并判定：
            正文框（交给调用方的 _checked_input_box 边界校验）、草稿是否为空、标题条指纹
            三条判据仍各自独立算、各自独立抛错或返回；任何一条不成立，调用方照旧拒绝发。
            want_ink 时顺带量墨迹（与 composer_ink 同一套掩码/控件剔除），供按键后的清空
            确认把这一帧当第一帧用，省掉一次抓帧。
            """
            frame, geometry = self._pixel_frame()
            with frame:
                info = self._inspect_composer(frame, geometry)
                box = tuple(info['draft_box'])
                state = draft_state(frame, box, composer=info)
                region = self._title_region_cached(frame, info)
                # 区域不确定时指纹给 None：调用方必须退回全族 OCR，不能当成"标题没变"。
                signature = (self._title_band_signature(frame, region, geometry)
                             if region is not None else None)
                ink = self._ink_measures(frame, box, info) if want_ink else None
            if state == 'caret_like':
                # 首行竖线与光标单帧不可区分：照旧交给原方法做闪烁观察，观察不到熄灭
                # 仍然算"有文字"。合并抓帧不许把这条底线放宽成单帧判决。
                has_text = self._input_box_has_text(box)
            else:
                has_text = state != 'empty'
            return {'box': box, 'has_text': bool(has_text), 'draft_state': state,
                    'title_signature': dict(signature) if signature is not None else None,
                    'geometry': tuple(geometry),
                    'ink': ({'pixels': ink[0], 'columns': ink[1], 'state': state}
                            if ink is not None else None)}

        def composer_ink(self, box):
            """量这一帧正文区域里的墨迹量；只读，不判断"算不算有文字"。

            为什么另开一个方法：PrintWindow 位图里光标那一层几乎一直在，draft_state
            很少能等到 'empty' 帧，只看"空/非空"会把真正的清空判成残留草稿。这里给出
            pixels（墨迹像素数）和 columns（横向连续墨迹列数，光标 1-2 列、一行中文
            十几列），让调用方能拿按键前后的变化判断，而不是等一个本环境下几乎不出现
            的帧。掩码阈值与控件像素剔除都与 draft_state 完全一致，两个判定的"墨迹"
            才是同一个东西。
            """
            if (self._pixel_composer is None or tuple(box or ()) != tuple(self._pixel_composer['draft_box'])):
                raise RuntimeError('正文区域未经像素核验')
            expected_geometry = self._pixel_geometry
            expected_frame = tuple(self._pixel_composer['layout']['box'])
            frame, geometry = self._pixel_frame()
            with frame:
                if geometry != expected_geometry:
                    raise RuntimeError('核验草稿时窗口位置改变')
                info = self._inspect_composer(frame, geometry)
                if (tuple(info['draft_box']) != tuple(box)
                        or tuple(info['layout']['box']) != expected_frame):
                    raise RuntimeError('核验草稿时输入框布局改变')
                state = draft_state(frame, box, composer=info)
                pixels, columns = self._ink_measures(frame, box, info)
            return {'pixels': pixels, 'columns': columns, 'state': state}

        @staticmethod
        def _ink_measures(frame, box, composer):
            x0, y0, x1, y1 = (int(v) for v in box)
            if not (0 <= x0 < x1 <= frame.width and 0 <= y0 < y1 <= frame.height):
                raise RuntimeError('正文区域超出图像')
            with frame.crop((x0, y0, x1, y1)).convert('RGB') as crop, \
                    mark_mask(crop, maximum=205, minimum=210) as mask:
                for x, y in composer_control_pixels(frame, box, composer):
                    if x0 <= x < x1 and y0 <= y < y1:
                        mask.putpixel((x - x0, y - y0), 0)
                pixels = mask.histogram()[255]
                # 逐列看有没有墨迹，连续的墨迹列合成一段：一个字通常 1 段，光标只有
                # 1-2 段，所以段数就是"还剩几个字形"的廉价近似，不需要 OCR。
                raw, width = mask.tobytes(), mask.width
                columns = 0
                inked = False
                for x in range(width):
                    hit = any(raw[x::width])
                    if hit and not inked:
                        columns += 1
                    inked = hit
            return pixels, columns

        def composer_cleared(self, box, before, window=3.0, first_ink=None):
            """按键之后确认正文有没有发出去；光标本身永远不算"残留草稿"。

            判据任一成立即算清空：
              1) state == 'empty'：光标这一帧确实熄灭，位图里没有墨迹；
              2) state == 'caret_like' 且墨迹确实比按下发送键前少，且 columns <= 2
                 （只剩光标那么窄）或 pixels 降到 max(2, 30%) 以内；
              3) 不论 state 是什么，pixels 降到 max(2, 40%) 以内且 columns <= 2：
                 真机 Delete 之后那一帧常被 draft_state 判成 'text'（光标层常驻位图），
                 前两条都不成立，明明清空了还判残留；墨迹掉到四成以下且只剩 <=2 个
                 竖列，只可能是光标或极少量残影，与 _ink_cleared 的同一条判据一致。
            第 2 条要求"确实比按键前少"，是因为单独一个标点（'.'、'。'）粘贴后
            draft_state 也可能给出 'caret_like'：它按键前后墨迹一样多，drop 判据不
            成立，不能被当成已清空——原先"只认闪灭帧"想保住的正是这条底线。
            光标/布局的抖动绝不判失败、不抛异常，超窗口返回 False；窗口或输入框布局
            真的变了，仍按既有语义抛 RuntimeError 给调用方。

            first_ink 是调用方刚在同一帧量到的墨迹（probe_composer(want_ink=True)）：
            它比这里再抓一帧更早，只用来省一次抓帧，判据一字不差；它不成立就照旧按窗口
            继续循环去抓新帧，绝不因为"已经有第一帧"而提前放行或提前判失败。
            """
            before_pixels = max(0, int((before or {}).get('pixels') or 0))
            limit = max(2, int(0.3 * before_pixels))
            drop_limit = max(2, int(0.4 * before_pixels))
            deadline = time.monotonic() + max(0.0, float(window))
            pending = first_ink if isinstance(first_ink, dict) else None
            while True:
                if pending is not None:
                    ink, pending = pending, None
                else:
                    ink = self.composer_ink(box)
                if ink['state'] == 'empty':
                    return True
                if (ink['state'] == 'caret_like' and ink['pixels'] < before_pixels
                        and (ink['columns'] <= 2 or ink['pixels'] <= limit)):
                    return True
                if ink['pixels'] <= drop_limit and ink['columns'] <= 2:
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(.1)

        def _ocr_crop(self, frame, region, *, scale=1, threshold=None, padding=0,
                      resample='nearest'):
            from PIL import Image, ImageOps
            x0, y0, x1, y1 = map(int, region)
            if not (0 <= x0 < x1 <= frame.width and 0 <= y0 < y1 <= frame.height):
                return []
            # 两种重采样都要留：小字用像素忠实放大才不糊笔画，中等字号在
            # LANCZOS + 不二值化下才读得全整行，由变体表决定用哪一种。
            method = {'nearest': Image.Resampling.NEAREST,
                      'smooth': Image.Resampling.LANCZOS}[resample]
            with frame.crop((x0, y0, x1, y1)) as crop:
                size = (round(crop.width * scale), round(crop.height * scale))
                with crop.resize(size, method) as enlarged:
                    if threshold is None:
                        prepared = enlarged.copy()
                    else:
                        # 硬二值化：把淡边去掉，多个倍率下都能读全整行。
                        with ImageOps.grayscale(enlarged) as gray:
                            prepared = gray.point(lambda value: 255 if value > threshold else 0)
                    with prepared, ImageOps.expand(prepared, border=padding, fill='white') as padded:
                        rows = self._pixel_ocr.recognize(padded)
                    # 无效/越界词框不可过滤后沿用剩下的部分标题。
                    if any(len(row) != 5 or any(type(value) is not int for value in row[1:])
                           or row[1] < padding or row[2] < padding or row[3] <= 0 or row[4] <= 0
                           or row[1] + row[3] > enlarged.width + padding
                           or row[2] + row[4] > enlarged.height + padding for row in rows):
                        return []
                    sx, sy = enlarged.width / crop.width, enlarged.height / crop.height
            result = []
            for label, x, y, w, h in rows:
                left, top = math.floor((x - padding) / sx), math.floor((y - padding) / sy)
                right, bottom = math.ceil((x + w - padding) / sx), math.ceil((y + h - padding) / sy)
                result.append((str(label), x0 + left, y0 + top, right - left, bottom - top))
            return result

        def ocr(self, region):
            frame, geometry = self._pixel_frame()
            with frame:
                if (self._pixel_composer is None or geometry != self._pixel_geometry
                        or tuple(region) != tuple(self._pixel_composer['send_region'])):
                    raise RuntimeError('发送按钮区域未经像素核验')
                # 返回值完全由结构闸门决定；同步等待可选加分 OCR 会消耗发送预算。
                # 标题的三份完整 OCR 与所有提交前核验仍照常执行。
                return locate_send_button(frame, region)

        def current_chat_title(self):
            # 指纹必须和标题文本同帧产生：每次读取先作废，读失败时绝不能留下上一次的指纹。
            self._pixel_title_signature = None
            self._pixel_title_ink = None
            self._pixel_title_evidence = {'state': 'capture_or_layout_error', 'passes': [],
                                          'family': '', 'families_tried': 0}
            try:
                return self._read_chat_title()
            except Exception as error:
                name = type(error).__name__
                self._pixel_title_evidence['exception_type'] = name if name in (
                    'OSError', 'RuntimeError', 'ProtocolError', 'TimeoutError', 'ValueError',
                    'TypeError', 'AttributeError', 'PermissionError', 'FileNotFoundError') else 'OtherError'
                raise

        def title_diagnostic(self):
            evidence = getattr(self, '_pixel_title_evidence', {})
            return dict(evidence, passes=[dict(row) for row in evidence.get('passes', [])])

        def title_ink_image(self):
            """最近一次标题读取算出的墨迹裁剪图（'L' 模式），供模板比对兜底使用。

            每次读取开头都会作废，所以拿到的一定是最近这一帧的像素；读取没能走到墨迹
            这一步（抓帧失败、标题区定不出来）时为 None。反过来，走到墨迹之后 OCR 仍
            表决失败时这里保留图像——模板比对兜底正是为"读不出文字"准备的那一手。
            """
            return getattr(self, '_pixel_title_ink', None)

        def _read_chat_title(self):
            frame, geometry = self._pixel_frame()
            with frame:
                info = self._inspect_composer(frame, geometry)
                region = self._title_region_cached(frame, info)
                if region is None:
                    self._pixel_title_evidence['state'] = 'header_missing'
                    return ''
                # 本帧的标题条指纹：调用方（wechat_guia_send）用它把"这行文字已经过全族
                # OCR 一致性核验"绑到这一帧的像素上，之后的核验点只比签名即可证明标题
                # 没变，不必重跑三份 OCR。与标题文本必须同帧，所以在这里记而不是另抓一帧。
                self._pixel_title_signature = self._title_band_signature(frame, region, geometry)
                ink = _title_ink_region(frame, region)
                # frame 是上下文管理器，退出 with 后 crop 就不能再取像素；这里先复制并
                # load 进内存，模板比对才能在同一次读取之外继续用这块墨迹。
                self._pixel_title_ink = frame.crop(ink).convert('L')
                self._pixel_title_ink.load()
                passes, glyph_height = _title_passes(frame, ink)
                self._pixel_title_evidence['glyph_height'] = glyph_height
                self._pixel_title_evidence['profile'] = (
                    'medium' if passes is _MEDIUM_TITLE_PASSES
                    else 'boundary' if passes is _BOUNDARY_TITLE_PASSES
                    else 'small' if passes is _SMALL_TITLE_PASSES
                    else 'legacy')
                deadline = time.monotonic() + _title_read_budget()
                # 逐变体投票：真机上不同窗口尺寸的标题条像素落在不同变体表里，单帧内能读全
                # 的变体很少且互不相同，"族内三份逐字一致"于是永远凑不齐（1918x1088 帧墨迹
                # 高 18 走 medium 档、最大化 1920x1079 帧墨迹高 12 走 small 档，两档各自族内
                # 都凑不齐），发送因此恒 title_unverified。改成每个变体各投一票、同一标题凑到
                # 3 票且领先次高 2 票才采信。全程不接收、不比较期望会话名，所以网格兜底不是
                # 因为"更接近目标"而胜出，只是前面的候选没投出达标票数。
                families = _title_pass_families(passes)
                owners = {}
                for name, variants in families:
                    for variant in variants:
                        owners.setdefault(variant, name)
                votes = {}
                first_family = {}
                families_tried = []
                # 只有"OCR 区域超过限制"是变体自身的属性（4x/5x 倍率会顶到
                # MAX_DIMENSION=4096），换下一个变体就有机会，不该中断整次读取；
                # 其余异常照旧上抛，由 current_chat_title 记 exception_type。
                from .isolated_wechat.protocol import ProtocolError
                for variant in _title_candidates(passes):
                    family = owners.get(variant, 'grid')
                    if family != 'grid' and family not in families_tried:
                        families_tried.append(family)
                        self._pixel_title_evidence['families_tried'] = len(families_tried)
                    # 每跑一个变体前核对总预算：候选排满时不能挤掉发送链路的既有时限。
                    if time.monotonic() >= deadline:
                        self._pixel_title_evidence['state'] = 'ocr_budget'
                        return ''
                    # 每个变体都用同一块实测墨迹，不用上一次的词框回推裁剪：
                    # 上一次漏读一个字时，回推出来的裁剪会连带丢掉证据。
                    self._pixel_title_evidence['state'] = 'ocr_error'
                    scale, threshold, padding, resample = variant
                    try:
                        rows = self._ocr_crop(frame, ink, scale=scale, threshold=threshold,
                                              padding=padding, resample=resample)
                    except ProtocolError as error:
                        if '超过限制' not in str(error):
                            raise
                        rows = []
                    rows = [(str(t).strip(), x, y, w, h) for t, x, y, w, h in rows
                            if str(t).strip() and h >= 8 and x >= info['pane_left'] + 6
                            and y + h <= region[3] and x + w <= region[2]]
                    evidence = {'family': family, 'rows': len(rows),
                                'characters': 0, 'complete': False}
                    self._pixel_title_evidence['passes'].append(evidence)
                    if not rows:
                        # 一行都读不出只是没为这份证据出力，够不够格由总票数算。
                        continue
                    top = min(row[2] for row in rows)
                    line = sorted((row for row in rows if abs(row[2] - top) <= 8),
                                  key=lambda row: row[1])
                    title = _title_words(line)
                    evidence['characters'] = len(title)
                    evidence['complete'] = bool(title and _complete_title_pixels(frame, region, line))
                    # 读到框外墨迹说明这个变体漏了字：只作废这一次读数，不再像旧实现那样
                    # 连带作废整族——别的变体仍可能读全整行。
                    if not evidence['complete']:
                        continue
                    votes[title] = votes.get(title, 0) + 1
                    first_family.setdefault(title, family)
                # 票数只在全部变体跑完后才算：真机「极客先锋社招新群（77）」上，错读在第 24
                # 个变体处以 5:3 触发了旧早停，正确读法随后才攒到 9 票，早停让它永远赢不了。
                ranked = sorted(votes.values(), reverse=True)
                if ranked and ranked[0] >= 3 and (len(ranked) == 1 or ranked[0] - ranked[1] >= 2):
                    winner = max(votes, key=votes.get)
                    self._pixel_title_evidence['state'] = 'verified'
                    self._pixel_title_evidence['family'] = first_family[winner]
                    return winner
                # 绝不返回未经达标表决的标题：末尾仍合并成原有那三个原因码，不新增语义。
                self._pixel_title_evidence['state'] = (
                    'disagreement' if len(votes) >= 2 else
                    'incomplete' if votes else 'ocr_empty')
                return ''

        def chat_title_signature(self):
            """标题条区域的像素指纹（sha1）：证明"标题文字没变"，不做 OCR。

            为什么要它：一次发送里会话名要核验 9 次，每次都是整族 3 个变体的全族 OCR，
            真机每次 recognize 约 1.15 秒，18 秒发送预算在按发送键之前就被吃光。标题
            文字的像素没变就说明标题文本没变，可以复用上一次已核验的标题；像素变了
            由调用方退回全族 OCR（读不到或读到别的会话名照旧拒绝）。区域几何与窗口
            矩形一起折进摘要：窗口移动/缩放后同一块文字也必须是不同的签名，否则会把
            "换了个位置的同一行"当成没变。只看像素、只读，不点击、不输入、不 OCR。
            """
            frame, geometry = self._pixel_frame()
            with frame:
                info = self._inspect_composer(frame, geometry)
                region = self._title_region_cached(frame, info)
                if region is None:
                    raise RuntimeError('未确认聊天标题栏区域：既没有标题分隔线，也没有可证的标题行')
                return self._title_band_signature(frame, region, geometry)

        @staticmethod
        def _title_band_signature(frame, region, geometry):
            x0, y0, x1, y1 = (int(value) for value in region)
            if not (0 <= x0 < x1 <= frame.width and 0 <= y0 < y1 <= frame.height):
                raise RuntimeError('标题条区域超出图像')
            with frame.crop((x0, y0, x1, y1)).convert('RGB') as crop:
                digest = hashlib.sha1()
                digest.update(('%d,%d,%d,%d;%d,%d,%d,%d' % (
                    x0, y0, x1, y1, int(geometry[0]), int(geometry[1]),
                    int(geometry[2]), int(geometry[3]))).encode('ascii'))
                digest.update(crop.tobytes())
            return {'digest': digest.hexdigest(), 'region': (x0, y0, x1, y1),
                    'window': tuple(int(value) for value in geometry)}

        def last_title_signature(self):
            """最近一次标题读取所用那一帧的标题条指纹；没读过或读失败时是 None。

            为什么要单独一个取值器：指纹必须和标题文本来自同一帧。调用方用它把"这次
            通过全族 OCR 一致性核验的标题"与"当时的像素"绑定起来，之后只比签名就能
            证明标题没变，不必为绑定再多抓一帧。
            """
            value = getattr(self, '_pixel_title_signature', None)
            return dict(value) if isinstance(value, dict) else None

    return PixelGUI


def make_pixel_gui(hwnd):
    from wechatauto.guia import WeChatGUI
    return readonly_gui_class(WeChatGUI)(hwnd=hwnd, provider=ocr_provider())
