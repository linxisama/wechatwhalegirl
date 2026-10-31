# -*- coding: utf-8 -*-
"""1.3.42 标题条指纹复用 + 粘贴前余量闸门 + 退出清理回归。

真机一次发送原本要 13 次 OCR recognize（9 个校验点各跑整族 3 个变体），18 秒预算被吃光，
余量闸门没机会按键；19:03 那次中止还把正文留在输入框里，之后所有发送被 draft_not_empty
永久堵死。这些用例锁三条不变式：
  1) 标题条指纹不变时复用已核验标题（零 OCR），指纹一变立刻退回全族 OCR；
  2) 拿不到指纹/全族 OCR 失败 ⇒ fail-closed 拒绝，绝不把"没核实"当成"已核实"；
  3) 余量不足必须在写剪贴板之前就拒绝；粘贴后失败必须把输入框清回去，且永不按回车。
"""
import unittest
from unittest.mock import patch

from src.wechat_guia_send import CONFIRM_RESERVE, DEFAULT_TIMEOUT

try:  # 正常以工程根为 sys.path[0] 运行
    from tests.test_wechat_guia_send import FakeClipboard, FakeGUI, channel_for
except ImportError:  # discover -s tests 以 tests 为起点时是扁平模块名
    from test_wechat_guia_send import FakeClipboard, FakeGUI, channel_for


REGION = (306, 8, 700, 40)              # 标题条区域（真驱动由 title_region() 算）
WINDOW = (100, 50, 1200, 900)           # 窗口矩形：窗口一动，指纹必须跟着变
DIGEST_A, DIGEST_B = 'a' * 40, 'b' * 40
CTRL, KEY_A, DELETE, KEY_V, ENTER = 0x11, 0x41, 0x2E, 0x56, 0x0D


class FakeClock:
    """只随 sleep 前进的模拟时钟：把预算消耗变成确定输入。"""

    def __init__(self, start=0.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))


class SignedGUI(FakeGUI):
    """带可编程标题条指纹的假驱动：指纹读取与标题 OCR 的计数彻底分开。"""

    def __init__(self, digest=DIGEST_A, signature_error=None, **kwargs):
        super().__init__(**kwargs)
        self.digest = digest
        self.signature_error = signature_error
        self.signature_calls = 0
        self.turn_at = None          # 第几次取指纹时"窗口里的标题被换掉"
        self._last = None

    def ocr_zoomed(self, box, scale=3):
        rows = super().ocr_zoomed(box, scale)
        # 真驱动里标题文本和指纹来自同一次抓帧，假驱动必须复现这个绑定，否则
        # "没有指纹的标题"会被当成已核验标题缓存下来。
        self._last = {'digest': self.digest, 'region': REGION, 'window': WINDOW}
        return rows

    def chat_title_signature(self):
        self.signature_calls += 1
        if self.signature_error is not None:
            raise self.signature_error
        if self.turn_at is not None and self.signature_calls == self.turn_at:
            self.digest, self.title = DIGEST_B, '别人的群'
        return {'digest': self.digest, 'region': REGION, 'window': WINDOW}

    def last_title_signature(self):
        return dict(self._last) if self._last is not None else None


class LateGUI(SignedGUI):
    """第一次取指纹时把预算烧掉 15.6 秒：余量只剩 2.4 秒 < CONFIRM_RESERVE。"""

    def __init__(self, clock, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock

    def chat_title_signature(self):
        value = super().chat_title_signature()
        if self.signature_calls == 1:
            self.clock.now += DEFAULT_TIMEOUT - (CONFIRM_RESERVE - 0.1)
        return value


class BreaksAfterPaste(SignedGUI):
    """粘完正文后布局读取失败：正文已经在输入框里，退出路径必须清回去。"""

    def get_input_box(self):
        if self.pasted:
            raise RuntimeError('粘贴后布局读取失败')
        return super().get_input_box()


class SwitchesAfterPaste(SignedGUI):
    """正文粘进输入框后用户手点切到别的聊天：布局读取失败的那一刻会话已经换了。"""

    def get_input_box(self):
        if self.pasted:
            self.digest, self.title = DIGEST_B, '别人的群'
            raise RuntimeError('粘贴后布局读取失败')
        return super().get_input_box()


class NoSignaturePaste(FakeGUI):
    """没有 chat_title_signature/last_title_signature 的驱动：无法自证还在目标会话。"""

    def get_input_box(self):
        if self.pasted:
            raise RuntimeError('粘贴后布局读取失败')
        return super().get_input_box()


def record_keys(gui):
    """照常投递所有 SendInput，把键盘组的 vk 序列记下来（鼠标点击不计）。"""
    keys = []
    deliver = gui.user32.SendInput.side_effect

    def record(n, events, size):
        if n != 1:
            keys.append(tuple(events[i].payload.keyboard.vk for i in range(n)))
        return deliver(n, events, size)

    gui.user32.SendInput.side_effect = record
    return keys


class TitleSignatureTest(unittest.TestCase):
    def test_stable_signature_reuses_the_verified_title(self):
        """① 指纹不变 ⇒ 九处核验只做一次全族 OCR，且照常发送成功。"""
        gui = SignedGUI()
        channel = channel_for(gui)
        result = channel.send(gui.title, '你好，指纹不变用例')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(gui.send_clicks), 1)
        self.assertEqual(gui.calls.count('ocr'), 1, '九处校验点只允许一次全族 OCR')
        self.assertGreaterEqual(gui.signature_calls, 6, '其余校验点必须靠指纹比对')

    def test_changed_signature_with_another_chat_is_refused(self):
        """② 指纹变了且全族 OCR 读到别的会话名 ⇒ 一个字都不输入、clipboard 不写。"""
        gui = SignedGUI()
        gui.turn_at = 1                      # 第 2 处核验点（聚焦前）时换会话
        clip = FakeClipboard(gui)
        channel = channel_for(gui, clipboard=clip)
        result = channel.send(gui.title, '你好，换会话用例')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'target_not_open')
        self.assertNotIn('input', gui.calls)
        self.assertEqual(clip.writes, [])
        self.assertEqual(clip.snapshots, 0, '连剪贴板快照都不该做')
        self.assertEqual(gui.send_clicks, [])
        self.assertGreaterEqual(gui.calls.count('ocr'), 2, '指纹一变必须退回全族 OCR')

    def test_missing_signature_falls_back_to_full_ocr_everywhere(self):
        """③ 取不到签名 ⇒ 每个校验点都退回全族 OCR，发送照常成功。"""
        gui = SignedGUI(signature_error=RuntimeError('取不到帧'))
        channel = channel_for(gui)
        result = channel.send(gui.title, '你好，退回全族 OCR 用例')
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(gui.calls.count('ocr'), 9, '九处校验点全部退回全族 OCR')
        self.assertGreaterEqual(gui.signature_calls, 1)

    def test_blank_title_without_signature_is_refused(self):
        """③' 退回全族 OCR 也读不到标题 ⇒ fail-closed 拒绝，不输入不按键。"""
        gui = SignedGUI(signature_error=RuntimeError('取不到帧'), title='')
        clip = FakeClipboard(gui)
        channel = channel_for(gui, clipboard=clip)
        result = channel.send('测试群', '你好，读不到标题用例')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, 'title_unverified')
        self.assertNotIn('input', gui.calls)
        self.assertEqual(clip.writes, [])
        self.assertEqual(gui.send_clicks, [])

    def test_reserve_shortfall_never_touches_the_clipboard(self):
        """⑤ 余量前移：CONFIRM_RESERVE 不足时剪贴板一次都没写、也没按键。"""
        clock = FakeClock()
        gui = LateGUI(clock)
        clip = FakeClipboard(gui)
        channel = channel_for(gui, clock=clock, sleep=clock.sleep, clipboard=clip)
        result = channel.send(gui.title, '你好，余量前移用例')
        self.assertFalse(result.ok)
        self.assertIn('未按发送键', result.reason)
        self.assertIn('未输入正文', result.reason)
        self.assertEqual(clip.writes, [], '余量不足时剪贴板一次都不许写')
        self.assertEqual(clip.snapshots, 0, '余量不足时连快照都不该做')
        self.assertEqual(gui.send_clicks, [])
        self.assertNotIn('input', gui.calls)

    def test_paste_failure_clears_the_draft_without_enter(self):
        """⑥ 粘完正文后失败 ⇒ Ctrl+A + Delete 清回输入框，日志写明，绝无回车。"""
        gui = BreaksAfterPaste()
        channel = channel_for(gui)
        keys = []
        original = gui.user32.SendInput.side_effect

        def record(n, events, size):
            if n == 1:                       # 鼠标：聚焦/发送点击照旧
                return original(n, events, size)
            keys.append(tuple(events[i].payload.keyboard.vk for i in range(n)))
            if n == 2:                       # 清空按键必须报"整组接受"，否则会被当成没清
                return n
            return original(n, events, size)

        gui.user32.SendInput.side_effect = record
        with patch('src.wechat_guia_send.log_send_phase') as log:
            result = channel.send(gui.title, '你好，草稿自愈用例')
        self.assertFalse(result.ok)
        self.assertIn('input', gui.calls, '正文确实粘进了输入框')
        self.assertEqual(gui.send_clicks, [], '粘贴后失败绝不许按发送键')
        self.assertIn((CTRL, KEY_A, KEY_A, CTRL), keys, '必须 Ctrl+A 选中残留正文')
        self.assertIn((DELETE, DELETE), keys, '必须按 Delete 删掉残留正文')
        self.assertIn((CTRL, KEY_V, KEY_V, CTRL), keys, '正文确实粘过')
        for sequence in keys:
            self.assertNotIn(ENTER, sequence, '清理绝不允许按回车')
        reasons = [item.kwargs.get('reason') for item in log.call_args_list]
        self.assertIn('已清回输入框', reasons, f'清理日志缺失：{reasons}')

    def test_chat_switched_after_paste_never_touches_the_other_draft(self):
        """⑦ 粘贴后切到别的会话 ⇒ 清理一个字都不碰，日志 reason=target_changed。

        真机上正文已经粘进目标会话输入框，用户这时手点切走；若不核验会话就 Ctrl+A +
        Delete，删掉的是用户在另一个会话里自己写的草稿，而正文仍留在目标会话草稿里。
        """
        gui = SwitchesAfterPaste()
        channel = channel_for(gui)
        keys = record_keys(gui)
        with patch('src.wechat_guia_send.log_send_phase') as log:
            result = channel.send(gui.title, '你好，切走会话用例')
        self.assertFalse(result.ok)
        self.assertIn('input', gui.calls, '正文确实粘进了目标会话输入框')
        self.assertEqual(keys, [(CTRL, KEY_V, KEY_V, CTRL)], f'切走后必须零清理按键，实际 {keys}')
        for sequence in keys:
            self.assertNotIn(KEY_A, sequence, '切走后不许发 Ctrl+A')
            self.assertNotIn(DELETE, sequence, '切走后不许发 Delete')
            self.assertNotIn(ENTER, sequence, '清理绝不允许按回车')
        keeps = [(item.args[0], item.kwargs.get('reason'))
                 for item in log.call_args_list if item.args[0] == 'guia_draft_keep']
        self.assertIn(('guia_draft_keep', 'target_changed'), keeps, f'保持日志缺失：{keeps}')
        self.assertTrue(result.diagnostic.get('input_touched'), '_pasted 是诊断证据，不许被清掉')

    def test_driver_without_signature_api_keeps_the_draft(self):
        """⑧ 驱动没有签名能力 ⇒ 无法自证在目标会话，不碰输入框，reason=target_unverified。"""
        gui = NoSignaturePaste()
        self.assertFalse(hasattr(gui, 'chat_title_signature'))
        self.assertFalse(hasattr(gui, 'last_title_signature'))
        channel = channel_for(gui)
        keys = record_keys(gui)
        with patch('src.wechat_guia_send.log_send_phase') as log:
            result = channel.send(gui.title, '你好，无签名能力用例')
        self.assertFalse(result.ok)
        self.assertIn('input', gui.calls, '正文确实粘进了输入框')
        self.assertEqual(keys, [(CTRL, KEY_V, KEY_V, CTRL)], f'无法自证时必须零清理按键，实际 {keys}')
        keeps = [(item.args[0], item.kwargs.get('reason'))
                 for item in log.call_args_list if item.args[0] == 'guia_draft_keep']
        self.assertIn(('guia_draft_keep', 'target_unverified'), keeps, f'保持日志缺失：{keeps}')


if __name__ == '__main__':
    unittest.main()
