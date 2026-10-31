r"""1.3.66 控制台↔DSH 零配置连接：ensure/status/watch/说明/接口。

全程离线：配置文件、provision、connector 全部用临时目录与替身，绝不碰真实 C:\Users\16522\.dsh。
"""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch as mock_patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import agent_mcp_connector
from src import dsh_autoconnect as ac
from src import dsh_autoconnect_api as api
from src import dsh_mcp_provision as pm

REGISTRY_ONLY = (
    '- id: agent-preset-registry\n'
    "  name: '@deepseek-ai/dsh-agent-preset-registry'\n"
    '  config:\n'
    '    default: standard\n'
    '    selectedDefault: cordis\n'
)
PYTHON = 'C:/Python312/python.exe'
TOKEN = 'tok-166'


class FakeDsh:
    def __init__(self, base='http://127.0.0.1:9175'):
        self.base = base
        self.last_announce = None

    def settings(self):
        return {'enabled': True, 'base_url': self.base, 'model': 'deepseek-flash'}


class FakeRuntime:
    def __init__(self, root, port=8091, base='http://127.0.0.1:9175'):
        self.root = Path(root)
        self.port = port
        self.dsh = FakeDsh(base)


class FakeConnector:
    """refresh_existing 的替身：默认按磁盘内容判断 current/missing，从不新建。"""

    def __init__(self, reason=None, place=None):
        self.reason = reason
        self.refresh_calls = 0
        self.place_calls = 0
        self.place = place or agent_mcp_connector.place_connector
        self.CONNECTOR = agent_mcp_connector.CONNECTOR
        self.generated_by_console = agent_mcp_connector.generated_by_console

    def refresh_existing(self, path):
        self.refresh_calls += 1
        target = Path(path)
        if self.reason is not None:
            return {'changed': False, 'backup': '', 'reason': self.reason}
        try:
            if target.is_file() and target.read_bytes() == agent_mcp_connector.CONNECTOR.encode('utf-8'):
                return {'changed': False, 'backup': '', 'reason': 'current'}
        except OSError:
            return {'changed': False, 'backup': '', 'reason': 'error'}
        return {'changed': False, 'backup': '', 'reason': 'missing'}

    def place_connector(self, path):
        self.place_calls += 1
        return self.place(path)


def unmanaged_text(root, python=PYTHON):
    """真实机器上的形态：旧版/手写的工作台预设，没有标记行，路径与凭据都是裸值。"""
    script = Path(root) / 'data' / 'agent_workspace' / 'whale_dsh_mcp.py'
    return ('- insert:\n'
            '    - id: preset-whale-workbench\n'
            "      name: '@deepseek-ai/dsh-agent-preset'\n"
            '      config:\n'
            '        id: whale-workbench\n'
            '        plugins:\n'
            '          - id: mcp-whale-wechat\n'
            "            name: '@deepseek-ai/dsh-mcp-client'\n"
            '            config:\n'
            '              serverName: whale_wechat\n'
            '              transport: stdio\n'
            '              command: ' + python + '\n'
            '              args:\n'
            '                - ' + str(script) + '\n'
            '              env:\n'
            '                WHALE_BOT_URL: http://127.0.0.1:8091\n'
            '                WHALE_AGENT_TOKEN: tok_plain_166\n')


def provision_fake(patch, *, token=TOKEN, python=PYTHON, boom=None):
    """provision 替身：真的往临时补丁文件里写标记块，但不签任何凭据。"""
    calls = []

    def run(bot, *, write=True):
        calls.append({'write': write})
        if boom is not None:
            raise boom
        script = Path(bot.root) / 'data' / 'agent_workspace' / 'whale_dsh_mcp.py'
        block = pm.build_block(python, script, token, ac.console_url(bot))
        placed = pm.write_block(patch, block)
        return {'success': True, 'server_name': 'whale_wechat', 'token': token, 'script': str(script),
                'patch': str(patch), 'written': True, 'backup': placed['backup'], 'connector_backup': '',
                'block': block, 'message': '已写入'}

    run.calls = calls
    return run


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = self.root / 'cordis.patch.yml'
        self.patch.write_text(REGISTRY_ONLY, 'utf-8')
        self.runtime = FakeRuntime(self.root)
        self.connector = FakeConnector()

    def ensure(self, **kwargs):
        kwargs.setdefault('provision', provision_fake(self.patch))
        kwargs.setdefault('connector', self.connector)
        kwargs.setdefault('python', PYTHON)
        return ac.ensure(self.runtime, patch=self.patch, **kwargs)

    def states(self, result):
        return [step['state'] for step in result['steps']]


class EnsureTests(Base):
    def test_fresh_install_runs_all_three_steps(self):
        result = self.ensure()
        self.assertEqual(self.states(result), ['generated', 'written', 'written'], result)
        self.assertEqual((result['state'], result['ok']), ('ready', True), result)
        text = self.patch.read_text('utf-8')
        self.assertIn(ac.USER_START, text)
        self.assertIn('- id: ' + ac.ROW_ID, text)
        self.assertIn(pm.START, text)
        self.assertTrue((self.root / 'data' / 'agent_workspace' / 'whale_dsh_mcp.py').is_file())

    def test_ready_install_short_circuits_without_new_credential(self):
        self.ensure()
        before = self.patch.read_bytes()
        second = provision_fake(self.patch)
        result = self.ensure(provision=second)
        self.assertEqual(second.calls, [], '已就绪时不该再签凭据/重写配置块')
        self.assertEqual(result['state'], 'ready', result)
        self.assertEqual(self.states(result), ['current', 'current', 'current'], result)
        self.assertEqual(self.patch.read_bytes(), before, '就绪时不应改动配置文件')

    def test_connector_foreign_is_left_untouched(self):
        target = self.root / 'data' / 'agent_workspace' / 'whale_dsh_mcp.py'
        target.parent.mkdir(parents=True)
        target.write_text('# 我自己改的连接器\nprint(1)\n', 'utf-8')
        before = target.read_bytes()
        result = self.ensure(connector=FakeConnector(reason='foreign'))
        self.assertEqual(target.read_bytes(), before, 'foreign 连接器必须原样保留')
        self.assertEqual(result['steps'][0]['state'], 'foreign')
        self.assertEqual(result['state'], 'partial')
        self.assertFalse(result['ok'])

    def test_provision_exception_is_recorded_not_raised(self):
        result = self.ensure(provision=provision_fake(self.patch, boom=RuntimeError('磁盘满了')))
        self.assertEqual(result['steps'][1]['name'], 'mcp-block')
        self.assertEqual(result['steps'][1]['state'], 'failed')
        self.assertIn('RuntimeError', result['steps'][1]['detail'])
        self.assertEqual((result['state'], result['ok']), ('failed', False))

    def test_default_row_failure_degrades_to_partial(self):
        with mock_patch.object(ac, 'write_user_block', side_effect=OSError('目标被占用')):
            result = self.ensure()
        self.assertEqual(result['steps'][2]['state'], 'failed')
        self.assertEqual(result['state'], 'partial')
        self.assertFalse(result['ok'])
        self.assertNotIn(ac.USER_START, self.patch.read_text('utf-8'))

    def test_second_run_is_idempotent_and_keeps_bytes(self):
        self.ensure()
        before = self.patch.read_bytes()
        result = self.ensure()
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(self.patch.read_bytes(), before)

    def test_runtime_none_still_returns_dict(self):
        result = ac.ensure(None, patch=self.root / 'missing.yml', connector=FakeConnector(),
                           provision=provision_fake(self.patch), python=PYTHON, root=self.root)
        self.assertIsInstance(result, dict)
        self.assertIn(result['state'], ('partial', 'failed'))
        self.assertEqual([step['name'] for step in result['steps']],
                         ['connector', 'mcp-block', 'new-session-default'])

    def test_broken_connector_module_does_not_block_other_steps(self):
        class Boom:
            CONNECTOR = agent_mcp_connector.CONNECTOR

            def refresh_existing(self, path):
                raise OSError('盘符掉了')

        result = self.ensure(connector=Boom())
        self.assertEqual(result['steps'][0]['state'], 'failed')
        self.assertEqual(result['steps'][1]['state'], 'written', '第一步失败不能让第二步不跑')
        self.assertEqual(result['state'], 'failed')

    def test_stale_block_is_rebuilt(self):
        provision_fake(self.patch, python='C:/OldPython/python.exe')(self.runtime)  # 先人为写入过期块
        fresh = provision_fake(self.patch)
        result = self.ensure(provision=fresh)
        self.assertEqual(len(fresh.calls), 1, '块里解释器过期时必须重写')
        self.assertEqual(result['steps'][1]['state'], 'written')
        self.assertIn('C:/Python312/python.exe', self.patch.read_text('utf-8'))

    def test_missing_patch_does_not_burn_credential(self):
        fake = provision_fake(self.patch)
        result = ac.ensure(self.runtime, provision=fake, connector=FakeConnector(),
                           python=PYTHON, patch=self.root / 'nope.yml', root=self.root)
        self.assertEqual(fake.calls, [], '没有 DSH 配置时不该签发凭据')
        self.assertEqual(result['steps'][1]['state'], 'pending')
        self.assertEqual(result['state'], 'partial')


    def test_unmanaged_equivalent_block_is_adopted_without_duplicate(self):
        # 真实机器的形态：旧版/手写的工作台预设没有标记行，路径与凭据都是裸值。
        self.patch.write_text(REGISTRY_ONLY + unmanaged_text(self.root, ), 'utf-8')
        fake = provision_fake(self.patch)
        result = ac.ensure(self.runtime, provision=fake, connector=FakeConnector(), python=PYTHON,
                           patch=self.patch, root=self.root)
        self.assertEqual(fake.calls, [], '已有等价预设时不许再写一份，避免重复 preset')
        self.assertEqual(result['steps'][1]['state'], 'current', result)
        text = self.patch.read_text('utf-8')
        self.assertNotIn(pm.START, text)
        self.assertIn('tok_plain_166', text)
        self.assertIn(ac.USER_START, text, '裸值凭据也要能读出来并写进用户级行')
        self.assertEqual(result['state'], 'ready', result)

    def test_unmanaged_stale_block_is_never_overwritten(self):
        self.patch.write_text(REGISTRY_ONLY + unmanaged_text(self.root, python='C:/OldPython/python.exe'), 'utf-8')
        fake = provision_fake(self.patch)
        result = ac.ensure(self.runtime, provision=fake, connector=FakeConnector(), python=PYTHON,
                           patch=self.patch, root=self.root)
        self.assertEqual(fake.calls, [], '手写预设内容不一致时也不能覆盖')
        text = self.patch.read_text('utf-8')
        self.assertIn(unmanaged_text(self.root, 'C:/OldPython/python.exe'), text, '手写预设段必须原样保留')
        self.assertEqual(text.count('- id: preset-whale-workbench'), 1, '不许追加第二份预设')
        self.assertIn(ac.USER_START, text, '只允许追加用户级插件行')
        self.assertEqual(result['steps'][1]['state'], 'foreign')
        self.assertEqual(result['state'], 'partial')


class StatusTests(Base):
    def test_ready_status_reports_connector_preset_and_actions(self):
        self.ensure()
        value = ac.status(self.runtime, patch=self.patch, python=PYTHON)
        self.assertEqual(value['state'], 'ready', value)
        self.assertEqual(value['base_url'], 'http://127.0.0.1:9175')
        self.assertEqual(value['preset'], 'cordis')
        self.assertTrue(value['default_preset_set'])
        self.assertEqual(value['connector']['version'], '2')
        self.assertEqual(len(value['connector']['sha256']), 64)
        self.assertTrue(value['connector']['path'].endswith('whale_dsh_mcp.py'))
        self.assertIsNone(value['last_announce_at'])
        self.assertIn('repair', [row['key'] for row in value['actions']])

    def test_pending_status_when_patch_missing(self):
        value = ac.status(self.runtime, patch=self.root / 'nope.yml')
        self.assertEqual(value['state'], 'pending')
        self.assertIn('patch_missing', value['issues'])
        self.assertIn('DeepSeek Harness', value['hint'])
        self.assertTrue(value['actions'])

    def test_base_url_falls_back_to_8091(self):
        class Broken:
            root = str(self.root)
            port = 8091

            class dsh:
                @staticmethod
                def settings():
                    raise RuntimeError('配置读不出来')

        value = ac.status(Broken(), patch=self.patch)
        self.assertEqual(value['base_url'], 'http://127.0.0.1:8091')

    def test_error_status_on_foreign_connector(self):
        target = self.root / 'data' / 'agent_workspace' / 'whale_dsh_mcp.py'
        target.parent.mkdir(parents=True)
        target.write_text('# 手写连接器\n', 'utf-8')
        value = ac.status(self.runtime, patch=self.patch)
        self.assertEqual(value['state'], 'error')
        self.assertIn('connector_foreign', value['issues'])
        self.assertTrue(value['message'])

    def test_status_accepts_unmanaged_equivalent_block(self):
        text = REGISTRY_ONLY + unmanaged_text(self.root, PYTHON)
        self.patch.write_text(text, 'utf-8')
        value = ac.status(self.runtime, patch=self.patch, python=PYTHON)
        self.assertNotIn('block_missing', value['issues'])
        self.assertNotIn('block_unmanaged', value['issues'])

    def test_last_announce_is_formatted(self):
        self.ensure()
        self.runtime.dsh.last_announce = {'at': 1700000000.0, 'state': 'connected', 'base_url': 'x'}
        value = ac.status(self.runtime, patch=self.patch)
        self.assertTrue(str(value['last_announce_at']).startswith('2023-'), value['last_announce_at'])

    def test_default_preset_route_counts_as_ready(self):
        # A 结论回归：selectedDefault 指向工作台预设时，即使没有用户级插件行也算新会话可见。
        self.patch.write_text(REGISTRY_ONLY.replace('selectedDefault: cordis', 'selectedDefault: ' + ac.PRESET_ID), 'utf-8')
        fake = provision_fake(self.patch)
        result = ac.ensure(self.runtime, provision=fake, connector=FakeConnector(), python=PYTHON,
                           patch=self.patch, root=self.root)
        self.assertEqual(result['steps'][2]['state'], 'current', result)
        self.assertNotIn(ac.USER_START, self.patch.read_text('utf-8'))
        value = ac.status(self.runtime, patch=self.patch, python=PYTHON)
        self.assertEqual(value['state'], 'ready')
        self.assertEqual(value['preset'], ac.PRESET_ID)
        self.assertTrue(value['default_preset_set'])


class WatchTests(unittest.TestCase):
    def test_watch_runs_cycles_until_stopped(self):
        stop = threading.Event()
        timer = threading.Timer(0.25, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        result = ac.watch(None, interval=0.05, stop_event=stop, status_fn=lambda: {'state': 'ready'})
        self.assertTrue(result['stopped'])
        self.assertGreaterEqual(result['cycles'], 2, result)
        self.assertEqual(result['errors'], 0)
        self.assertEqual(result['last_state'], 'ready')

    def test_watch_backs_off_on_errors_without_dying(self):
        calls = {'n': 0}

        def flaky():
            calls['n'] += 1
            if calls['n'] <= 2:
                raise RuntimeError('读状态失败')
            return {'state': 'pending'}

        stop = threading.Event()
        timer = threading.Timer(0.4, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        result = ac.watch(None, interval=0.02, stop_event=stop, status_fn=flaky, backoff=(0.01, 0.04))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(result['errors'], 2, result)
        self.assertGreaterEqual(result['cycles'], 1, result)
        self.assertEqual(result['last_state'], 'pending')
        self.assertLess(elapsed, 2.0, '退避必须可注入，测试不许真等 1 秒起步')

    def test_no_module_level_mutable_state_and_independent_watches(self):
        mutable = [name for name, value in vars(ac).items()
                   if not name.startswith('__') and isinstance(value, (list, dict, set, bytearray))]
        self.assertEqual(mutable, [], 'watch/ensure 不允许用模块级可变全局')
        results = {}

        def run(key, state):
            stop = threading.Event()
            timer = threading.Timer(0.2, stop.set)
            timer.start()
            try:
                results[key] = ac.watch(None, interval=0.02, stop_event=stop,
                                        status_fn=lambda: {'state': state}, backoff=(0.01, 0.02))
            finally:
                timer.cancel()

        threads = [threading.Thread(target=run, args=('a', 'ready')),
                   threading.Thread(target=run, args=('b', 'pending'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
        self.assertEqual(results['a']['last_state'], 'ready')
        self.assertEqual(results['b']['last_state'], 'pending')
        self.assertGreaterEqual(results['a']['cycles'], 1)
        self.assertGreaterEqual(results['b']['cycles'], 1)


class InstructionsTests(unittest.TestCase):
    def test_instructions_are_short_and_copyable(self):
        text = ac.connect_instructions()
        lines = text.splitlines()
        self.assertGreaterEqual(len(lines), 3)
        self.assertLessEqual(len(lines), 5)
        self.assertIn('whale_wechat', text)
        self.assertIn('「', text, '必须给出可直接复制的话术')
        self.assertNotIn('None', text)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.runtime = FakeRuntime(self.tmp.name)
        self.app = FastAPI()
        self.service = api.DshConnectService(
            self.runtime,
            status=lambda runtime: {'state': 'pending', 'base_url': 'http://127.0.0.1:8091'},
            ensure=lambda runtime: {'ok': True, 'state': 'ready', 'at': '2026-01-01T00:00:00',
                                    'steps': [{'name': 'connector', 'state': 'current', 'detail': '好'}],
                                    'message': '已就绪'},
            instructions=lambda: '1\n2\n3',
            watch=self.quick_watch)
        self.app.state.dsh_connect_service = self.service
        api.register(self.app, self.runtime)
        self.client = TestClient(self.app)

    @staticmethod
    def quick_watch(runtime, interval=30.0, stop_event=None):
        stop_event.wait(0.05)
        return {'cycles': 1, 'errors': 0, 'interval': interval, 'stopped': True, 'last_state': 'ready'}

    def test_get_connect_returns_status_and_instructions(self):
        body = self.client.get('/api/dsh/connect').json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['status']['state'], 'pending')
        self.assertEqual(body['instructions'].count('\n') + 1, 3)

    def test_repair_returns_steps(self):
        body = self.client.post('/api/dsh/connect/repair').json()
        self.assertEqual((body['ok'], body['state']), (True, 'ready'))
        self.assertEqual(body['steps'][0]['name'], 'connector')
        self.assertEqual(body['at'], '2026-01-01T00:00:00')

    def test_watch_endpoint_starts_and_stops_thread_handle_on_runtime(self):
        started = self.client.post('/api/dsh/connect/watch', json={'enabled': True}).json()
        self.assertTrue(started['ok'])
        box = getattr(self.runtime, ac.WATCH_ATTR)
        self.assertIsInstance(box['thread'], threading.Thread)
        stopped = self.client.post('/api/dsh/connect/watch', json={'enabled': False}).json()
        self.assertTrue(stopped['ok'])
        self.assertFalse(stopped['enabled'])
        self.assertFalse(box['thread'].is_alive())
        self.assertFalse(getattr(self.runtime, ac.WATCH_ATTR)['enabled'])

    def test_start_and_stop_watch_helpers(self):
        started = api.start_watch(self.app, interval=0.05)
        self.assertTrue(started.get('ok'), started)
        self.assertTrue(getattr(self.runtime, ac.WATCH_ATTR)['thread'].is_alive())
        stopped = api.stop_watch(self.app)
        self.assertFalse(stopped.get('enabled'))
        self.assertFalse(getattr(self.runtime, ac.WATCH_ATTR)['thread'].is_alive())

    def test_lazy_runtime_proxy_is_supported(self):
        # 真实宿主：create_app 里注册时 app.state.bot 还不存在（_runtime_proxy 延迟代理）。
        class Proxy:
            def __init__(self, app):
                self.app = app

            def __getattr__(self, name):
                return getattr(self.app.state.bot, name)

        app = FastAPI()
        api.register(app, Proxy(app))
        app.state.bot = FakeRuntime(self.tmp.name)
        service = getattr(app.state, api.SERVICE_ATTR)
        service._status = lambda runtime: {'state': 'pending', 'base_url': ac.base_url(runtime)}
        service._watch = ApiTests.quick_watch
        body = TestClient(app).get('/api/dsh/connect').json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['status']['base_url'], 'http://127.0.0.1:9175')
        started = TestClient(app).post('/api/dsh/connect/watch', json={'enabled': True}).json()
        self.assertTrue(started['ok'], started)
        stopped = TestClient(app).post('/api/dsh/connect/watch', json={'enabled': False}).json()
        self.assertFalse(stopped['enabled'])

    def test_exception_becomes_reason_and_message(self):
        def boom(runtime):
            raise RuntimeError('炸了')

        self.app2 = FastAPI()
        self.app2.state.dsh_connect_service = api.DshConnectService(self.runtime, status=boom,
                                                                    instructions=lambda: 'x')
        api.register(self.app2, self.runtime)
        body = TestClient(self.app2).get('/api/dsh/connect').json()
        self.assertEqual((body['ok'], body['reason']), (False, 'internal'))
        self.assertIn('RuntimeError', body['message'])


class ConclusionRegressionTests(unittest.TestCase):
    def test_user_row_is_written_to_the_top_level_list(self):
        block = ac.build_user_block(PYTHON, 'D:/w/whale_dsh_mcp.py', 'http://127.0.0.1:8091', TOKEN)
        lines = block.splitlines()
        self.assertEqual(lines[1], '- insert:', '必须是用户级（顶层）entry list')
        self.assertTrue(any(line == '    - id: ' + ac.ROW_ID for line in lines))
        self.assertIn(ac.USER_START, lines[0])
        self.assertEqual((ac.PRESET_ID, ac.ROW_ID), ('whale-workbench', 'mcp-whale-wechat'))

    def test_registry_reader_prefers_selected_default(self):
        value = ac.read_registry(REGISTRY_ONLY)
        self.assertEqual((value['present'], value['default'], value['selected']), (True, 'standard', 'cordis'))
        missing = ac.read_registry('- id: other\n  config:\n    default: standard\n')
        self.assertFalse(missing['present'])


if __name__ == '__main__':
    unittest.main()
