"""隔离微信原生自动化：文本优先走 UIA 控件，控件缺失时才降级「坐标+OCR」。

为什么要有降级：微信 4.1.x 恢复窗口后只暴露 Qt51514QWindowIcon /
MMUIRenderSubWindowHW 外壳，一个 mmui:: 控件都没有，UIA 路径必然失败。
「坐标+OCR」是用户 2026-10-26 批准的唯一例外通道，并且只在**还没按下回车**
之前才允许切换，避免同一条回复被发两次。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import deque
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.logging_setup import (mask_chat_name, log_send_failure, log_send_phase,
                               record_exception, setup_logging)


# 内部 stage（wechat_sender.PHASE_NAMES 那套中文阶段）→ 统一阶段码。连续重复的码
# 只写一行，所以一次 UIA 发送落盘正好是 locate_window→check_chat→input_text→
# press_send→verify_watermark 五行，绝不逐字符刷日志。
SEND_STAGE_MAP = {
    'window': 'locate_window',
    'open_chat': 'check_chat',
    'conversation': 'check_chat',
    'input_control': 'input_text',
    'focus': 'input_text',
    'input': 'input_text',
    'readback': 'input_text',
    'send_key': 'press_send',
    'confirmation': 'verify_watermark',
}
# 失败原因码 -> 运行记录里那行可读的处理建议
SEND_FAILURE_HINT = {
    'send_uncertain': '已按过发送键，结果待核实；本次不重发，请核对聊天记录',
    'target_not_open': '当前会话不是目标会话，请先手动点开（程序不会替你切会话）',
    'input_failed': '正文没有完整写入输入框，本次未按发送键',
    'input_unavailable': '没有可用的聊天输入框，请确认微信未最小化且客户端版本兼容',
    'window_not_found': '未能定位微信窗口，请确认微信已登录且未最小化',
    'send_failed': '本次未发送成功，请核对聊天记录后再决定是否重发',
}


class SendTrace:
    """一次发送的阶段日志：把内部 stage 翻成统一阶段码写进 logs/backend.log。

    只负责记录，不参与任何判断；写日志抛异常也一律吞掉，绝不让日志连累发送结果。
    """

    def __init__(self, who, text, channel='uia'):
        self.who = who
        self.length = len(str(text or ''))
        self.channel = channel
        self.last = ''

    def __call__(self, stage):
        try:
            code = SEND_STAGE_MAP.get(stage)
            if not code or code == self.last:
                return
            self.last = code
            log_send_phase(code, self.who, self.length, channel=self.channel)
        except BaseException:
            pass


def uia_failure_reason(stages) -> str:
    """按走过的阶段反推原因码，让 UIA 与「坐标+OCR」两条通道用同一套词。"""
    reached = set(stages)
    if 'send_key' in reached:
        return 'send_uncertain'   # 按过回车：结果不确定，绝不换道重发
    if 'input_control' in reached:
        return 'input_failed'
    if 'conversation' in reached:
        return 'target_not_open'
    if 'window' in reached:
        return 'input_unavailable'
    return 'window_not_found'


def valid_chat_name(who):
    name = str(who or '').strip()
    return bool(name and not name.endswith('@chatroom') and not name.startswith(
        ('wxid_', 'wechat|', 'group:', 'private:')) and not re.search(r'[\r\n\x00]', name))


def find_scoped(root, predicate, max_depth=40, timeout=2.0, diagnostic=None):
    """只遍历微信窗口，避免多个 EditControl.Exists 超时及全桌面搜索。"""
    pending = deque([(root, 0)])
    seen = 0
    deadline = time.monotonic() + timeout
    classes = set()
    errors = 0
    depth_limited = False
    def finish(status):
        if diagnostic is not None:
            diagnostic.update(visited=seen, search_status=status, classes=sorted(classes), errors=errors)
    while pending and seen < 2000 and time.monotonic() < deadline:
        control, depth = pending.popleft()
        seen += 1
        try:
            classes.add(str(control.ClassName or ''))
            if predicate(control):
                finish('found')
                return control
            if depth < max_depth:
                # 浅层输入框优先，避免历史消息分支先耗尽节点预算。
                children = control.GetChildren()
                available = max(0, 2000 - seen - len(pending))
                pending.extend((child, depth + 1) for child in children[:available])
                depth_limited |= len(children) > available
            else:
                depth_limited = True
        except Exception:
            errors += 1
            continue
    finish('budget' if pending or depth_limited else 'exhausted')
    return None


def optimize_lookups(uia):
    # 微信新版把搜索框收成按钮；逐个查旧类名会先白等数秒才去展开它。
    uia._search_box_present = lambda win: find_scoped(
        win, lambda c: c.ControlTypeName == "EditControl" and (c.Name or "").strip() == "搜索")
    uia._chat_input = lambda win=None: find_scoped(
        win if win is not None else uia._win,
        lambda c: c.ClassName == "mmui::ChatInputField" or
        str(c.AutomationId or "").split(".")[-1] == "chat_input_field")


def restrict_driver(uia, hot_activate=False):
    """禁止依赖库修改微信进程或自动操作登录。

    保留旧hot_activate形参兼容，但任何值都不能放行进程写入。
    """
    original = uia.ensure_window
    uia.ensure_window = lambda wake=False, timeout=None: original(wake=False, timeout=timeout)
    uia.ensure_materialized = lambda *args, **kwargs: False
    uia._wake_accessibility = lambda *args, **kwargs: False
    uia._hot_activate_accessibility = lambda *args, **kwargs: False
    uia._auto_login = lambda *args, **kwargs: False
    return {'wake':None, 'hot':None}


def enable_send_compat(uia, entrance) -> bool:
    """旧入口仅保留调用兼容；不保存、不恢复、更不调用进程写入函数。"""
    return False


def literal_keys(text: str) -> str:
    # 模型输出里的 {Ctrl} 必须作为文字，换行必须是 Shift+Enter 而非发送。
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join("{{}" if c == "{" else "{}}" if c == "}" else
                   "{Shift}{Enter}" if c == "\n" else c for c in text)


def _foreground_window() -> int:
    """当前前台窗口句柄；拿不到就是 0。"""
    try:
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _restore_foreground(hwnd: int) -> None:
    """把前台还给发送前的窗口 —— 用户要求"不抢鼠标"，这是最低成本的兑现方式。

    打开会话/输入必然要激活一次微信；发完立刻把焦点还回去，用户就不会发现自己
    的窗口被顶掉。只在原窗口仍然存在且不是微信自己时才还原。
    """
    if not hwnd:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return
        if user32.GetForegroundWindow() == hwnd:
            return
        # Windows 只允许"前台进程"抢焦点：子进程直接 SetForegroundWindow 会被忽略。
        # 先用 AttachThreadInput 借用前台线程的输入队列，再设置即可生效。
        current = ctypes.windll.kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        attached = False
        try:
            if target_thread:
                attached = bool(user32.AttachThreadInput(current, target_thread, True))
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current, target_thread, False)
    except Exception:
        pass


def open_target_chat(uia, who, phase):
    """按名字搜索并打开目标会话；打开后**再校验一次**，不对就不往下走。

    为什么需要它：发送链要求"当前会话就是目标会话"，否则拒绝输入（防止把回复
    打进别人的聊天窗口）。用户明确要求像最初的程序那样"自动搜索、打开会话再发"，
    所以这里用库自带的 `open_chat()`（搜索框输入 → 收结果 → 优先精确匹配 → 点击 →
    校验标题），仍然保留"名字对不上就不输入"这条底线。
    """
    phase('open_chat')
    try:
        opened = uia.open_chat(who)
    except Exception as exc:
        return False, f'自动打开会话「{who}」失败：{type(exc).__name__}: {exc}；本次未输入正文'
    if not opened:
        # 兜底：打不开就沿用**当前已经打开的**会话。微信 4.1.15.56 起搜索入口的控件变了，
        # 自动打开会失败；只要用户手动点开的就是目标会话，就不该整条回复报废。
        # 仍然保留"名字对不上就不输入"的底线。
        try:
            current = uia.current_chat()
        except Exception:
            current = None
        if current and current == who:
            return True, ''
        return False, (f'没能自动打开会话「{who}」（当前会话是「{current or "未知"}」，'
                       '请手动点开目标会话或确认它存在）；本次未输入正文')
    try:
        current = uia.current_chat()
    except Exception:
        current = None
    if current != who:
        return False, f'自动打开后会话是「{current}」，不是目标「{who}」；本次未输入正文'
    return True, ''


def send_text(uia, who: str, text: str, phase=None, open_chat=False):
    phase = phase or (lambda stage: None)
    if not valid_chat_name(who):
        return False, '目标仍是内部会话标识，未操作微信；请先解析群名或联系人备注'
    phase('window')
    ready = uia.ensure_window()
    if not ready and open_chat:
        # 微信没有打开任何聊天时，界面上根本没有输入框 —— 这时先按名字把目标会话打开，
        # 再重新定位窗口/输入框。以前是先要求"输入框存在"，导致这种情况永远打不开也发不出。
        opened, failure = open_target_chat(uia, who, phase)
        if not opened:
            return False, failure
        phase('window')
        ready = uia.ensure_window()
    if not ready:
        reason = getattr(uia, '_window_error', '')
        return False, reason if isinstance(reason, str) and reason else "当前微信未提供可用输入控件，本次未发送；不会修改微信进程或自动登录"
    phase('conversation')
    if uia.current_chat() != who:
        if not open_chat:
            return False, "当前不是目标会话，请手动打开对应聊天；不会自动搜索或切换会话，本次未输入正文"
        ok, detail = open_target_chat(uia, who, phase)
        if not ok:
            return False, detail
    phase('input_control')
    control = uia._chat_input()
    if control is None:
        diagnostic = getattr(uia, '_window_diagnostic', None)
        if isinstance(diagnostic, dict):
            diagnostic.update(ready=False, input_available=False, reason='找到了会话，但没有可用的聊天输入框')
        return False, "找到了会话，但没有可用的聊天输入框"
    phase('focus')
    control.SetFocus()
    if not control.HasKeyboardFocus:
        return False, "聊天输入框未获得键盘焦点，未输入正文"
    before = uia._read_edit_value(control)
    if before is None or before != '':
        return False, '输入框已有草稿或无法回读，保留草稿，本次未输入正文'
    phase('input')
    value_written = False
    value_attempted = False
    try:
        pattern = control.GetValuePattern()
        if pattern is not None and not pattern.IsReadOnly:
            # Qt 输入法会把混合中英文的 SendInput 当候选选择；直接写值可避免丢字。
            if uia.current_chat() != who or uia._read_edit_value(control) != '':
                return False, '写入前会话或草稿已变化，本次未输入正文'
            value_attempted = True
            pattern.SetValue(text)
            control.SetFocus()
            value_written = True
    except Exception:
        if value_attempted:
            return False, '正文写入异常，可能保留部分草稿，本次未按发送键且不更换输入方式'
    if not value_written:
        if uia.current_chat() != who or uia._read_edit_value(control) != '':
            return False, '输入前会话或草稿已变化，本次未输入正文'
        control.SendKeys(literal_keys(text), interval=0, waitTime=0.15, charMode=True)
    phase('readback')
    got = uia._read_edit_value(control)
    normalize = lambda value: str(value).replace('\r\n', '\n')
    if value_written and got is not None and normalize(got) != normalize(text):
        # SetValue 通常同步完成；只有回读尚未追上时才等，避免每条固定白等。
        deadline = time.monotonic()+0.15
        while time.monotonic()<deadline and normalize(got) != normalize(text):
            time.sleep(0.01)
            try:
                got = uia._read_edit_value(control)
            except Exception:
                break
            if got is None:
                break
    if got is None:
        return False, "输入框不提供正文回读，无法安全确认输入，本次未按发送键"
    if normalize(got) != normalize(text):
        return False, "正文没有完整写入聊天输入框，本次未按发送键"
    if uia.current_chat() != who or not control.HasKeyboardFocus:
        return False, "输入后会话或焦点发生变化，本次未按发送键"
    phase('send_key')
    control.SendKeys("{Enter}", waitTime=0.1)
    phase('confirmation')
    remaining = uia._read_edit_value(control)
    if remaining is not None and remaining.strip():
        deadline = time.monotonic()+0.2
        while time.monotonic()<deadline and remaining.strip():
            time.sleep(0.01)
            try:
                remaining = uia._read_edit_value(control)
            except Exception:
                break
            if remaining is None:
                break
    if remaining is None or remaining.strip():
        return False, "已按发送键但输入框未确认清空，结果待核实；请检查微信发送快捷键"
    return True, "正文已写入，发送键执行后输入框已清空"


def send_picture(uia, who, clipboard_ready=lambda: True, phase=None, open_chat=False):
    phase = phase or (lambda stage: None)
    if not valid_chat_name(who):
        return False, '图片目标仍是内部会话标识，未操作微信'
    phase('clipboard')
    if not clipboard_ready():
        return False, '剪贴板已被用户更改，本次未粘贴图片'
    phase('window')
    ready = uia.ensure_window()
    if not ready and open_chat:
        # 同上：没开会话就没有输入框，先打开目标会话再重试
        opened, failure = open_target_chat(uia, who, phase)
        if not opened:
            return False, failure
        phase('window')
        ready = uia.ensure_window()
    if not ready:
        reason = getattr(uia, '_window_error', '')
        return False, reason if isinstance(reason, str) and reason else '定位微信主窗口失败，本轮未发送图片'
    phase('conversation')
    if uia.current_chat() != who:
        if not open_chat:
            return False, '当前不是图片目标会话，请手动打开对应聊天；不会自动切换，本次未发送图片'
        ok, detail = open_target_chat(uia, who, phase)
        if not ok:
            return False, detail
    phase('input_control')
    control = uia._chat_input()
    if control is None:
        diagnostic = getattr(uia, '_window_diagnostic', None)
        if isinstance(diagnostic, dict):
            diagnostic.update(ready=False, input_available=False, reason='没有可用输入框，未发送图片')
        return False, '没有可用输入框，未发送图片'
    phase('focus')
    control.SetFocus()
    before = uia._read_edit_value(control)
    if not control.HasKeyboardFocus or before is None or before.strip() or not clipboard_ready():
        return False, '输入框有草稿或无法确认焦点，未发送图片'
    phase('input')
    control.SendKeys('{Ctrl}v', waitTime=0.1)
    deadline = time.monotonic() + 1.5
    while True:
        draft = uia._read_edit_value(control)
        if draft and '\ufffc' in draft:
            break
        if time.monotonic() >= deadline:
            return False, '控件未确认图片草稿，本次不按发送键、不重新粘贴'
        time.sleep(0.05)
    if uia.current_chat() != who or not control.HasKeyboardFocus:
        return False, '粘贴后会话或焦点改变，未发送图片'
    phase('send_key')
    control.SendKeys('{Enter}', waitTime=0.1)
    phase('confirmation')
    remaining = uia._read_edit_value(control)
    if remaining is None or remaining.strip():
        return False, '已按图片发送键，结果待核实，本次不重发'
    return True, '图片草稿已确认，发送后输入框已清空'


def guia_probe(account_path='', who=''):
    """探测「坐标+OCR」通道是否可用；只做只读检查，不改动微信。"""
    from src.wechat_guia_send import probe
    return probe(account_path=account_path, who=who)


def send_text_via_guia(who: str, text: str, account_path='', phase=None, auto_open=None):
    """降级通道发送入口（只发当前已打开的会话，由 src/wechat_guia_send 校验标题栏）。"""
    # auto_open 是设置页「自动打开会话」这一项：本进程按次新起，改环境变量只影响这次发送，
    # 用户改完设置下一次就生效，不需要重连微信。
    from src.wechat_guia_send import AUTO_OPEN_CHAT_ENV, send_text_via_guia as send
    if auto_open is not None:
        os.environ[AUTO_OPEN_CHAT_ENV] = '1' if auto_open else '0'
    return send(who, text, account_path=account_path, phase=phase)


def _hot_activate(job: dict) -> dict:
    """旧任务也不得重新启用写进程通道。"""
    return {'ok':False,'detail':'进程热激活已移除；缺少客户端控件时等待'}


def execute(job: dict, phase=None):
    phase = phase or (lambda stage: None)
    if job["action"] == "self-test":
        from src.native_dependencies import check_native_dependencies
        check_native_dependencies()
        assert literal_keys("{Ctrl}\n你好") == "{{}Ctrl{}}{Shift}{Enter}你好"
        from types import SimpleNamespace
        calls = []
        driver = SimpleNamespace(ensure_window=lambda **kw: calls.append(kw) or True)
        restrict_driver(driver)
        assert driver.ensure_window() and calls == [{'wake': False, 'timeout': None}]
        assert not driver._auto_login() and not driver._hot_activate_accessibility(1)
        from src.wechat_window import prepare_window, select_window
        root = SimpleNamespace(ClassName='mmui::MainWindow')
        fixture_window = {'hwnd': 10, 'minimized': True}
        def fixture_restore(hwnd):
            fixture_window['minimized'] = False
            calls.append('restore')
            return True
        adapter = SimpleNamespace(candidates=lambda: [dict(fixture_window)],
                                  restore=fixture_restore,
                                  minimized=lambda hwnd: False, control=lambda hwnd: root)
        driver._activate = lambda win: calls.append('activate')
        assert prepare_window(driver, adapter, lambda: (True, ''))
        assert calls[-2:] == ['restore', 'activate'] and driver._win is root
        adapter.candidates = lambda: [{'hwnd': 10, 'pid': 1, 'minimized': False},
                                      {'hwnd': 20, 'pid': 2, 'minimized': False}]
        adapter.matches_account = lambda pid, path: pid == 2 and path == 'bound-account'
        selected, diagnostic = select_window(adapter, 'bound-account')
        assert selected['hwnd'] == 20 and diagnostic['matched_count'] == 1
        assert select_window(adapter, 'different-account')[0] is None
        return True, "微信发送子进程离线自检通过"
    job.pop('hot_activate',None)
    if job.get('background_send'):
        # 严格后台在加载任何会激活窗口的驱动之前分流，不继承兼容模式的隐式操作。
        from src.wechat_background import inspect_background, send_background
        if job['action'] == 'window-check':
            phase('window')
            info, _ = inspect_background(job.get('account_path', ''), job.get('who', ''),
                                         submit=job.get('background_submit', 'invoke'))
            job['_diagnostic'] = info
            return info['ready'], info['reason']
        if job['action'] != 'text':
            return False, '严格后台模式仅支持文字，本次未发送图片'
        return send_background(job, phase)
    if job['action'] == 'window-check':
        phase('window')
        gate = None
        if job.get('send_compat'):
            # 能力检查不加载写进程驱动，更不能为了“检查”修改微信。
            gate = {'send_compat': True, 'materialized': False, 'process_write_blocked':True}
        from src.wechat_window import inspect_window
        diagnostic = inspect_window(job.get('account_path', ''),
                                    send_compat=bool(job.get('send_compat')),
                                    auto_open_chat=False, allow_restore=False)
        if gate is not None:
            diagnostic['gate'] = gate
        if job.get('guia_send') and not diagnostic['ready'] and diagnostic.get('waiting_code') != 'minimized':
            # 控件树缺失是这个客户端（4.1.x）的常态而不是故障：只要「坐标+OCR」
            # 通道可用就放行，否则发送卡会永远停在"等待微信输入控件"。
            channel = guia_probe(account_path=job.get('account_path', ''), who=job.get('who', ''))
            diagnostic['guia'] = channel.diagnostic
            if channel.ok:
                diagnostic.update(ready=True, guia_channel=True, waiting_code='',
                                  reason='微信只暴露窗口外壳；已改用「坐标+OCR」通道发送（只发当前已打开的会话）')
            else:
                # 上层用当前可用通道的原因码判断会话是否不符；不能仍沿用
                # UIA 缺控件的旧码，否则其他会话会占住队首直到过期。
                diagnostic['native_waiting_code'] = diagnostic.get('waiting_code', '')
                diagnostic.update(ready=False, guia_channel=True,
                    waiting_code=str(channel.reason_code or channel.diagnostic.get('waiting_code')
                                     or diagnostic.get('waiting_code') or 'guia_unavailable'))
                diagnostic['reason'] = f"{diagnostic.get('reason') or ''}｜{channel.reason}"
        job['_diagnostic'] = diagnostic
        return bool(diagnostic['ready']), diagnostic['reason']
    if job['action'] != 'text' and not (job.get('as_image') and len(job.get('paths', [])) == 1):
        return False, '精简版只支持文字和每轮一张表情图片'
    if not valid_chat_name(job.get('who')):
        return False, '目标仍是内部会话标识，未操作微信；请核对会话名称映射'
    from src.interactive_desktop import interactive_desktop_status
    phase('desktop')
    ready, reason = interactive_desktop_status()
    if not ready:
        return False, reason
    phase('dependencies')
    from wechatauto.uia_driver import WeChatUIA
    uia = WeChatUIA(timeout=8, search_timeout=0.5)
    entrance = restrict_driver(uia)
    if job.get('send_compat'):
        # 旧兼容标记只选择控件发送路径，已永久移除写进程唤醒。
        woke = enable_send_compat(uia, entrance)
        job['_gate'] = {'send_compat': True, 'materialized': woke}
        phase('accessibility')
        if job['action'] == 'text' and not job.get('foreground_only'):
            # 树已物化，先试**不抢鼠标**的路径：UIA 写值 + Invoke 发送按钮，
            # 不 SetFocus、不 SendKeys、不激活窗口。用户明确要求"不抢鼠标"，
            # 一旦尝试写入就不能转前台：失败也可能已提交，回退会抢焦点甚至重复发送。
            try:
                from src.wechat_background import inspect_background, send_background
                info, controls = inspect_background(job.get('account_path', ''), job['who'])
                if controls is not None:
                    job['_quiet'] = True
                    job['_diagnostic'] = info
                    return send_background(job, phase)
            except Exception as exc:
                # 驱动异常不能证明没有写入/提交，保留草稿并结束这一次任务。
                job['_quiet_error'] = type(exc).__name__
                return False, ('后台发送检查或提交异常（' + type(exc).__name__ +
                               '），请核对草稿与聊天记录；本次不回退前台、不重发')
    else:
        job['_gate'] = {'send_compat': False, 'materialized': False}
    optimize_lookups(uia)
    from src.wechat_chat_name import current_chat
    uia.current_chat = lambda: current_chat(uia, str(job.get("who") or ""))
    from src.wechat_window import install_window_discovery
    install_window_discovery(uia, job.get('account_path', ''))
    # 账号安全反馈后停用自动搜索/切会话；旧配置不能重新放行。
    auto_open = False
    previous_foreground = _foreground_window()
    if job['action'] == 'text':
        stages = []
        trace = SendTrace(job.get('who'), job.get('text'))
        length = len(str(job.get('text') or ''))
        def track(stage):
            stages.append(stage)
            phase(stage)
            trace(stage)
        try:
            result = send_text(uia, job["who"], job["text"], track, open_chat=auto_open)
        except Exception as exc:
            # 控件树中途消失、驱动报错等也归入"UIA 通道不可用"，交给降级通道兜住。
            result = (False, f'{type(exc).__name__}: {exc}')
            # Qt 关掉了槽里的异常，不自己记就只剩一句失败原因，堆栈无从查起
            record_exception(exc, message=(
                'send failed channel=uia reason=send_failed who='
                f'{mask_chat_name(job.get("who"))} len={length} hint=UIA 通道中断，完整堆栈见下'))
        window_diagnostic = getattr(uia, '_window_diagnostic', None)
        job['_diagnostic'] = dict(window_diagnostic) if isinstance(window_diagnostic, dict) else {}
        # 只有**没按过发送键**才允许换通道：按过回车再试 guia 会把同一条发两次。
        if (not result[0] and job.get('guia_send')
                and not any(stage in stages for stage in ('focus', 'input', 'readback', 'send_key'))):
            reason = uia_failure_reason(stages)
            log_send_phase('fallback_switch', job['who'], length, reason=reason, channel='guia')
            fallback = send_text_via_guia(job['who'], job['text'],
                                          account_path=job.get('account_path', ''), phase=phase,
                                          auto_open=job.get('auto_open_chat'))
            diagnostic = dict(fallback.diagnostic, uia_channel=result[1])
            if not fallback.ok:
                job['_send_uncertain'] = bool(diagnostic.get('send_uncertain'))
                job['_send_failure_logged'] = True   # guia 通道已写下带原因码的 ERROR
            job['_diagnostic'] = diagnostic
            _restore_foreground(previous_foreground)
            if fallback.ok:
                return True, fallback.reason + '（UIA 控件通道不可用，已切换坐标+OCR：' + result[1] + '）'
            return False, fallback.reason
        if not result[0]:
            # 走到这里就是最终结果（不换道或按过回车）：运行记录里必须留下带阶段码的红字
            reason = uia_failure_reason(stages)
            log_send_failure(reason, job.get('who'), length, channel='uia',
                             hint=SEND_FAILURE_HINT.get(reason, ''))
            job['_send_failure_logged'] = True
        job['_diagnostic'].update(submission_state=('confirmed' if result[0] else 'uncertain')
                                  if 'send_key' in stages else 'not_submitted',
                                  send_key='send_key' in stages, delivery_verified=False,
                                  input_touched='input' in stages)
        _restore_foreground(previous_foreground)
        return result
    import ctypes
    sequence = job.get('clipboard_sequence')
    result = send_picture(uia, job['who'], lambda: isinstance(sequence, int) and
                          ctypes.windll.user32.GetClipboardSequenceNumber() == sequence, phase,
                          open_chat=auto_open)
    job['_diagnostic'] = getattr(uia, '_window_diagnostic', {})
    _restore_foreground(previous_foreground)
    return result


def worker_log_folder(job, request):
    """子进程写哪份 backend.log：父进程在 job['log_dir'] 里指定，老任务按目录约定回退。"""
    folder = job.get('log_dir') if isinstance(job, dict) else None
    return Path(folder) if folder else Path(request).parent.parent / 'logs'


def main(request_path: str) -> int:
    request = Path(request_path)
    output = request.with_suffix(".result.json")
    status = request.with_suffix('.status.json')
    started = time.monotonic()
    state = {'stage': 'starting', 'send_key': False, 'pid': os.getpid(),
             'submission_state': 'not_submitted'}

    def phase(stage):
        if stage in ('focus', 'input', 'send_key'):
            from src.interactive_desktop import interactive_desktop_status
            ready, reason = interactive_desktop_status()
            if not ready:
                raise RuntimeError(reason)
        state.update(stage=stage, elapsed_ms=round((time.monotonic() - started) * 1000))
        if stage == 'send_key':
            state['send_key'] = True
            state['submission_state'] = 'uncertain'
        if stage == 'input':
            state['input_touched'] = True
        temporary = status.with_suffix('.tmp')
        temporary.write_text(json.dumps(state), encoding='utf-8')
        temporary.replace(status)

    stack_watchdog = None
    job = {}
    try:
        from src.send_worker_diagnostics import start_stack_watchdog
        logs = request.parent.parent / 'logs'
        stack_watchdog = start_stack_watchdog(logs)
        phase('request')
        job = json.loads(request.read_text(encoding="utf-8"))
        # 父子进程写同一个 logs/backend.log：两个进程都配同一个目录才不会各写一份，
        # 行尾的 pid 用来分辨这条是父进程还是发送子进程留下的。
        setup_logging(worker_log_folder(job, request))
        ok, detail = execute(job, phase)
        if not ok and job.get('action') == 'window-check':
            logging.getLogger(__name__).info('wechat capability check ready=false code=%s',
                str((job.get('_diagnostic') or {}).get('waiting_code') or 'not_ready'))
        elif not ok and not job.get('_send_failure_logged'):
            # 兜底：没走到上面那些分支的失败（缺控件、桌面锁定、名称不合法……）
            # 也必须在运行记录里留一行 ERROR，否则用户只看到卡片红字、日志里空无一物
            reason = str((job.get('_diagnostic') or {}).get('waiting_code') or 'send_failed')
            log_send_failure(reason, job.get('who'), len(str(job.get('text') or '')),
                             channel='uia',
                             hint=SEND_FAILURE_HINT.get(reason, '本次未发送成功，请对照上方阶段日志'))
    except Exception as exc:
        inspection = job.get('action') == 'window-check'
        ok, detail = False, f"微信{'能力检查' if inspection else '发送步骤'}异常：{type(exc).__name__}: {exc}"
        if inspection:
            logging.getLogger(__name__).warning('wechat capability check failed error=%s', type(exc).__name__)
        else:
            record_exception(exc, message=(
                'send failed channel=uia reason=send_failed who='
                f'{mask_chat_name(job.get("who"))} len={len(str(job.get("text") or ""))} '
                'hint=发送子进程异常中断，完整堆栈见下'))
        from src.native_dependencies import dependency_error_hint
        hint = dependency_error_hint(exc)
        if hint:
            detail += '；' + hint
    finally:
        if stack_watchdog is not None:
            stack_watchdog.cancel()
    diagnostic = dict(job.get('_diagnostic') or {})
    state['send_key'] = bool(state['send_key'] or diagnostic.get('send_key') or job.get('_send_uncertain'))
    state['submission_state'] = diagnostic.get('submission_state') or (
        ('confirmed' if ok else 'uncertain') if state['send_key'] else 'not_submitted')
    diagnostic.update(submission_state=state['submission_state'], send_key=state['send_key'],
                      delivery_verified=False)
    if not ok:
        diagnostic.update(ready=False, reason=detail)
    output.write_text(json.dumps({"ok": ok, "message": detail, **state,
                                  'requires_reconnect': bool(state['send_key'] or job.get('_send_uncertain')),
                                  'diagnostic': diagnostic}, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
