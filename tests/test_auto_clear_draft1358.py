# -*- coding: utf-8 -*-
"""1.3.58：输入框里已有的草稿改由发送前自动清空（用户 m10623「之后你自动清」）。

这些用例把「一键清空」的边界钉死：只发 Ctrl+A + Delete、永不按 Enter、不碰剪贴板；
必须先自证还在目标会话才敢按；清不干净就拒绝输入，绝不带着残稿粘贴。
"""
import os
import unittest
from unittest.mock import patch

from src.wechat_guia_send import DRAFT_CLEAR_WINDOW, GuiaChannel, _GuardFailed


class FakeGui:
    """只有正文框与草稿判定的替身；texts 按调用次序消费，最后一个值会一直复用。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.main_hwnd = 1234
        self.pid = 5678
        self.right_pane_left = 0
        self.render_w = 1280
        self.render_h = 800

    def get_input_box(self):
        return (10, 20, 200, 100)

    def _input_box_has_text(self, box):
        if len(self.texts) > 1:
            return self.texts.pop(0)
        return self.texts[0]


def make_channel(gui, title='文件传输助手'):
    channel = GuiaChannel(gui_factory=lambda: gui)
    channel._win32 = lambda: object()
    channel._target = '文件传输助手'
    channel._assert_binding = lambda gui: None
    channel._require_foreground = lambda gui, deadline, **kw: None
    channel._verified_title = lambda gui, deadline, **kw: title
    channel._checked_input_box = lambda gui, box: tuple(box)
    return channel


class AutoClearDraftTests(unittest.TestCase):
    def keys(self):
        seen = []
        patches = (
            patch('src.wechat_guia_send._paste_keys',
                  side_effect=lambda user32, vk=0x56: seen.append(('ctrl', vk))),
            patch('src.wechat_guia_send._tap_key',
                  side_effect=lambda user32, vk: seen.append(('key', vk))),
        )
        return seen, patches

    def test_dirty_composer_is_cleared_with_ctrl_a_then_delete(self):
        gui = FakeGui([True, False])
        channel = make_channel(gui)
        seen, (p1, p2) = self.keys()
        with p1, p2:
            box = channel._empty_input_destination(gui, channel._clock() + 5)
        self.assertEqual(box, (10, 20, 200, 100))
        self.assertEqual(seen, [('ctrl', 0x41), ('key', 0x2E)])
        self.assertTrue(channel._draft_cleared)

    def test_still_dirty_after_clearing_refuses_the_paste(self):
        gui = FakeGui([True])
        channel = make_channel(gui)
        seen, (p1, p2) = self.keys()
        with p1, p2, self.assertRaises(_GuardFailed) as ctx:
            channel._empty_input_destination(gui, channel._clock() + 5)
        self.assertEqual(ctx.exception.code, 'draft_not_empty')
        self.assertIn('自动清空失败', ctx.exception.detail)
        self.assertEqual(seen, [('ctrl', 0x41), ('key', 0x2E)])
        self.assertFalse(channel._draft_cleared)

    def test_env_kill_switch_keeps_the_old_refusal(self):
        gui = FakeGui([True])
        channel = make_channel(gui)
        seen, (p1, p2) = self.keys()
        with patch.dict(os.environ, {'WHALE_AUTO_CLEAR_DRAFT': '0'}), p1, p2:
            with self.assertRaises(_GuardFailed) as ctx:
                channel._empty_input_destination(gui, channel._clock() + 5)
        self.assertEqual(ctx.exception.code, 'draft_not_empty')
        self.assertIn('请手动核对', ctx.exception.detail)
        self.assertEqual(seen, [], '关掉自动清空后一个键都不许按')

    def test_title_must_match_before_any_key_is_pressed(self):
        """换到别的会话时连 Ctrl+A 都不许发：删掉的会是用户手写的草稿。"""
        gui = FakeGui([True])
        channel = make_channel(gui, title='另一个会话')
        seen, (p1, p2) = self.keys()
        with p1, p2, self.assertRaises(_GuardFailed) as ctx:
            channel._empty_input_destination(gui, channel._clock() + 5)
        self.assertEqual(ctx.exception.code, 'target_not_open')
        self.assertEqual(seen, [])


class InkGui(FakeGui):
    """真机驱动的能力子集：清空后位图里还留着光标，_input_box_has_text 会一直报「有文字」。"""

    def __init__(self, cleared_results=(True,), ink=None, boom=False):
        FakeGui.__init__(self, [True])
        self._cleared_results = list(cleared_results)
        self._ink = ink if ink is not None else {'pixels': 120, 'columns': 12, 'state': 'text'}
        self._boom = boom
        self.ink_calls = []
        self.clear_calls = []

    def composer_ink(self, box):
        if self._boom:
            raise RuntimeError('正文区域未经像素核验')
        self.ink_calls.append(tuple(box))
        return dict(self._ink)

    def composer_cleared(self, box, before, window=3.0, first_ink=None):
        self.clear_calls.append((tuple(box), dict(before), window))
        if len(self._cleared_results) > 1:
            return self._cleared_results.pop(0)
        return self._cleared_results[0]


class CaretGui(InkGui):
    """真机第二种失败：清空后输入框变矮，旧框量不到墨迹；光标帧还被判成 'text'。"""

    def __init__(self, ink_after, has_text=True):
        InkGui.__init__(self, [False])      # composer_cleared 一直说"没清空"
        self._after = dict(ink_after)
        self._has_text = has_text
        self.probe_calls = 0

    def probe_composer(self, want_ink=False):
        self.probe_calls += 1
        return {'box': (10, 20, 200, 100), 'has_text': self._has_text,
                'draft_state': self._after.get('state'), 'ink': dict(self._after),
                'title_signature': None}


class AutoClearDraftInkTests(unittest.TestCase):
    """真机第一次上机时暴露的判据问题：清空后 draft_state 仍是 caret_like。"""

    def keys(self):
        seen = []
        patches = (
            patch('src.wechat_guia_send._paste_keys',
                  side_effect=lambda user32, vk=0x56: seen.append(('ctrl', vk))),
            patch('src.wechat_guia_send._tap_key',
                  side_effect=lambda user32, vk: seen.append(('key', vk))),
        )
        return seen, patches

    def test_caret_like_frame_still_counts_as_cleared_when_ink_drops(self):
        gui = InkGui([True])
        channel = make_channel(gui)
        seen, (p1, p2) = self.keys()
        with p1, p2:
            box = channel._empty_input_destination(gui, channel._clock() + 5)
        self.assertEqual(box, (10, 20, 200, 100))
        self.assertEqual(seen, [('ctrl', 0x41), ('key', 0x2E)])
        self.assertTrue(channel._draft_cleared)
        self.assertEqual(gui.ink_calls, [(10, 20, 200, 100)], '清空前只量一次墨迹')
        self.assertEqual(len(gui.clear_calls), 1)
        self.assertEqual(gui.clear_calls[0][1], {'pixels': 120, 'columns': 12, 'state': 'text'})
        self.assertEqual(gui.clear_calls[0][2], DRAFT_CLEAR_WINDOW)

    def test_ink_driver_saying_not_cleared_still_refuses(self):
        gui = InkGui([False])
        channel = make_channel(gui)
        seen, (p1, p2) = self.keys()
        with p1, p2, self.assertRaises(_GuardFailed) as ctx:
            channel._empty_input_destination(gui, channel._clock() + 5)
        self.assertEqual(ctx.exception.code, 'draft_not_empty')
        self.assertIn('自动清空失败', ctx.exception.detail)
        self.assertFalse(channel._draft_cleared)
        self.assertEqual(seen, [('ctrl', 0x41), ('key', 0x2E)])

    def test_missing_ink_falls_back_to_has_text(self):
        gui = InkGui([True], boom=True)
        gui.texts = [True, False]
        channel = make_channel(gui)
        seen, (p1, p2) = self.keys()
        with p1, p2:
            box = channel._empty_input_destination(gui, channel._clock() + 5)
        self.assertEqual(box, (10, 20, 200, 100))
        self.assertTrue(channel._draft_cleared)
        self.assertEqual(gui.clear_calls, [], '量不到墨迹就不许走墨迹判据')
    def test_caret_only_frame_counts_as_cleared_even_if_confirm_says_no(self):
        gui = CaretGui({'pixels': 69, 'columns': 1, 'state': 'text'})
        channel = make_channel(gui)
        seen, (p1, p2) = self.keys()
        with p1, p2:
            box = channel._empty_input_destination(gui, channel._clock() + 5)
        self.assertEqual(box, (10, 20, 200, 100))
        self.assertEqual(seen, [('ctrl', 0x41), ('key', 0x2E)])
        self.assertTrue(channel._draft_cleared)

    def test_real_draft_left_after_clearing_still_refuses(self):
        gui = CaretGui({'pixels': 6006, 'columns': 22, 'state': 'text'})
        channel = make_channel(gui)
        seen, (p1, p2) = self.keys()
        with p1, p2, self.assertRaises(_GuardFailed) as ctx:
            channel._empty_input_destination(gui, channel._clock() + 5)
        self.assertEqual(ctx.exception.code, 'draft_not_empty')
        self.assertFalse(channel._draft_cleared)



class DropCriterionTests(unittest.TestCase):
    """1.3.88 缺陷 1：真机 Delete 之后那一帧被判成 'text'，两条旧判据都不成立。

    新增第三条判据（与 composer_cleared 同形）：不论 state，墨迹掉到原来 40% 以下且
    只剩 <=2 个竖列就算清空。这里同时钉住「只降 10%、还剩 8 列」必须继续拒绝。
    """

    def test_text_state_dropping_to_a_quarter_with_one_column_is_cleared(self):
        before = {'pixels': 120, 'columns': 12, 'state': 'text'}
        after = {'pixels': 30, 'columns': 1, 'state': 'text'}      # 25%，只剩光标列
        self.assertTrue(GuiaChannel._ink_cleared(after, before))

    def test_ten_percent_drop_with_eight_columns_still_refuses(self):
        before = {'pixels': 120, 'columns': 12, 'state': 'text'}
        after = {'pixels': 108, 'columns': 8, 'state': 'text'}     # 只降 10%，还有 8 列
        self.assertFalse(GuiaChannel._ink_cleared(after, before))

    def test_quarter_drop_with_many_columns_still_refuses(self):
        # 单独钉住「必须同时 columns <= 2」这半个条件：墨迹确实掉到 25%，但还剩下
        # 8 个竖列，那是一片真实的短草稿，不能被当成光标放行。
        before = {'pixels': 120, 'columns': 12, 'state': 'text'}
        after = {'pixels': 30, 'columns': 8, 'state': 'text'}      # 25%，但有 8 列
        self.assertFalse(GuiaChannel._ink_cleared(after, before))

    def test_empty_and_caret_like_branches_do_not_regress(self):
        before = {'pixels': 120, 'columns': 12, 'state': 'text'}
        self.assertTrue(GuiaChannel._ink_cleared({'pixels': 0, 'columns': 0,
                                                 'state': 'empty'}, before))
        self.assertTrue(GuiaChannel._ink_cleared({'pixels': 60, 'columns': 1,
                                                  'state': 'caret_like'}, before))
        # 光标帧墨迹没变少（且高于 CARET_INK_MAX=120）：caret_like 这条仍要靠
        # 「确实比按键前少」拦住，不能被新的四成判据顺手放行。
        wide = {'pixels': 400, 'columns': 12, 'state': 'text'}
        same = {'pixels': 400, 'columns': 1, 'state': 'caret_like'}
        self.assertFalse(GuiaChannel._ink_cleared(same, wide))

    def test_driver_path_counts_a_quarter_drop_with_two_columns_as_cleared(self):
        # 端到端：驱动 composer_cleared 说没清空，墨迹回读 900/4000 且只剩 2 列。
        gui = CaretGui({'pixels': 900, 'columns': 2, 'state': 'text'})
        gui._ink = {'pixels': 4000, 'columns': 40, 'state': 'text'}
        channel = make_channel(gui)
        seen, (p1, p2) = self.keys()
        with p1, p2:
            box = channel._empty_input_destination(gui, channel._clock() + 5)
        self.assertEqual(box, (10, 20, 200, 100))
        self.assertEqual(seen, [('ctrl', 0x41), ('key', 0x2E)])
        self.assertTrue(channel._draft_cleared)

    def keys(self):
        seen = []
        patches = (
            patch('src.wechat_guia_send._paste_keys',
                  side_effect=lambda user32, vk=0x56: seen.append(('ctrl', vk))),
            patch('src.wechat_guia_send._tap_key',
                  side_effect=lambda user32, vk: seen.append(('key', vk))),
        )
        return seen, patches


if __name__ == '__main__':
    unittest.main()
