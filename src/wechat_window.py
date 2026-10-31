"""按句柄发现微信主窗；不读取进程模块/内存、不点击面板。"""
import re
import time
import math
from pathlib import Path

_MAIN_TITLE = re.compile(r'(?:微信|Weixin|WeChat)(?:\s*[（(]\d+[）)])?', re.IGNORECASE)
_OUTER_CLASS = re.compile(r'Qt\d+QWindowIcon')
_MAIN_WINDOW_BUTTONS = 0x30000  # WS_MINIMIZEBOX | WS_MAXIMIZEBOX


def _discovery_error(error):
    # 原生错误正文可能包含用户路径；只保留固定类别。
    name = type(error).__name__
    return name if name in ('PermissionError', 'AccessDenied', 'NoSuchProcess',
        'ProcessLookupError', 'OSError', 'ValueError', 'error') else 'OtherError'


class NativeWindowAccess:
    def __init__(self):
        import win32gui
        import win32process
        import psutil
        import uiautomation
        self.gui, self.process, self.psutil, self.auto = win32gui, win32process, psutil, uiautomation
        self.auto.InitializeUIAutomationInCurrentThread()

    def _main_window_role(self, hwnd, native_class):
        if native_class == 'mmui::MainWindow':
            return True
        if not _OUTER_CLASS.fullmatch(native_class):
            return False
        # 实测新版说明窗与聊天主窗同标题、同进程且无 owner；Qt 主窗的
        # 最小化/最大化样式才能区分这类说明窗，不能按面积或前台顺序猜。
        style = self.gui.GetWindowLong(hwnd, -16)  # GWL_STYLE
        if type(style) is not int:
            raise ValueError('window role unavailable')
        return bool(style & _MAIN_WINDOW_BUTTONS)

    def candidates(self):
        rows, processes = [], {}
        diagnostic = dict(enumeration_complete=False, scanned_windows=0,
            owned_windows=0, tool_windows=0, auxiliary_windows=0, query_errors=0, other_query_errors=0,
            enumeration_errors=0, error_types=[])
        self.discovery_diagnostic = diagnostic
        def failure(error, candidate=False):
            key = 'query_errors' if candidate else 'other_query_errors'
            diagnostic[key] += 1
            category = _discovery_error(error)
            if category not in diagnostic['error_types']:
                diagnostic['error_types'].append(category)
        def collect(hwnd, _):
            candidate = False
            diagnostic['scanned_windows'] += 1
            try:
                minimized = bool(self.gui.IsIconic(hwnd))
                if not self.gui.IsWindowVisible(hwnd) and not minimized:
                    return True
                title = self.gui.GetWindowText(hwnd).strip()
                if not _MAIN_TITLE.fullmatch(title):
                    return True
                native_class = self.gui.GetClassName(hwnd)
                if native_class != 'mmui::MainWindow' and not _OUTER_CLASS.fullmatch(native_class):
                    return True
                candidate = True
                _, pid = self.process.GetWindowThreadProcessId(hwnd)
                if pid not in processes:
                    process = self.psutil.Process(pid)
                    name = process.name().lower()
                    created = process.create_time() if name == 'weixin.exe' else None
                    processes[pid] = (name, created)
                name, created = processes[pid]
                if name != 'weixin.exe':
                    return True
                if type(created) not in (int, float) or not math.isfinite(created) or created <= 0:
                    raise ValueError('process identity unavailable')
                owner = self.gui.GetWindow(hwnd, 4)  # GW_OWNER
                if type(owner) is not int:
                    raise ValueError('window owner unavailable')
                if owner:
                    diagnostic['owned_windows'] += 1
                    return True
                style = self.gui.GetWindowLong(hwnd, -20)  # GWL_EXSTYLE
                if type(style) is not int:
                    raise ValueError('window style unavailable')
                if style & 0x80:  # WS_EX_TOOLWINDOW，不把微信附属工具窗当作主窗。
                    diagnostic['tool_windows'] += 1
                    return True
                if not self._main_window_role(hwnd, native_class):
                    diagnostic['auxiliary_windows'] += 1
                    return True
                rows.append({'hwnd': hwnd, 'pid': pid, 'minimized': minimized,
                             'native_class': native_class, 'process_created': created})
            except Exception as error:
                failure(error, candidate)
            return True
        try:
            self.gui.EnumWindows(collect, None)
            diagnostic['enumeration_complete'] = True
        except Exception as error:
            diagnostic['enumeration_errors'] += 1
            failure(error, True)
        return rows

    def validate_candidate(self, row):
        """账号查询可能跨越进程退出；仅重读已选句柄，不激活或恢复窗口。"""
        hwnd, pid = row['hwnd'], row['pid']
        if not self.gui.IsWindow(hwnd) or self.process.GetWindowThreadProcessId(hwnd)[1] != pid:
            return False
        process = self.psutil.Process(pid)
        if process.name().lower() != 'weixin.exe' or process.create_time() != row['process_created']:
            return False
        if (not self.gui.IsWindowVisible(hwnd) and not self.gui.IsIconic(hwnd)
                or self.gui.GetClassName(hwnd) != row['native_class']
                or not _MAIN_TITLE.fullmatch(self.gui.GetWindowText(hwnd).strip())):
            return False
        owner, style = self.gui.GetWindow(hwnd, 4), self.gui.GetWindowLong(hwnd, -20)
        if type(owner) is not int or type(style) is not int or owner or style & 0x80:
            return False
        if not self._main_window_role(hwnd, row['native_class']):
            return False
        # 读取状态时句柄仍必须属于同一进程代次。
        current = (self.process.GetWindowThreadProcessId(hwnd)[1] == pid
                   and process.create_time() == row['process_created'])
        if current:
            row['minimized'] = bool(self.gui.IsIconic(hwnd))
        return current

    def restore(self, hwnd):
        # 只改变已验证窗口的显示状态，不模拟鼠标、不操作托盘或会话。
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user.ShowWindowAsync.restype = wintypes.BOOL
        return bool(user.ShowWindowAsync(hwnd, 9))

    def minimized(self, hwnd):
        return bool(self.gui.IsIconic(hwnd))

    def control(self, hwnd):
        return self.auto.ControlFromHandle(hwnd)

    def matches_account(self, pid, account_path):
        expected = (Path(account_path).resolve() / 'db_storage')
        return any(Path(item.path).resolve().is_relative_to(expected)
                   for item in self.psutil.Process(pid).open_files())

    def post_enter(self, hwnd):
        """仅投递到已验证的目标窗口，不改变系统焦点、鼠标或全局键态。"""
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        user.PostMessageW.restype = wintypes.BOOL
        scan = int(user.MapVirtualKeyW(0x0D, 0))
        down = bool(user.PostMessageW(hwnd, 0x0100, 0x0D, 1 | (scan << 16)))
        if not down:
            return False
        return bool(user.PostMessageW(hwnd, 0x0101, 0x0D, 1 | (scan << 16) | (3 << 30)))


def select_window(access, account_path=''):
    rows = access.candidates()
    diagnostic = {'candidate_count': len(rows), 'matched_count': 0, 'ready': False}
    discovery = getattr(access, 'discovery_diagnostic', None)
    if isinstance(discovery, dict):
        diagnostic['window_discovery'] = dict(discovery)
        if discovery.get('enumeration_complete') is not True or discovery.get('query_errors', 0):
            diagnostic.update(waiting_code='window_query_failed', reason=
                '微信候选窗口查询未完成，无法把已选账号与唯一主窗口对应；请检查进程查询权限后复查，本轮未操作')
            return None, diagnostic
    if not rows and isinstance(discovery, dict):
        diagnostic.update(waiting_code='window_not_found', reason=
            '未找到可见微信主窗口，无法把已选账号与窗口对应；请手动从托盘打开微信并完成登录，不会自动登录')
        return None, diagnostic
    if account_path:
        matches, checked, errors = [], {}, 0
        for row in rows:
            try:
                pid = row['pid']
                if pid not in checked:
                    checked[pid] = access.matches_account(pid, account_path)
                if checked[pid]:
                    matches.append(row)
            except Exception:
                errors += 1
        diagnostic.update(matched_count=len(matches), account_checks_failed=errors)
        if len(matches) != 1 or errors:
            # 同名聊天可存在于多个账号，唯一窗口不能替代正向账号证据。
            diagnostic['waiting_code'] = 'account_unverified'
            diagnostic['reason'] = ('部分微信账号窗口查询失败，无法把已选账号与唯一窗口对应，本轮未操作'
                if errors else '已选账号对应多个微信窗口，无法唯一确认，本轮未操作'
                if matches else '无法把已选账号与微信窗口对应；请确认该账号已登录，并检查进程查询权限')
            return None, diagnostic
        row = matches[0]
    else:
        if len(rows) != 1:
            diagnostic['reason'] = ('检测到多个微信主窗口，但未收到机器人账号绑定信息，本轮未操作'
                                    if rows else '未找到可见微信主窗口；请手动从托盘打开微信并完成登录，不会自动登录')
            return None, diagnostic
        row = rows[0]
        diagnostic['matched_count'] = 1
    # 只调用适配器类型明确声明的方法；Mock 的魔法属性不能成为核验能力。
    validator = getattr(type(access), 'validate_candidate', None)
    if callable(validator):
        try:
            valid = validator(access, row) is True
        except Exception:
            valid = False
        if not valid:
            diagnostic.update(waiting_code='window_changed', reason=
                '账号查询期间微信窗口或进程代次已改变，未沿用旧句柄；请复查当前窗口，本轮未操作')
            return None, diagnostic
    diagnostic.update(pid=row.get('pid'), hwnd=row['hwnd'], minimized=bool(row['minimized']))
    if 'process_created' in row:
        diagnostic['process_created'] = row['process_created']
    return row, diagnostic


def same_window_identity(current, pinned):
    """旧替身仅带句柄/PID；真实候选一旦固定进程代次，后续不能缺失它。"""
    if not isinstance(current, dict) or not isinstance(pinned, dict) or not current or not pinned:
        return False
    return (current.get('hwnd') == pinned.get('hwnd') and current.get('pid') == pinned.get('pid')
            and ('process_created' not in pinned
                 or ('process_created' in current and current['process_created'] == pinned['process_created'])))


def inspect_window(account_path='', access=None, desktop_status=None, send_compat=False,
                   auto_open_chat=False, allow_restore=True):
    from .interactive_desktop import interactive_desktop_status
    from .wechat_send_worker import find_scoped
    desktop_status = desktop_status or interactive_desktop_status
    ready, reason = desktop_status()
    if not ready:
        return {'ready': False, 'reason': reason, 'desktop_ready': False}
    access = access or NativeWindowAccess()
    row, diagnostic = select_window(access, account_path)
    diagnostic['desktop_ready'] = True
    if row is None:
        return diagnostic
    pinned = dict(row)
    # 严格只读模式不能先访问最小化窗口控件，再将它报告为不可用。
    if row['minimized'] and not allow_restore:
        diagnostic.update(ready=False, waiting_code='minimized', reason='微信已最小化，请手动恢复并打开目标聊天；不会自动弹出窗口')
        return diagnostic
    lookup = {}
    try:
        anchor = access.control(row['hwnd'])
        diagnostic['uia_class'] = str(anchor.ClassName or '')
        field = find_scoped(anchor, lambda c: c.ClassName == 'mmui::ChatInputField' or
                            'chat_input_field' in str(c.AutomationId or '').split('.'), timeout=2,
                            diagnostic=lookup)
    except Exception as error:
        diagnostic.update(ready=False, waiting_code='input_unavailable', input_available=False,
            error_type=type(error).__name__, reason='微信窗口控件读取失败，本次无法确认输入框；未输入、未发送')
        return diagnostic
    classes = set(lookup.get('classes') or ())
    # "微信没打开任何聊天"是常见状态：控件树正常，但没有会话详情页，自然没有输入框。
    # 此时发送链（auto_open_chat 开）会自己按名字打开目标会话，所以不能判成"发不出去"。
    no_chat_open = (field is None and lookup.get('search_status') == 'exhausted'
                    and not lookup.get('errors') and bool(classes)
                    and 'mmui::ChatInputField' not in classes
                    and any(name.startswith('mmui::') for name in classes)
                    and 'mmui::ChatDetailView' in classes)
    diagnostic['input_lookup'] = lookup
    diagnostic['input_available'] = field is not None
    diagnostic['auto_open_chat'] = bool(auto_open_chat)
    # 最小化**不算**未就绪：真正的发送走 `prepare_window()`，它会先把窗口恢复再输入。
    # 检查若把"最小化"判成不可发送，界面就会一直挂着"发送未就绪"，
    # 而运行时那道闸（wechat_app.py 的 _wait_send_environment）本来就容忍最小化 —— 口径必须一致。
    diagnostic['ready'] = field is not None or (no_chat_open and auto_open_chat)
    diagnostic['send_compat'] = bool(send_compat)
    # 类名带后缀：实测 4.1.15.13 是 MMUIRenderSubWindowHW，原来的 fullmatch 匹配不上，
    # 于是"只给外壳"这种情况一直被归到"没找到输入控件"里，提示词也就没法指导用户。
    shell_only = lookup.get('search_status') == 'exhausted' and not lookup.get('errors') and bool(
        lookup.get('classes')) and all(
        re.fullmatch(r'Qt\d+QWindowIcon|MMUIRenderSubWindow\w*|MMUIRenderSubWindow', name)
        for name in lookup.get('classes', []))
    if shell_only and not send_compat:
        missing = ('微信 4.x 默认不生成控件树（只给窗口外壳），所以现在发不出消息：'
                   '请手动打开目标聊天再只读检查；旧“发送兼容模式”开关不再唤醒进程，也不能保证开放控件')
    elif shell_only:
        missing = ('已开启发送兼容模式，但仍未读出控件树：'
                   '旧开关不会修改或唤醒微信进程；请手动打开目标聊天再只读检查，缺少控件时继续等待')
    elif no_chat_open and not auto_open_chat:
        missing = ('微信当前没有打开任何聊天，界面上没有输入框；'
                   '请手动点开要回复的群/联系人，再点「测试微信」；程序不会自动切换会话')
    elif no_chat_open:
        missing = '微信当前没有打开任何聊天（自动打开目标会话已开启，发送时会自己打开）'
    else:
        missing = ('账号窗口已匹配，输入控件查找达到预算上限；本次无法确认发送环境，请保留测试诊断'
                   if lookup.get('search_status') == 'budget' else
                   '账号窗口已匹配，但未找到聊天输入控件；请确认已手动打开聊天，或检查控件读取权限')
    diagnostic['reason'] = (
        ('微信当前没有打开任何聊天；自动打开目标会话已开启，发送时会自己打开并校验会话名'
         '（本次未输入、未发送）' if no_chat_open else
         '账号窗口和输入控件已确认（窗口当前最小化，发送时会自动恢复窗口；本次未输入、未发送）'
         if row['minimized'] else '账号窗口和输入控件已确认（未输入、未发送）')
        if diagnostic['ready'] else missing)
    if not diagnostic['ready']:
        diagnostic.setdefault('waiting_code', 'input_unavailable')
        return diagnostic
    # 控件查询是慢操作，不能将查询开始时的账号/桌面身份用于成功结论。
    ready, reason = desktop_status()
    if not ready:
        diagnostic.update(ready=False, desktop_ready=False, waiting_code='desktop_unavailable', reason=reason)
        return diagnostic
    try:
        current_row, current = select_window(access, account_path)
    except Exception as error:
        diagnostic.update(ready=False, waiting_code='window_query_failed', error_type=type(error).__name__,
            reason='输入控件读取后窗口复核失败，本次无法确认发送环境；未输入、未发送')
        return diagnostic
    diagnostic['window_recheck'] = current
    if current_row is None:
        diagnostic.update(ready=False, waiting_code=current.get('waiting_code', 'window_changed'),
                          reason=current.get('reason') or '窗口复核失败，本次未输入、未发送')
        return diagnostic
    if not same_window_identity(current_row, pinned):
        diagnostic.update(ready=False, waiting_code='window_changed',
            reason='控件读取期间微信窗口或进程代次已改变，未沿用旧控件；本次未输入、未发送')
        return diagnostic
    diagnostic['minimized'] = bool(current_row['minimized'])
    if current_row['minimized'] and not allow_restore:
        diagnostic.update(ready=False, waiting_code='minimized',
            reason='控件读取期间微信已最小化，请手动恢复并打开目标聊天；不会自动弹出窗口')
        return diagnostic
    ready, reason = desktop_status()
    if not ready:
        diagnostic.update(ready=False, desktop_ready=False, waiting_code='desktop_unavailable', reason=reason)
        return diagnostic
    if current_row['minimized'] and not no_chat_open:
        diagnostic['reason'] = '账号窗口和输入控件已确认（窗口当前最小化，发送时会自动恢复窗口；本次未输入、未发送）'
    return diagnostic


def prepare_window(uia, access=None, desktop_status=None, account_path='', allow_restore=True):
    from .interactive_desktop import interactive_desktop_status
    from .wechat_send_worker import find_scoped
    desktop_status = desktop_status or interactive_desktop_status
    ready, reason = desktop_status()
    if not ready:
        uia._window_error = reason
        return False
    access = access or NativeWindowAccess()
    row, diagnostic = select_window(access, account_path)
    uia._window_diagnostic = diagnostic
    if row is None:
        uia._window_error = diagnostic['reason']
        return False
    hwnd = row['hwnd']
    if row['minimized']:
        if not allow_restore:
            uia._window_error = '微信已最小化，请手动恢复并打开目标聊天；不会自动弹出窗口'
            diagnostic.update(ready=False, waiting_code='minimized', reason=uia._window_error)
            return False
        if not access.restore(hwnd):
            uia._window_error = '微信主窗口最小化且无法恢复，请手动打开微信；本轮未输入'
            return False
        deadline = time.monotonic() + 1.5
        while access.minimized(hwnd):
            if not desktop_status()[0] or time.monotonic() >= deadline:
                uia._window_error = '等待微信窗口恢复超时或桌面已锁定；请解锁并手动打开微信'
                return False
            time.sleep(0.05)
        diagnostic['minimized'] = False
    anchor = access.control(hwnd)
    main = find_scoped(anchor, lambda control: control.ClassName == 'mmui::MainWindow', timeout=0.5)
    if main is None:
        # 新版原生 Qt 外壳类不等于 UIA 类；仅在同一已验证句柄内查找明确输入锚点。
        field = find_scoped(anchor, lambda control: control.ClassName == 'mmui::ChatInputField' or
                            str(control.AutomationId or '').split('.')[-1] == 'chat_input_field', timeout=1)
        if field is None:
            uia._window_error = ('微信窗口已找到，但未暴露聊天输入控件：微信 4.x 默认不生成控件树，'
                                 '请手动打开目标聊天；若仍无控件，当前客户端不支持此发送通道，不会修改微信进程')
            diagnostic['reason'] = uia._window_error
            diagnostic['input_available'] = False
            return False
        main = anchor
    if not desktop_status()[0]:
        uia._window_error = '桌面已锁定，本轮未输入；请解锁后再试'
        return False
    # 查找控件期间也可能关闭/更换微信；不能激活已失去账号归属的旧句柄。
    try:
        current, checked = select_window(access, account_path)
    except Exception:
        current, checked = None, {}
    if not same_window_identity(current, row):
        uia._window_error = '输入控件读取期间微信窗口或进程已改变，本轮未激活或输入'
        diagnostic.update(ready=False, waiting_code='window_changed', reason=uia._window_error)
        return False
    if current.get('minimized'):
        uia._window_error = '输入控件读取期间微信已最小化，本轮未激活或输入'
        diagnostic.update(ready=False, minimized=True, waiting_code='minimized', reason=uia._window_error)
        return False
    if not desktop_status()[0]:
        uia._window_error = '桌面已锁定，本轮未激活或输入；请解锁后再试'
        return False
    uia._win = main
    uia._window_error = ''
    diagnostic.update(ready=True, reason='已确认账号窗口')
    uia._activate(main)
    return True


def install_window_discovery(uia, account_path=''):
    uia.ensure_window = lambda wake=False, timeout=None: prepare_window(uia, account_path=account_path, allow_restore=False)
