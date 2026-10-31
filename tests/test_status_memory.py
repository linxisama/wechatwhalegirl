"""status() 里要带 memory 概览（控制台记忆卡的数据源）。"""
import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from src.wechat_app import WeChatRuntime

ROOT = Path(__file__).resolve().parent.parent


class StatusMemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        shutil.copytree(ROOT / 'styles', self.root / 'styles')
        config = self.root / 'config.wechat.yaml'
        config.write_text((ROOT / 'config.wechat.yaml').read_text('utf-8'), 'utf-8')
        self.bot = WeChatRuntime(root=self.root, config_path=config,
                                 db_path=self.root / 'messages.sqlite3')

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass
        self._tmp.cleanup()

    def test_memory_block_exists_with_zeros_on_empty_db(self):
        data = self.bot.status()['memory']
        for key in ('manual', 'confirmed', 'candidate', 'learned', 'people'):
            self.assertEqual(data[key], 0, key)

    def test_counts_reflect_rows(self):
        now = time.time()
        self.bot.db.execute("INSERT INTO chat_memories VALUES(?,?,?,?,?,?,?,?,?)",
                            ('m1', 'wechat|x|group:g', 'wxid_a', '喜欢蓝色', 'confirmed', 'src', 2, now, now))
        self.bot.db.execute("INSERT INTO chat_memories VALUES(?,?,?,?,?,?,?,?,?)",
                            ('m2', 'wechat|x|group:g', 'wxid_a', '住在杭州', 'candidate', 'src', 1, now, now))
        self.bot.db.execute("INSERT INTO chat_memories VALUES(?,?,?,?,?,?,?,?,?)",
                            ('m3', 'wechat|x|group:g', 'wxid_b', '养了只猫', 'confirmed', 'src', 2, now, now))
        self.bot.db.commit()
        data = self.bot.status()['memory']
        self.assertEqual(data['confirmed'], 2)
        self.assertEqual(data['candidate'], 1)
        self.assertEqual(data['people'], 2)


if __name__ == '__main__':
    unittest.main()
