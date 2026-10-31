"""失败原因码建议与一行诊断摘要：只允许几何数字与原因码出现。"""
import os
import unittest

from PIL import Image

from src.layout_hints import (MAX_EXPLAIN, UNKNOWN_HINT, explain, explain_composer,
                              hint_for)
from src.wechat_layout import inspect_composer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAMES = os.path.join(ROOT, '.tmp-sandbox-1339', 'guest-out')
CAP47 = os.path.join(FRAMES, 'cap47.png')
GUEST_FULL = os.path.join(FRAMES, 'guest-full-0.png')

# 只允许这些非字母数字字符出现：诊断文本里不得混入任何正文/昵称/路径。
ALLOWED_CHARS = '｜，、；。：（）×=…-_.,/!? '

LAYOUT_REASONS = (
    'image_too_small', 'pane_divider_unconfirmed', 'no_closed_frame',
    'one_closed_frame_in_bottom_right_chat_pane',
    'ambiguous_closed_frames_in_bottom_right_chat_pane',
    'no_closed_frame_in_bottom_right_chat_pane',
    'invalid_box', 'box_out_of_image', 'box_too_small',
)
REJECT_REASONS = ('left_of_chat_pane', 'outside_bottom_input_area',
                  'too_narrow_for_chat_pane', 'frame_not_aligned_to_chat_pane')
TOOLBAR_REASONS = (
    'unconfirmed', 'box_too_small_or_image_too_large', 'bottom_band_too_short',
    'too_few_dark_pixels', 'bottom_band_too_dense', 'too_many_bottom_components',
    'fewer_than_three_independent_icons', 'ambiguous_icon_rows',
    'aligned_bottom_icon_row_unconfirmed',
)


def cap47_layout():
    """按实测帧 cap47.png（会话未打开）复现的 layout 证据，供不依赖图片的用例用。"""
    return {'size': [1118, 809], 'box': None, 'pixel_confirmed': False,
            'reason': 'no_closed_frame_in_bottom_right_chat_pane',
            'candidate_count': 0, 'closed_frame_candidate_count': 5,
            'pane_divider': {'x': 382, 'y0': 121, 'y1': 687, 'support': 1.0},
            'rejected_frames': [{'box': [143, 337, 387, 418],
                                 'reason': 'left_of_chat_pane'}] * 5}


class LayoutHintTests(unittest.TestCase):
    def test_every_known_reason_has_a_short_actionable_hint(self):
        for reason in LAYOUT_REASONS + REJECT_REASONS + TOOLBAR_REASONS:
            hint = hint_for(reason)
            self.assertTrue(hint, reason)
            self.assertNotEqual(hint, UNKNOWN_HINT, reason)
            self.assertNotIn('None', hint, reason)
            self.assertLessEqual(len(hint), 60, reason)

    def test_unknown_and_missing_reason_fall_back_to_one_fixed_sentence(self):
        for reason in ('no_such_code', '', None, 0, ['left_of_chat_pane']):
            self.assertEqual(hint_for(reason), UNKNOWN_HINT)

    def test_layout_direct_and_info_wrapper_explain_identically(self):
        layout = cap47_layout()
        direct = explain(layout)
        wrapped = explain({'confirmed': False, 'reason': layout['reason'], 'layout': layout})
        self.assertEqual(direct, wrapped)
        self.assertEqual(explain_composer(layout), direct)
        self.assertIn('left_of_chat_pane×5', direct)
        self.assertIn('分栏 x=382', direct)
        self.assertIn('闭合候选 5（可用 0）', direct)

    def test_real_cap47_frame_points_at_left_pane_and_no_open_chat(self):
        if not os.path.exists(CAP47):
            self.skipTest('real frame missing: %s' % CAP47)
        with Image.open(CAP47) as image:
            info = inspect_composer(image.convert('RGB'))
        text = explain(info)
        self.assertNotIn('None', text)
        self.assertIn('帧 1118x809', text)
        self.assertIn('像素确认否', text)
        self.assertIn('left_of_chat_pane×5', text)
        self.assertIn('没有打开', text)
        self.assertIn('no_closed_frame_in_bottom_right_chat_pane', text)
        self.assertLessEqual(len(text), MAX_EXPLAIN)

    def test_real_open_chat_frame_is_confirmed_so_no_failure_message(self):
        if not os.path.exists(GUEST_FULL):
            self.skipTest('real frame missing: %s' % GUEST_FULL)
        with Image.open(GUEST_FULL) as image:
            info = inspect_composer(image.convert('RGB'))
        # 已确认时 _inspect_composer 走正常分支，绝不构造失败消息。
        self.assertTrue(info['confirmed'])
        self.assertIsNone(info.get('reason'))
        text = explain(info)
        self.assertIn('像素确认是', text)
        self.assertIn('已唯一确认', text)
        box = info['layout']['box']
        self.assertEqual(len(box), 4)
        self.assertIn('box %d,%d,%d,%d' % tuple(box), text)

    def test_explain_stays_within_the_message_budget(self):
        fat = dict(cap47_layout(), box=[143, 337, 387, 418], candidate_count=20,
                   closed_frame_candidate_count=12345,
                   rejected_frames=[{'box': [1, 2, 3, 4], 'reason': reason}
                                    for reason in REJECT_REASONS * 7])
        text = explain({'confirmed': False, 'reason': 'pane_divider_unconfirmed',
                        'layout': fat})
        self.assertLessEqual(len(text), MAX_EXPLAIN)
        message = '输入框或工具栏没有唯一像素证据（pane_divider_unconfirmed）｜%s' % text
        self.assertLessEqual(len(message), MAX_EXPLAIN)

    def test_explain_never_leaks_chat_text_nickname_or_paths(self):
        leaky = {'confirmed': False, 'reason': 'no_closed_frame_in_bottom_right_chat_pane',
                 'layout': dict(cap47_layout(), box=[143, 337, 387, 418],
                                nickname='张三', wxid='wxid_abc123',
                                note='张三: 你好，明天见面',
                                draft_text='我把文件发你'),
                 'path': r'C:\Users\16522\AppData\chat.db'}
        text = explain(leaky)
        for secret in ('张三', 'wxid', '你好', '明天见面', '我把文件发你',
                       'Users', 'AppData', 'chat.db', 'C:'):
            self.assertNotIn(secret, text)
        for char in text:
            self.assertTrue(char.isalnum() or char in ALLOWED_CHARS, repr(char))

    def test_missing_reason_never_raises(self):
        for info in ({}, {'layout': {}}, {'layout': None}, None, {'reason': None},
                     {'reason': 123}, {'layout': {'size': [10, 10]}}):
            text = explain(info)
            self.assertIsInstance(text, str)
            self.assertTrue(text)
            self.assertNotIn('None', text)
            self.assertIn('建议：', text)


if __name__ == '__main__':
    unittest.main()
