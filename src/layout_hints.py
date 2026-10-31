"""像素定位失败原因码的中文建议与一行诊断摘要（纯标准库）。

失败时用户只看到一句「没有唯一像素证据」，在别人的电脑上无从判断该修什么；
而 inspect_composer 手里的 info 已经带着全套几何证据。这里把 reason 码与几何数字
翻成一句可操作的建议，供异常消息与日志共用。

硬约束：只输出几何数字与原因码，绝不输出聊天正文、联系人昵称、wxid 或文件路径。
"""
from __future__ import annotations

UNKNOWN_HINT = '这条原因码没有对应建议，请把整行日志发给维护者'
MAX_EXPLAIN = 600
SEPARATOR = '｜'

# 每个码一句可操作建议（<=60 字）。键名必须与 wechat_layout/wechat_toolbar 实际
# 抛出的 reason 字符串逐字一致：改码不改文案，文案就永远匹配不上。
_HINTS = {
    # inspect_layout / draft_pixels（src/wechat_layout.py）
    'image_too_small': '微信窗口太小，量不到输入框边框；请把窗口拉大或最大化后重试',
    'pane_divider_unconfirmed': '没量到左右分栏竖线；请关掉遮挡的弹窗或悬浮窗，让会话列表和聊天区都露出',
    'no_closed_frame': '画面里没有闭合的输入框边框；请确认微信停在聊天界面且没被其它窗口遮住',
    'one_closed_frame_in_bottom_right_chat_pane': '右栏输入框已唯一确认，可以直接发送',
    'ambiguous_closed_frames_in_bottom_right_chat_pane': '右栏量到多个闭合边框无法唯一确认；请关掉表情面板或浮层后重试',
    'no_closed_frame_in_bottom_right_chat_pane': '微信当前没有打开任何聊天，请手动点开要回复的群/联系人再试',
    'invalid_box': '框坐标不是四个整数；请把整行日志发给维护者',
    'box_out_of_image': '框坐标超出截图范围（窗口可能刚被移动）；请直接重试一次',
    'box_too_small': '框太小取不到正文像素；请把微信窗口高度拉大后重试',
    # rejected_frames 逐条原因（_filter_input_candidates）
    'left_of_chat_pane': '候选边框落在左侧会话列表里，不是右栏输入框；请先点开一个聊天',
    'outside_bottom_input_area': '候选边框不在底部输入区；请把窗口拉到正常大小，让输入框贴住窗口底部',
    'too_narrow_for_chat_pane': '候选边框比右栏窄太多，不像输入框；请确认聊天区没有被侧栏挤窄',
    'frame_not_aligned_to_chat_pane': '候选边框和右栏左右边界对不齐；请取消贴边或缩放后把窗口摆正再试',
    # inspect_toolbar（src/wechat_toolbar.py）
    'unconfirmed': '底部工具栏没有确认；请确认输入框下沿的表情、文件图标整排可见',
    'box_too_small_or_image_too_large': '输入框太小或截图过大，量不到底部图标；请调整窗口大小后重试',
    'bottom_band_too_short': '输入框底部留白不足，量不到工具栏；请把窗口高度拉大后重试',
    'too_few_dark_pixels': '输入框下方太亮，看不到工具栏图标；请确认表情、文件图标没被隐藏',
    'bottom_band_too_dense': '输入框下方像素太密，分不开图标；请关掉表情面板或输入法候选框',
    'too_many_bottom_components': '输入框下方碎块过多，聚不成图标行；请关掉表情面板或悬浮窗后重试',
    'fewer_than_three_independent_icons': '底部独立图标不足 3 个；请把窗口拉宽，让表情、文件等图标全部露出',
    'ambiguous_icon_rows': '底部量到多排图标无法唯一确认；请关掉弹出的表情面板后重试',
    'aligned_bottom_icon_row_unconfirmed': '底部没有对齐成行的图标；请确认输入框下沿的图标排没被裁掉',
}


def hint_for(reason):
    """reason 码 -> 一句可操作建议；未知或缺失一律返回固定的兜底句。"""
    if isinstance(reason, str):
        hint = _HINTS.get(reason)
        if hint:
            return hint
    return UNKNOWN_HINT


def _as_layout(info):
    """explain 同时接受 layout 直给和 inspect_composer 的 {..., 'layout': {...}}。"""
    if not isinstance(info, dict):
        return {}
    inner = info.get('layout')
    if isinstance(inner, dict):
        return inner
    return info


def _reason_of(info, layout):
    """外层 reason 优先（工具栏失败时它在 info 上），否则退回 layout 自己的码。"""
    if isinstance(info, dict):
        reason = info.get('reason')
        if isinstance(reason, str) and reason:
            return reason
    reason = layout.get('reason')
    return reason if isinstance(reason, str) and reason else ''


def _whole(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _rejected_summary(rejected):
    """前 3 条被拒帧的原因 + 全量计数，例如 left_of_chat_pane×5。"""
    if not isinstance(rejected, (list, tuple)) or not rejected:
        return ''
    counts, order = {}, []
    for row in rejected:
        reason = row.get('reason') if isinstance(row, dict) else None
        if isinstance(reason, str) and reason:
            counts[reason] = counts.get(reason, 0) + 1
            if reason not in order:
                order.append(reason)
    listed = []
    for row in rejected[:3]:
        reason = row.get('reason') if isinstance(row, dict) else None
        if reason in counts and reason not in listed:
            listed.append(reason)
    if not listed:
        return ''
    return '被拒 ' + '，'.join('%s×%d' % (reason, counts[reason]) for reason in listed)


def explain(info):
    """一行诊断摘要：只含几何数字、计数与原因码，无则省略该段，永不含 None。"""
    layout = _as_layout(info)
    reason = _reason_of(info, layout)
    parts = []
    size = layout.get('size')
    if isinstance(size, (list, tuple)) and len(size) == 2:
        width, height = _whole(size[0]), _whole(size[1])
        if width is not None and height is not None:
            parts.append('帧 %dx%d' % (width, height))
    parts.append('像素确认是' if layout.get('pixel_confirmed') is True else '像素确认否')
    closed = _whole(layout.get('closed_frame_candidate_count'))
    usable = _whole(layout.get('candidate_count'))
    parts.append('闭合候选 %d（可用 %d）' % (closed or 0, usable or 0))
    divider = layout.get('pane_divider')
    if isinstance(divider, dict):
        x = _whole(divider.get('x'))
        if x is not None:
            parts.append('分栏 x=%d' % x)
    rejected = _rejected_summary(layout.get('rejected_frames'))
    if rejected:
        parts.append(rejected)
    box = layout.get('box')
    if (isinstance(box, (list, tuple)) and len(box) == 4
            and all(_whole(value) is not None for value in box)):
        parts.append('box %d,%d,%d,%d' % tuple(box))
    else:
        parts.append('box 无')
    if reason:
        parts.append('原因 %s' % reason)
    parts.append('建议：%s' % hint_for(reason))
    text = SEPARATOR.join(parts)
    return text if len(text) <= MAX_EXPLAIN else text[:MAX_EXPLAIN - 1] + '…'


def explain_composer(info):
    """同 explain；名字更贴近调用点（inspect_composer 的返回）。"""
    return explain(info)
