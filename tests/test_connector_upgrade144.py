"""1.3.44 补丁：旧连接器安全升级。离线测试，全部落在临时目录，绝不碰真机数据目录。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src import dsh_mcp_provision as dsh
from src.agent_mcp_connector import (CONNECTOR, CONNECTOR_SIGNATURE, export_connector,
                                     generated_by_console, place_connector)
from src.agent_workspace import WorkspaceError

LIMIT = '接入文件已被本地修改，为保留修改没有覆盖：'
NAME = 'whale_dsh_mcp.py'
BAK = r'^whale_dsh_mcp[.]py[.]bak-[0-9]{8}-[0-9]{6}(-[0-9]+)?$'
PATCH = "- id: mcp-whale-tools\n  name: '@deepseek-ai/dsh-mcp-client'\n"

# 升级前的旧连接器：首行与两个特征函数都在，但没有 announce。
OLD = (CONNECTOR_SIGNATURE[0] + '''
import os
import sys


def endpoint():
    return None


def control(name, args):
    return None
''')

FOREIGN = '''# 我自己改的连接器
'''


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


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.script = self.root / NAME

    def backups(self):
        return sorted(p.name for p in self.root.iterdir() if '.bak-' in p.name)

    def test_missing_file_is_written(self):
        self.assertFalse(self.script.exists())
        placed = place_connector(self.script)
        self.assertEqual(placed['path'], self.script)
        self.assertTrue(placed['changed'])
        self.assertEqual(placed['backup'], '')
        self.assertEqual(self.script.read_text('utf-8'), CONNECTOR)
        self.assertIn('announce', self.script.read_text('utf-8'))
        self.assertEqual(self.backups(), [])

    def test_identical_content_is_not_rewritten(self):
        self.script.write_bytes(CONNECTOR.encode('utf-8'))
        stamp = time.time() - 86400
        os.utime(self.script, (stamp, stamp))
        before = self.script.stat().st_mtime_ns
        placed = place_connector(self.script)
        self.assertFalse(placed['changed'])
        self.assertEqual(placed['backup'], '')
        self.assertEqual(self.script.stat().st_mtime_ns, before)
        self.assertEqual(self.script.read_text('utf-8'), CONNECTOR)
        self.assertEqual(self.backups(), [])

    def test_old_connector_is_backed_up_then_upgraded(self):
        self.script.write_bytes(OLD.encode('utf-8'))
        self.assertNotIn('announce', OLD)
        placed = place_connector(self.script)
        print('样例备份文件名：' + placed['backup'])
        self.assertTrue(placed['changed'])
        self.assertRegex(placed['backup'], BAK)
        self.assertEqual(placed['message'], '已更新，原文件备份为 ' + placed['backup'])
        self.assertEqual(self.backups(), [placed['backup']])
        backup = self.root / placed['backup']
        self.assertEqual(backup.read_bytes(), OLD.encode('utf-8'))
        raw = self.script.read_bytes()
        text = raw.decode('utf-8')
        self.assertEqual(text, CONNECTOR)
        self.assertIn('announce', text)
        self.assertNotEqual(raw[:3], bytes([0xEF, 0xBB, 0xBF]))
        self.assertNotIn(b'\r\n', raw)

    def test_foreign_file_keeps_the_original_error(self):
        self.script.write_bytes(FOREIGN.encode('utf-8'))
        with self.assertRaises(WorkspaceError) as caught:
            place_connector(self.script)
        self.assertEqual(str(caught.exception), LIMIT + NAME)
        self.assertEqual(self.script.read_bytes(), FOREIGN.encode('utf-8'))
        self.assertEqual(self.backups(), [])

    def test_backup_failure_leaves_the_old_file_alone(self):
        self.script.write_bytes(OLD.encode('utf-8'))
        real = Path.write_bytes

        def boom(target, data):
            if '.bak-' in target.name:
                raise OSError(28, '磁盘空间不足')
            return real(target, data)

        with mock.patch.object(Path, 'write_bytes', boom):
            with self.assertRaises(WorkspaceError) as caught:
                place_connector(self.script)
        self.assertIn('备份旧接入文件失败', str(caught.exception))
        self.assertEqual(self.script.read_bytes(), OLD.encode('utf-8'))
        self.assertEqual(self.backups(), [])

    def test_place_connector_result_carries_the_backup(self):
        self.script.write_bytes(OLD.encode('utf-8'))
        placed = place_connector(self.script)
        self.assertEqual(sorted(placed), ['backup', 'changed', 'message', 'path'])
        self.assertEqual(placed['path'], self.script)
        self.assertTrue((self.root / placed['backup']).is_file())

    def test_export_connector_api_exposes_the_backup(self):
        """界面/API 那条路径（/api/agent/workspace/connector）也要能看到备份名。"""
        folder = self.root / 'area'
        script = folder / 'whale_workspace_mcp_v2.py'
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_bytes(OLD.encode('utf-8'))

        class Area:
            @staticmethod
            def folder():
                return folder / 'notes'

        placed = export_connector(Area())
        self.assertRegex(placed['backup'],
                         r'^whale_workspace_mcp_v2[.]py[.]bak-[0-9]{8}-[0-9]{6}(-[0-9]+)?$')
        self.assertTrue(placed['message'].startswith('已更新，原文件备份为 ' + placed['backup']))
        self.assertIn('已生成MCP v2：', placed['message'])
        self.assertEqual(script.read_text('utf-8'), CONNECTOR)
        self.assertTrue((folder / placed['backup']).is_file())

    def test_signature_needs_the_header_and_both_functions(self):
        self.assertTrue(generated_by_console(CONNECTOR))
        self.assertTrue(generated_by_console(OLD))
        self.assertFalse(generated_by_console(OLD.replace('def control(name, args):', 'def control():')))
        self.assertFalse(generated_by_console(OLD.replace('def endpoint():', 'def base():')))
        self.assertFalse(generated_by_console('# ' + OLD))
        self.assertFalse(generated_by_console(''))
        # 特征首行必须仍等于模板首行，否则升级判据会跟模板脱节。
        self.assertEqual(CONNECTOR.splitlines()[0].rstrip(), CONNECTOR_SIGNATURE[0])

    def test_provision_reports_the_connector_backup(self):
        root = self.root / 'bot'
        auth = bind(root)
        patch = root / 'cordis.patch.yml'
        patch.write_text(PATCH, 'utf-8')
        script = root / 'data' / 'agent_workspace' / NAME
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_bytes(OLD.encode('utf-8'))
        with mock.patch.dict(os.environ, {'WHALE_DSH_PATCH': str(patch)}):
            data = dsh.provision(Bot(root))
        self.assertTrue(data['success'], data['message'])
        self.assertRegex(data['connector_backup'], BAK)
        self.assertIn('已更新，原文件备份为 ' + data['connector_backup'], data['message'])
        self.assertEqual(script.read_text('utf-8'), CONNECTOR)
        self.assertTrue((script.parent / data['connector_backup']).is_file())
        self.assertEqual(json.loads(auth.read_text('utf-8'))['credentials'][0]['peer'], 'dsh-app')


if __name__ == '__main__':
    unittest.main()
