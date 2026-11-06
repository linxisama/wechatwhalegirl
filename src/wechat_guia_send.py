# -*- coding: utf-8 -*-
"""微信 4.x 控件树缺失时的「坐标 + OCR」发送通道（用户批准的唯一例外）。

为什么需要它：微信 4.1.15.13 恢复窗口后 UIA 树只剩 Qt51514QWindowIcon /
MMUIRenderSubWindowHW，一个 mmui:: 控件都没有，wechat_send_worker 里走
mmui::ChatInputField 的路径必然 send_failed；Win32 PostMessage 直投文本实测无效
（窗口差异 0.0205 < 空转噪声 0.0272）。所以发送侧补一条基于
wechatauto.guia 的「截图 → OCR 定位 → 真实鼠标点击 → 剪贴板粘贴」通道。

用户拍板、不得越过的边界：
    * 只发到**当前已打开**的会话：发送前读标题栏 OCR 校验会话名，对不上就
      明确失败、绝不输入。因此**不调用** guia 的 open_chat / send_msg（它们内部
      会切会话），也不点会话行、不搜索切换；
    * 禁止 hot_activate：guia 的 _get_uia() 会尝试热激活（写微信进程内存唤醒
      控件树）。本模块建实例时把实例级 _get_uia 覆盖成恒返回 None，全程走 OCR；
    * 不写微信进程内存、不做主动外发；结果未确认成功**不重发**（回车只按一次）。

限时与串行：每次发送有总时限（默认 18s，小于外层 24s 子进程闸门），超时只
会在按回车之前放弃；一旦按过回车就只报「结果不确定、本次不重发」。串行闸门与
发送间隔仍由 wechat_sender 的单线程队列 + guia 内部 rhythm.gate('send') 保证。
"""
from __future__ import annotations

import ctypes
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .logging_setup import (drain_send_phases, log_send_failure, log_send_phase, mask_chat_name,
                            mark_send_phase, start_send_phases,
                            record_exception)
from .wechat_ocr_labels import SCAN_TOP_MARGIN


# 结构化失败原因码 -> 控制台发送卡可直接显示的中文。
REASON_TEXT = {
    'chat_not_open': '已核验的当前会话不是目标',
    'title_unverified': '标题识别未通过，本次不继续输入或提交',
    'guia_unavailable': '「坐标+OCR」发送通道不可用',
    'minimized': '微信窗口最小化且自动恢复失败，无法点击发送',
    'target_not_open': '当前会话不是目标会话，本次未输入、未发送',
    'invalid_target': '目标会话名不合法，本次未操作微信',
    'draft_not_empty': '当前输入框已有草稿，发送尚未就绪',
    'send_failed': '「坐标+OCR」发送失败',
    'blocked_by_window': '微信窗口被其他程序遮挡，请关闭或最小化遮挡窗口后重试',
    'foreground_lost': '微信失去前台焦点，为避免把内容输入到别的窗口，本次未输入',
}

# 原因码 -> 运行记录里那行红字后面跟的处理建议（用户不用再来问"我该做什么"）
REASON_HINT = {
    'chat_not_open': '请手动打开目标会话；程序不会自动切换',
    'title_unverified': '请保留标题识别诊断；程序不会放宽目标核验或重发',
    'guia_unavailable': '请确认微信已登录、窗口未最小化、系统 OCR 可用后重试',
    'minimized': '微信窗口最小化时会在发送前自动恢复（只显示窗口，不抢前台）；恢复失败才需要手动恢复',
    'target_not_open': '请先手动点开目标会话（程序不会替你切会话）',
    'invalid_target': '请检查目标会话名是否为空或含换行',
    'draft_not_empty': '草稿会在发送前自动清空（只发 Ctrl+A + Delete）；自动清空失败时请手动清空后重试',
    'send_failed': '请核对聊天记录；输入框里的残留草稿需要手动清空',
    'blocked_by_window': '请关闭或最小化遮挡微信的窗口，手动点开目标会话后重试',
    'foreground_lost': '请手动点击微信并保持前台；若已粘贴，请先核对并清空微信草稿',
}

DEFAULT_TIMEOUT = 18.0
# 按下发送键之后留给"确认正文已清空"的观察窗，以及按键之前必须留出的余量（秒）。
# 真机实测：定位→按下发送键已经花掉十几秒，按键之后只剩约 4 秒；而"等光标熄灭再看
# 空框"在 PrintWindow 位图里几乎等不到，于是已经发出去的消息被判成"未确认清空"。
# 改成墨迹对比后需要一点观察时间，这段时间只能从按键之前省出来。
CONFIRM_WINDOW = 6.0
CONFIRM_RESERVE = 2.5
# SendInput 鼠标事件标志位（winuser.h）：绝对坐标 + 左键按下/抬起。
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
# 标题横带：guia 自己在 _chat_is_open 里用的是「顶到 185px」这条实测值，这里沿用；
# 窗口矮时按 22% 收窄下限 120px。带取太狠会把正文第一行 OCR 进来，可能把别人刚发
# 的消息误当成会话名，所以只认这一条带里**最上面**的那行文字。
TITLE_BAND_MAX = 185
TITLE_BAND_MIN = 120
TITLE_BAND_RATIO = 0.22
# 标题行判定容差：同一个标题会被 OCR 按字形切成好几段，实测 'O' 与 '西莲寺紫'
# 的 y 只差 1px。取候选里最小的 y 当基线，基线 + 这个容差以内的片段拼成一整行。
TITLE_LINE_TOLERANCE = 12
# 抢前台的复查节奏：真机实测 ALT 轻敲后 150ms 内 GetForegroundWindow 可能还没切过来
# （SetForegroundWindow 已返回 1，窗口稍后才真正到前台），所以要复查而不是查一次就放弃。
ALT_TAP_TRIES = 12
ALT_TAP_INTERVAL = 0.05


# 发送前自动清空输入框里已有的草稿（用户 2026-11 明确要求：草稿不再让用户手动清）。
# 关掉的办法：环境变量 WHALE_AUTO_CLEAR_DRAFT=0。清空只发 Ctrl+A + Delete，永不按 Enter、
# 不写剪贴板、不切会话，且必须先通过「绑定窗口 + 微信在前台 + 标题等于目标」三重核验。
AUTO_OPEN_CHAT_ENV = 'WHALE_AUTO_OPEN_CHAT'
OPEN_CHAT_SEARCH_WAIT = 0.5
OPEN_CHAT_RESULT_WAIT = 1.2

AUTO_CLEAR_DRAFT_ENV = 'WHALE_AUTO_CLEAR_DRAFT'
# 清空后等墨迹确实变少的窗口（与发送确认同一个量级）。
DRAFT_CLEAR_WINDOW = 3.0
# 只剩光标时的墨迹量上限（真机实测空框 62~69 px、1 列；一个汉字约 200~400 px）。
# 光标偶尔会被 draft_state 判成 'text'，只看"空/非空"就会把已经清干净的框判成还有草稿。
CARET_INK_MAX = 120


def auto_clear_draft_enabled() -> bool:
    """草稿自动清空默认开启；只有显式设成 0/false/off/no 才关闭。"""
    raw = str(os.environ.get(AUTO_CLEAR_DRAFT_ENV) or '').strip().lower()
    return raw not in ('0', 'false', 'off', 'no')

def auto_open_chat_enabled() -> bool:
    """默认开启；显式设 0/false/no/off 才关。"""
    raw = str(os.environ.get(AUTO_OPEN_CHAT_ENV) or '').strip().lower()
    return raw not in ('0', 'false', 'no', 'off')


@dataclass
class GuiaSendResult:
    """一次可用性探测或发送的结果；ok=False 时 reason_code 一定是上面那几种之一。"""
    ok: bool
    reason_code: str = ''
    reason: str = ''
    diagnostic: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def _fail(code: str, detail: str = '', uncertain: bool = False) -> GuiaSendResult:
    """uncertain=True 表示回车可能已经按下：调用方必须当成「结果待核实」处理，不得重发。"""
    text = REASON_TEXT.get(code) or REASON_TEXT['send_failed']
    if code == 'foreground_lost' and detail.startswith('粘贴后'):
        text = '微信失去前台焦点，本次未按发送键'
    text = text + ('：' + detail if detail else '')
    diagnostic = {'guia_channel': True, 'ready': False, 'waiting_code': code, 'reason': text}
    if uncertain:
        diagnostic['send_uncertain'] = True
    return GuiaSendResult(False, code, text, diagnostic)


def _ok(detail: str) -> GuiaSendResult:
    return GuiaSendResult(True, '', detail, {'guia_channel': True, 'ready': True,
                                             'waiting_code': '', 'reason': detail})


# 阶段时刻 → 固定键名的毫秒耗时（取不到的键整键省略，绝不参与任何判定）。
_TIMING_SPANS = {'gui_ready': ('begin', 'guia_probe'),
                 'write': ('guia_check_title', 'guia_input'),
                 'press': ('guia_input', 'guia_click_send'),
                 'confirm': ('guia_click_send', 'guia_verify'),
                 'total': ('begin', 'end')}


def _send_timings(marks) -> dict:
    """把 drain 出来的阶段时刻换算成耗时；只是归因数据，缺键即略。"""
    at = {item['phase']: item['at_ms'] for item in marks or []}
    timings = {}
    for key, (first, last) in _TIMING_SPANS.items():
        if first in at and last in at and at[last] >= at[first]:
            timings[key] = round(at[last] - at[first], 3)
    return timings


def _default_factory(hwnd=None):
    """生产用的 guia 实例工厂（测试里换成假对象，绝不碰真窗口）。"""
    if not hwnd:
        raise RuntimeError('未绑定已核验账号窗口，禁止任意选择微信窗口')
    from .wechat_pixel_gui import make_pixel_gui
    return make_pixel_gui(hwnd)


def _iconic(hwnd) -> bool:
    """主窗口是否最小化；查不到就按未最小化处理，交给后续 OCR 兜住。"""
    try:
        import ctypes
        return bool(hwnd) and bool(ctypes.windll.user32.IsIconic(int(hwnd)))
    except Exception:
        return False


def _visible(hwnd) -> bool:
    """主窗口是否可见；查不到（含句柄已不存在）就按可见处理，交给后续校验兜底。"""
    try:
        import ctypes
        hwnd = int(hwnd or 0)
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        # 句柄不存在（离线测试的假 hwnd、或窗口已销毁）属于「未知」：按可见处理，由
        # 绑定校验/is_alive 负责报错。绝不能把「查不到」当成「被隐藏」，否则会对着
        # 无效句柄空等恢复。
        if not user32.IsWindow(ctypes.c_void_p(hwnd)):
            return True
        return bool(user32.IsWindowVisible(ctypes.c_void_p(hwnd)))
    except Exception:
        return True


# 自动恢复最小化/隐藏窗口（1.3.59 只认最小化；1.3.87 补上「被隐藏」）：真机上用户
# 发来消息、回复也生成了，可发送阶段直接 guia_unavailable「账号与窗口绑定已失效」
# ——微信窗口缩在托盘里或被系统隐藏，用户看到的是「发了却没回」。这里只改窗口的显示
# 状态（SW_SHOWNOACTIVATE，显示但不抢前台），不写微信进程内存、不切会话、不点会话
# 列表，也不发送任何按键。
RESTORE_WINDOW_SECONDS = 3.0
RESTORE_WINDOW_POLL = 0.15
RESTORE_SHOW_FLAG = 4          # SW_SHOWNOACTIVATE


def _restore_minimized(hwnd, sleep=None, clock=None, seconds=RESTORE_WINDOW_SECONDS,
                       user32=None) -> bool:
    """把最小化或被隐藏的窗口恢复显示；返回是否已经可见且不再最小化。"""
    try:
        hwnd = int(hwnd or 0)
    except Exception:
        return False
    if not hwnd:
        return False
    try:
        user32 = user32 if user32 is not None else _user32()
        # 老替身没有 IsWindowVisible：缺这个方法就按可见处理，旧测试照旧能跑。
        is_visible = getattr(user32, 'IsWindowVisible', None)

        def shown():
            if user32.IsIconic(hwnd):
                return False
            return True if is_visible is None else bool(is_visible(hwnd))

        # 已经可见且未最小化时一个 ShowWindow 都不调（零副作用）。
        if shown():
            return True
        user32.ShowWindow(hwnd, RESTORE_SHOW_FLAG)
    except Exception:
        return False
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    end = clock() + float(seconds)
    while True:
        try:
            if shown():
                return True
        except Exception:
            return False
        if clock() >= end:
            return False
        sleep(RESTORE_WINDOW_POLL)


def _user32():
    """取 user32；延迟 import 让本模块在没有 ctypes.windll 的平台上也能被导入。"""
    import ctypes
    return ctypes.windll.user32


def foreground_owner(user32=None):
    """当前占着前台的窗口类名与 pid，只读、异常一律当未知返回 ('', 0)。

    为什么记它：微信抢不到前台时，用户只看到"请手动点一下微信窗口"，
    真正挡路的是谁（真机实测是 class=metasequoiaime_windows 的置顶窗口）
    只能靠日志反推；类名 + pid 足够判断是不是被置顶窗口挡住。
    只取类名与 pid，绝不取窗口标题（标题可能是用户的聊天内容）。
    """
    try:
        import ctypes
        user32 = user32 if user32 is not None else _user32()
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return ('', 0)
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
        return (str(buf.value or ''), _foreground_pid(user32, hwnd))
    except Exception:
        return ('', 0)


def _foreground_pid(user32, hwnd) -> int:
    """窗口属于哪个进程；查不到返回 0，安全闸门必须拒绝未知归属。"""
    try:
        import ctypes
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _alt_tap_activate(hwnd, sleep: Optional[Callable[[float], None]] = None,
                      tries: int = ALT_TAP_TRIES,
                      interval: float = ALT_TAP_INTERVAL,
                      owner_pid: Optional[int] = None) -> bool:
    """轻敲一次 ALT 松开 Windows 的前台锁，再把微信拉到前台；成功返回是否已成前台。

    为什么需要它：别的进程占着前台时（实测 QQ / Chrome_WidgetWin_1 在 pid 37712），
    系统会拒绝后台进程的 SetForegroundWindow，guia 的 bring_to_front 连试 5 次
    都返回 False，于是发送死在第一步。按一下 ALT 等于告诉系统"用户有动作"，
    前台锁随之解除，随后 SetForegroundWindow 返回 1、GetForegroundWindow 就是微信。
    边界：只按 VK_MENU(0x12) 单击，不发 Alt+Tab、不打字符、不改任何系统设置。

    判据为何不只看等于 main_hwnd：真机实测前台有时是微信自己的另一个顶层窗口
    （GetForegroundWindow 给的不是 main_hwnd），此时微信其实就在最前面、点击坐标
    照样落在微信上；窗口句柄不相等就判失败会让整条通道白等。因此"前台窗口属于
    微信进程"也算成功——真正保证点击不误伤的是 wx_click 里的进程归属校验。
    """
    sleep = sleep or time.sleep
    if not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.keybd_event(0x12, 0, 0, 0)      # VK_MENU down
        user32.keybd_event(0x12, 0, 2, 0)      # VK_MENU up
        sleep(0.15)
        user32.SetForegroundWindow(int(hwnd))
        target = int(hwnd)
        for _ in range(max(1, int(tries))):
            fg = int(user32.GetForegroundWindow() or 0)
            if fg == target:
                return True
            if fg and owner_pid and _foreground_pid(user32, fg) == int(owner_pid):
                return True
            sleep(interval)
        return False
    except Exception:
        return False


_MAX_CLIPBOARD_FORMATS = 64
# 别人的 GDI 句柄格式：跨进程复制不了，直接去取还会在 GlobalLock 上炸（真机见过的就是 CF_BITMAP）。
_HANDLE_FORMATS = {2, 3, 9, 14}
# 应用私有格式（如 0xBFxx/0xC0xx 这类，Windows 只保证 >= 0x0400 才留给应用）：
# 读不出来时只要还有标准格式顶得住粘贴，就不该为了它把整条回复卡死。
_PRIVATE_FORMAT_FLOOR = 0x0400


def _exc_label(exc, limit=80):
    """异常的人话标签：pywintypes.error 的类名就叫 error，光看名字等于没信息。"""
    text = str(exc).strip().replace('\n', ' ')
    name = type(exc).__name__
    if not text:
        return name
    if len(text) > limit:
        text = text[:limit] + '…'
    return f'{name}({text})'


def _is_win32_error(exc):
    """只有 Windows API 自己的错误才算「这个格式读不出来」；测试里的 TypeError/OSError 仍要原样上抛。"""
    try:
        import pywintypes
    except Exception:
        return False
    return isinstance(exc, pywintypes.error)


# 1.3.86：剪贴板占用重试。真机 2026-10-31 20:48 两次发送的收尾都死在
# pywintypes.error (5, 'OpenClipboard', '拒绝访问')——微信粘贴后会短暂持有剪贴板。
# 有界重试几百毫秒就能过去，超过预算才允许判失败。
CLIPBOARD_OPEN_BUDGET = 1.2
CLIPBOARD_OPEN_FIRST_DELAY = 0.02
CLIPBOARD_OPEN_MAX_DELAY = 0.16


def _open_clipboard(win32clipboard, budget=None, sleep=None, clock=None):
    """OpenClipboard 有界重试：被别的程序占住时不要立刻把整条回复判死。

    只重试 Windows API 自己的错误；替身/编程错误（TypeError 等）仍原样上抛，
    否则真正的 bug 会被当成环境问题掩掉。
    """
    budget = CLIPBOARD_OPEN_BUDGET if budget is None else budget
    sleep = sleep or time.sleep
    clock = clock or time.perf_counter
    deadline = clock() + max(0.0, budget)
    delay = CLIPBOARD_OPEN_FIRST_DELAY
    while True:
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception as exc:
            if not _is_win32_error(exc) or clock() >= deadline:
                raise
            sleep(delay)
            delay = min(delay * 2, CLIPBOARD_OPEN_MAX_DELAY)


class _Clipboard:
    """先物化原剪贴板字节；存不住的格式记名跳过，绝不让剪贴板挡住房回复发送。"""

    def __init__(self):
        self.skipped = ()
        self.partial = False
        self.restore_error = ''

    def snapshot(self):
        """物化原剪贴板字节（best-effort）：存不住的格式记进 self.skipped，照常返回已存部分。

        为什么要分两步：先原样枚举一遍再判定，才能知道"同一份快照里还有没有别的格式能顶上"。
        真机实测（用户刚截过图）：剪贴板里有 CF_BITMAP(2)、CF_DIB(8)、CF_DIBV5(17)、PNG/JPEG 等 17 种格式，
        pywin32 从 CF_BITMAP 拿回来的是别人的位图句柄（int），既不能复制也不能跨进程保留；
        但同一次快照里有 CF_DIB/CF_DIBV5 的字节，恢复后系统会按需把 DIB 合成为 CF_BITMAP，
        所以跳过句柄格式不会丢内容。存不住的格式一律记名跳过：剪贴板是临时数据，
        丢一份远好过机器人整条回复发不出去（真机 20:48 / 22:28 两次都是被剪贴板挡住的）。
        """
        import win32clipboard
        _open_clipboard(win32clipboard)
        try:
            items = []
            formats = set()
            fmt = win32clipboard.EnumClipboardFormats(0)
            # 上限与去重都是防呆：真实剪贴板格式数远小于 64，
            # 而某些替身/异常实现会一直返回同一个格式，不能因此死循环。
            skipped = []
            while fmt and len(items) < _MAX_CLIPBOARD_FORMATS and fmt not in formats:
                formats.add(fmt)
                if fmt in _HANDLE_FORMATS:
                    # 句柄格式不去取：pywin32 的 GetClipboardData(CF_BITMAP) 会 GlobalLock
                    # 别人的 GDI 句柄，真机 20:48 抛过 (6, 'GetClipboardData:GlobalLock', '句柄无效')。
                    # 只要有 CF_DIB/CF_DIBV5 这类同内容字节，恢复时系统会重新合成位图。
                    skipped.append(fmt)
                else:
                    try:
                        items.append((fmt, win32clipboard.GetClipboardData(fmt)))
                    except Exception as exc:
                        # 编程错误/替身异常仍要暴露；Windows API 自己的错误则是环境问题，跳过。
                        if not _is_win32_error(exc):
                            raise
                        # 私有的和标准的都一样：读不出就记名跳过。真机 20:48（GDI 句柄 GlobalLock）、
                        # 22:28（标准格式被别的程序持有）都栽在这里——剪贴板是临时数据，
                        # 丢一份远好过机器人整条回复发不出去。
                        skipped.append(fmt)
                fmt = win32clipboard.EnumClipboardFormats(fmt)
        finally:
            win32clipboard.CloseClipboard()
        saved = []
        for fmt, value in items:
            if isinstance(value, (str, bytes)):
                saved.append((fmt, value))
            elif fmt == 15 and isinstance(value, list) and all(isinstance(p, str) for p in value):
                saved.append((fmt, value))     # CF_HDROP：pywin32 收字符串列表，能原样写回
            else:
                # 句柄/自动转换出来的怪值也存不住，记名跳过（恢复时安静地少一项）。
                skipped.append(fmt)
        self.skipped = tuple(skipped)
        self.partial = bool(skipped)
        return saved

    def copy(self, text):
        import win32clipboard
        try:
            _open_clipboard(win32clipboard)
        except Exception as exc:
            if _is_win32_error(exc):
                raise _GuardFailed('send_failed',
                                   '系统剪贴板被其它程序占用（OpenClipboard 被拒绝），本次未输入正文') from exc
            raise
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

    def restore(self, snapshot):
        """把快照写回剪贴板：这是清理动作，永远不抛。

        真机 20:48 两次发送都是正文已粘好、回读也通过了，最后卡在 finally 里的 restore
        抛 (5,'OpenClipboard','拒绝访问')，把「已确认清空前放弃」整条判成通道内部异常。
        剪贴板是用户的临时数据，恢复失败只该记一笔，不该改变这条回复的结论。
        """
        self.restore_error = ''
        import win32clipboard
        try:
            _open_clipboard(win32clipboard)
            try:
                win32clipboard.EmptyClipboard()
                for fmt, value in snapshot or ():
                    win32clipboard.SetClipboardData(fmt, value)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception as exc:
            self.restore_error = _exc_label(exc)
            return False

    def text(self):
        import win32clipboard
        _open_clipboard(win32clipboard)
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return None
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()


def _keyboard_inputs(sequence):
    """按 (vk, flags) 序列构造 SendInput 的 INPUT 数组，返回 (events, sizeof)。

    键盘与鼠标必须共用同一个 40 字节负载：sizeof(INPUT) 与 Win32 不一致时 SendInput
    会整组拒收，清空草稿这类补救动作就会静默失效。
    """
    from ctypes import wintypes

    class Keyboard(ctypes.Structure):
        _fields_ = [('vk', wintypes.WORD), ('scan', wintypes.WORD),
                    ('flags', wintypes.DWORD), ('time', wintypes.DWORD),
                    ('extra', ctypes.c_size_t)]

    class Mouse(ctypes.Structure):
        _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                    ('data', wintypes.DWORD), ('flags', wintypes.DWORD),
                    ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]

    class Payload(ctypes.Union):
        _fields_ = [('keyboard', Keyboard), ('mouse', Mouse)]

    class Input(ctypes.Structure):
        _fields_ = [('type', wintypes.DWORD), ('payload', Payload)]

    events = (Input * len(sequence))()
    for event, (vk, flags) in zip(events, sequence):
        event.type = 1                      # INPUT_KEYBOARD
        event.payload.keyboard = Keyboard(vk, 0, flags, 0, 0)
    return events, ctypes.sizeof(Input)


def _looks_like_chat_missing(result) -> bool:
    """探测失败看起来是「没有打开的会话」时才值得去搜索打开。"""
    code = str(getattr(result, 'reason_code', '') or '')
    text = str(getattr(result, 'reason', '') or '')
    if code == 'target_not_open':
        return True
    for signal in ('分隔线', '标题栏', 'no_closed_frame', '没有打开的会话', '未打开会话'):
        if signal in text:
            return True
    return False


def _paste_keys(user32, key=0x56):
    """一次 SendInput 投递 Ctrl+V；不清草稿、不用拼音回退、不重试正文。"""
    events, size = _keyboard_inputs(((0x11, 0), (key, 0), (key, 2), (0x11, 2)))
    if user32.SendInput(4, events, size) != 4:
        # 部分投递也不能补发 Ctrl+V；只松开可能按住的键，防止污染之后的用户操作。
        user32.keybd_event(key, 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)
        raise RuntimeError('系统未完整接受粘贴按键，本次不重试')


def _tap_key(user32, vk):
    """投递一次不带修饰键的按键（按下 + 抬起）；只用于清空已粘贴的正文。

    为什么不复用 _paste_keys：那个函数整组按住 Ctrl（Ctrl+V / Ctrl+A / Ctrl+C），而
    清空要的是"全选之后按 Delete"；Ctrl+Delete 是另一个语义（按词删除），所以这里必须
    是一次干净的单键，且永远不是 Enter。
    """
    events, size = _keyboard_inputs(((vk, 0), (vk, 2)))
    if user32.SendInput(2, events, size) != 2:
        # 部分投递也不能补发：只松开可能按住的键，避免把用户的键盘卡在按下状态。
        user32.keybd_event(vk, 0, 2, 0)
        raise RuntimeError('系统未接受清空按键，本次不重试')


def _send_input_click(user32, x, y, sleep=None, before_press=None):
    """一次 SendInput 绝对坐标左键点击（按下/抬起各一次，总点击数恰好 1）。

    为什么不用 gui.wx_click 按发送键（真机实测，微信 4.1.15.13 / DPI 125%）：
    wx_click 在点击前临时清掉渲染子窗 MMUIRenderSubWindow 的 WS_EX_TRANSPARENT，
    点击于是直接投给这个不处理输入的渲染层——实测点消息区后输入框选区像素哈希
    完全没变（连焦点都没换），按发送键也毫无反应，草稿仍全选留在输入框里。
    不清该样式的 SendInput 绝对坐标点击命中测试仍归主窗，微信按正常点击处理：
    实测点发送键后 0.5s 内草稿变空、消息真的进了聊天记录。
    因此这里镜像 wechatauto WinInput.send_input_click 的做法（SetCursorPos 移动
    可见光标 + 两次绝对坐标 SendInput），但由本模块自己投递，便于注入与单测。
    """
    from ctypes import wintypes

    class Mouse(ctypes.Structure):
        _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                    ('data', wintypes.DWORD), ('flags', wintypes.DWORD),
                    ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]

    class Payload(ctypes.Union):
        _fields_ = [('mouse', Mouse)]

    class Input(ctypes.Structure):
        _fields_ = [('type', wintypes.DWORD), ('payload', Payload)]

    nap = sleep if sleep is not None else time.sleep
    # 绝对坐标须基于整个虚拟桌面：主屏尺寸无法表示左侧副屏的负坐标。
    # MOVE 也必须显式带上，否则 SendInput 会忽略 dx/dy，只在当前光标位置按下。
    try:
        left, top, width, height = (int(user32.GetSystemMetrics(index))
                                    for index in (76, 77, 78, 79))
        x, y = int(x), int(y)
    except Exception as exc:
        raise RuntimeError('无法核验虚拟桌面坐标，本次不点击') from exc
    if width < 2 or height < 2 or not (left <= x < left + width and top <= y < top + height):
        raise RuntimeError('发送键落点不在有效虚拟桌面内，本次不点击')
    if not user32.SetCursorPos(x, y):
        raise RuntimeError('无法将光标移动到已核验发送键，本次不点击')
    nap(0.15)
    # 持久化 send_key 和悬停都可能让出执行权，必须在唯一一次按下之前再核验。
    if before_press is not None:
        before_press()
    try:
        current_bounds = tuple(int(user32.GetSystemMetrics(index)) for index in (76, 77, 78, 79))
    except Exception as exc:
        raise RuntimeError('无法复核虚拟桌面坐标，本次不点击') from exc
    if current_bounds != (left, top, width, height):
        raise RuntimeError('虚拟桌面在发送前发生变化，本次不点击')
    nx = (x - left) * 65535 // (width - 1)
    ny = (y - top) * 65535 // (height - 1)
    position_flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    for flags in (position_flags | MOUSEEVENTF_LEFTDOWN,
                  position_flags | MOUSEEVENTF_LEFTUP):
        event = Input()
        event.type = 0                      # INPUT_MOUSE
        event.payload.mouse = Mouse(nx, ny, 0, flags, 0, 0)
        try:
            accepted = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(Input))
        except Exception:
            # API 异常也不能证明按下未生效；只释放按键，不补发点击。
            try:
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            except Exception:
                pass
            raise
        if accepted != 1:
            # 部分投递也不能补发按下；只松开左键，防止把用户鼠标卡在按下状态。
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            raise RuntimeError('系统未接受发送键点击，本次不重试')
        nap(0.06)


# 微信群标题栏末尾的成员数后缀：「群名（77）」。全角/半角括号各认一对，不认混搭。
_MEMBER_SUFFIX = re.compile(r'(?:（\d+）|\(\d+\))$')


def _strip_member_suffix(text):
    """剥掉群标题结尾的成员数后缀；没有该后缀时原样返回（同一个对象）。

    为什么只认这一种形态：微信群标题栏固定是「群名（成员数）」，1:1 会话没有后缀。
    只剥"结尾一对括号 + 纯数字"，不做前缀/子串/相似度匹配——否则
    「极客先锋社招新群（77）」会匹配上更短的「极客先锋社招新」，那是会发错人的假阳性。
    """
    if not isinstance(text, str):
        return text
    match = _MEMBER_SUFFIX.search(text)
    return text[:match.start()] if match else text


class _GuardFailed(Exception):
    def __init__(self, code, detail=''):
        self.code, self.detail = code, detail


class GuiaChannel:
    """「坐标+OCR」发送通道。所有依赖（gui 实例 / 时钟 / 抢前台）都可注入，便于离线单测。"""

    def __init__(self, gui_factory: Optional[Callable] = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 activate: Optional[Callable[[object], bool]] = None,
                 user32=None, clipboard=None, send_gate=None, account_path='',
                 expected_window=None, window_access=None, phase=None):
        self._expected_window = dict(expected_window or {})
        self._account_path = str(account_path or '')
        self._window_access = window_access
        self._factory = gui_factory or (lambda: _default_factory(self._expected_window.get('hwnd')))
        self._phase = phase or (lambda stage: None)
        self._submitted = False
        self.timeout = float(timeout)
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._user32 = user32
        self._clipboard = clipboard if clipboard is not None else _Clipboard()
        self._send_gate = send_gate or self._default_send_gate
        self._activate = activate or (
            lambda gui: _alt_tap_activate(getattr(gui, 'main_hwnd', 0), self._sleep,
                                          owner_pid=getattr(gui, 'pid', None)))
        self._gui = None
        self._title = ''      # 最近一次 OCR 读到的标题（脱敏后写进日志，便于事后对账）
        self._foreground_owner = ''   # 抓前台失败时占着前台的是谁（class:pid），写进失败日志
        self._exc_logged = False
        self._point_pid = 0
        self._foreground_pid = 0
        self._pasted = False
        self._draft_recovered = False  # 退出清理只尝试一次；不能复用 _pasted，它是“正文已粘”的证据
        self._draft_cleared = False    # 清理是否真的执行过：失败文案据此决定要不要让用户核对草稿
        self._press_ink = None   # 按下发送键前那一帧的墨迹量，供按键后确认清空
        self._title_cache = None  # 已核验标题 + 当帧标题条指纹；只在单次发送内有效
        self._body_len = 0
        # 1.3.82：Ctrl+F 抢不到搜索框时（微信 4.1.15 已知）在进程内停用自动打开，
        # 只活到本进程退出，不落盘——升级微信后自然恢复，不会永久坑住用户。
        self._auto_open_disabled = False
        self._auto_open_note = ''

    # ---------------------------------------------------------------
    # 通道对象
    # ---------------------------------------------------------------
    def _build(self):
        if self._gui is not None:
            return self._gui
        gui = self._factory()
        self._restore_window_if_needed(gui)
        self._assert_binding(gui)
        # 关键：guia 的 _get_uia() 一旦跑起来就会尝试热激活（写微信进程内存）。
        # 实例级覆盖成恒 None，_chat_is_open / _pending_text_ok 全部自动退回 OCR 分支。
        gui._uia = None
        gui._uia_tried = True
        gui._get_uia = lambda refresh=False: None
        # 这里原先用实例属性把 _grab_screen 换成 ImageGrab（为了双屏 all_screens），
        # 结果连 PixelGUI 的遮挡无关抓帧一起盖掉了：窗口被别的程序完全遮挡时，
        # 抓回来的是遮挡者的像素，布局检查必然失败。双屏正确性已由 PixelGUI 的
        # 回退路径（grab_screen → ImageGrab(all_screens=True)）保留，故不再覆盖。
        self._gui = gui
        return gui

    def _assert_binding(self, gui):
        if not self._expected_window:
            return  # 仅供显式注入假驱动的离线测试；默认工厂拒绝没有 hwnd 的实例。
        from .wechat_window import select_window, same_window_identity
        row, info = select_window(self._window_access, self._account_path)
        expected = self._expected_window
        if (not self._account_path or row is None or info.get('matched_count') != 1
                or info.get('fallback') or not same_window_identity(row, expected) or row.get('minimized')
                or int(gui.main_hwnd) != int(expected['hwnd'])
                or int(gui.pid) != int(expected['pid'])):
            raise _GuardFailed('guia_unavailable', '账号与窗口绑定已失效，本次不操作微信')

    def _restore_window_if_needed(self, gui):
        """窗口最小化或被隐藏时先恢复显示（不抢前台）；本来正常时零副作用。

        为什么放在 _build 里：send / probe / available 三条路径都先构造 gui，窗口缩在
        托盘里（或被别的程序隐藏）时后面每一步都会 fail-closed 成 guia_unavailable，
        而这跟「账号绑定失效」是两回事。只对已绑定账号的那一个 hwnd 动手，恢复不了
        照旧由 _assert_binding 报错。
        """
        hwnd = int((self._expected_window or {}).get('hwnd') or getattr(gui, 'main_hwnd', 0) or 0)
        minimized = bool(hwnd) and _iconic(hwnd)
        hidden = bool(hwnd) and not _visible(hwnd)
        if not hwnd or not (minimized or hidden):
            return True
        restored = _restore_minimized(hwnd, sleep=self._sleep, clock=self._clock)
        if minimized:
            note = '窗口最小化，已自动恢复' if restored else '窗口最小化，自动恢复失败'
        else:
            note = '窗口被隐藏，已自动恢复' if restored else '窗口被隐藏，自动恢复失败'
        log_send_phase('guia_restore_window', getattr(self, '_target', '') or '', 0, note, 'guia')
        return restored

    def _restore_hidden_after_key(self, gui, target):
        """按键之后窗口若被弄成隐藏，立刻恢复；绝不把「窗口消失」留给下一次调用。"""
        hwnd = int((self._expected_window or {}).get('hwnd')
                   or getattr(gui, 'main_hwnd', 0) or 0)
        if not hwnd or _visible(hwnd):
            return
        _restore_minimized(hwnd, sleep=self._sleep, clock=self._clock)
        log_send_phase('guia_restore_window', target, 0,
                       '窗口在按键后变为隐藏，已自动恢复', 'guia')

    def available(self) -> GuiaSendResult:
        """可用性探测：窗口在、未最小化、guia 可导入、系统 OCR 可用。"""
        try:
            gui = self._build()
        except Exception as exc:
            return _fail('guia_unavailable', f'{type(exc).__name__}: {exc}')
        try:
            if not gui.is_alive():
                return _fail('guia_unavailable', '微信窗口已关闭，请从托盘打开微信并登录')
        except Exception as exc:
            return _fail('guia_unavailable', f'{type(exc).__name__}: {exc}')
        hwnd = getattr(gui, 'main_hwnd', 0)
        minimized = _iconic(hwnd)
        hidden = bool(hwnd) and not _visible(hwnd)
        if minimized or hidden:
            # reason 码不新增：隐藏与最小化都属于「窗口不在可点击状态」，交给用户手动恢复。
            return _fail('minimized', '微信窗口最小化且自动恢复失败，请手动恢复后再发'
                         if minimized else '微信窗口被隐藏且自动恢复失败，请手动显示后再发')
        try:
            check = getattr(gui, 'ocr_available', None)
            if callable(check):
                ocr_ready = check()
            else:
                from wechatauto.guia import ScreenOCR
                ocr_ready = ScreenOCR.available()
        except Exception as exc:
            return _fail('guia_unavailable', f'OCR 不可用：{type(exc).__name__}: {exc}')
        if not ocr_ready:
            return _fail('guia_unavailable', '系统 OCR 不可用（需要 winsdk / Windows.Media.Ocr）')
        try:
            check = getattr(gui, 'readonly_ready', None)
            if callable(check) and not check():
                return _fail('guia_unavailable', '未唯一确认输入框、工具栏与标题区域')
        except Exception as exc:
            return _fail('guia_unavailable', f'只读布局检查失败：{type(exc).__name__}: {exc}')
        return _ok('「坐标+OCR」发送通道可用')

    # ---------------------------------------------------------------
    # 会话名校验（只读标题栏，绝不切换会话）
    # ---------------------------------------------------------------
    def current_chat(self, gui=None) -> str:
        """OCR 右侧面板标题行，返回拼好的会话标题；识别不到返回空串（fail-closed）。

        两条真机教训（微信 4.1.15.13，DPI 125%，right_pane_left=278）：
        * 标题不是一行文本，而是被 OCR 按字形切开的多段（'O' 与 '西莲寺紫' 的 y 只差
          1px）。只取最上面那一段会得到单字符 'O'，于是每次发送都误判 target_not_open。
          所以按基线 + TITLE_LINE_TOLERANCE 收齐同一条标题的片段，再按 x 升序拼接；
        * 横带左缘为了看清贴边的首字会向左多留 60px，那段属于左侧会话列表
          （实测 '社招新群' 就在 x=218）。侧栏绝不能参与标题，否则"侧栏里有同名
          会话"就会变成标题匹配成功——那是会发错人的假阳性，故按 x >= right_pane_left 过滤。
        横带高度仍只到标题区，正文/消息预览不进候选。
        """
        self._title_evidence = {'state': 'ocr_empty'}
        gui = gui or self._build()
        getter = getattr(gui, 'current_chat_title', None)
        if callable(getter):
            try:
                title = str(getter() or '')
                diagnostic = getattr(gui, 'title_diagnostic', None)
                evidence = diagnostic() if callable(diagnostic) else None
                states = ('not_read', 'verified', 'ocr_error', 'ocr_empty', 'incomplete', 'disagreement',
                          'capture_or_layout_error', 'header_missing', 'ocr_budget')
                if isinstance(evidence, dict) and evidence.get('state') in states:
                    self._title_evidence = evidence
                    return title if evidence['state'] == 'verified' else ''
                self._title_evidence = {'state': 'verified' if title else 'ocr_empty'}
                return title
            except Exception as error:
                name = type(error).__name__
                self._title_evidence = {'state': 'ocr_error', 'exception_type': name if name in (
                    'OSError', 'RuntimeError', 'ProtocolError', 'TimeoutError', 'ValueError',
                    'TypeError', 'AttributeError', 'PermissionError', 'FileNotFoundError') else 'OtherError'}
                try:
                    diagnostic = getattr(gui, 'title_diagnostic', None)
                    evidence = diagnostic() if callable(diagnostic) else None
                    if isinstance(evidence, dict) and evidence.get('state') in (
                            'ocr_error', 'ocr_empty', 'incomplete', 'disagreement',
                            'capture_or_layout_error', 'header_missing'):
                        self._title_evidence = evidence
                except Exception:
                    pass
                return ''
        try:
            self._geometry(gui)
            height = int(gui.render_h)
            band = int(min(TITLE_BAND_MAX, max(TITLE_BAND_MIN, height * TITLE_BAND_RATIO)))
            pane_left = int(gui.right_pane_left)
            left = max(0, pane_left - 60)   # 标题贴右面板左缘，向左多留一点再按 x 过滤
            lines = gui.ocr_zoomed((left, 0, int(gui.render_w), band), scale=3)
        except Exception:
            return ''
        rows = []
        for text, x, y, _w, _h in lines:
            text = str(text or '').strip()
            if not text:
                continue
            try:
                fx, fy = float(x), float(y)
            except (TypeError, ValueError):
                continue
            if fx < pane_left:              # 左侧会话列表，不属于标题
                continue
            rows.append((fy, fx, text))
        if not rows:
            return ''
        baseline = min(row[0] for row in rows)
        title = ''.join(text for fy, _fx, text in sorted(rows, key=lambda row: row[1])
                        if fy <= baseline + TITLE_LINE_TOLERANCE)
        title = title.strip()
        self._title_evidence = {'state': 'verified' if title else 'ocr_empty'}
        return title

    def title_diagnostic(self):
        # 不复制标题、异常正文或未知适配器字段。
        value = getattr(self, '_title_evidence', {})
        states = ('not_read', 'verified', 'ocr_error', 'ocr_empty', 'incomplete', 'disagreement',
                  'capture_or_layout_error', 'header_missing', 'ocr_budget')
        result = {'state': value.get('state') if value.get('state') in states else 'ocr_empty'}
        # profile 只是"这一帧用了哪张变体表"的说明性字段，不是安全判定依据；像素层现在
        # 会产出 boundary / small / medium_fallback / medium_binarized 等新档，按档名白名单
        # 过滤只会让真机现场丢掉定位线索，所以原样透传任意字符串档名。
        if isinstance(value.get('profile'), str):
            result['profile'] = value['profile']
        if type(value.get('glyph_height')) is int and 0 <= value['glyph_height'] <= 4096:
            result['glyph_height'] = value['glyph_height']
        if value.get('exception_type') in ('OSError', 'RuntimeError', 'ProtocolError', 'TimeoutError',
                'ValueError', 'TypeError', 'AttributeError', 'PermissionError', 'FileNotFoundError', 'OtherError'):
            result['exception_type'] = value['exception_type']
        result['passes'] = []
        passes = value.get('passes')
        for row in passes[:3] if isinstance(passes, list) else []:
            if (isinstance(row, dict) and type(row.get('rows')) is int and 0 <= row['rows'] <= 128
                    and type(row.get('characters')) is int and 0 <= row['characters'] <= 512
                    and type(row.get('complete')) is bool):
                result['passes'].append({key: row[key] for key in ('rows', 'characters', 'complete')})
        return result

    @staticmethod
    def title_matches(title: str, who: str) -> bool:
        """OCR 只允许完整标题相等；群标题末尾的成员数后缀剥掉后再逐字比一次。

        截断或包含关系仍不能证明收件人身份：剥后缀后必须与目标名完全相等，
        「极客先锋社招新群（77）」对「极客先锋社招新」依旧 False。
        """
        title = str(title or '').strip()
        who = str(who or '').strip()
        if not title or not who:
            return False            # 读不到就当对不上：宁可失败也不发错人
        return title == who or _strip_member_suffix(title) == who

    # ---------------------------------------------------------------
    # 标题核验：一次全族 OCR + 标题条像素签名复用
    # ---------------------------------------------------------------
    @staticmethod
    def _signature_key(value):
        """把驱动给的指纹归一成可比对标量；形状不对一律当"拿不到签名"。"""
        if not isinstance(value, dict):
            return None
        digest, region, window = value.get('digest'), value.get('region'), value.get('window')
        if not isinstance(digest, str) or len(digest) != 40:
            return None
        for box in (region, window):
            if (not isinstance(box, (tuple, list)) or len(box) != 4
                    or any(type(number) is not int for number in box)):
                return None
        return (digest, tuple(region), tuple(window))

    @classmethod
    def _last_signature(cls, gui):
        """上一次标题读取那一帧的指纹（不抓新帧）；驱动没提供就当没有。"""
        getter = getattr(gui, 'last_title_signature', None)
        if not callable(getter):
            return None
        try:
            return cls._signature_key(getter())
        except Exception:
            return None

    def _title_signature(self, gui, deadline):
        """取当前帧的标题条指纹；没有该方法、驱动报错或形状不对一律返回 None。"""
        getter = getattr(gui, 'chat_title_signature', None)
        if not callable(getter):
            return None
        try:
            return self._signature_key(self._read(deadline, getter))
        except TimeoutError:
            raise          # 超时仍按既有语义失败：不能把"没核验"当成"标题没变"
        except Exception:
            return None

    def _current_signature(self, gui):
        """清理前自证用的当前帧标题条指纹；没有该能力、形状不对或取帧抛异常一律 None。

        为什么不复用 _title_signature：那个助手把读取套进发送预算（_read 需要 deadline），
        而清理只发生在失败之后，预算往往已经用完——用它等于让"超时中止"这个最需要清理的
        场景永远清不掉。归一化仍只有 _signature_key 一处，不另写一套。
        """
        getter = getattr(gui, 'chat_title_signature', None)
        if not callable(getter):
            return None
        try:
            return self._signature_key(getter())
        except Exception:
            return None

    def _verified_title(self, gui, deadline, signature=None):
        """核验当前会话标题：指纹没变就复用已核验标题（零 OCR），否则退回全族 OCR。

        校验点一个都没少：每处仍然要拿到"这一帧的标题是哪一行"，换的只是取证方式——
        标题条指纹相同即证明标题文字与核验时逐像素一致，可以复用那次通过三份一致
        OCR 的标题；指纹不同、没有缓存、取不到帧或任何异常，一律退回全族 OCR，读不到
        或读到别的会话名照旧拒绝（fail-closed，不把"没核实"当成"已核实"）。

        signature 是调用方在同一帧（probe_composer）里顺手拿到的标题条指纹：用它只是
        省掉再抓一帧，判定完全不变——归一化仍只有 _signature_key 一处，形状不对等同于
        "拿不到签名"，照样退回全族 OCR，绝不把它当成"标题没变"。
        """
        cached = self._title_cache
        if cached:
            key = (self._signature_key(signature) if signature is not None
                   else self._title_signature(gui, deadline))
            if key is not None and key == cached['key']:
                self._title = cached['title']
                return self._title
        title = self._read(deadline, lambda: self.current_chat(gui))
        self._title = title
        # 只有真正通过全族核验的标题才进缓存，而且必须绑定同一帧的指纹；绑定不上就
        # 不缓存（之后每处继续走全族 OCR），绝不用"没有指纹"的标题冒充"已核验"。
        key = self._last_signature(gui) if title else None
        self._title_cache = {'title': title, 'key': key} if key is not None else None
        return title

    # ---------------------------------------------------------------
    # 发送
    # ---------------------------------------------------------------
    def send(self, who: str, text: str) -> GuiaSendResult:
        """发送入口：_attempt 只负责发，失败后在这里补一行带原因码的 ERROR。

        拆两层是为了失败原因只有一个出口：每个 _fail 分支不必各写一遍日志，
        运行记录里也永远只有一条红字，不会一条失败刷出好几块。
        """
        body = str(text or '')
        self._title = ''
        self._foreground_owner = ''
        self._exc_logged = False
        self._point_pid = 0
        self._foreground_pid = 0
        self._pasted = False
        self._draft_recovered = False
        self._draft_cleared = False
        self._submitted = False
        self._press_ink = None
        self._title_cache = None     # 已核验标题绝不跨发送复用：换了会话必须重新全族 OCR
        self._body_len = len(body)
        self._target = str(who or '').strip()
        self._title_evidence = {'state': 'not_read'}
        # 阶段时刻只做归因：不动任何键鼠事件、顺序或阈值。
        start_send_phases()
        try:
            result = self._attempt(who, body)
        finally:
            mark_send_phase('end')
            phases = drain_send_phases()
        if result.reason_code == 'target_not_open' and not self._title:
            result = _fail('title_unverified', '未能核验完整标题；如已输入请保留草稿核对，不重发')
        if self._draft_cleared and not result.ok and '请核对微信草稿' in result.reason:
            # 清回已确认执行（且清理前已核验仍在目标会话）：这时再让用户“核对草稿”是误导，
            # 改成核对是否还有残留；keep 分支（target_changed/target_unverified/foreground_lost）
            # 的文案保持原样——那才是真的需要用户去核对草稿。
            result.reason = result.reason.replace(
                '请核对微信草稿', '输入框已按目标会话核验清回，请核对是否还有残留')
            result.diagnostic['reason'] = result.reason
        if hasattr(self, '_title_evidence'):
            result.diagnostic['title_read'] = self.title_diagnostic()
        result.diagnostic.update(send_key=self._submitted, input_touched=self._pasted,
                                 delivery_verified=False,
                                 submission_state=('confirmed' if result.ok else 'uncertain')
                                 if self._submitted else 'not_submitted')
        if not result.ok:
            if not self._foreground_owner:
                cls, pid = foreground_owner(self._win32())
                self._foreground_owner = f'{cls}:{pid}'
                self._foreground_pid = pid
            result.diagnostic.update(point_pid=self._point_pid,
                                     foreground_pid=self._foreground_pid)
        if not result.ok and not self._exc_logged:
            log_send_failure(result.reason_code, who, len(body), channel='guia',
                             hint=(REASON_HINT.get(result.reason_code, '') +
                                   f' point_pid={self._point_pid} foreground_pid={self._foreground_pid}'),
                             title=self._title, foreground=self._foreground_owner)
        timings = _send_timings(phases)
        if timings:
            result.diagnostic['timings'] = timings
        skipped_clip = tuple(getattr(self._clipboard, 'skipped', ()) or ())
        if skipped_clip:
            # 只报格式号，绝不带剪贴板内容；用于事后核对"这次是不是丢了原剪贴板的一部分"。
            result.diagnostic['clipboard_skipped'] = list(skipped_clip)
        return result

    def _win32(self):
        if self._user32 is None:
            self._user32 = _user32()
            # 默认 ctypes 返回 c_int，会截断 64 位窗口句柄。
            self._user32.GetForegroundWindow.restype = ctypes.c_void_p
            self._user32.WindowFromPoint.restype = ctypes.c_void_p
            self._user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            self._user32.GetAncestor.restype = ctypes.c_void_p
        return self._user32

    def _bound_window_root(self, gui, hwnd):
        """渲染子窗可以属于主窗，同进程的其他顶层弹窗不能作为输入目标。"""
        try:
            hwnd = int(hwnd or 0)
            expected = int(gui.main_hwnd or 0)
            pid = int(gui.pid or 0)
            if not hwnd or not expected or not pid or _foreground_pid(self._win32(), hwnd) != pid:
                return False
            root = self._win32().GetAncestor(ctypes.c_void_p(hwnd), 2)  # GA_ROOT，不能沿owner链跨入弹窗。
            return bool(root) and int(root) == expected
        except Exception:
            return False

    @staticmethod
    def _default_send_gate():
        from wechatauto import rhythm
        rhythm.gate('send')

    def _read(self, deadline, fn):
        """只读探测受总预算约束。超时线程没有点击/剪贴板/按键的后续任务。"""
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError()
        result = queue.Queue(maxsize=1)

        def run():
            try:
                result.put((True, fn()))
            except Exception as exc:
                result.put((False, exc))

        threading.Thread(target=run, daemon=True, name='guia-read').start()
        try:
            ok, value = result.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError() from None
        self._check_deadline(deadline)
        if not ok:
            raise value
        return value

    def _check_deadline(self, deadline):
        if self._clock() >= deadline:
            raise TimeoutError()

    @staticmethod
    def _geometry(gui):
        gui._update_render_rect()
        return (int(gui.origin_x), int(gui.origin_y), int(gui.render_w), int(gui.render_h))

    @staticmethod
    def _checked_input_box(gui, box):
        # 初次定位、粘贴后伸缩与发送后收缩都用同一边界，不能把未知区域当成空草稿。
        if (not isinstance(box, (tuple, list)) or len(box) != 4
                or any(type(value) is not int for value in box)
                or not (gui.right_pane_left <= box[0] < box[2] <= gui.render_w
                        and gui.render_h // 2 <= box[1] < box[3] <= gui.render_h)):
            raise _GuardFailed('send_failed', '输入框位置不在已核验聊天文本区，本次不继续操作')
        return tuple(box)

    def _probe(self, gui, deadline, *, want_ink=False):
        """在同一帧里取"正文框 + 草稿判定 + 标题条指纹"；驱动没这能力就返回 None。

        老驱动与既有测试替身不提供 probe_composer，一律返回 None，调用方照旧逐项抓帧，
        行为与调用计数完全不变；真驱动才有合并机会。返回形状不对同样当没有，绝不猜。
        """
        probe = getattr(gui, 'probe_composer', None)
        if not callable(probe):
            return None
        value = self._read(deadline, lambda: probe(want_ink=want_ink))
        if not isinstance(value, dict) or 'box' not in value or 'has_text' not in value:
            return None
        return value

    def _empty_input_destination(self, gui, deadline):
        """复核当前会话与可用输入框；草稿不再拒绝，改为先自动清空再复核。

        顺序是有意的：标题核验必须排在动手之前——只有先自证「还在目标会话」，
        Ctrl+A + Delete 才敢按；否则删掉的可能是用户在另一个会话里写的草稿。
        """
        self._assert_binding(gui)
        self._require_foreground(gui, deadline)
        probe = self._probe(gui, deadline)
        if probe is None:
            box = self._checked_input_box(gui, self._read(deadline, gui.get_input_box))
            has_text = bool(self._read(deadline, lambda: gui._input_box_has_text(box)))
            self._title = self._verified_title(gui, deadline)
        else:
            # 同一帧只抓一次，三条判据仍各自独立执行：框越界照样由 _checked_input_box 拒、
            # 标题指纹不符照样退回全族 OCR 再比对——合并的只是取证次数。
            box = self._checked_input_box(gui, probe.get('box'))
            has_text = bool(probe.get('has_text'))
            self._title = self._verified_title(gui, deadline,
                                               signature=probe.get('title_signature'))
        if not self.title_matches(self._title, self._target):
            raise _GuardFailed('target_not_open', '粘贴前当前会话发生变化，本次未输入正文')
        self._assert_binding(gui)
        self._require_foreground(gui, deadline)
        if has_text:
            self._clear_existing_draft(gui, deadline, box)
            self._title = self._verified_title(gui, deadline)
            if not self.title_matches(self._title, self._target):
                raise _GuardFailed('target_not_open', '清空草稿后当前会话发生变化，本次未输入正文')
        self._check_deadline(deadline)
        return box

    def _clear_existing_draft(self, gui, deadline, box):
        """发送前把输入框里已有的草稿清掉：Ctrl+A + Delete，只做一次，清不干净就拒绝输入。

        为什么要做（用户 m10623「之后你自动清」）：真机输入框里悬着上一次留下的图片/文字
        卡片时，每次发送都在「粘贴前检测到草稿」这一步被拒，只能由用户手动去点微信清空。
        绝不越过的边界：只在「绑定窗口 + 微信在前台 + 标题已核验等于目标」之后动手；只发
        Ctrl+A 与 Delete，永不发 Enter/回车、不写剪贴板、不点会话列表、不切会话；清完必须
        用同一驱动复核真的空了，仍非空就拒绝本次输入（绝不带着残稿粘贴）。
        """
        if not auto_clear_draft_enabled():
            raise _GuardFailed('draft_not_empty', '粘贴前检测到已有草稿，本次未输入；请手动核对')
        failure = ''
        before_ink = self._composer_ink(gui, box)
        try:
            _paste_keys(self._win32(), 0x41)   # Ctrl+A：选中草稿
        except Exception as exc:
            failure = f'Ctrl+A 未投递({type(exc).__name__})'
        if not failure:
            try:
                _tap_key(self._win32(), 0x2E)  # Delete：删掉选中内容，不按 Enter
            except Exception as exc:
                failure = f'Delete 未投递({type(exc).__name__})'
        cleared = bool(not failure and self._cleared_after_keys(gui, box, before_ink, deadline))
        if cleared:
            self._draft_cleared = True
        log_send_phase('guia_draft_clear' if cleared else 'guia_draft_clear_failed',
                       self._target, self._body_len,
                       reason='发送前自动清空草稿' if cleared else (failure or '清空后仍有草稿'),
                       channel='guia')
        if cleared:
            return
        raise _GuardFailed('draft_not_empty',
                           '输入框里的草稿自动清空失败，本次未输入；'
                           '请手动点进输入框按 Ctrl+A 再 Delete 后重试')

    @staticmethod
    def _composer_ink(gui, box):
        """清空前量一次正文墨迹；驱动没有 composer_ink（离线替身/老驱动）时返回 None。"""
        measure = getattr(gui, 'composer_ink', None)
        if not callable(measure):
            return None
        try:
            ink = measure(box)
        except Exception:
            return None
        if not isinstance(ink, dict) or 'pixels' not in ink:
            return None
        return ink

    def _cleared_after_keys(self, gui, box, before_ink, deadline):
        """Ctrl+A + Delete 之后判断输入框真的空了。

        真机教训（1.3.58 首次上机验证）：PrintWindow 位图里光标层常在，清空后
        draft_state 仍是 'caret_like'，_input_box_has_text 等不到闪灭帧就报「有文字」，
        于是明明已经清干净了还会按「自动清空失败」拒绝输入。这里优先用 1.3.42 为同一种
        误判引入的墨迹判据：state=='empty'，或 caret_like 且墨迹确实比清空前少、
        columns<=2 或降到 max(2, 30%) 以内；驱动没有那套能力时照旧退回 has_text。
        """
        confirm = getattr(gui, 'composer_cleared', None)
        if callable(confirm) and before_ink is not None:
            until = min(deadline, self._clock() + DRAFT_CLEAR_WINDOW)
            try:
                if self._read(until, lambda: confirm(box, before_ink,
                                                     window=DRAFT_CLEAR_WINDOW)):
                    return True
            except Exception:
                pass
            # 真机教训之二（1.3.58 第二次上机）：多行草稿删掉后输入框会变矮，拿清空前
            # 那个旧框去量墨迹会被驱动判「布局改变」；光标帧也可能被判成 'text'。
            # 所以再抓一帧新布局，用「只剩光标就算清空」的同一套判据复核一次。
            settled = self._probe(gui, deadline, want_ink=True)
            if settled is None:
                return False
            if self._ink_cleared(settled.get('ink'), before_ink):
                return True
            return not bool(settled.get('has_text'))
        settled = self._probe(gui, deadline)
        if settled is None:
            return not bool(self._read(deadline, lambda: gui._input_box_has_text(box)))
        return not bool(settled.get('has_text'))

    @staticmethod
    def _ink_cleared(ink, before_ink=None):
        """一帧墨迹是否已经算清空：空、或只剩光标、或墨迹掉到四成以下只剩 <=2 列、
        或光标态下墨迹确实变少。第三条与 composer_cleared 的同名判据必须一致。"""
        if not isinstance(ink, dict) or 'pixels' not in ink:
            return False
        state = ink.get('state')
        pixels = int(ink.get('pixels') or 0)
        columns = int(ink.get('columns') or 0)
        if state == 'empty':
            return True
        if pixels <= CARET_INK_MAX and columns <= 2:
            return True
        before_pixels = int((before_ink or {}).get('pixels') or 0)
        limit = max(2, int(0.3 * before_pixels))
        # 真机 Delete 之后那一帧常被 draft_state 判成 'text'（光标层常驻位图），
        # 上面两条都不成立；墨迹掉到四成以下且只剩 <=2 个竖列，只可能是光标或极少量
        # 残影，与 composer_cleared 的第 3 条判据同形，两处必须一致。
        if pixels <= max(2, int(0.4 * before_pixels)) and columns <= 2:
            return True
        return bool(state == 'caret_like' and pixels < before_pixels
                    and (columns <= 2 or pixels <= limit))

    def _confirmation_target(self, gui, deadline):
        """按键之后复核目标会话；顺带返回同一帧的正文框与墨迹，供清空确认当首帧。"""
        # 另一个会话的空输入框不能证明本次正文已提交；空白读取前后都核对目标。
        self._assert_binding(gui)
        self._require_foreground(gui, deadline, pasted=True)
        # 按键之后比标题条指纹、零 OCR（CHANGES.md 早就写着"按键后不再重复标题 OCR"，
        # 这里是代码去对齐文档）：指纹没变即证明标题文字与按键前逐像素一致；指纹变了
        # （窗口移动/缩放、标题换了）才退回全族 OCR，读到别的会话名照旧拒绝。
        # 指纹取自已合并的那一帧（标题条、正文框、墨迹同帧）；取不到时退回抓新帧。
        probe = self._probe(gui, deadline, want_ink=True)
        self._title = self._verified_title(gui, deadline,
                                           signature=(probe or {}).get('title_signature'))
        if not self.title_matches(self._title, self._target):
            raise _GuardFailed('target_not_open', '发送确认期间当前会话发生变化，本次不重发')
        self._assert_binding(gui)
        self._require_foreground(gui, deadline, pasted=True)
        self._check_deadline(deadline)
        return probe

    def _point(self, gui, x, y, geometry):
        self._assert_binding(gui)
        if self._geometry(gui) != geometry:
            raise _GuardFailed('send_failed', '微信窗口位置或尺寸发生变化，请保持窗口不动后重试')
        from ctypes import wintypes
        hwnd = self._win32().WindowFromPoint(wintypes.POINT(int(x), int(y)))
        self._point_pid = _foreground_pid(self._win32(), hwnd) if hwnd else 0
        if not gui.pid or self._point_pid != int(gui.pid) or not self._bound_window_root(gui, hwnd):
            raise _GuardFailed('blocked_by_window')

    def _require_foreground(self, gui, deadline, wait=False, pasted=False):
        # 点击后至多等 300ms；即使注入的时钟/sleep 不前进，也有固定次数上限。
        until = min(deadline, self._clock() + (0.3 if wait else 0))
        for attempt in range(16 if wait else 1):
            self._check_deadline(deadline)
            cls, pid = foreground_owner(self._win32())
            self._foreground_pid = pid
            if (pid and pid == int(gui.pid or 0)
                    and self._bound_window_root(gui, self._win32().GetForegroundWindow())):
                return
            self._foreground_owner = f'{cls}:{pid}'
            if not wait or self._clock() >= until or attempt == 15:
                break
            self._sleep(min(0.02, max(0, until - self._clock())))
        raise _GuardFailed('foreground_lost',
                           '粘贴后焦点发生变化，本次未按发送键；请核对微信草稿' if pasted else '')

    def _paste(self, gui, body, deadline):
        # 粘正文之前必须有一次只读复核（会话仍在目标 + 输入框仍空）。它排在剪贴板
        # 快照之前是有意的：真机上出现草稿时，拒绝发生在写剪贴板之前（tests/
        # test_guia_sendcost1350 用例锁死写入次数为 0），退回去改顺序会碰用户剪贴板。
        gui._last_input_box = self._empty_input_destination(gui, deadline)
        snapshot = self._clipboard.snapshot()
        try:
            self._require_foreground(gui, deadline)
            self._clipboard.copy(body)
            # 写剪贴板也可能花时间；同一主窗切聊天/出现草稿也须在Ctrl+V前拒绝。
            gui._last_input_box = self._empty_input_destination(gui, deadline)
            self._pasted = True  # SendInput 即使部分投递也可能已输入，后续文案不得声称未输入。
            _paste_keys(self._win32())
            self._sleep(min(0.2, max(0, deadline - self._clock())))
            self._require_foreground(gui, deadline, pasted=True)
            # 粘贴本身即可撑高输入框，首次检查正文前就要取得新布局，不能等发送键阶段。
            probe = self._probe(gui, deadline)
            if probe is None:
                gui._last_input_box = self._checked_input_box(gui, self._read(deadline, gui.get_input_box))
                if not self._read(deadline, lambda: gui._input_box_has_text(gui._last_input_box)):
                    raise _GuardFailed('send_failed', '正文没有粘进输入框，本次未按发送键')
            else:
                # 新布局与"正文确实进了框"同一帧取得；框越界/草稿为空仍各自独立判定。
                gui._last_input_box = self._checked_input_box(gui, probe.get('box'))
                if not probe.get('has_text'):
                    raise _GuardFailed('send_failed', '正文没有粘进输入框，本次未按发送键')
        finally:
            self._clipboard.restore(snapshot)

    def _readback(self, gui, body, deadline):
        """只选择/复制已写入正文；不用 Delete，不以非空像素冒充完整正文。"""
        self._require_foreground(gui, deadline, pasted=True)
        self._assert_binding(gui)
        self._title = self._verified_title(gui, deadline)
        if not self.title_matches(self._title, self._target):
            raise _GuardFailed('target_not_open', '回读前当前会话发生变化，本次未按发送键；请核对微信草稿')
        snapshot = self._clipboard.snapshot()
        marker = 'whale-readback-' + uuid.uuid4().hex
        try:
            self._clipboard.copy(marker)
            self._require_foreground(gui, deadline, pasted=True)
            _paste_keys(self._win32(), 0x41)
            self._require_foreground(gui, deadline, pasted=True)
            _paste_keys(self._win32(), 0x43)
            until = min(deadline, self._clock() + 0.5)
            copied = None
            for _ in range(26):
                self._require_foreground(gui, deadline, pasted=True)
                try:
                    copied = self._clipboard.text()
                except Exception:
                    copied = None
                if copied is not None and copied != marker:
                    break
                if self._clock() >= until:
                    break
                self._sleep(min(0.02, max(0, until - self._clock())))
            if not isinstance(copied, str) or copied.replace('\r\n', '\n') != body.replace('\r\n', '\n'):
                raise _GuardFailed('send_failed', '正文回读不一致或不可读取，本次未按发送键；请核对微信草稿')
        finally:
            self._clipboard.restore(snapshot)

    def _recover_draft(self, gui):
        """粘贴后失败时把输入框清回粘贴前的状态：Ctrl+A + Delete，绝不按 Enter。

        为什么必须做：真机 19:03 中止时正文已经粘进输入框没清掉，之后每次发送的就绪
        探测都报 draft_not_empty，后续所有发送被永久堵死，只能人工清。所以"正文已粘
        进、发送键没按下"的失败路径都要把正文去掉。
        绝不越过的边界：一旦按过发送键（结果不确定）就一个字都不能碰——消息可能已经
        发出去了；清理只发 Ctrl+A 与 Delete，永不发 Enter/回车，也不写剪贴板（用户原
        剪贴板由粘贴路径自己恢复）；微信不在前台时不做，否则会删掉别的窗口的内容。
        还必须自证仍在目标会话：只有本轮缓存过已核验标题 + 同帧标题条指纹、且当前帧指纹与
        缓存相等时才清；指纹不同、驱动没有签名能力或取帧失败一律一个字都不碰。
        """
        if not self._pasted or self._submitted or self._draft_recovered:
            return
        # _pasted 必须留着：诊断里的 input_touched 就取自它，清掉等于声称“输入框没被动过”，
        # 而正文确实已经粘进去了（正文回读不一致时用户还要照着输入框核对草稿）。
        self._draft_recovered = True   # 只尝试一次：任何路径都不会触发第二次清理
        try:
            if not self._wechat_is_foreground(gui):
                log_send_phase('guia_draft_keep', self._target, 0,
                               reason='foreground_lost', channel='guia')
                return
            # 必须自证还在目标会话：粘贴后用户手点切到别的聊天，Ctrl+A + Delete 删掉的就是
            # 用户自己在另一会话写的草稿；而正文仍留在目标会话草稿里，清理也不解决自堵。
            # 唯一证据：本轮缓存过「已核验标题 + 同帧标题条指纹」且当前帧指纹与它相等。
            cached_key = (self._title_cache or {}).get('key')
            current_key = self._current_signature(gui) if cached_key is not None else None
            if cached_key is None or current_key is None:
                log_send_phase('guia_draft_keep', self._target, 0,
                               reason='target_unverified', channel='guia')
                return
            if current_key != cached_key:
                log_send_phase('guia_draft_keep', self._target, 0,
                               reason='target_changed', channel='guia')
                return
            cleared = True
            try:
                _paste_keys(self._win32(), 0x41)      # Ctrl+A：选中已粘贴的正文
            except Exception:
                cleared = False
            if cleared:
                try:
                    _tap_key(self._win32(), 0x2E)     # Delete：删掉选中内容，不按 Enter
                except Exception:
                    cleared = False
            self._draft_cleared = cleared   # 清回真的发生了：文案不能再叫用户去核对草稿
            log_send_phase('guia_draft_clear' if cleared else 'guia_draft_clear_failed',
                           self._target, self._body_len,
                           reason='已清回输入框' if cleared else '清回输入框未确认',
                           channel='guia')
        except Exception:
            pass    # 补救动作的任何异常都不能盖住真正的失败原因

    def _wechat_is_foreground(self, gui) -> bool:
        """前台必须是绑定主窗或其渲染子窗；同进程的另一顶层窗也要拒绝。"""
        hwnd = int(getattr(gui, 'main_hwnd', 0) or 0)
        if not hwnd:
            return False
        try:
            fg = int(self._win32().GetForegroundWindow() or 0)
        except Exception:
            return False
        if not fg:
            return False
        return self._bound_window_root(gui, fg)

    def _foreground(self, gui) -> bool:
        """把微信拉到前台；已经在最前面时一个按键都不发。

        先只读看一眼是必要的：微信本来就在前台时还去按 ALT，等于平白发按键给
        系统（真机实测前台已是微信也会多按一次），而这种多余动作正是我们要避免的。
        前台不是微信时才走 ALT 轻敲 → gui.bring_to_front → 补敲的链路。
        """
        if self._wechat_is_foreground(gui):
            return True
        if self._try_activate(gui):
            return True
        try:
            if gui.bring_to_front(keep_topmost=True):
                return True
        except Exception:
            pass
        # 老路径也失败时再敲一次：实测前台锁偶尔要第二次轻敲才松开，而这里只多一次
        # VK_MENU，仍不碰任何其它按键或系统设置。
        return self._try_activate(gui)

    def _try_activate(self, gui) -> bool:
        try:
            return bool(self._activate(gui))
        except Exception:
            return False

    def _open_target_chat(self, gui, target: str, deadline):
        """1.3.61：用 Ctrl+F 搜索会话名 → Enter 打开 → 标题 OCR 核对。

        只在**真发送**路径、且当前不是目标会话时才调用；可用性轮询永远不会走到这里，
        所以不会平白弹窗。成功返回 None，失败返回 GuiaSendResult（一个键都没往正文里打）。
        """
        if not auto_open_chat_enabled():
            return _fail('target_not_open',
                         f'当前不是「{target}」会话，且 {AUTO_OPEN_CHAT_ENV}=0 关了自动打开；请手动点开该会话')
        if self._auto_open_disabled:
            # 上一次已经证明这版微信的搜索框抢不到焦点：再按一次 Ctrl+F / Ctrl+V 只会把会话名
            # 又打进正文框，毫无收益，所以直接拒绝，一个键都不发。
            note = self._auto_open_note or '上一次自动打开失败'
            return _fail('target_not_open',
                         f'当前不是「{target}」会话；本进程已停用自动打开（{note}），'
                         f'请手动点开该会话后再让机器人回复')
        try:
            if not self._foreground(gui):
                return _fail('guia_unavailable', '无法把微信窗口置前，本次未尝试打开会话')
            snapshot = self._clipboard.snapshot()
        except Exception as exc:
            return _fail('guia_unavailable', f'{type(exc).__name__}: {exc}')
        try:
            # 1.3.87：每一批发往窗口的按键之前都只读核一次前台。只在开始时抢一次前台
            # 是不够的——真机上中途前台被 Chrome 抢走，会话名就被粘进了浏览器。
            self._require_foreground(gui, deadline)
            self._clipboard.copy(target)
            user32 = self._win32()
            _paste_keys(user32, 0x46)            # Ctrl+F：搜索
            self._sleep(0.35)
            self._require_foreground(gui, deadline)
            _paste_keys(user32, 0x56)            # Ctrl+V：会话名
            self._sleep(OPEN_CHAT_SEARCH_WAIT)
            # 搜索框没抢到焦点时，名字会落进输入框；此时按 Enter 等于把会话名发出去。
            # 先只读看一眼输入框，有字就清掉并放弃，绝不按 Enter。
            box = None
            try:
                box = gui.get_input_box()
            except Exception:
                box = None
            if box and self._read(deadline, lambda: gui._input_box_has_text(box)):
                _paste_keys(user32, 0x41)        # Ctrl+A
                _tap_key(user32, 0x2E)           # Delete
                _tap_key(user32, 0x1B)           # Esc
                # 1.3.87：按完 Esc 立刻回看窗口是否被这次按键弄成隐藏，是就当场恢复。
                self._restore_hidden_after_key(gui, target)
                log_send_phase('guia_open_chat', target, 0, reason='search_unavailable', channel='guia')
                # 1.3.82：搜索框没抢到焦点不等于「没法打开」——用户可能在这半秒里自己点开了
                # 目标会话。清完框再只读核一次标题，已经是目标会话就直接当成功返回，省一次失败。
                recovered = ''
                try:
                    recovered = self._verified_title(gui, deadline)
                except Exception:
                    recovered = ''
                if self.title_matches(recovered, target):
                    log_send_phase('guia_open_chat', target, 0, '搜索框未聚焦，但当前已是目标会话', 'guia')
                    mark_send_phase('guia_open_chat')
                    self._title = recovered
                    return None
                self._auto_open_disabled = True
                self._auto_open_note = '这版微信不响应 Ctrl+F 搜索'
                log_send_phase('guia_open_chat', target, 0, reason='auto_open_disabled', channel='guia')
                return _fail('target_not_open',
                             '搜索框没有拿到焦点（会话名落进了输入框，已清掉）；这版微信可能不响应 '
                             'Ctrl+F（4.1.15 已知），已停用自动打开。请先手动点开'
                             f'「{target}」，再让机器人回复')
            # Enter 会把搜索词发出去：猜不到前台是谁时绝不按。
            self._require_foreground(gui, deadline)
            _tap_key(user32, 0x0D)               # Enter：打开第一条搜索结果
            self._sleep(OPEN_CHAT_RESULT_WAIT)
            title = self._verified_title(gui, deadline)
            if self.title_matches(title, target):
                log_send_phase('guia_open_chat', target, 0, '已用搜索打开目标会话', 'guia')
                mark_send_phase('guia_open_chat')
                self._title = title
                return None
            _tap_key(user32, 0x1B)               # Esc：退回，不进正文
            # 1.3.87：失败前这一下 Esc 也可能让窗口消失，恢复完再退出本次打开。
            self._restore_hidden_after_key(gui, target)
            log_send_phase('guia_open_chat', target, 0, reason='title_mismatch', channel='guia')
            return _fail('target_not_open',
                         f'自动打开后标题读到「{title or "识别失败"}」，与「{target}」不一致，本次未输入正文')
        except _GuardFailed as exc:
            # 前台守卫失败必须保留自己的 reason 码（foreground_lost），不能被下面的兜底
            # 异常改写成 target_not_open；diagnostic 里带上占着前台的是谁，便于事后对账。
            failed = _fail(exc.code, exc.detail or '前台已不是微信窗口，本次未继续按键')
            failed.diagnostic['foreground'] = self._foreground_owner
            return failed
        except Exception as exc:
            # 自动打开只是尽力而为：按键被系统拒收、搜索框读不出等一律降级为「本次没打开」，
            # 绝不把异常抛回发送主流程（那会变成 send_failed，掩盖真正的原因）。
            return _fail('target_not_open',
                         f'自动打开会话失败（{type(exc).__name__}: {exc}），本次未输入正文')
        finally:
            try:
                self._clipboard.restore(snapshot)
            except Exception:
                pass

    def _attempt(self, who: str, body: str) -> GuiaSendResult:
        target = str(who or '').strip()
        if not target or any(ch in target for ch in '\r\n\x00') or not body:
            return _fail('invalid_target', '会话名为空/含换行，或正文为空')
        deadline = self._clock() + self.timeout
        self._phase('window')
        ready = self.available()
        if not ready.ok and _looks_like_chat_missing(ready):
            # 只读探测失败且看起来是「没有打开的会话」：试着用搜索打开目标会话再看一次。
            opened = self._open_target_chat(getattr(self, '_gui', None), target, deadline)
            if opened is None:
                ready = self.available()
        if not ready.ok:
            return ready
        log_send_phase('guia_probe', target, len(body), channel='guia')
        mark_send_phase('guia_probe')
        gui = self._gui
        submitted = False
        try:
            if not self._foreground(gui):
                cls, pid = foreground_owner(self._win32())
                self._foreground_owner = f'{cls}:{int(pid or 0)}'
                self._foreground_pid = int(pid or 0)
                # 光说"请手动点一下微信窗口"用户无从判断是谁挡的：把前台窗口类名写进红字
                detail = (f'微信无法切到前台（当前前台是 {cls}），请手动点一下微信窗口后再重试'
                          if cls else '无法把微信窗口置前，请手动点一下微信窗口后重试')
                failed = _fail('guia_unavailable', detail)
                failed.diagnostic['foreground'] = self._foreground_owner
                return failed
            # 1) 发送前校验：当前会话必须是目标会话，否则一个字都不输入。
            # 标题条指纹与正文框取自同一帧：核验点一个没少，只是不再为两件事各抓一帧。
            self._phase('conversation')
            probe = self._probe(gui, deadline)
            title = self._verified_title(gui, deadline,
                                         signature=(probe or {}).get('title_signature'))
            self._title = title
            if not self.title_matches(title, target):
                log_send_phase('guia_check_title', target, len(body),
                               reason='target_not_open', channel='guia')
                if title:
                    # 1.3.61：读到的是「另一个会话名」→ 用搜索打开目标会话再看一次。
                    # 读不到标题（空串）时一个键都不按：连在哪个会话都不知道，不能盲搜。
                    if self._open_target_chat(gui, target, deadline) is None:
                        probe = self._probe(gui, deadline)
                        title = self._verified_title(gui, deadline,
                                                     signature=(probe or {}).get('title_signature'))
                        self._title = title
                if not self.title_matches(title, target):
                    return _fail('target_not_open',
                                 f'标题栏读到「{title or "识别失败"}」，请先在微信里手动点开「{target}」')
            log_send_phase('guia_check_title', target, len(body), channel='guia')
            mark_send_phase('guia_check_title')
            # 2) 定位只读、有截止时间；过期结果永远不能接着触发输入。
            geometry = self._geometry(gui)
            self._phase('input_control')
            box = (probe.get('box') if probe is not None
                   else self._read(deadline, gui.get_input_box))
            if not box:
                return _fail('send_failed', '没有定位到聊天输入框，本次未输入正文')
            # 合并取证不合并判定：框越界照样在这里拒绝，和逐个抓帧时一模一样。
            box = self._checked_input_box(gui, box)
            x0, y0, x1, y1 = box
            cx, cy = geometry[0] + (x0 + x1) // 2, geometry[1] + (y0 + y1) // 2
            self._check_deadline(deadline)
            self._point(gui, cx, cy, geometry)
            self._phase('focus')
            self._require_foreground(gui, deadline)
            self._point(gui, cx, cy, geometry)

            def before_focus():
                self._require_foreground(gui, deadline)
                self._point(gui, cx, cy, geometry)
                self._title = self._verified_title(gui, deadline)
                if not self.title_matches(self._title, target):
                    raise _GuardFailed('target_not_open', '聚焦前当前会话发生变化，本次未输入正文')
                self._require_foreground(gui, deadline)
                self._point(gui, cx, cy, geometry)
                self._check_deadline(deadline)

            # 输入框同样不能用会改渲染子窗样式的 wx_click；只有一次受保护的聚焦点击。
            _send_input_click(self._win32(), cx, cy, self._sleep, before_press=before_focus)
            self._require_foreground(gui, deadline, wait=True)
            # 1.3.58：输入框里已有草稿时不再让用户手动清（用户 m10623「之后你自动清」）。
            # 敢在这里按 Ctrl+A + Delete 的前提是上面 before_focus 刚刚核验过完整标题，
            # 删掉的一定是本会话里的残留；清不干净仍由 _clear_existing_draft 拒绝输入。
            if self._read(deadline, lambda: gui._input_box_has_text(box)):
                self._clear_existing_draft(gui, deadline, box)
                self._title = self._verified_title(gui, deadline)
                if not self.title_matches(self._title, target):
                    return _fail('target_not_open',
                                 '清空草稿后当前会话发生变化，本次未输入正文')
                self._require_foreground(gui, deadline)
            gui._last_input_box = box
            # 余量必须在写剪贴板/粘正文**之前**核对：粘完才发现余量不足，正文已经进了
            # 输入框，真机 19:03 那次就是这样把草稿留在框里，之后所有发送被
            # draft_not_empty 永久堵死。余量不足时连剪贴板都不写、一个字都不粘。
            if deadline - self._clock() < CONFIRM_RESERVE:
                return _fail('send_failed',
                             '发送前剩余时间不足，本次未按发送键；本次未输入正文')
            self._phase('input')
            self._paste(gui, body, deadline)
            # 1.3.57：正文是否真的进了框由 _paste 内部同一帧判定（probe 的 has_text 或
            # 退化的 _input_box_has_text），这里不再补抓一帧重复判定；框越界/草稿为空
            # 仍然各自 fail-closed，失败的文案与 reason 一字未改。
            log_send_phase('guia_input', target, len(body), channel='guia')
            mark_send_phase('guia_input')
            self._phase('readback')
            # 3) 输入后复核会话：焦点/会话变了就绝不回车，把草稿留给用户自己处理
            self._require_foreground(gui, deadline, pasted=True)
            self._title = self._verified_title(gui, deadline)
            if not self.title_matches(self._title, target):
                log_send_phase('guia_check_title', target, len(body),
                               reason='target_not_open', channel='guia')
                return _fail('target_not_open',
                             '输入后当前会话发生变化，本次未按发送键；请在微信里清空残留草稿')
            # 4) 粘贴后重做输入框/工具栏像素核验再取发送键区域：正文变多时输入框会变高、
            #    工具栏随之下移，沿用粘贴前的区域（或缓存）会点到错误的位置。
            #    只点一次结构定位的发送键；guia.click_send 内部会多次回车/点击，不能调用。
            box = self._checked_input_box(gui, self._read(deadline, gui.refresh_composer))
            gui._last_input_box = box
            geometry = self._geometry(gui)
            region = tuple(gui.send_button_region)
            rows = self._read(deadline, lambda: gui.ocr(region))
            buttons = [(int(x), int(y), int(w), int(h)) for label, x, y, w, h in rows
                       if str(label or '').strip() in ('发送', '发送(S)', '发送（S）')
                       and w > 0 and h > 0
                       and region[0] <= x and x + w <= region[2]
                       and region[1] - SCAN_TOP_MARGIN <= y and y + h <= region[3]
                       and x >= gui.right_pane_left]
            if len(buttons) != 1:
                return _fail('send_failed', '未唯一定位到发送按钮，本次未按发送键；请核对微信草稿')
            x, y, w, h = buttons[0]
            # 点绿色块自身中心：区域的水平中心在真机上偏左，可能压到圆角外的空白。
            sx, sy = geometry[0] + x + w // 2, geometry[1] + y + h // 2
            self._read(deadline, self._send_gate)
            # 等待间隔内用户可能改写正文，提交前仍必须完整核对。
            self._readback(gui, body, deadline)
            self._title = self._verified_title(gui, deadline)
            if not self.title_matches(self._title, target):
                return _fail('target_not_open', '等待发送期间当前会话发生变化，本次未按发送键；请核对微信草稿')
            self._require_foreground(gui, deadline, pasted=True)
            self._point(gui, sx, sy, geometry)
            self._check_deadline(deadline)
            log_send_phase('guia_click_send', target, len(body), channel='guia')
            mark_send_phase('guia_click_send')
            submitted = True  # 在调用前标记，点击中异常也必须当结果不确定，绝不重发。
            self._submitted = True
            self._phase('send_key')  # 父进程状态文件先落盘，崩溃也不能假报“未执行”。
            # 这里必须是 SendInput 绝对坐标点击：gui.wx_click 走 mouse_event 且会临时
            # 清掉渲染子窗的 WS_EX_TRANSPARENT，真机上点击根本不被微信处理（详见
            # _send_input_click 的注释），草稿会一直留在输入框里。
            def before_press():
                nonlocal submitted
                self._require_foreground(gui, deadline, pasted=True)
                self._point(gui, sx, sy, geometry)
                # phase 回调或悬停期间可能在同一主窗切换会话/改写草稿。
                # 最后一轮完整回读仍不输入正文，任何变化都保留草稿并放弃点击。
                self._readback(gui, body, deadline)
                self._require_foreground(gui, deadline, pasted=True)
                self._point(gui, sx, sy, geometry)
                self._check_deadline(deadline)
                # 按下发送键之前量一次墨迹：确认清空只能跟"按下去之前到底有多少墨迹"比，
                # 只看按键之后那一帧分不清光标和残留标点（位图里光标层几乎一直在）。
                try:
                    self._press_ink = gui.composer_ink(box)
                except Exception:
                    self._press_ink = None    # 量不出来就 fail-closed：不按发送键
                # 下面两条都发生在 SendInput 之前（本函数抛异常就不会有任何点击），所以
                # 如实退回"未按发送键"：报"结果不确定"会让用户以为消息已经发出去了。
                if self._press_ink is None:
                    submitted, self._submitted = False, False
                    raise _GuardFailed('send_failed',
                                       '发送前没能测量输入框墨迹，本次未按发送键；请重试')
                # 抓一帧要几百毫秒；余量不足还按下去，确认窗口会被全局截止时间吃掉，
                # 真机上就是这样把已经发出去的消息判成"未确认清空"的。
                if deadline - self._clock() < CONFIRM_RESERVE:
                    submitted, self._submitted = False, False
                    raise _GuardFailed('send_failed',
                                       '发送前剩余时间不足，本次未按发送键；请重试')

            click_at = self._clock()   # 按键之前的时刻
            _send_input_click(self._win32(), sx, sy, self._sleep, before_press=before_press)
            click_at = self._clock()   # 按键返回后的时刻：确认窗口按最晚的这个时刻起算
            self._phase('confirmation')
            # 1.3.57：首帧观察从 0.5s 提前到 0.2s——判据（墨迹明显变少）一字不改，
            # 没清空时 composer_cleared 仍在窗口内继续抓新帧，超时照样判未清空。
            self._sleep(min(0.2, max(0, deadline - self._clock())))
            confirm_probe = self._confirmation_target(gui, deadline)
            # 发送后输入框可能从多行收缩；以核验到的区域确认清空，不能沿用旧框。
            # 这个框与"会话仍是目标"取自同一帧；取不到框就照旧再抓一帧，绝不沿用旧框。
            if confirm_probe is not None and confirm_probe.get('box'):
                confirmation_box = self._checked_input_box(gui, confirm_probe.get('box'))
            else:
                confirmation_box = self._checked_input_box(gui, self._read(deadline, gui.get_input_box))
            # 光标层几乎一直在 PrintWindow 位图里，'empty' 帧基本等不到；改判"墨迹是否
            # 比按下发送键前明显变少"，光标本身不再算残留草稿。
            # 这一帧的墨迹已经随确认帧量好，当第一次观测用；判据一字不改，它不成立时
            # composer_cleared 照旧在窗口内继续抓新帧循环，仍超时才判未清空。
            first_ink = (confirm_probe or {}).get('ink') or None
            window = min(deadline, click_at + CONFIRM_WINDOW)
            if first_ink is not None:
                confirmed = self._read(window, lambda: gui.composer_cleared(
                    confirmation_box, self._press_ink, first_ink=first_ink))
            else:
                confirmed = self._read(window, lambda: gui.composer_cleared(
                    confirmation_box, self._press_ink))
            if not confirmed:
                log_send_phase('guia_verify', target, len(body),
                               reason='send_failed', channel='guia')
                mark_send_phase('guia_verify')
                return _fail('send_failed',
                             '已尝试发送但输入框未确认清空，结果不确定，本次不重发；请核对聊天记录',
                             uncertain=True)
            # 按键之后不再复核标题：前面已经两次核验过会话名，这里再 OCR 一次只会把已经
            # 发出去的发送重新翻成"结果不确定"（标题 OCR 一抖就翻转结论），而不多拦住任何
            # 东西——要拦的窗口/会话变化在按下发送键之前就已经拦过了。
            log_send_phase('guia_verify', target, len(body), channel='guia')
            mark_send_phase('guia_verify')
            return _ok(f'已通过「坐标+OCR」通道发送到「{target}」')
        except _GuardFailed as exc:
            if submitted:
                return _fail('send_failed', '发送后窗口状态变化，结果不确定，本次不重发', uncertain=True)
            return _fail(exc.code, exc.detail)
        except TimeoutError:
            return _fail('send_failed',
                         ('超过发送时限，结果不确定，本次不重发' if submitted else
                          '超过发送时限，本次未按发送键；请核对微信草稿' if self._pasted else
                          '超过发送时限，本次未输入正文'), uncertain=submitted)
        except Exception as exc:
            # 点了发送键之后炸掉 = 结果不确定：留下堆栈，但绝不重发。
            # 这行本身就是可提取的失败记录，所以不再让 send() 补第二条 ERROR。
            self._exc_logged = True
            record_exception(exc, message=(
                'send failed channel=guia reason=send_failed who='
                f'{mask_chat_name(target)} len={len(body)} '
                f'point_pid={self._point_pid} foreground_pid={self._foreground_pid} '
                'hint=通道内部异常，本次不重发'))
            return _fail('send_failed',
                         (f'{_exc_label(exc)}；结果不确定，本次不重发；请核对聊天记录' if submitted else
                          f'{_exc_label(exc)}；本次未按发送键，请核对微信草稿'),
                         uncertain=submitted)
        finally:
            # 正文已经粘进输入框、发送键又没按下去时，必须把它清回去：否则下一次发送的
            # 就绪探测永远读到 draft_not_empty（真机 19:03 之后所有发送被永久堵死）。
            try:
                self._recover_draft(gui)
            except Exception:
                pass
            try:
                gui.restore_zorder()
            except Exception:
                pass


def _channel(gui_factory=None, account_path='', phase=None, timeout=DEFAULT_TIMEOUT):
    if gui_factory is not None:
        return GuiaChannel(gui_factory=gui_factory, timeout=timeout, phase=phase)
    from .wechat_window import NativeWindowAccess, select_window
    access = NativeWindowAccess()
    row, info = select_window(access, account_path)
    if not account_path or row is None or info.get('matched_count') != 1 or info.get('fallback'):
        raise RuntimeError('未唯一核验账号与微信窗口，禁止切换到坐标+OCR通道')
    return GuiaChannel(account_path=account_path, expected_window=row,
                       window_access=access, phase=phase, timeout=timeout)


def probe(gui_factory: Optional[Callable] = None, account_path='', who='') -> GuiaSendResult:
    """不改动微信的可用性探测，供发送环境检查（window-check）复用。"""
    try:
        channel = _channel(gui_factory, account_path)
        result = channel.available()
        if result.ok and str(who or '').strip():
            title = channel.current_chat()
            if not title:
                failed = _fail('title_unverified', '未能核验完整标题；本次只读检查未输入、未发送')
                failed.diagnostic['title_read'] = channel.title_diagnostic()
                return failed
            if not channel.title_matches(title, who):
                return _fail('chat_not_open', '完整标题与目标不符；不会自动切换')
        if result.ok:
            # 窗口和标题可读不等于能接收新正文。体检提前报告占用，避免
            # 界面显示就绪、模型生成回复后才发现用户草稿；此处绝不点击或清理。
            gui = channel._build()
            box = gui.get_input_box()
            if not box:
                return _fail('guia_unavailable', '未确认当前输入框，本次检查未操作微信')
            if gui._input_box_has_text(box):
                if not auto_clear_draft_enabled():
                    return _fail('draft_not_empty', '请先核对并手动处理草稿；本次检查未输入、未发送')
                # 自动清空模式下草稿不是故障：真正发送前会先 Ctrl+A + Delete 清掉再复核。
                # 体检本身仍然只读——一个字都不输入、不点击、不碰剪贴板。
                result.diagnostic['draft_present'] = True
                result.diagnostic['draft_note'] = '输入框里有草稿，发送前会自动清空；本次检查未操作微信'
        return result
    except Exception as exc:
        return _fail('guia_unavailable', f'{type(exc).__name__}: {exc}')


def send_text_via_guia(who: str, text: str, timeout: Optional[float] = None,
                       gui_factory: Optional[Callable] = None, account_path='', phase=None) -> GuiaSendResult:
    """发送入口：只发到当前已打开的会话，失败返回结构化原因码 + 中文原因。"""
    try:
        channel = _channel(gui_factory, account_path, phase,
                           DEFAULT_TIMEOUT if timeout is None else timeout)
        return channel.send(who, text)
    except Exception as exc:
        record_exception(exc, message=(
            'send failed channel=guia reason=guia_unavailable who='
            f'{mask_chat_name(who)} len={len(str(text or ""))} hint=通道启动失败，本次未发送'))
        return _fail('guia_unavailable', f'{type(exc).__name__}: {exc}')
