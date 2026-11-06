"""1.3.92 DSH 连接优化：发现缓存与单飞、探测并发上限、守护退避、连接码与面板降级。

全程离线：进程快照、线索端口、cookie 与 DSH 的 rpc 都是替身，不读真实 ~/.dsh、不打真实网络。
"""
import asyncio
import os
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import dsh_autoconnect as ac
from src import dsh_api
from src import dsh_discovery as discovery
from src.dsh_bridge import DshError

DSH = {'pid': 4242, 'parent': 0, 'name': 'DeepSeek Harness.exe'}
CODES = ('dsh_unreachable', 'dsh_auth_failed', 'dsh_version_mismatch', 'dsh_timeout')
_APP = None


def listening(port, address='127.0.0.1', pid=4242):
    return {'pid': pid, 'address': address, 'port': port}


class FakeService:
    """只实现发现用到的两处：已保存设置与只读 rpc；urls 之外的地址一律拒绝。"""

    def __init__(self, urls=(), saved='http://127.0.0.1:3080'):
        self.urls = set(urls)
        self.saved = saved
        self.calls = []

    def settings(self):
        return {'base_url': self.saved, 'enabled': True}

    async def rpc(self, client, base, method, request=None):
        self.calls.append((base, method))
        if base not in self.urls:
            raise DshError('连不上')
        if method == 'settings/describe':
            return {'models': [], 'providers': []}
        return {'presets': [{'id': 'whale-wechat-chat'}]}


class Clock:
    """可控时钟：验 TTL 不必真的等 5 秒。"""

    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


@contextmanager
def fake_scan(scans, inventory=None):
    """把「扫进程树 + 挖 ~/.dsh + 读 cookie」全换成替身，并数一数真扫了几次。"""
    inventory = ([DSH], [listening(3080)]) if inventory is None else inventory

    def snapshot():
        scans['n'] += 1
        return inventory

    with patch.object(discovery, 'snapshot', snapshot), \
         patch.object(discovery, 'dsh_home', lambda: Path('C:/nonexistent-dsh-1392')), \
         patch.object(discovery, 'hint_ports', lambda *a, **k: []), \
         patch.object(discovery, 'cookie_for', lambda base: 'opaque'):
        yield


class DiscoveryCacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = discovery.DiscoveryCache()
        patcher = patch.object(discovery, 'CACHE', self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_repeated_discovery_reuses_result_until_ttl_expires(self):
        service = FakeService({'http://127.0.0.1:3080'})
        clock = Clock()
        self.cache.clock = clock
        scans = {'n': 0}
        with fake_scan(scans):
            first = asyncio.run(discovery.discover(service))
            second = asyncio.run(discovery.discover(service))
            clock.value += discovery.DISCOVERY_TTL + 0.5
            third = asyncio.run(discovery.discover(service))
        self.assertEqual(first['state'], 'ok')
        self.assertFalse(first['cached'])
        self.assertTrue(second['cached'], 'TTL 内必须复用上一轮结论')
        self.assertEqual(second['state'], 'ok')
        self.assertFalse(third['cached'], '过期后必须重新扫一遍')
        self.assertEqual(scans['n'], 2, 'TTL 内只扫一次')

    def test_failed_discovery_is_cached_only_briefly(self):
        clock = Clock()
        self.cache.clock = clock
        kind, token = self.cache.begin('k')
        self.assertEqual(kind, 'lead')
        self.cache.finish('k', {'state': 'dsh_offline'}, token)
        self.assertEqual(self.cache.begin('k')[0], 'hit')
        clock.value += discovery.DISCOVERY_FAIL_TTL + 0.1
        kind, token = self.cache.begin('k')
        self.assertEqual(kind, 'lead', '失败结论不能钉住刚启动的 DSH')
        self.cache.abort(token)

    def test_concurrent_discovery_runs_one_scan_and_shares_it(self):
        service = FakeService({'http://127.0.0.1:3080'})
        scans = {'n': 0}

        def slow_snapshot():
            scans['n'] += 1
            time.sleep(0.05)
            return ([DSH], [listening(3080)])

        async def run_all():
            return await asyncio.gather(*(discovery.discover(service) for _ in range(3)))

        with patch.object(discovery, 'snapshot', slow_snapshot), \
             patch.object(discovery, 'dsh_home', lambda: Path('C:/nonexistent-dsh-1392')), \
             patch.object(discovery, 'hint_ports', lambda *a, **k: []), \
             patch.object(discovery, 'cookie_for', lambda base: 'opaque'):
            values = asyncio.run(run_all())
        self.assertEqual(scans['n'], 1, '并发发现只能真扫一次')
        self.assertEqual(len(values), 3)
        self.assertTrue(all(value['state'] == 'ok' for value in values), values)


class WatchBackoffTests(unittest.TestCase):
    def test_unhealthy_state_backs_off_and_stops_at_cap(self):
        stop = threading.Event()
        timer = threading.Timer(0.3, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        result = ac.watch(None, interval=0.01, stop_event=stop,
                          status_fn=lambda: {'state': 'pending'}, backoff=(0.01, 0.04))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(result['unhealthy'], 2, result)
        self.assertLessEqual(result['last_delay'], 0.04)
        self.assertEqual(result['cap'], 0.04)
        self.assertEqual(result['last_state'], 'pending')
        self.assertLess(elapsed, 2.0, '退避必须可注入，测试不许真等 1 秒起步')

    def test_ready_state_resets_backoff_to_base(self):
        calls = {'n': 0}

        def flaky():
            calls['n'] += 1
            return {'state': 'ready'} if calls['n'] > 2 else {'state': 'error'}

        stop = threading.Event()
        timer = threading.Timer(0.3, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        result = ac.watch(None, interval=0.01, stop_event=stop, status_fn=flaky, backoff=(0.01, 0.04))
        self.assertGreaterEqual(result['unhealthy'], 1, result)
        self.assertLessEqual(result['last_delay'], 0.04)
        self.assertEqual(result['last_state'], 'ready')


class StatusCodeTests(unittest.TestCase):
    """断线后 /api/dsh 必须给出真实码，不能因为「上次探通过」继续报连接正常。"""

    def status(self, stage, last_error=''):
        app = FastAPI()
        app.state.bot = SimpleNamespace(dsh=SimpleNamespace(
            stage=stage, last_error=last_error, last_announce=None, checked_at=None,
            settings=lambda: {'enabled': True, 'base_url': 'http://127.0.0.1:3080'}))
        dsh_api.add_routes(app)
        return TestClient(app).get('/api/dsh').json()

    def test_failed_stage_is_reported_as_unreachable_not_ok(self):
        body = self.status('failed', '无法连接 DSH 地址（连接被拒绝或超时）')
        self.assertEqual(body['stage'], 'failed')
        self.assertEqual(body['code'], 'dsh_unreachable')
        self.assertNotEqual(body['code_text'], discovery.CODE_TEXT['dsh_ok'])
        self.assertTrue(body['code_hint'])

    def test_ready_stage_is_reported_as_ok(self):
        body = self.status('ready')
        self.assertEqual(body['code'], 'dsh_ok')

    def test_preset_missing_stage_has_its_own_code(self):
        body = self.status('preset_missing', 'DSH已连接，缺少专用无工具人设')
        self.assertEqual(body['code'], 'dsh_preset_missing')
        self.assertTrue(body['code_hint'])


class StageCodeTests(unittest.TestCase):
    """断线原因要分得清：同样是 stage=failed，鉴权失败与端口没开不是一回事。"""

    def status(self, stage, last_error):
        app = FastAPI()
        app.state.bot = SimpleNamespace(dsh=SimpleNamespace(
            stage=stage, last_error=last_error, last_announce=None, checked_at=None,
            settings=lambda: {'enabled': True, 'base_url': 'http://127.0.0.1:3080'}))
        dsh_api.add_routes(app)
        return TestClient(app).get('/api/dsh').json()

    def test_failed_stage_keeps_auth_failure_apart_from_unreachable(self):
        auth = self.status('failed', 'DSH接口不可用或鉴权失败，请检测地址/登录态')
        self.assertEqual(auth['code'], 'dsh_auth_failed')
        self.assertNotEqual(auth['code_hint'], discovery.CODE_HINT['dsh_unreachable'])
        refused = self.status('failed', '无法连接 DSH 地址（连接被拒绝或超时）')
        self.assertEqual(refused['code'], 'dsh_unreachable')

    def test_unclassifiable_failure_falls_back_to_the_stage_code(self):
        body = self.status('failed', 'RuntimeError')
        self.assertEqual(body['code'], 'dsh_unreachable')
        self.assertTrue(body['code_text'])


class BudgetTests(unittest.TestCase):
    """并发等待不能把「一次发现的预算」乘成两倍，否则先撞 15 秒硬超时。"""

    def test_waiter_never_outlives_the_leader_budget(self):
        cache = discovery.DiscoveryCache()
        cache.clock = Clock()
        patcher = patch.object(discovery, 'CACHE', cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        service = FakeService({'http://127.0.0.1:3080'})
        key = discovery.normalized(service.settings()['base_url'])
        kind, token = cache.begin(key, budget=4.0)
        self.assertEqual(kind, 'lead')
        cache.clock.value += 3.5            # 领跑者已经用掉 3.5 秒，只剩 0.5 秒
        started = time.monotonic()
        value = asyncio.run(discovery.discover(service, budget=4.0))
        elapsed = time.monotonic() - started
        cache.abort(token)
        self.assertEqual(value['state'], 'timeout')
        self.assertLess(elapsed, 2.0, '等的人只能等到领跑者预算用完，不能自己再等一整份')


class CodeTableTests(unittest.TestCase):
    def test_three_failure_codes_have_chinese_text_and_actionable_hint(self):
        for code in CODES:
            with self.subTest(code=code):
                text = discovery.describe_code(code)
                hint = discovery.hint_for_code(code)
                self.assertTrue(text and text != code, text)
                self.assertTrue(hint, hint)
                self.assertTrue(any('\u4e00' <= char <= '\u9fff' for char in text + hint))

    def test_unknown_code_returns_empty_instead_of_guessing(self):
        for value in ('dsh_unknown_thing', '', None):
            with self.subTest(value=value):
                self.assertEqual(discovery.describe_code(value), '')
                self.assertEqual(discovery.hint_for_code(value), '')

    def test_every_state_maps_to_a_known_code(self):
        for state, code in discovery._STATE_CODE.items():
            with self.subTest(state=state):
                self.assertIn(code, discovery.CODE_TEXT)
                self.assertIn(code, discovery.CODE_HINT)
                self.assertEqual(discovery.code_for_state(state), code)

    def test_refused_connection_is_unreachable_not_timeout(self):
        refused = '无法连接 DSH 地址（连接被拒绝或超时）；请先打开桌面 DSH，再点「自动查找桌面DSH」'
        self.assertEqual(discovery.code_for_message(refused), 'dsh_unreachable')
        self.assertEqual(discovery.code_for_message('DSH模型回合超时，结果未知，本轮不重复提交'),
                         'dsh_timeout')
        self.assertEqual(discovery.code_for_message('DSH接口不可用或鉴权失败，请检测地址/登录态'),
                         'dsh_auth_failed')
        self.assertEqual(discovery.code_for_message('DSH协议信封格式不兼容'), 'dsh_version_mismatch')
        self.assertEqual(discovery.code_for_message('缺少专用无工具人设'), 'dsh_preset_missing')
        self.assertEqual(discovery.code_for_message('完全看不懂的失败'), '')


def qt_app():
    """整个模块共用一个 QApplication：局部变量被回收会把还活着的对话框一起删掉。"""
    global _APP
    if _APP is None:
        from PyQt6.QtWidgets import QApplication
        _APP = QApplication.instance() or QApplication([])
    return _APP


class VerifyConcurrencyTests(unittest.TestCase):
    """并发核验必须被 PROBE_CONCURRENCY 卡住：再多端口也不能同时打一堆请求。"""

    def test_probe_concurrency_is_capped_and_still_parallel(self):
        in_flight = {'now': 0, 'max': 0}
        lock = threading.Lock()

        class SlowService:
            def settings(self):
                return {'base_url': 'http://127.0.0.1:1'}

            async def rpc(self, client, base, method, request=None):
                with lock:
                    in_flight['now'] += 1
                    in_flight['max'] = max(in_flight['max'], in_flight['now'])
                try:
                    await asyncio.sleep(0.02)
                    raise DshError('连不上')
                finally:
                    with lock:
                        in_flight['now'] -= 1

        rows = [dict(pid=0, process='probe', base_url=f'http://127.0.0.1:{20000 + i}')
                for i in range(discovery.PROBE_CONCURRENCY * 3)]
        with patch.object(discovery, 'cookie_for', lambda base: 'opaque'):
            verified = asyncio.run(discovery.verify(SlowService(), rows))
        self.assertEqual(len(verified), len(rows), '核验失败的行也要原样回来，界面才数得清端口')
        self.assertFalse(any(row['verified'] for row in verified))
        self.assertLessEqual(in_flight['max'], discovery.PROBE_CONCURRENCY, in_flight)
        self.assertGreater(in_flight['max'], 1, '并发核验要真的并行，否则慢端口会拖死整轮')


class DegradedTextTests(unittest.TestCase):
    """端点全不可达时不能只说「失败」，必须给一条能照做的中文下一步。"""

    def test_all_hint_ports_dead_names_not_running_with_action(self):
        service = FakeService(saved='http://127.0.0.1:3080')

        def dead_snapshot():
            return ([], [])

        with patch.object(discovery, 'snapshot', dead_snapshot), \
             patch.object(discovery, 'dsh_home', lambda: Path('.')), \
             patch.object(discovery, 'cookie_for', lambda base: 'opaque'):
            value = asyncio.run(discovery.discover(service, hints=[9527]))
        self.assertEqual(value['state'], 'dsh_offline')
        self.assertEqual(value['code'], 'dsh_not_running')
        self.assertEqual(value['code_hint'], discovery.hint_for_code('dsh_not_running'))
        self.assertIn('桌面 DSH', value['hint'])
        self.assertIn('桌面 DSH', value['code_hint'])
        self.assertEqual(value['candidates'], [])
        self.assertEqual(value['probed_ports'], 1)

    def test_timeout_degraded_result_has_the_same_shape(self):
        value = discovery._degraded('timeout', discovery.CODE_HINT['dsh_timeout'],
                                    'DSH自动查找超时，未完成端口核验', 'http://127.0.0.1:3080')
        self.assertTrue(value['success'])
        self.assertEqual(value['state'], 'timeout')
        self.assertEqual(value['code'], 'dsh_timeout')
        self.assertTrue(value['code_text'] and value['code_hint'])
        self.assertIn('重试', value['code_hint'])
        for key in ('candidates', 'process_count', 'checked_ports', 'unverified_ports', 'drift',
                    'saved_base_url', 'hint', 'message', 'probed_ports', 'port_owner_known', 'cached'):
            with self.subTest(key=key):
                self.assertIn(key, value, '降级结果必须和正常结果同形，界面不写第二套分支')

    def test_degraded_result_renders_in_panel(self):
        qt_app()
        from src.gui.dsh_panel import DshDialog
        dialog = DshDialog(lambda *a, **k: None)
        self.addCleanup(dialog.close)
        value = discovery._degraded('timeout', discovery.CODE_HINT['dsh_timeout'],
                                    'DSH自动查找超时，未完成端口核验')
        dialog.discovered(value)
        text = dialog.status.text()
        self.assertIn('dsh_timeout', text)
        self.assertIn('下一步', text)
        self.assertTrue(dialog.discover_button.isEnabled())


class PanelCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def dialog(self):
        from src.gui.dsh_panel import DshDialog
        calls = []

        def api(owner, method, path, data=None, **kwargs):
            calls.append((method, path))
            return None

        dialog = DshDialog(api)
        self.addCleanup(dialog.close)
        return dialog, calls

    def test_result_renders_code_and_hint_lines(self):
        dialog, _ = self.dialog()
        dialog.result({'success': False, 'state': 'dsh_offline', 'code': 'dsh_not_running',
                       'code_text': discovery.describe_code('dsh_not_running'),
                       'code_hint': discovery.hint_for_code('dsh_not_running')})
        text = dialog.status.text()
        self.assertIn('dsh_not_running', text)
        self.assertIn('下一步', text)

    def test_result_survives_code_without_text_or_hint(self):
        dialog, _ = self.dialog()
        dialog.result({'success': False, 'code': 'dsh_unreachable'})
        self.assertIn('dsh_unreachable', dialog.status.text())
        dialog.result({'success': False, 'code': None, 'code_text': None, 'code_hint': None})
        self.assertTrue(dialog.status.text())
        dialog.result({'success': False, 'state': 'timeout', 'code': 1392})
        self.assertTrue(dialog.status.text(), '码类型异常时也不许崩')


if __name__ == '__main__':
    unittest.main()
