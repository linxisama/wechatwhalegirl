"""覆盖升级必须保留既有文件与超过旧上限的对话，不只检查目录存在。"""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.gui.upgrade_check import seed_fixture,verify_fixture,HISTORY_COUNT
from src.wechat_app import WeChatRuntime

ROOT=Path(__file__).resolve().parent.parent


def uninstall_delete_lines(source):
    """取出 installer.iss 里 [UninstallDelete] 段到下一个 [Section] 之间的所有行（已去首尾空白）。"""
    lines=[]
    inside=False
    for line in source.splitlines():
        text=line.strip()
        if text.startswith('[') and text.endswith(']'):
            inside=(text.lower()=='[uninstalldelete]')
            continue
        if inside:
            lines.append(text)
    return lines


class UpgradePreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'用户数据'

    def tearDown(self):
        self.temp.cleanup()

    def seed(self):
        return seed_fixture(self.root,ROOT/'styles')

    def test_reload_preserves_config_plugins_persona_password_memory_and_all_history(self):
        self.seed()
        result=verify_fixture(self.root,True)
        self.assertTrue(result['config_preserved'])
        self.assertTrue(result['history_preserved'])
        self.assertTrue(result['runtime_reloaded'])
        self.assertEqual(result['history_count'],HISTORY_COUNT)
        self.assertGreater(result['history_count'],20000)
        self.assertGreater(result['files_preserved'],10)
        # 第二次重载也不能以“数据迁移”为由重新初始化或裁剪。
        self.assertEqual(verify_fixture(self.root,True),result)

    def test_changed_or_missing_configuration_is_detected(self):
        self.seed()
        config=self.root/'config.wechat.yaml'
        original=config.read_bytes()
        config.write_text('style: {}','utf-8')
        with self.assertRaisesRegex(AssertionError,'用户文件'):
            verify_fixture(self.root)
        config.write_bytes(original)
        config.unlink()
        with self.assertRaisesRegex(AssertionError,'用户文件'):
            verify_fixture(self.root)

    def test_history_loss_is_detected_even_if_database_and_user_directory_still_exist(self):
        self.seed()
        db=sqlite3.connect(self.root/'data/wechat-only/messages.sqlite3')
        try:
            db.execute("DELETE FROM inbox WHERE key='upgrade:0'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(AssertionError):
            verify_fixture(self.root)

    def test_seed_refuses_nonempty_directory_without_overwriting_files(self):
        self.root.mkdir()
        file=self.root/'original-user-data.txt'
        file.write_text('保留我','utf-8')
        with self.assertRaisesRegex(ValueError,'拒绝覆盖'):
            self.seed()
        self.assertEqual(file.read_text('utf-8'),'保留我')

    def test_fixture_does_not_read_directory_without_marker(self):
        self.root.mkdir()
        with self.assertRaises(FileNotFoundError):
            verify_fixture(self.root,True)

    def test_outbox_maintenance_preserves_old_completed_records(self):
        self.seed()
        bot=WeChatRuntime(root=self.root)
        try:
            bot.config.set('wechat.proactive.enabled',True)
            bot.bridge.online=True
            bot.transport.send_to=AsyncMock(side_effect=AssertionError('测试不可发送消息'))
            asyncio.run(bot._drain_outbox())
            self.assertEqual(bot.db.execute('SELECT text FROM chat_outbox').fetchall(),[('旧版发送记录',)])
            bot.transport.send_to.assert_not_called()
        finally:
            bot.bridge.online=False
            asyncio.run(bot.close())
        verify_fixture(self.root)

    def test_installer_keeps_identity_path_and_publishes_only_program_files(self):
        source=(ROOT/'installer.iss').read_text('utf-8-sig')
        self.assertIn("'{CC9C9945-D704-468B-94E5-30AB08B37A21}'",source)
        for setting in ('UsePreviousAppDir=yes','UsePreviousTasks=yes','UninstallLogMode=append'):
            self.assertIn(setting,source)
        self.assertNotIn('[InstallDelete]',source)
        # 卸载段允许存在，但段内只允许清掉安装目录里的回滚备份：覆盖升级会在 {app} 留一个
        # WhaleGirlWeChat.prev.exe（安装包自己产生的中间文件，不是用户数据），卸载不清就会
        # 在 {app} 里剩下一个孤零零的旧程序。用户的配置、聊天记录、插件、人设全在数据根
        # （{localappdata}\WhaleGirlWeChat 或安装目录 data\），卸载一条都不许碰 —— 这才是
        # 这条守卫的本意，所以断言写成「段内白名单 + 用户数据路径黑名单」，而不是原来那句
        # 「整段不许出现」（批1 加入回滚备份清理后，那句禁的是正确行为本身）。
        uninstall=[line for line in uninstall_delete_lines(source) if line and not line.startswith(';')]
        self.assertEqual(uninstall,['Type: files; Name: "{app}\WhaleGirlWeChat.prev.exe"'])
        for line in uninstall_delete_lines(source):
            for banned in ('{localappdata}','{userappdata}','{userprofile}','data\\',
                           'messages.sqlite3','config.wechat.yaml','plugin','styles'):
                self.assertNotIn(banned.lower(),line.lower(),
                                 '卸载段出现用户数据路径：'+banned+' / '+line)
        self.assertNotIn('Source: "dist\\WhaleGirlWeChat\\*"',source)
        self.assertIn('Source: "dist\\WhaleGirlWeChat\\_internal\\*"',source)
        self.assertNotIn('unins000.exe',source)
