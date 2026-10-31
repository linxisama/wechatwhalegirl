"""1.3.51 批4：启动探测线程必须能在关停时等到。

为什么单独立这一个文件：note_startup_probe 起的线程会在数据根真实建 .write-probe-*.tmp
并写、fsync、改名。lifespan 关停后如果立刻删掉临时目录（测试清理、覆盖升级换目录都是
这个动作），线程还攥着句柄就删不动，报 PermissionError: [WinError 32]。修法是关停时
join_startup_probe 等它收尾 —— 这里的四个用例把这条契约钉住，谁把它删了就会红。
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import install_guard as guard


class FakeBot:
    """只带探测需要的那几样：root（数据根）与 note（提示回调）。"""

    def __init__(self, root):
        self.root = root
        self.notes = []

    def note(self, text):
        self.notes.append(text)


def probe_leftovers(root):
    """数据根里残留的探测临时文件（.write-probe-*.tmp / .renamed）。"""
    return sorted(item.name for item in Path(root).glob('.write-probe-*'))


class StartupProbeShutdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # 登记表是进程内状态：先清干净，才能断言「没有线程」那条分支。
        guard._LIVE_PROBES.clear()

    def test_thread_is_reachable_from_bot_and_join_leaves_nothing_behind(self):
        bot = FakeBot(self.root)
        try:
            guard.note_startup_probe(bot)
            thread = getattr(bot, 'data_root_probe_thread', None)
            self.assertIsNotNone(thread, 'note_startup_probe 没把线程挂到 bot 上，关停时等不到')
            guard.join_startup_probe(bot)
            self.assertFalse(thread.is_alive(), 'join 返回后探测线程还活着')
            self.assertEqual(probe_leftovers(self.root), [], '探测线程留下了 .write-probe-* 残留')
        finally:
            guard.join_startup_probe(bot, 5.0)

    def test_join_waits_for_a_slow_probe(self):
        bot = FakeBot(self.root)
        real = guard.resolve_data_root

        def slow(root=None, **kwargs):
            time.sleep(0.3)
            return real(root)

        with patch.object(guard, 'resolve_data_root', slow):
            guard.note_startup_probe(bot)
            thread = bot.data_root_probe_thread
            self.assertTrue(thread.is_alive(), '慢探针还没开始就结束了，这条用例失去意义')
            self.assertTrue(guard.join_startup_probe(bot, 2.0), 'join(2) 没等到 0.3 秒的慢探针')
            self.assertFalse(thread.is_alive())
        self.assertEqual(probe_leftovers(self.root), [], '慢探针跑完留下了 .write-probe-* 残留')

    def test_probe_and_note_errors_never_escape_join(self):
        bot = FakeBot(self.root)

        def boom(root=None, **kwargs):
            raise RuntimeError('模拟探测崩溃')

        with patch.object(guard, 'resolve_data_root', boom):
            guard.note_startup_probe(bot)
            guard.join_startup_probe(bot, 2.0)   # 探测自己炸了也不许往上抛
            self.assertFalse(bot.data_root_probe_thread.is_alive())
            self.assertEqual(bot.notes, [], '探测崩溃不该被当成「数据目录写不进去」报给用户')

        class LoudBot(FakeBot):
            def note(self, text):
                raise RuntimeError('note 回调也炸了')

        loud = LoudBot(self.root)
        with patch.object(guard, 'resolve_data_root',
                          lambda root=None, **kw: {'writable': False, 'reason': '写入探测失败'}):
            guard.note_startup_probe(loud)
            guard.join_startup_probe(loud, 2.0)   # note 抛错同样不许影响关停
            self.assertFalse(loud.data_root_probe_thread.is_alive())

    def test_join_without_a_thread_returns_false(self):
        bot = FakeBot(self.root)
        self.assertFalse(guard.join_startup_probe(bot), '没有探测线程时 join 应为 False')
        self.assertFalse(guard.join_startup_probe(bot, seconds=None), '坏参数也不许抛')
        done = threading.Thread(target=lambda: None)
        done.start()
        done.join()
        bot.data_root_probe_thread = done
        self.assertFalse(guard.join_startup_probe(bot), '线程早就结束时 join 也应返回 False')
        self.assertFalse(done.is_alive())


if __name__ == '__main__':
    unittest.main()
