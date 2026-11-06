# -*- coding: utf-8 -*-
"""1.3.50 发送段耗时收敛：同帧合并 + 标题区域指纹复用 + 首帧墨迹前移。

真机一次成功发送的发送段 10.02s 里，最大开销是 PrintWindow 抓帧（src/wechat_guia_send.py
里那句"抓一帧要几百毫秒"）：离线用计数替身量到一次发送要 24 次"要抓一帧才能回答"的
驱动调用，合并取证后降到 17 次。省下来的只能是取证次数，不能是任何一条判据。这些用例
锁四条不变式：
  1) 同一帧内容的第二次标题条读取命中缓存：区域解析次数 N -> 1、OCR 替身调用 0 次，
     返回值逐字相同；
  2) 标题区域像素一变，缓存立刻失效并重新解析，指纹跟着变；
  3) 合并抓帧后任何一条子判据（正文框越界 / 草稿非空 / 发送键证据不唯一 / 墨迹没变少）
     仍然各自 fail-closed：剪贴板不写、正文不输入、发送键不按；
  4) 计时键一个都没少。
全部用合成帧、假驱动与假 OCR，不碰真实微信、不点击、不发送。
"""
import collections
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import ImageDraw

import src.wechat_pixel_gui as pixel_gui
from src.wechat_guia_send import _TIMING_SPANS
from src.wechat_pixel_gui import readonly_gui_class

try:  # 正常以工程根为 sys.path[0] 运行
    from tests import test_pixel_title_passes1321 as fixture
    from tests.test_guia_titlesig1343 import DIGEST_A, DIGEST_B, SignedGUI
    from tests.test_pixel_title_medium1341 import BAND, TITLE, ScriptedOCR, medium_frame
    from tests.test_wechat_guia_send import FakeClipboard, FakeGUI, channel_for
except ImportError:  # discover -s tests 以 tests 为起点时是扁平模块名
    import test_pixel_title_passes1321 as fixture
    from test_guia_titlesig1343 import DIGEST_A, DIGEST_B, SignedGUI
    from test_pixel_title_medium1341 import BAND, TITLE, ScriptedOCR, medium_frame
    from test_wechat_guia_send import FakeClipboard, FakeGUI, channel_for

ROOT = Path(__file__).resolve().parents[1]
BODY = '你好，1.3.50 发送段取证用例'
BOX = (300, 680, 1200, 850)   # 与 FakeGUI.get_input_box 同一块正文区域（不经过 calls 记录）
CHANNEL_TIMING_KEYS = ('gui_ready', 'write', 'press', 'confirm', 'total')


class CountingOCR:
    """只数调用次数的假 OCR：标题条读取路径不该调用它。"""

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def recognize(self, image):
        self.calls += 1
        return []


def counting_title_region():
    """把 title_region 换成"照常算 + 记一次"的替身，用来证明缓存真的省掉了整窗扫描。"""
    calls = []
    real = pixel_gui.title_region

    def wrapper(frame, pane_left):
        calls.append(pane_left)
        return real(frame, pane_left)

    return calls, patch('src.wechat_pixel_gui.title_region', side_effect=wrapper)


class CountingGUI(SignedGUI):
    """数"要抓一帧才能回答"的驱动调用；老路径与合并路径用同一把尺子量。

    真驱动里这些方法每一个都至少调用一次 _pixel_frame（_input_box_has_text 在闪烁观察
    时最多 13 次），所以这里的计数就是抓帧次数的保守下界。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.frames = 0
        self.breakdown = collections.Counter()
        self.cleared_inks = []

    def _frame(self, name):
        self.frames += 1
        self.breakdown[name] += 1

    def _ink(self):
        if self.pasted and not self.sent:
            return {'pixels': 400, 'columns': 12, 'state': 'text'}
        return {'pixels': 0, 'columns': 0, 'state': 'empty'}

    def get_input_box(self):
        self._frame('get_input_box')
        return FakeGUI.get_input_box(self)

    def _input_box_has_text(self, box=None):
        self._frame('_input_box_has_text')
        return FakeGUI._input_box_has_text(self, box)

    def chat_title_signature(self):
        self._frame('chat_title_signature')
        return SignedGUI.chat_title_signature(self)

    def ocr_zoomed(self, box, scale=3):
        self._frame('title_ocr')
        return SignedGUI.ocr_zoomed(self, box, scale)

    def refresh_composer(self):
        self._frame('refresh_composer')
        return FakeGUI.refresh_composer(self)

    def ocr(self, region):
        self._frame('send_button_ocr')
        return FakeGUI.ocr(self, region)

    def composer_ink(self, box):
        self._frame('composer_ink')
        return self._ink()

    def composer_cleared(self, box, before, window=3.0, first_ink=None):
        # 判据与真驱动一致（empty 或墨迹确实变少）；first_ink 只省一次抓帧。
        self.cleared_inks.append(first_ink)
        ink = first_ink if first_ink is not None else (self._frame('composer_ink') or self._ink())
        if ink['state'] == 'empty':
            return True
        limit = max(2, int(0.3 * int((before or {}).get('pixels') or 0)))
        return bool(ink['state'] == 'caret_like' and ink['pixels'] < int((before or {}).get('pixels') or 0)
                    and (ink['columns'] <= 2 or ink['pixels'] <= limit))


class MergedProbeGUI(CountingGUI):
    """带 probe_composer 的假驱动：一次调用同时回答正文框、草稿、标题条指纹与墨迹。"""

    def probe_composer(self, *, want_ink=False):
        self._frame('probe_composer')
        box = BOX
        state = self._ink()['state']
        signature = SignedGUI.chat_title_signature(self)
        has_text = FakeGUI._input_box_has_text(self, box)
        ink = self._ink() if want_ink else None
        return {'box': box, 'has_text': bool(has_text), 'draft_state': state,
                'title_signature': signature,
                'geometry': (self.origin_x, self.origin_y, self.render_w, self.render_h),
                'ink': ink}


class BadBoxProbe(MergedProbeGUI):
    """合并帧里的正文框落到右侧聊天面板之外：必须照旧拒绝。"""

    def probe_composer(self, *, want_ink=False):
        value = super().probe_composer(want_ink=want_ink)
        value['box'] = (0, 10, 100, 40)
        return value


class DraftProbe(MergedProbeGUI):
    """合并帧里的草稿判定为"有字"：必须照旧拒绝，不许因为省抓帧就放过。"""

    def probe_composer(self, *, want_ink=False):
        value = super().probe_composer(want_ink=want_ink)
        value['has_text'] = True
        return value


class ResidueProbe(MergedProbeGUI):
    """按键后墨迹和按键前一样多（残留一个标点）：复用首帧也绝不能判成已清空。"""

    def probe_composer(self, *, want_ink=False):
        value = super().probe_composer(want_ink=want_ink)
        if want_ink:
            value['ink'] = {'pixels': 400, 'columns': 1, 'state': 'caret_like'}
        return value


class TitleRegionCacheTests(unittest.TestCase):
    """手段 (a)：同一帧同一区域的证据相同就不重复解析/OCR。"""

    def make_gui(self, frame, provider):
        gui = readonly_gui_class(fixture.QuietBase)(hwnd=44, provider=provider)
        gui._pixel_frame = lambda: (frame.copy(), (0, 0, frame.width, frame.height))
        return gui

    def medium_provider(self, frame):
        ink = pixel_gui._title_ink_region(frame, BAND)
        variants = [variant for _family, family in pixel_gui._title_pass_families(
            pixel_gui._title_passes(frame, ink)[0]) for variant in family]
        return ScriptedOCR(ink, variants, [TITLE, TITLE, TITLE], (330, 20, 18), 96)

    def test_second_title_signature_read_reuses_the_parsed_region(self):
        """① 同一帧内容连读两次指纹：区域解析 2 次 -> 1 次，OCR 替身 0 次，返回值相同。"""
        frame = medium_frame()
        provider = CountingOCR()
        gui = self.make_gui(frame, provider)
        calls, patcher = counting_title_region()
        with patcher:
            first = gui.chat_title_signature()
            second = gui.chat_title_signature()
        self.assertEqual(len(calls), 1, f'两次取指纹只许解析一次标题区域，实际 {len(calls)} 次')
        self.assertEqual(first, second, '同一帧内容必须给出逐字相同的指纹')
        self.assertEqual(provider.calls, 0, '取指纹不需要 OCR')

    def test_title_read_then_signature_shares_one_parse_and_no_extra_ocr(self):
        """①' 先读标题再取指纹：共用一次区域解析，第二次不新增任何 OCR 调用。"""
        frame = medium_frame()
        provider = self.medium_provider(frame)
        gui = self.make_gui(frame, provider)
        ink = pixel_gui._title_ink_region(frame, BAND)
        plan = pixel_gui._title_candidates(pixel_gui._title_passes(frame, ink)[0])
        calls, patcher = counting_title_region()
        with patcher:
            title = gui.current_chat_title()
            after_title = len(provider.calls)
            signature = gui.chat_title_signature()
        self.assertEqual(title, TITLE)
        # 1.3.90-A 去早停后票只在整张候选表跑完才算：profile 族 + 48 组基础网格各一次。
        self.assertEqual(after_title, len(plan), '候选表每个变体各一次')
        self.assertEqual(len(provider.calls), after_title, '取指纹不许再调用 OCR')
        self.assertEqual(len(calls), 1, '标题读取与随后的指纹读取必须共用一次区域解析')
        self.assertEqual(signature['digest'], gui.last_title_signature()['digest'],
                         '同帧指纹必须与标题读取那一帧完全一致')

    def test_changed_title_pixels_invalidate_the_cached_region(self):
        """② 标题条像素一变：缓存失效、重新解析，指纹跟着变。"""
        frame = medium_frame()
        provider = CountingOCR()
        gui = self.make_gui(frame, provider)
        calls, patcher = counting_title_region()
        with patcher:
            first = gui.chat_title_signature()
            ImageDraw.Draw(frame).rectangle((600, 20, 615, 37), fill='black')  # 标题条内新增墨迹
            second = gui.chat_title_signature()
        self.assertNotEqual(first['digest'], second['digest'], '标题条像素变了，指纹必须跟着变')
        self.assertEqual(len(calls), 2, '像素一变就必须重新解析标题区域，不许沿用缓存')

    def test_merged_probe_answers_three_questions_in_one_frame(self):
        """手段 (b)+ 手段 (c)：一帧回答三个问题，且真的省掉两次抓帧。"""
        frame = medium_frame()
        provider = CountingOCR()
        gui = self.make_gui(frame, provider)
        calls, patcher = counting_title_region()
        frames = []
        gui._pixel_frame = lambda: (frames.append(1) or (frame.copy(), (0, 0, frame.width, frame.height)))
        with patcher:
            probe = gui.probe_composer(want_ink=True)
        self.assertEqual(len(frames), 1, '合并取证只许抓一帧')
        self.assertEqual(tuple(probe['box']), tuple(gui._pixel_composer['draft_box']))
        self.assertFalse(probe['has_text'], '空输入框在合并帧里也必须判成没有文字')
        self.assertEqual(len(probe['title_signature']['digest']), 40)
        self.assertEqual(probe['draft_state'], probe['ink']['state'],
                         '墨迹与草稿判定必须来自同一帧、同一套掩码')
        self.assertEqual(len(calls), 1)


class MergedFrameFailClosedTests(unittest.TestCase):
    """手段 (b) 的反面：合并只是少抓帧，任一条子判据失败都必须照旧拒绝。"""

    def send(self, gui, clipboard=None):
        clip = clipboard if clipboard is not None else FakeClipboard(gui)
        return channel_for(gui, clipboard=clip).send(gui.title, BODY), clip

    def auto_open_evidence(self, gui, clip):
        # 1.3.61：标题是别人的会话时先用 Ctrl+F 搜索打开目标会话（会粘一次会话名），
        # 所以这里守的是「正文一个字都没进输入框、发送键一次都没按、剪贴板用完还原」。
        self.assertEqual(clip.snapshots, 1, '自动打开前必须给剪贴板做一次快照')
        self.assertEqual(clip.value, '原剪贴板', '用完必须把剪贴板还原成打开前的样子')
        self.assertNotIn(BODY, gui.draft_body, '被拒时正文一个字都不许粘')
        self.assertEqual(gui.send_clicks, [], '被拒时发送键一次都不许按')

    def refuse_evidence(self, gui, clip):
        self.assertEqual(clip.writes, [], '被拒时剪贴板一次都不许写')
        self.assertEqual(clip.snapshots, 0, '被拒时连快照都不该做')
        self.assertNotIn('input', gui.calls, '被拒时一个字都不许粘')
        self.assertEqual(gui.send_clicks, [], '被拒时发送键一次都不许按')

    def test_box_outside_the_chat_pane_is_refused(self):
        gui = BadBoxProbe()
        result, clip = self.send(gui)
        self.assertFalse(result.ok)
        self.assertIn('输入框位置不在已核验聊天文本区', result.reason)
        self.refuse_evidence(gui, clip)

    def test_draft_reported_by_the_merged_frame_is_refused(self):
        gui = DraftProbe()
        result, clip = self.send(gui)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'draft_not_empty')
        self.assertIn('已有草稿', result.reason)
        self.refuse_evidence(gui, clip)

    def test_send_button_without_unique_evidence_still_blocks_the_key(self):
        """白像素簇/绿块证据不唯一（这里 ocr 返回 0 个「发送」块）必须拒绝。"""
        gui = MergedProbeGUI()
        gui.send_rows = []
        result, clip = self.send(gui)
        self.assertFalse(result.ok)
        self.assertIn('未唯一定位到发送按钮', result.reason)
        self.assertEqual(gui.send_clicks, [], '结构闸门没过绝不许按发送键')
        self.assertIn('input', gui.calls, '正文确实粘过，拒绝发生在按下发送键之前')

    def test_residual_ink_after_the_key_is_not_reported_as_cleared(self):
        """复用首帧墨迹不许把"没清空"判成"已清空"：等量残留仍是结果不确定。"""
        gui = ResidueProbe()
        result, clip = self.send(gui)
        self.assertFalse(result.ok)
        self.assertIn('未确认清空', result.reason)
        self.assertIn('结果不确定', result.reason)
        self.assertEqual(len(gui.send_clicks), 1, '按键发生过，所以只能报结果不确定')

    def test_prefetched_signature_of_another_chat_is_refused(self):
        """预取指纹只省抓帧：指纹对应的仍是别人的会话名，就必须一个字都不输入。"""
        gui = MergedProbeGUI()
        gui.turn_at = 1                      # 第一次取指纹就换成了别的会话
        result, clip = self.send(gui)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'target_not_open')
        self.auto_open_evidence(gui, clip)

    def test_changed_signature_mid_send_falls_back_to_full_ocr(self):
        """粘贴前指纹变了：必须退回全族 OCR 重新读标题，读到别的会话名就拒绝。"""
        gui = MergedProbeGUI()
        gui.turn_at = 3
        result, clip = self.send(gui)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'target_not_open')
        self.assertGreaterEqual(gui.calls.count('ocr'), 2, '指纹一变必须重新做全族 OCR')
        self.assertNotIn('input', gui.calls)
        self.assertEqual(gui.send_clicks, [])


class SendCostTests(unittest.TestCase):
    """手段 (b)+(c) 的整条链路：抓帧次数下降，判定一个不少。"""

    def test_merged_path_needs_fewer_frames_and_the_same_ocr(self):
        before = CountingGUI()
        result_before = channel_for(before).send(before.title, BODY)
        after = MergedProbeGUI()
        result_after = channel_for(after).send(after.title, BODY)
        self.assertTrue(result_before.ok, result_before.reason)
        self.assertTrue(result_after.ok, result_after.reason)
        self.assertEqual(before.frames, 23, f'改动前的抓帧基线：{dict(before.breakdown)}')
        self.assertEqual(after.frames, 16, f'合并后的抓帧次数：{dict(after.breakdown)}')
        self.assertEqual(before.calls.count('ocr'), after.calls.count('ocr'),
                         '标题全族 OCR 次数不许因为合并取证而变化')
        self.assertEqual(before.calls.count('box'), 6, '老路径每次发送读 6 次正文框')
        self.assertEqual(after.calls.count('box'), 1, '合并后只剩刷新布局那一次')
        self.assertIsNotNone(after.cleared_inks[0],
                             '确认段第一帧墨迹必须由合并帧顺带给出（省掉一次抓帧）')

    def test_timing_keys_stay_complete(self):
        """④ 计时键一个都没少：通道五键照落，运行时七键清单原样。"""
        gui = MergedProbeGUI()
        result = channel_for(gui).send(gui.title, BODY)
        self.assertTrue(result.ok, result.reason)
        timings = result.diagnostic.get('timings') or {}
        for key in CHANNEL_TIMING_KEYS:
            self.assertIn(key, timings, f'缺计时键 {key}：{timings}')
            self.assertIsInstance(timings[key], (int, float))
        self.assertEqual(set(_TIMING_SPANS), set(CHANNEL_TIMING_KEYS))
        source = (ROOT / 'src' / 'isolated_wechat' / 'guest_service.py').read_text(encoding='utf-8')
        self.assertIn("SEND_TIMING_KEYS = ('total', 'receive', 'agent', 'gui_ready', "
                      "'press', 'confirm', 'write')", source)


if __name__ == '__main__':
    unittest.main()
