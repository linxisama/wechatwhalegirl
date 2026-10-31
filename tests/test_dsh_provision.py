"""DSH一键接入：离线测试，全部落在临时目录，不触碰真实 .dsh。"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import dsh_mcp_provision as dsh
from src.agent_authority import CAPABILITIES
from src.agent_mcp_connector import CONNECTOR

PATCH = ("- id: mcp-whale-tools\n"
         "  name: '@deepseek-ai/dsh-mcp-client'\n"
         "  config:\n"
         "    serverName: whale_tools\n"
         "    transport: stdio\n")


class Bot:
    def __init__(self, root):
        self.root = Path(root)
        self.port = 8091


def bind(root):
    path = Path(root) / 'data' / 'agent_authority.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'revision': 1, 'credentials': [],
                                'admin': {'platform': 'desktop', 'account': 'local', 'subject': 'owner'}}), 'utf-8')
    return path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ProvisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = bind(self.root)
        self.patch = self.root / 'cordis.patch.yml'
        self.patch.write_text(PATCH, 'utf-8')
        env = mock.patch.dict(os.environ, {'WHALE_DSH_PATCH': str(self.patch)})
        env.start()
        self.addCleanup(env.stop)
        self.bot = Bot(self.root)

    def credentials(self):
        return json.loads(self.auth.read_text('utf-8'))['credentials']

    def test_block_format(self):
        block = dsh.build_block(r'C:\Program Files\py\python.exe', r'C:\ws\whale_dsh_mcp.py', 'tok-abc')
        self.assertEqual(block, '\n'.join([
            '# >>> whale-wechat-workbench v1（微信控制台自动生成；删除本段即移除该预设）',
            '- insert:',
            '    - id: preset-whale-workbench',
            "      name: '@deepseek-ai/dsh-agent-preset'",
            '      config:',
            '        id: whale-workbench',
            "        name: '鲸鱼娘工作台'",
            "        description: '通过 whale_wechat 的 MCP 工具读写微信控制台工作区/记忆/学习，并执行控制台动作'",
            '        plugins:',
            '          - id: mcp-whale-wechat',
            "            name: '@deepseek-ai/dsh-mcp-client'",
            '            config:',
            '              serverName: whale_wechat',
            '              transport: stdio',
            r"              command: 'C:\Program Files\py\python.exe'",
            '              args:',
            r"                - 'C:\ws\whale_dsh_mcp.py'",
            '              env:',
            "                WHALE_BOT_URL: 'http://127.0.0.1:8091'",
            "                WHALE_AGENT_TOKEN: 'tok-abc'",
            "                PYTHONUTF8: '1'",
            '              toolCallTimeoutMs: 75000',
            '# <<< whale-wechat-workbench v1',
        ]))
        self.assertNotIn('"', block)
        self.assertEqual(dsh.build_block('p', 's', "a'b").splitlines()[19], "                WHALE_AGENT_TOKEN: 'a''b'")

    def test_provision_appends_block_and_returns_token(self):
        data = dsh.provision(self.bot)
        self.assertTrue(data['success'])
        self.assertTrue(data['written'])
        self.assertEqual(data['server_name'], 'whale_wechat')
        self.assertTrue(data['block'].startswith('# >>> whale-wechat-workbench v1'))
        self.assertIn(data['token'], data['block'])
        self.assertIn('重启 DeepSeek Harness', data['message'])
        text = self.patch.read_text('utf-8')
        self.assertTrue(text.startswith(PATCH))
        self.assertTrue(text.endswith(dsh.END + ' v1\n'))
        self.assertTrue(Path(data['script']).is_file())

    def test_replace_in_place_keeps_other_bytes(self):
        old = '\n' + dsh.build_block('C:\\old\\python.exe', 'C:\\old\\s.py', 'old-token') + '\n'
        tail = '- id: after\n  name: keep\n'
        self.patch.write_text(PATCH + old + tail, 'utf-8')
        block = dsh.build_block('C:\\new\\python.exe', 'C:\\new\\s.py', 'new-token')
        self.patch.write_text(PATCH + '\n' + block + '\n' + tail, 'utf-8')
        expected = self.patch.read_bytes()
        dsh.write_block(self.patch, dsh.build_block('C:\\old\\python.exe', 'C:\\old\\s.py', 'old-token'))
        self.assertNotEqual(self.patch.read_bytes(), expected)
        dsh.write_block(self.patch, block)
        self.assertEqual(self.patch.read_bytes(), expected)
        plain = expected.decode('utf-8').replace('\r\n', '\n')
        self.assertTrue(plain.startswith(PATCH))
        self.assertTrue(plain.endswith(tail))

    def test_write_twice_is_idempotent(self):
        block = dsh.build_block('C:\\venv\\python.exe', 'C:\\ws\\whale_dsh_mcp.py', 'tok-x')
        first = dsh.write_block(self.patch, block)
        after_first = digest(self.patch)
        second = dsh.write_block(self.patch, block)
        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'])
        self.assertEqual(second['backup'], '')
        self.assertEqual(digest(self.patch), after_first)

    def test_marker_outside_bytes_kept_with_crlf(self):
        def crlf(text):
            return text.replace('\n', '\r\n').encode('utf-8')
        head = '# 头部注释\n- id: keep-head\n  name: keep\n'
        tail = '- id: keep-tail\n  name: keep\n'
        old = dsh.build_block('C:\\old\\py.exe', 'C:\\old\\s.py', 'old-token')
        self.patch.write_bytes(crlf(head + old + '\n' + tail))
        fresh = dsh.build_block('C:\\new\\py.exe', 'C:\\new\\s.py', 'new-token')
        self.assertTrue(dsh.write_block(self.patch, fresh)['changed'])
        expected = crlf(head + fresh + '\n' + tail)
        self.assertEqual(self.patch.read_bytes(), expected)
        # CRLF 保持：成对的 \r\n 去掉后不应剩下裸 \n
        self.assertNotIn(b'\n', self.patch.read_bytes().replace(b'\r\n', b''))
        self.assertFalse(dsh.write_block(self.patch, fresh)['changed'])
        self.assertEqual(self.patch.read_bytes(), expected)

    def test_block_parses_as_single_preset(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('本机没有 PyYAML，块文本已由 test_block_format 逐行断言')
        block = dsh.build_block('C:\\venv\\python.exe', 'C:\\ws\\whale_dsh_mcp.py', 'tok-yaml')
        data = yaml.safe_load(block)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(sorted(data[0]), ['insert'])
        preset = data[0]['insert'][0]
        self.assertEqual(preset['id'], 'preset-whale-workbench')
        self.assertEqual(preset['name'], '@deepseek-ai/dsh-agent-preset')
        self.assertEqual(preset['config']['id'], 'whale-workbench')
        self.assertEqual(preset['config']['name'], '鲸鱼娘工作台')
        client = preset['config']['plugins'][0]
        self.assertEqual(client['id'], 'mcp-whale-wechat')
        self.assertEqual(client['name'], '@deepseek-ai/dsh-mcp-client')
        self.assertEqual(client['config']['serverName'], 'whale_wechat')
        self.assertEqual(client['config']['args'], ['C:\\ws\\whale_dsh_mcp.py'])
        self.assertEqual(client['config']['env']['WHALE_AGENT_TOKEN'], 'tok-yaml')
        self.assertEqual(client['config']['env']['PYTHONUTF8'], '1')
        self.assertEqual(client['config']['toolCallTimeoutMs'], 75000)

    def test_backup_holds_original_bytes(self):
        result = dsh.write_block(self.patch, dsh.build_block('p', 's', 't'))
        backup = Path(result['backup'])
        self.assertTrue(backup.is_file())
        self.assertRegex(backup.name, r'^cordis\.patch\.yml\.bak-\d{14}$')
        self.assertEqual(backup.read_text('utf-8'), PATCH)

    def test_missing_patch_only_builds_block(self):
        missing = self.root / 'nope' / 'cordis.patch.yml'
        with mock.patch.dict(os.environ, {'WHALE_DSH_PATCH': str(missing)}):
            data = dsh.provision(self.bot)
        self.assertTrue(data['success'])
        self.assertFalse(data['written'])
        self.assertIn('未找到 DSH 桌面配置文件', data['message'])
        self.assertTrue(data['block'])
        self.assertFalse(missing.exists())

    def test_preview_issues_nothing_and_keeps_the_live_credential(self):
        first = dsh.provision(self.bot)
        self.assertTrue(first['token'])
        before_bytes = self.auth.read_bytes()
        preview = dsh.provision(self.bot, write=False)
        self.assertTrue(preview['success'])
        self.assertEqual(preview['token'], '')
        self.assertFalse(preview['written'])
        self.assertIn(dsh.PREVIEW_TOKEN, preview['block'])
        self.assertNotIn(first['token'], preview['block'])
        # 预览不能碰凭据：字节不变，在用的那条才不会被顶成废票
        self.assertEqual(self.auth.read_bytes(), before_bytes)
        self.assertEqual(len(self.credentials()), 1)
        self.assertEqual(self.credentials()[0]['digest'], hashlib.sha256(first['token'].encode()).hexdigest())

    def test_preview_does_not_touch_the_patch_file(self):
        before = self.patch.read_bytes()
        preview = dsh.provision(self.bot, write=False)
        self.assertTrue(preview['success'])
        self.assertEqual(self.patch.read_bytes(), before)
        self.assertEqual(self.credentials(), [])

    def test_missing_python_fails_closed(self):
        before = self.auth.read_bytes()
        with mock.patch('src.dsh_mcp_provision.shutil.which', return_value=None):
            data = dsh.provision(self.bot)
        self.assertFalse(data['success'])
        self.assertIn('未找到可用的 Python 3.10+ 解释器', data['message'])
        self.assertEqual(data['token'], '')
        self.assertEqual(self.auth.read_bytes(), before)
        self.assertEqual(self.credentials(), [])
        self.assertEqual(self.patch.read_text('utf-8'), PATCH)

    def test_modified_script_is_not_overwritten(self):
        script = self.root / 'data' / 'agent_workspace' / 'whale_dsh_mcp.py'
        script.parent.mkdir(parents=True)
        script.write_text('# 我自己改的连接器\n', 'utf-8')
        data = dsh.provision(self.bot)
        self.assertFalse(data['success'])
        self.assertIn('接入文件已被本地修改', data['message'])
        self.assertEqual(script.read_text('utf-8'), '# 我自己改的连接器\n')
        self.assertEqual(self.credentials(), [])
        self.assertEqual(self.patch.read_text('utf-8'), PATCH)

    def test_old_credential_revoked_and_full_capabilities(self):
        authority = __import__('src.agent_authority', fromlist=['authority']).authority(self.bot)
        stale = authority.issue('dsh-app', ['read_console'], 1)
        other = authority.issue('other-agent', ['read_memory'], 1)
        data = dsh.provision(self.bot)
        rows = self.credentials()
        peers = sorted(row['peer'] for row in rows)
        self.assertEqual(peers, ['dsh-app', 'other-agent'])
        fresh = [row for row in rows if row['peer'] == 'dsh-app'][0]
        self.assertNotEqual(fresh['id'], stale['id'])
        self.assertEqual(fresh['capabilities'], sorted(CAPABILITIES))
        self.assertIn(other['id'], [row['id'] for row in rows])
        self.assertEqual(fresh['digest'], hashlib.sha256(data['token'].encode()).hexdigest())

    def test_connector_control_tools(self):
        namespace = {'__name__': 'dsh_connector_probe'}
        exec(compile(CONNECTOR, 'whale_dsh_mcp.py', 'exec'), namespace)
        calls = []

        def fake_send(base, token, method, path, payload=None):
            calls.append((base, token, method, path, payload))
            return {'success': True}

        namespace['send'] = fake_send
        env = {'WHALE_BOT_URL': 'http://127.0.0.1:8091', 'WHALE_AGENT_TOKEN': 'tok-connector'}
        with mock.patch.dict(os.environ, env):
            namespace['control']('console_catalog', {})
            namespace['call']('console_execute', {'action': 'workspace.info'})
            for bad in ({'action': 'workspace.info', 'params': {'name': 1}},
                        {'action': 'workspace.info', 'query': {'n': [1]}},
                        {'action': 'workspace.info', 'extra': 1},
                        {'action': ''}):
                with self.assertRaises(ValueError):
                    namespace['control']('console_execute', bad)
        self.assertEqual(calls[0][:4], ('http://127.0.0.1:8091', 'tok-connector', 'GET', '/api/agent/control/catalog'))
        self.assertIsNone(calls[0][4])
        self.assertEqual(calls[1][:4], ('http://127.0.0.1:8091', 'tok-connector', 'POST', '/api/agent/control/execute'))
        payload = calls[1][4]
        self.assertEqual(payload['action'], 'workspace.info')
        self.assertEqual(payload['params'], {})
        self.assertRegex(payload['request_id'], r'[A-Za-z0-9_.:-]{8,100}')
        self.assertEqual(len(calls), 2)
        self.assertIn('/api/agent/control/catalog', CONNECTOR)
        self.assertIn('/api/agent/control/execute', CONNECTOR)
        self.assertNotIn('/api/agent/control/catalog', str(namespace['SPECS'].get('workspace_info')))


if __name__ == '__main__':
    unittest.main()
