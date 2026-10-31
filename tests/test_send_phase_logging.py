# -*- coding: utf-8 -*-
"""发送链阶段日志的离线回归：阶段序列、正文不入日志、失败留 ERROR、日志坏了不许连累发送。

全部在临时目录里跑：不真机发送、不点真实微信窗口、不碰工程真实的 logs/backend.log。
"""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import logging_setup
from src.logging_setup import extract_error_blocks, mask_chat_name, setup_logging
from src.wechat_guia_send import DEFAULT_TIMEOUT, GuiaChannel, GuiaSendResult

# 9 个字：正文一个字都不许出现在日志里，只允许以 len=9 的形式存在
SECRET = '这是秘密内容ABC'


from tests.test_wechat_guia_send import FakeGUI, FakeClipboard, fake_user32


class ReadyChannel(GuiaChannel):
    """跳过真实 OCR 可用性探测，直接走发送阶段（探测本身另有测试覆盖）。"""

    def __init__(self, gui_factory, **kwargs):
        gui = gui_factory()
        user32 = fake_user32()
        gui.user32 = user32
        clipboard = FakeClipboard(gui)
        def paste(n, events, size):
            if n == 1:      # 发送键由通道自己用 SendInput 绝对坐标点击投递
                if events._obj.payload.mouse.flags & 0x0004:   # MOUSEEVENTF_LEFTUP
                    gui.mouse_click(*user32.SetCursorPos.call_args.args)
                return 1
            key = events[1].payload.keyboard.vk
            if key == 0x56:
                gui.calls.append('input')
                gui.pasted = True
                gui.draft_body = clipboard.value
            elif key == 0x43:
                clipboard.value = gui.draft_body
            return 4
        user32.SendInput.side_effect = paste
        super().__init__(gui_factory=lambda: gui, user32=user32,
                         clipboard=clipboard, activate=lambda g: False,
                         sleep=lambda s: None, send_gate=lambda: None, **kwargs)

    def available(self):
        self._build()
        return GuiaSendResult(True, '', '「坐标+OCR」发送通道可用',
                              {'ready': True, 'guia_channel': True})


class LogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        root_logger = logging.getLogger()
        self._saved_handlers = list(root_logger.handlers)
        self._saved_level = root_logger.level
        setup_logging(self.folder, logging.INFO)

    def tearDown(self):
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            if handler not in self._saved_handlers:
                root_logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        for handler in self._saved_handlers:
            if handler not in root_logger.handlers:
                root_logger.addHandler(handler)
        root_logger.setLevel(self._saved_level)
        self.tmp.cleanup()

    # ── 取样 ───────────────────────────────────────────────────────
    def log_text(self) -> str:
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass
        target = logging_setup.backend_log_path()
        return target.read_text(encoding='utf-8') if target and target.exists() else ''

    def phase_codes(self):
        codes = []
        for line in self.log_text().splitlines():
            if ' send phase=' in line:
                codes.append(line.split('send phase=', 1)[1].split()[0])
        return codes

    # ── 驱动 ───────────────────────────────────────────────────────
    def job(self, **extra):
        job = {'action': 'text', 'who': '测试群', 'text': SECRET, 'guia_send': True,
               'send_compat': False, 'auto_open_chat': False}
        job.update(extra)
        return job

    def run_execute(self, job, send_text, guia=None):
        from src.wechat_send_worker import execute
        phases = []
        fallback = (dict(side_effect=guia) if guia is not None
                    else dict(side_effect=AssertionError('不得降级')))
        with patch('src.interactive_desktop.interactive_desktop_status', return_value=(True, '')), \
             patch('wechatauto.uia_driver.WeChatUIA', return_value=Mock()), \
             patch('src.wechat_window.install_window_discovery'), \
             patch('src.wechat_send_worker._foreground_window', return_value=11), \
             patch('src.wechat_send_worker._restore_foreground'), \
             patch('src.wechat_send_worker.send_text', side_effect=send_text), \
             patch('src.wechat_send_worker.send_text_via_guia', **fallback):
            return execute(job, phases.append)


def uia_stages(*stages, ok=True, detail='foreground'):
    """造假驱动：按真实 send_text 的顺序回调阶段，可选地在中途失败。"""
    def send(driver, who, text, phase=None, open_chat=False):
        for stage in stages:
            phase(stage)
        return ok, detail
    return send


class UiaPhaseSequenceTest(LogCase):
    def test_success_writes_five_codes_in_order(self):
        result = self.run_execute(self.job(), uia_stages(
            'window', 'conversation', 'input_control', 'focus', 'input',
            'readback', 'send_key', 'confirmation'))
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.phase_codes(),
                         ['locate_window', 'check_chat', 'input_text', 'press_send',
                          'verify_watermark'])

    def test_repeated_stages_never_duplicate_lines(self):
        result = self.run_execute(self.job(), uia_stages(
            'window', 'conversation', 'conversation', 'input_control', 'focus', 'input',
            'readback', 'readback', 'send_key', 'confirmation'))
        self.assertTrue(result[0], result[1])
        self.assertEqual(len(self.phase_codes()), 5)


class GuiaPhaseSequenceTest(LogCase):
    def test_fallback_writes_guia_codes_and_the_switch(self):
        gui = FakeGUI()
        result = self.run_execute(
            self.job(),
            uia_stages('window', 'conversation', 'input_control', ok=False,
                       detail='找到了会话，但没有可用的聊天输入框'),
            guia=lambda who, text, **kwargs: ReadyChannel(gui_factory=lambda: gui,
                                                timeout=DEFAULT_TIMEOUT).send(who, text))
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.phase_codes(),
                         ['locate_window', 'check_chat', 'input_text', 'fallback_switch',
                          'guia_probe', 'guia_check_title', 'guia_input',
                          'guia_click_send', 'guia_verify'])
        self.assertIn('restore_zorder', gui.calls)


class NoBodyInLogTest(LogCase):
    def test_body_only_appears_as_length(self):
        result = self.run_execute(self.job(), uia_stages('window', 'send_key'))
        self.assertTrue(result[0], result[1])
        text = self.log_text()
        self.assertNotIn(SECRET, text)
        self.assertIn(f'len={len(SECRET)}', text)
        self.assertIn('reason=', text)

    def test_chat_name_is_masked_to_eight_characters(self):
        long_name = '这是一个很长的微信群名字啊'
        result = self.run_execute(self.job(who=long_name), uia_stages('window'))
        self.assertTrue(result[0], result[1])
        text = self.log_text()
        self.assertNotIn(long_name, text)
        self.assertIn('who=' + mask_chat_name(long_name), text)
        self.assertEqual(mask_chat_name(long_name), long_name[:8] + '…')


class FailureErrorTest(LogCase):
    def assert_single_extractable_error(self, reason):
        text = self.log_text()
        blocks = extract_error_blocks(text)
        self.assertEqual(len(blocks), 1, text)
        self.assertIn(f'send failed channel=guia reason={reason}', blocks[0])
        self.assertIn('hint=', blocks[0])
        self.assertNotIn(SECRET, text)

    def test_guia_unavailable(self):
        def broken():
            raise RuntimeError('未找到微信主窗口，请确认微信已登录并运行')
        result = GuiaChannel(gui_factory=broken).send('测试群', SECRET)
        self.assertEqual(result.reason_code, 'guia_unavailable')
        self.assert_single_extractable_error('guia_unavailable')

    def test_target_not_open(self):
        result = ReadyChannel(gui_factory=lambda: FakeGUI(title='别人的群'),
                              timeout=DEFAULT_TIMEOUT).send('测试群', SECRET)
        self.assertEqual(result.reason_code, 'target_not_open')
        self.assert_single_extractable_error('target_not_open')
        self.assertIn('phase=guia_check_title', self.log_text())

    def test_observed_title_is_masked(self):
        long_title = '别人的超长群名字测试十二字'
        ReadyChannel(gui_factory=lambda: FakeGUI(title=long_title),
                     timeout=DEFAULT_TIMEOUT).send('测试群', SECRET)
        self.assertNotIn(long_title, self.log_text())
        self.assertIn(long_title[:8] + '…', self.log_text())

    def test_send_failed(self):
        result = ReadyChannel(gui_factory=lambda: FakeGUI(send_ok=False),
                              timeout=DEFAULT_TIMEOUT).send('测试群', SECRET)
        self.assertEqual(result.reason_code, 'send_failed')
        self.assert_single_extractable_error('send_failed')

    def test_blocked_point_logs_point_and_foreground_pids_without_body(self):
        channel = ReadyChannel(gui_factory=FakeGUI)
        channel._user32.WindowFromPoint.return_value = 999
        result = channel.send('测试群', SECRET)
        self.assertEqual(result.reason_code, 'blocked_by_window')
        self.assert_single_extractable_error('blocked_by_window')
        self.assertIn('point_pid=9492', self.log_text())
        self.assertIn('foreground_pid=42164', self.log_text())

    def test_foreground_lost_logs_owner_without_writing_clipboard(self):
        channel = ReadyChannel(gui_factory=FakeGUI)
        channel._user32.GetForegroundWindow.return_value = 999
        with patch.object(channel, '_foreground', return_value=True):
            result = channel.send('测试群', SECRET)
        self.assertEqual(result.reason_code, 'foreground_lost')
        self.assertEqual(channel._clipboard.writes, [])
        channel._user32.SendInput.assert_not_called()
        self.assert_single_extractable_error('foreground_lost')
        self.assertIn('point_pid=42164', self.log_text())
        self.assertIn('foreground_pid=9492', self.log_text())

    def test_uia_failure_without_fallback_also_logs_error(self):
        result = self.run_execute(
            self.job(guia_send=None),
            uia_stages('window', 'conversation', 'input_control', ok=False,
                       detail='没有可用的聊天输入框'))
        self.assertFalse(result[0])
        blocks = extract_error_blocks(self.log_text())
        self.assertEqual(len(blocks), 1, blocks)
        self.assertIn('send failed channel=uia reason=input_failed', blocks[0])


class BrokenLoggerTest(LogCase):
    """日志本身炸掉时，发送结果必须与没有日志时完全一致。"""

    class Boom:
        def info(self, *args, **kwargs):
            raise RuntimeError('log down')

        def log(self, *args, **kwargs):
            raise RuntimeError('log down')

    def test_uia_success_result_is_untouched(self):
        with patch.object(logging_setup, '_send_logger', return_value=self.Boom()):
            result = self.run_execute(self.job(), uia_stages('window', 'send_key'))
        self.assertEqual(result, (True, 'foreground'))

    def test_uia_failure_result_is_untouched(self):
        with patch.object(logging_setup, '_send_logger', return_value=self.Boom()):
            result = self.run_execute(
                self.job(guia_send=None),
                uia_stages('window', ok=False, detail='没有可用的聊天输入框'))
        self.assertEqual(result, (False, '没有可用的聊天输入框'))

    def test_guia_failure_result_is_untouched(self):
        with patch.object(logging_setup, '_send_logger', return_value=self.Boom()):
            result = ReadyChannel(gui_factory=lambda: FakeGUI(title='别人的群'),
                                  timeout=DEFAULT_TIMEOUT).send('测试群', SECRET)
        self.assertEqual(result.reason_code, 'target_not_open')
        self.assertFalse(result.ok)


class LogDirHandoverTest(unittest.TestCase):
    def test_prepare_job_hands_the_worker_the_live_log_dir(self):
        from src.wechat_send_worker import worker_log_folder
        from src.wechat_sender import IsolatedWeChatSender
        sender = IsolatedWeChatSender(Path('data/wechat-only'))
        job = sender._prepare_job({'action': 'text'})
        self.assertEqual(worker_log_folder(job, Path('data/wechat-only/send_jobs/x.json')),
                         sender.log_dir)

    def test_legacy_job_falls_back_to_the_send_jobs_parent(self):
        from src.wechat_send_worker import worker_log_folder
        self.assertEqual(
            worker_log_folder({}, Path('data/wechat-only/send_jobs/x.json')),
            Path('data/wechat-only/logs'))


if __name__ == '__main__':
    unittest.main()
