"""发送子进程的有界 Python 堆栈快照；不从无 GIL 的原生定时器遍历帧。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading

STACK_DELAY = 10.0
STACK_ROTATE_BYTES = 2 * 1024 * 1024
MAX_THREADS = 64
MAX_DEPTH = 32
MAX_FILENAME = 512
MAX_FUNCTION = 128


def _text(value, limit):
    return value[:limit] if type(value) is str else '<unavailable>'


def stack_summary():
    """先在正常 Python 执行中取强帧引用，只物化位置，不读取源码或局部变量。"""
    frames = sys._current_frames()
    frame = code = None
    rows = []
    try:
        for ident in tuple(frames)[:MAX_THREADS]:
            frame = frames[ident]
            stack = []
            for _ in range(MAX_DEPTH):
                if frame is None:
                    break
                code = frame.f_code
                line = frame.f_lineno
                stack.append((_text(code.co_filename, MAX_FILENAME),
                              _text(code.co_name, MAX_FUNCTION),
                              line if type(line) is int and line >= 0 else 0))
                frame = frame.f_back
            rows.append((ident, tuple(stack)))
        return tuple(rows)
    finally:
        # 日志 I/O 之前释放帧与 code；不能让诊断延长聊天正文等局部对象的生命周期。
        frame = code = None
        frames.clear()


class StackWatchdog:
    def __init__(self, folder):
        self.path = Path(folder) / 'send-worker-stacks.log'
        self._cancelled = threading.Event()
        self._timer = None

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > STACK_ROTATE_BYTES:
            self.path.replace(self.path.with_suffix('.log.1'))
        timer = threading.Timer(STACK_DELAY, self._dump)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _dump(self):
        try:
            if self._cancelled.is_set():
                return
            summary = stack_summary()
            if self._cancelled.is_set():
                return
            # 只持有已物化的基本类型；每次 Timer 自己打开/关闭文件，不共享悬空 fd。
            text = 'Timeout (0:00:10)! Python stack snapshot\n'
            text += json.dumps(summary, ensure_ascii=True, separators=(',', ':')) + '\n'
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(text)
        except Exception:
            # 抓取或写日志失败不能改变发送结果，更不能触发发送重做。
            pass

    def cancel(self):
        self._cancelled.set()
        try:
            if self._timer is not None:
                self._timer.cancel()
        except Exception:
            pass


def start_stack_watchdog(folder):
    watchdog = None
    try:
        watchdog = StackWatchdog(folder)
        watchdog.start()
        return watchdog
    except Exception:
        if watchdog is not None:
            watchdog.cancel()
        return None
