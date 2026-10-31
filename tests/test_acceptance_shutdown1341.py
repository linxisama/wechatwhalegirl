"""关停逻辑的反证测试：该失败的必须失败，且退出时刻不能被外部网络拖住。

背景（真实证据，原 bug）：验收后台线程用 asyncio.run() 跑 uvicorn。插件
web_context.on_pre_ai 会用 asyncio.to_thread() 抓真实要闻/热搜（urllib urlopen，
timeoutSec 默认 8 秒）。asyncio.run 收尾时执行
loop.shutdown_default_executor(THREAD_JOIN_TIMEOUT)，而 Python 3.12 的
THREAD_JOIN_TIMEOUT 是 300 秒，于是"后台线程何时退出"由那几个 HTTPS 请求的真实耗时
决定，Backend.close() 的 join(10) 偶发超时，报「验收后台没有正常退出」。
-- 用临时采样线程抓到的栈（serve 线程）：
   acceptance.py in serve <- asyncio.run(main())
   asyncio/runners.py in run / __exit__ / close <- loop.run_until_complete(
   asyncio/base_events.py in run_until_complete / run_forever / _run_once
   asyncio/windows_events.py in select
-- 同一时刻 asyncio_1（默认线程池工人）栈：
   concurrent/futures/thread.py in _worker / run
   src/plugins/web_context.py in _fetch_hot -> _get_json
   urllib/request.py in urlopen -> https_open -> http/client.py getresponse -> ssl.py recv_into
本文件把这个机制固化成断言：修好之后 shutdown_loop 不许再等线程池；而线程真的
不退出时，stop_backend 必须继续失败（fail-closed），不能靠放宽等待蒙过去。
"""
import asyncio
import socket
import threading
import time
import unittest

from src.gui.acceptance import shutdown_loop, stop_backend


class _FakeServer:
    def __init__(self):
        self.should_exit = False


class ShutdownLoopTests(unittest.TestCase):
    def test_does_not_wait_for_blocked_default_executor(self):
        """复刻根因：线程池里有个卡住的网络请求时，关 loop 不许等它。"""
        loop = asyncio.new_event_loop()
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocked_fetch():
            started.set()
            release.wait(60)
            finished.set()

        async def main():
            asyncio.ensure_future(asyncio.to_thread(blocked_fetch))
            while not started.is_set():
                await asyncio.sleep(0.01)

        try:
            loop.run_until_complete(main())
            began = time.monotonic()
            shutdown_loop(loop, limit=1.0)
            elapsed = time.monotonic() - began
            self.assertLess(elapsed, 5.0, 'shutdown_loop 被线程池里的网络请求拖住了 %0.1fs' % elapsed)
            self.assertFalse(finished.is_set(), 'shutdown_loop 竟然等到了线程池里的抓取结束')
        finally:
            release.set()

    def test_asyncio_run_teardown_is_what_blocked(self):
        """反方向对照：证实"等线程池"这一步本身会阻塞（即原 bug 的机制）。"""
        loop = asyncio.new_event_loop()
        release = threading.Event()
        started = threading.Event()

        def blocked_fetch():
            started.set()
            release.wait(60)

        async def main():
            asyncio.ensure_future(asyncio.to_thread(blocked_fetch))
            while not started.is_set():
                await asyncio.sleep(0.01)

        try:
            loop.run_until_complete(main())
            began = time.monotonic()
            loop.run_until_complete(loop.shutdown_default_executor(0.3))
            elapsed = time.monotonic() - began
            self.assertGreaterEqual(elapsed, 0.25, 'shutdown_default_executor 没有等线程池，反证不成立')
            shutdown_loop(loop, limit=1.0)
        finally:
            release.set()


class StopBackendTests(unittest.TestCase):
    def test_never_stopping_thread_fails_closed(self):
        """反证：线程真的不退出时，stop_backend 必须断言失败并带出线程栈。"""
        release = threading.Event()

        def never_stop():
            release.wait(60)

        thread = threading.Thread(target=never_stop, name='test-never-stop', daemon=True)
        thread.start()
        server = _FakeServer()
        sock = socket.socket()
        try:
            began = time.monotonic()
            with self.assertRaises(AssertionError) as caught:
                stop_backend(server, thread, sock, grace=0.3)
            elapsed = time.monotonic() - began
            message = str(caught.exception)
            self.assertIn('验收后台没有正常退出', message)
            self.assertIn('never_stop', message, '失败信息里没有线程栈，等于没有现场')
            self.assertGreaterEqual(elapsed, 0.2, '没有真正等过 grace 就失败了')
            self.assertTrue(server.should_exit, '关闭前没有先置 should_exit')
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(5)

    def test_stopping_thread_passes(self):
        """正方向对照：会退出的线程必须被正常判定为退出。"""
        thread = threading.Thread(target=lambda: time.sleep(0.05), daemon=True)
        thread.start()
        server = _FakeServer()
        stop_backend(server, thread, socket.socket(), grace=10.0)
        self.assertTrue(server.should_exit)
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
