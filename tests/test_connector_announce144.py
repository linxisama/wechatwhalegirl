"""1.3.44 连接器启动上报：离线测试，全部落在临时目录，不连真实控制台。"""
import compileall
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from src.agent_mcp_connector import CONNECTOR, place_connector

ENV = {'WHALE_BOT_URL': 'http://127.0.0.1:8091', 'WHALE_AGENT_TOKEN': 'tok-announce'}


def namespace():
    """按真实解释器编译并执行模板，拿到其中函数；__name__不是__main__，不会自动跑主循环。"""
    space = {'__name__': 'dsh_connector_announce_probe'}
    exec(compile(CONNECTOR, '<connector>', 'exec'), space)
    return space


class Response:
    def __init__(self, raw=b'{"success":true}'):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size):
        return self.raw


class Opener:
    """替身opener：记录请求与超时；failure非空时在open里抛出。"""

    def __init__(self, record, failure=None):
        self.record, self.failure = record, failure

    def open(self, request, timeout=None):
        if self.failure is not None:
            raise self.failure
        self.record.append((request, timeout))
        return Response()


class Recorder:
    def __init__(self, failure=None):
        self.record, self.failure = [], failure

    def __call__(self, *args, **kwargs):
        return Opener(self.record, self.failure)


class FakeThread:
    """假线程：只记录不执行，用来证明启动路径不等上报结果。"""

    instances = []

    def __init__(self, target=None, daemon=None):
        self.target, self.daemon, self.started = target, daemon, False
        FakeThread.instances.append(self)

    def start(self):
        self.started = True


class Announce144Test(unittest.TestCase):
    def setUp(self):
        FakeThread.instances = []

    def announce(self, space, recorder):
        with mock.patch.dict(os.environ, ENV), mock.patch.object(urllib.request, 'build_opener', recorder):
            thread = space['announce']()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        return thread

    def test_template_is_compilable_and_carries_announce(self):
        compile(CONNECTOR, '<connector>', 'exec')
        space = namespace()
        self.assertTrue(callable(space['announce']))
        self.assertIn('dsh.announce', CONNECTOR)
        self.assertEqual(CONNECTOR.count('dsh.announce'), 1)
        self.assertIn('/api/agent/control/execute', CONNECTOR)
        self.assertIn('timeout=3', CONNECTOR)
        self.assertIn('daemon=True', CONNECTOR)
        # 只上报一次：主循环之前调一次，且不引入轮询/重试。
        self.assertEqual(CONNECTOR.count('\n    announce()\n'), 1)
        self.assertNotIn('time.sleep', CONNECTOR)
        self.assertNotIn('WHALE_DSH_BASE_URL', CONNECTOR)

    def test_announce_posts_once_with_three_second_timeout(self):
        space, recorder = namespace(), Recorder()
        self.announce(space, recorder)
        self.assertEqual(len(recorder.record), 1)
        request, timeout = recorder.record[0]
        self.assertEqual(timeout, 3)
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_full_url(), 'http://127.0.0.1:8091/api/agent/control/execute')
        self.assertEqual(request.get_header('Authorization'), 'Bearer tok-announce')
        payload = json.loads(request.data)
        self.assertEqual(payload['action'], 'dsh.announce')
        self.assertEqual(payload['body'], {})
        self.assertEqual((payload['params'], payload['query']), ({}, {}))
        self.assertRegex(payload['request_id'], r'[A-Za-z0-9_.:-]{8,100}')

    def test_announce_failures_never_escape_or_block(self):
        for failure in (urllib.error.URLError('refused'),
                        urllib.error.HTTPError('http://127.0.0.1:8091/api/agent/control/execute', 401, 'no', {}, None),
                        urllib.error.HTTPError('http://127.0.0.1:8091/api/agent/control/execute', 409, 'conflict', {}, None),
                        OSError('socket gone'), TimeoutError('slow')):
            space = namespace()
            with self.subTest(failure=type(failure).__name__):
                self.announce(space, Recorder(failure))
        # 没有凭据或地址非法也只是一次静默失败，且绝不真的往外发请求。
        for bad in ({'WHALE_BOT_URL': 'http://127.0.0.1:8091', 'WHALE_AGENT_TOKEN': ''},
                    {'WHALE_BOT_URL': 'http://10.0.0.1:8091', 'WHALE_AGENT_TOKEN': 't'}):
            space, recorder = namespace(), Recorder()
            with mock.patch.dict(os.environ, bad), mock.patch.object(urllib.request, 'build_opener', recorder):
                thread = space['announce']()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(recorder.record, [])

    def test_announce_writes_nothing_to_stdout_or_stderr(self):
        space, recorder = namespace(), Recorder()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.announce(space, recorder)
        space, recorder = namespace(), Recorder(urllib.error.URLError('refused'))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.announce(space, recorder)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(err.getvalue(), '')

    def test_announce_returns_without_waiting_in_daemon_thread(self):
        space, recorder = namespace(), Recorder()
        shim = type('shim', (), {'Thread': FakeThread})
        space['threading'] = shim
        with mock.patch.dict(os.environ, ENV), mock.patch.object(urllib.request, 'build_opener', recorder):
            thread = space['announce']()
            # 上报体没被主线程执行过：主循环不需要等它。
            self.assertEqual(recorder.record, [])
            thread.target()
        self.assertIs(thread, FakeThread.instances[0])
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.started)
        self.assertEqual(len(recorder.record), 1)

    def test_tool_calls_do_not_announce(self):
        space = namespace()
        calls = []
        space['announce'] = lambda: calls.append('announce')
        space['send'] = lambda base, token, method, path, payload=None, timeout=75: {'success': True}
        with mock.patch.dict(os.environ, ENV):
            space['call']('console_catalog', {})
            space['handle']({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(calls, [])

    def test_generated_connector_file_compiles(self):
        folder = tempfile.mkdtemp(prefix='announce144-')
        try:
            path = place_connector(Path(folder) / 'whale_dsh_mcp.py')['path']
            text = path.read_text('utf-8')
            self.assertEqual(text, CONNECTOR)
            compile(text, str(path), 'exec')
            self.assertTrue(compileall.compile_dir(folder, quiet=1))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
