"""1.3.114 沙盒面板：提权之后自动核对结果。
全程替身：不跑 dism、不弹 UAC、不读真实日志，只验证面板自己的节律与收尾。
"""
from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def _load_module():
    try:
        from src.gui import sandbox_provision_panel
    except Exception:
        return None
    return sandbox_provision_panel


class FakeService:
    """面板只用到 status/enable/check/create_channel/rollback_text 五个方法。"""

    def __init__(self, verdict=None):
        self.verdict = verdict or {'ok': False, 'state': 'pending', 'reason': 'no_result',
                                   'message': '提权进程似乎还在运行'}
        self.check_calls = 0
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return {'ok': True, 'state': 'disabled', 'next_step': 'enable', 'message': '未启用'}

    def enable(self):
        return {'state': 'elevation_started', 'already': False, 'log_path': 'x.log'}

    def check(self, *_args, **_kwargs):
        self.check_calls += 1
        return dict(self.verdict)

    def create_channel(self):
        return {'ok': True, 'message': '已建通道'}

    def rollback_text(self):
        return '禁用方法'


class BoomService(FakeService):
    def check(self, *_args, **_kwargs):
        self.check_calls += 1
        raise RuntimeError('boom')


class PanelPollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()
        if cls.module is None:
            raise unittest.SkipTest('没有可用的 Qt 绑定')
        from PyQt6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def make_panel(self, service=None):
        service = service or FakeService()
        panel = self.module.SandboxProvisionPanel(service, autostart=False)
        self.addCleanup(panel.deleteLater)
        self.addCleanup(panel._poll_timer.stop)
        return panel, service

    def spin_until(self, predicate, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.app.processEvents()
        return predicate()

    def log_text(self, panel):
        return panel.log_view.toPlainText()

    # ── 启停条件 ──────────────────────────────────────────────────────────
    def test_elevation_started_starts_polling(self):
        panel, _service = self.make_panel()
        panel.apply_status({'state': 'elevation_started', 'message': '等授权'})
        self.assertTrue(panel._poll_timer.isActive())
        self.assertIn('自动核对', self.log_text(panel))

    def test_pending_state_also_polls(self):
        panel, _service = self.make_panel()
        panel.apply_status({'state': 'pending'})
        self.assertTrue(panel._poll_timer.isActive())

    def test_pending_reboot_does_not_poll(self):
        panel, _service = self.make_panel()
        panel.apply_status({'state': 'pending_reboot', 'needs_reboot': True, 'message': '要重启'})
        self.assertFalse(panel._poll_timer.isActive())
        self.assertIn('需要重启', panel.status_label.text())

    def test_plain_state_stops_polling(self):
        panel, _service = self.make_panel()
        panel.apply_status({'state': 'elevation_started'})
        self.assertTrue(panel._poll_timer.isActive())
        panel.apply_status({'state': 'disabled', 'next_step': 'enable'})
        self.assertFalse(panel._poll_timer.isActive())

    def test_start_poll_is_idempotent(self):
        panel, _service = self.make_panel()
        panel.apply_status({'state': 'elevation_started'})
        panel.apply_status({'state': 'elevation_started'})
        self.assertEqual(self.log_text(panel).count('自动核对'), 1)

    # ── 节流与上限 ────────────────────────────────────────────────────────
    def test_tick_skips_while_busy(self):
        panel, service = self.make_panel()
        panel.apply_status({'state': 'elevation_started'})
        panel._busy = True
        panel._poll_tick()
        time.sleep(0.05)
        self.assertEqual(service.check_calls, 0)
        self.assertFalse(panel._poll_inflight)

    def test_tick_skips_while_previous_check_inflight(self):
        panel, service = self.make_panel()
        panel.apply_status({'state': 'elevation_started'})
        panel._poll_inflight = True
        panel._poll_tick()
        time.sleep(0.05)
        self.assertEqual(service.check_calls, 0)

    def test_tick_cap_stops_and_hints(self):
        panel, service = self.make_panel()
        panel.apply_status({'state': 'elevation_started'})
        panel._poll_ticks = self.module._POLL_WAIT_TICKS
        panel._poll_tick()
        self.assertFalse(panel._poll_timer.isActive())
        self.assertEqual(service.check_calls, 0)
        self.assertIn('重新检测', self.log_text(panel))

    def test_seen_log_extends_the_cap(self):
        panel, _service = self.make_panel()
        panel.apply_status({'state': 'elevation_started'})
        panel._poll_seen_log = True
        panel._poll_ticks = self.module._POLL_WAIT_TICKS
        panel._poll_tick()
        self.assertTrue(panel._poll_timer.isActive())

    def test_poll_never_blocks_buttons(self):
        panel, _service = self.make_panel()
        panel.apply_status({'state': 'elevation_started'})
        panel._poll_tick()
        self.assertFalse(panel._busy)
        self.assertTrue(panel.recheck_button.isEnabled())

    # ── 结论处理 ──────────────────────────────────────────────────────────
    def test_pending_verdict_keeps_polling_silently(self):
        panel, service = self.make_panel()
        panel.apply_status({'state': 'elevation_started'})
        before = self.log_text(panel)
        panel._poll_tick()
        self.assertTrue(self.spin_until(lambda: not panel._poll_inflight))
        self.assertEqual(service.check_calls, 1)
        self.assertTrue(panel._poll_timer.isActive())
        self.assertEqual(self.log_text(panel), before)

    def test_pending_reboot_verdict_stops_and_renders(self):
        panel, _service = self.make_panel(FakeService(
            {'ok': True, 'state': 'pending_reboot', 'needs_reboot': True,
             'message': '启用成功，但需要重启电脑才生效'}))
        panel.apply_status({'state': 'elevation_started'})
        panel._poll_tick()
        self.assertTrue(self.spin_until(lambda: not panel._poll_timer.isActive()))
        self.assertIn('等待重启', panel.status_label.text())
        self.assertIn('需要重启电脑', self.log_text(panel))

    def test_enabled_verdict_opens_channel_and_skips_status(self):
        panel, service = self.make_panel(FakeService(
            {'ok': True, 'state': 'enabled', 'message': 'Windows 沙盒功能已启用成功'}))
        panel.apply_status({'state': 'elevation_started'})
        panel._poll_tick()
        self.assertTrue(self.spin_until(lambda: not panel._poll_timer.isActive()))
        self.assertTrue(panel.channel_button.isEnabled())
        self.assertTrue(panel.recheck_button.isEnabled())
        # 关键：轮询只读提权日志，不能去问 status()——功能要重启才可见，status() 会误报未启用。
        self.assertEqual(service.status_calls, 0)

    def test_failed_verdict_allows_retry(self):
        panel, _service = self.make_panel(FakeService(
            {'ok': False, 'state': 'failed', 'exit_code': 1, 'message': '启用失败：错误 0x80070005'}))
        panel.apply_status({'state': 'elevation_started'})
        panel._poll_tick()
        self.assertTrue(self.spin_until(lambda: not panel._poll_timer.isActive()))
        self.assertTrue(panel.enable_button.isEnabled())
        self.assertIn('启用失败', panel.status_label.text())

    def test_internal_error_logs_once_and_keeps_waiting(self):
        panel, _service = self.make_panel(BoomService())
        panel.apply_status({'state': 'elevation_started'})
        panel._poll_tick()
        self.assertTrue(self.spin_until(lambda: not panel._poll_inflight))
        first = self.log_text(panel)
        self.assertIn('boom', first)
        self.assertTrue(panel._poll_timer.isActive())
        panel._poll_tick()
        self.assertTrue(self.spin_until(lambda: not panel._poll_inflight))
        self.assertEqual(self.log_text(panel), first)


if __name__ == '__main__':
    unittest.main()
