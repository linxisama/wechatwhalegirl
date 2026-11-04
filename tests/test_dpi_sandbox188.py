"""1.3.88：DPI 探测的三级降级，以及找不到 wsb.exe 时两个面板说同一句话。

用户报的两件事：144 DPI 的机器上自检说「缩放 100%」（GetDpiForWindow 挂错到 shcore，
异常被当成「老系统没这个 API」吞掉），以及 /api/sandbox/status 与
/api/sandbox/provision/status 各说各话。这里钉住降级顺序和文案。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import sandbox_api as api
from src import sandbox_manager as manager
from src import site_profile

OLD_MESSAGE = '未找到 Windows Sandbox 命令行，请先启用并打开 Windows 沙盒'


class _FakeDll:
    """只实现用例点名的函数；没实现的名字访问时抛 AttributeError，正是老系统的真实行为。"""

    def __init__(self, **functions):
        for name, value in functions.items():
            setattr(self, name, value)


class _FakeWinDll:
    def __init__(self, user32, shcore=None):
        self._user32 = user32
        self._shcore = shcore

    def __call__(self, name, **kwargs):
        if name == 'user32':
            return self._user32
        if name == 'shcore' and self._shcore is not None:
            return self._shcore
        raise OSError('这台机器上没有 ' + name)


def _scale_with(user32, shcore=None):
    with patch.object(site_profile.ctypes, 'WinDLL', _FakeWinDll(user32, shcore)):
        return site_profile._probe_scale()


class ScaleProbeTests(unittest.TestCase):
    # ── 144 DPI 就是 150%：旧实现每次都退回系统 DPI，把这台机器报成 100% ──────
    def test_window_dpi_is_measured_and_144_dpi_is_150_percent(self):
        user32 = _FakeDll(GetForegroundWindow=lambda: 4242,
                          GetDpiForWindow=lambda hwnd: 144,
                          GetDpiForSystem=lambda: 96)
        item = _scale_with(user32)
        self.assertEqual(item['data']['dpi'], 144)
        self.assertEqual(item['data']['scale_percent'], 150)
        self.assertEqual(item['data']['dpi_source'], 'window')
        self.assertEqual(item['data']['window_dpi'], 144)
        self.assertIn('150%', item['detail'])

    # ── GetDpiForWindow 缺失（Win10 1607 以前）→ 退到 GetDpiForMonitor ────────
    def test_missing_window_api_falls_back_to_monitor_dpi(self):
        def get_dpi_for_monitor(monitor, kind, dpi_x, dpi_y):
            self.assertEqual(kind, 0)  # MDT_EFFECTIVE_DPI
            dpi_x._obj.value = 144
            dpi_y._obj.value = 144
            return 0

        user32 = _FakeDll(GetForegroundWindow=lambda: 4242,
                          MonitorFromWindow=lambda hwnd, flag: 7,
                          GetDpiForSystem=lambda: 96)
        shcore = _FakeDll(GetDpiForMonitor=get_dpi_for_monitor)
        item = _scale_with(user32, shcore)
        self.assertEqual(item['data']['dpi'], 144)
        self.assertEqual(item['data']['dpi_source'], 'monitor')
        self.assertEqual(item['data']['scale_percent'], 150)

    # ── 两级都拿不到 → 系统 DPI 兜底，但必须在 data 里标明是兜底 ──────────────
    def test_system_dpi_fallback_is_marked_in_data(self):
        user32 = _FakeDll(GetForegroundWindow=lambda: 4242,
                          MonitorFromWindow=lambda hwnd, flag: 0,
                          GetDpiForSystem=lambda: 120)
        item = _scale_with(user32)
        self.assertEqual(item['data']['dpi_source'], 'system')
        self.assertEqual(item['data']['dpi'], 120)
        self.assertEqual(item['data']['scale_percent'], 125)
        self.assertIsNone(item['data']['window_dpi'])
        self.assertEqual(item['status'], 'warn')
        self.assertIn('兜底', item['detail'])

    # ── 一个数都拿不到时宁可 unknown，也不许静默写 100% ──────────────────────
    def test_no_dpi_source_raises_instead_of_reporting_100_percent(self):
        user32 = _FakeDll(GetForegroundWindow=lambda: 0)
        with self.assertRaises(RuntimeError):
            _scale_with(user32)

    def test_collect_degrades_scale_to_unknown_without_inventing_100_percent(self):
        with patch.object(site_profile, '_probe_scale',
                          side_effect=RuntimeError('三个来源都没有 DPI')):
            result = site_profile.collect()
        item = {entry['key']: entry for entry in result['items']}['scale']
        self.assertEqual(item['status'], 'unknown')
        self.assertNotEqual(item['data'].get('scale_percent'), 100)
        self.assertIn('三个来源都没有 DPI', item['detail'])


class _NoWsb:
    """伪造一台没有 wsb.exe 的 Windows：System32 与 WindowsApps 都空，PATH 也空。"""

    def __init__(self, *names):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'System32').mkdir()
        for name in names:
            (self.root / 'System32' / name).write_bytes(b'x')

    def __enter__(self):
        self._env = patch.dict(os.environ, {
            'SystemRoot': str(self.root),
            'LOCALAPPDATA': str(self.root / 'no-local-appdata'),
            'PATH': ''})
        self._env.start()
        # 解析器还会问 Known Folder API，一并钉到临时目录，免得本机真装的 wsb.exe 让用例失效
        self._known = patch.object(site_profile, '_known_local_appdata',
                                   return_value=str(self.root / 'no-local-appdata'))
        self._known.start()
        return self

    def __exit__(self, *exc):
        self._known.stop()
        self._env.stop()
        self._tmp.cleanup()
        return False


class FindWsbTests(unittest.TestCase):
    # ── 旧文案只说「请先启用并打开沙盒」，对没有 wsb.exe 的 Win10 是错的指路 ──
    def test_missing_wsb_uses_the_new_hint_and_drops_the_old_sentence(self):
        with _NoWsb('WindowsSandbox.exe'):
            with self.assertRaises(manager.SandboxError) as caught:
                manager.find_wsb()
        message = str(caught.exception)
        self.assertNotIn(OLD_MESSAGE, message)
        self.assertIn('wsb.exe', message)
        self.assertIn('Windows 10', message)
        self.assertIn('/api/sandbox/provision/status', message)
        self.assertIn('沙盒诊断.bat', message)

    # ── 契约不变：找到就返回路径（4 处 patch.object(manager,'find_wsb') 依赖）──
    def test_existing_wsb_is_returned_as_a_path(self):
        with _NoWsb('wsb.exe') as env:
            found = manager.find_wsb()
        self.assertEqual(Path(found), env.root / 'System32' / 'wsb.exe')


class _StubBot:
    def __init__(self, root):
        self.root = root
        self.running = False
        self._sandbox_preparing = False
        self.config = {'wechat.environment': 'local', 'wechat.sandbox_channel_id': ''}


class StatusMessageTests(unittest.TestCase):
    # ── /api/sandbox/status 的 message 必须和 find_wsb 抛的原文逐字一致 ────────
    def test_status_api_repeats_the_same_missing_wsb_message(self):
        with _NoWsb('WindowsSandbox.exe') as env:
            root = env.root / 'bot-root'
            root.mkdir()
            app = FastAPI()
            app.state.bot = _StubBot(root)
            api.add_routes(app)
            with TestClient(app) as client:
                response = client.get('/api/sandbox/status')
            with self.assertRaises(manager.SandboxError) as caught:
                manager.find_wsb()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body['available'])
        self.assertEqual(body['message'], str(caught.exception))
        self.assertNotIn(OLD_MESSAGE, body['message'])


if __name__ == '__main__':
    unittest.main()
