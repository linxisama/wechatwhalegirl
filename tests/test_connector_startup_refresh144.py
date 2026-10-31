"""1.3.44 收尾补丁：控制台启动时尽力刷新一次已存在的连接器。全部落在临时目录。"""
import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src import wechat_app
from src.agent_mcp_connector import CONNECTOR, CONNECTOR_SIGNATURE, refresh_existing
from src.wechat_app import create_app

NAME = 'whale_dsh_mcp.py'
BAK = r'^whale_dsh_mcp[.]py[.]bak-[0-9]{8}-[0-9]{6}(-[0-9]+)?$'

# 升级前的旧连接器：首行与两个特征函数都在，但没有 announce（与 1.3.44 升级用例同源）。
OLD = CONNECTOR_SIGNATURE[0] + '''
import os
import sys


def endpoint():
    return None


def control(name, args):
    return None
'''

FOREIGN = '# 我自己改的连接器\n'


def connector_of(root):
    """控制台里连接器的位置：bot.root / data / agent_workspace / whale_dsh_mcp.py。"""
    return Path(root) / 'data' / 'agent_workspace' / NAME


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = connector_of(self.root)
        self.path.parent.mkdir(parents=True)

    def backups(self):
        return sorted(p.name for p in self.path.parent.iterdir() if '.bak-' in p.name)

    def test_missing_file_is_not_created(self):
        """缺失：只回 missing，连目录都不许顺手建出来。"""
        absent = self.root / 'deep' / 'nope' / NAME
        result = refresh_existing(absent)
        self.assertEqual(result, {'changed': False, 'backup': '', 'reason': 'missing'})
        self.assertFalse(absent.exists())
        self.assertFalse((self.root / 'deep').exists())

    def test_current_content_is_left_alone(self):
        """一致：changed=False 且 mtime 一个字节都不动。"""
        self.path.write_bytes(CONNECTOR.encode('utf-8'))
        stamp = time.time() - 86400
        os.utime(self.path, (stamp, stamp))
        before = self.path.stat().st_mtime_ns
        result = refresh_existing(self.path)
        self.assertEqual(result, {'changed': False, 'backup': '', 'reason': 'current'})
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertEqual(self.backups(), [])

    def test_old_connector_is_backed_up_then_upgraded(self):
        """旧版（特征命中）：先落 .bak-*，再原地升级到含 announce 的模板。"""
        self.path.write_bytes(OLD.encode('utf-8'))
        self.assertNotIn('announce', OLD)
        result = refresh_existing(self.path)
        self.assertEqual(sorted(result), ['backup', 'changed', 'reason'])
        self.assertIs(result['changed'], True)
        self.assertEqual(result['reason'], 'upgraded')
        self.assertRegex(result['backup'], BAK)
        self.assertEqual(self.backups(), [result['backup']])
        self.assertEqual((self.path.parent / result['backup']).read_bytes(), OLD.encode('utf-8'))
        text = self.path.read_text('utf-8')
        self.assertEqual(text, CONNECTOR)
        self.assertIn('announce', text)

    def test_foreign_file_is_ignored_without_raising(self):
        """外来文件不做改动，也不像 place() 那样抛错——这是启动路径。"""
        self.path.write_bytes(FOREIGN.encode('utf-8'))
        result = refresh_existing(self.path)
        self.assertEqual(result, {'changed': False, 'backup': '', 'reason': 'foreign'})
        self.assertEqual(self.path.read_bytes(), FOREIGN.encode('utf-8'))
        self.assertEqual(self.backups(), [])

    def test_undecodable_file_is_treated_as_foreign(self):
        """非 UTF-8 的既有文件同样不许抛错、不许动。"""
        raw = bytes([0xFF, 0xFE, 0x00, 0x41])
        self.path.write_bytes(raw)
        result = refresh_existing(self.path)
        self.assertEqual(result['reason'], 'foreign')
        self.assertFalse(result['changed'])
        self.assertEqual(self.path.read_bytes(), raw)

    def test_backup_failure_leaves_the_old_file_alone(self):
        """备份写不进去就绝不覆盖，且不抛错。"""
        self.path.write_bytes(OLD.encode('utf-8'))
        real = Path.write_bytes

        def boom(target, data):
            if '.bak-' in target.name:
                raise OSError(28, '磁盘空间不足')
            return real(target, data)

        with mock.patch.object(Path, 'write_bytes', boom):
            result = refresh_existing(self.path)
        self.assertFalse(result['changed'])
        self.assertNotEqual(result['reason'], 'upgraded')
        self.assertEqual(result['backup'], '')
        self.assertEqual(self.path.read_bytes(), OLD.encode('utf-8'))
        self.assertEqual(self.backups(), [])


class StartupTest(unittest.TestCase):
    """启动钩子：位置对、被 try 包住、刷新失败也不影响启动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def bot(self):
        bot = mock.MagicMock()
        bot.root = self.root
        bot.close = mock.AsyncMock()
        return bot

    @staticmethod
    def start(app):
        async def run():
            async with app.router.lifespan_context(app):
                return True
        return asyncio.run(run())

    def test_refresh_call_sits_inside_try(self):
        lines = Path(wechat_app.__file__).read_text('utf-8').splitlines()
        hits = [i for i, line in enumerate(lines) if 'refresh_existing(app.state.bot.root' in line]
        self.assertEqual(len(hits), 1, hits)
        index = hits[0]
        indent = len(lines[index]) - len(lines[index].lstrip())
        above = lines[max(0, index - 5):index]
        tries = [line for line in above
                 if line.strip() == 'try:' and (len(line) - len(line.lstrip())) < indent]
        self.assertTrue(tries, above)
        try_indent = len(tries[-1]) - len(tries[-1].lstrip())
        below = lines[index + 1:index + 4]
        self.assertTrue([line for line in below
                         if line.strip().startswith('except')
                         and (len(line) - len(line.lstrip())) == try_indent], below)

    def test_lifespan_refreshes_the_real_connector_path(self):
        """端到端：lifespan 真的把 data/agent_workspace 下的旧连接器刷成新模板。"""
        target = connector_of(self.root)
        target.parent.mkdir(parents=True)
        target.write_bytes(OLD.encode('utf-8'))
        real = wechat_app.refresh_existing
        seen = []

        def spy(path):
            seen.append(Path(path))
            return real(path)

        with mock.patch.object(wechat_app, 'refresh_existing', spy):
            self.assertTrue(self.start(create_app(self.bot())))
        self.assertEqual(seen, [target])
        text = target.read_text('utf-8')
        self.assertEqual(text, CONNECTOR)
        self.assertIn('announce', text)

    def test_startup_survives_a_failing_refresh(self):
        """替身直接抛错：启动照样走完，后续 start 一步不少。"""
        bot = self.bot()
        app = create_app(bot)

        def boom(path):
            raise RuntimeError('模拟连接器刷新崩溃')

        with mock.patch.object(wechat_app, 'refresh_existing', boom):
            self.assertTrue(self.start(app))
        bot.image_chat.start.assert_called_once()
        bot.diary_writer.start.assert_called_once()
        bot.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
