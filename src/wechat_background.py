"""严格后台文字发送：仅调用控件能力，绝不退回键鼠、置前或切会话。"""
import re
import time

from .interactive_desktop import interactive_desktop_status
from .wechat_window import NativeWindowAccess, select_window, same_window_identity
from .wechat_send_worker import find_scoped, valid_chat_name


def chat_name(field, expected=''):
    name = str(field.Name or '').strip()
    # 与依赖的输入框标题格式保持一致，不按模糊昵称猜目标。
    from .wechat_chat_name import clean_chat_name
    return clean_chat_name(name, expected)


def _send_button(root, submit, diagnostic=None, key='button_search'):
    """每次从当前树读取唯一按钮；不能跨写入复用 Qt 旧元素的调用模式。"""
    buttons, lookup = [], {}
    def collect(control):
        if control.ControlTypeName == 'ButtonControl' and str(control.Name or '').strip() in ('发送', 'Send'):
            buttons.append(control)
        return False
    find_scoped(root, collect, diagnostic=lookup)
    # 三种拒绝理由要分开说：只报"唯一确认"会让"树里有读错误"和"开着别的发送框"
    # 看起来是同一种情况，用户照着提示关对话框永远修不好。
    if diagnostic is not None:
        diagnostic[key] = dict(lookup, buttons=len(buttons))
    if not buttons:
        return None, None, '没有可直接调用的发送按钮；当前客户端不支持此后台通道'
    if len(buttons) != 1:
        return None, None, '无法唯一确认发送按钮，请关闭微信内的其他发送对话框后复查'
    if lookup.get('errors'):
        return None, None, ('发送按钮查找遇到 %s 个读取错误，无法唯一确认；请关闭微信内的其他发送对话框后复查'
                            % lookup['errors'])
    if lookup.get('search_status') != 'exhausted':
        return None, None, ('发送按钮查找未遍历完整棵控件树（%s），无法唯一确认；请关闭微信内的其他发送对话框后复查'
                            % (lookup.get('search_status') or 'unknown'))
    button = buttons[0]
    invoke = button.GetInvokePattern() if submit == 'invoke' else None
    if invoke is None and submit == 'invoke':
        return None, None, '发送按钮不支持后台调用；当前客户端不支持此后台通道'
    return button, invoke, ''


def _current_bound_window(access, account_path, pinned, desktop_status):
    """长读取或阶段落盘后重新核验，不能用旧控件证明窗口仍属于原账号。"""
    ready, reason = desktop_status()
    if not ready:
        return None, {'desktop_ready': False}, reason, 'desktop_unavailable'
    try:
        row, current = select_window(access, account_path)
    except Exception as error:
        return None, {'window_check_exception_type': type(error).__name__}, '账号窗口复核失败', 'account_unverified'
    if not same_window_identity(row, pinned):
        return None, {}, '账号窗口已改变，未读取或操作其他窗口', 'account_unverified'
    if row.get('minimized'):
        return None, {'minimized': True}, '微信已最小化，未恢复窗口', 'minimized'
    ready, reason = desktop_status()
    if not ready:
        return None, {'desktop_ready': False}, reason, 'desktop_unavailable'
    return row, {'desktop_ready': True}, '', ''


def inspect_background(account_path='', who='', access=None, desktop_status=None, submit='invoke'):
    ready, reason = (desktop_status or interactive_desktop_status)()
    diagnostic = {'ready': False, 'background_send': True, 'desktop_ready': ready,
                  'submit_verified': False}
    if not ready:
        return dict(diagnostic, waiting_code='desktop_unavailable', reason=reason), None
    access = access or NativeWindowAccess()
    row, window = select_window(access, account_path)
    diagnostic.update(window)
    # 每个失败出口都必须带码：裸 not_ready 在三张码表里都查不到，用户只会看到原始英文。
    def unavailable(reason, code):
        return dict(diagnostic, ready=False, waiting_code=code,reason='严格后台等待：' + reason), None
    if row is None:
        return unavailable(window['reason'], window.get('waiting_code') or 'window_query_failed')
    if submit not in ('invoke', 'posted_enter'):
        return unavailable('后台提交方式无效，未尝试输入', 'unsupported_submit')
    diagnostic['submit_method'] = submit
    if submit == 'posted_enter' and (not account_path or window.get('matched_count') != 1):
        return unavailable('窗口投递试验需要正向核对账号，不能沿用未验证的单窗口兜底', 'account_unverified')
    if row['minimized']:
        return unavailable('微信已最小化，请手动恢复并打开目标聊天；不会自动弹出窗口','minimized')
    # 第一次标准 UIA 查询可能先得到尚未物化的外壳。只给这类完整、无读取
    # 错误的短树一次纯读取刷新；共用原来的两秒查询预算，绝不调用唤醒或置前。
    search_deadline = time.monotonic() + 2.0
    def read_input():
        lookup = {}
        try:
            root = access.control(row['hwnd'])
            field = find_scoped(root, lambda c: c.ClassName == 'mmui::ChatInputField' or
                'chat_input_field' in str(c.AutomationId or '').split('.'),
                timeout=max(0, search_deadline - time.monotonic()), diagnostic=lookup)
            return root, field, lookup
        except Exception as exc:
            return None, None, {'visited': 0, 'search_status': 'error', 'errors': 1,
                                'classes': [], 'error_type': type(exc).__name__}
    root, field, lookup = read_input()
    diagnostic.update(input_search=lookup, input_search_attempts=[dict(lookup)], input_recheck_count=0)
    shell_classes = lookup.get('classes') or []
    cold_shell = (field is None and lookup.get('search_status') == 'exhausted'
        and not lookup.get('errors') and 0 < lookup.get('visited', 0) <= 3
        and shell_classes and all(re.fullmatch(r'Qt\d+QWindowIcon|MMUIRenderSubWindow\w*', name)
                                  for name in shell_classes))
    if cold_shell:
        if search_deadline - time.monotonic() <= .15:
            diagnostic['input_recheck_skipped'] = 'budget'
        else:
            time.sleep(.15)
            ready, reason = (desktop_status or interactive_desktop_status)()
            diagnostic['desktop_ready'] = ready
            if not ready:
                return unavailable(reason, 'desktop_unavailable')
            current_row, current_window = select_window(access, account_path)
            if not same_window_identity(current_row, row):
                return unavailable('只读复查期间账号窗口已改变，未读取其他窗口', 'account_unverified')
            if current_row.get('minimized'):
                diagnostic['minimized'] = True
                return unavailable('只读复查期间微信已最小化，未恢复窗口', 'minimized')
            diagnostic.update(current_window)
            if time.monotonic() >= search_deadline:
                diagnostic['input_recheck_skipped'] = 'budget'
            else:
                root, field, lookup = read_input()
                diagnostic['input_search'] = lookup
                diagnostic['input_search_attempts'].append(dict(lookup))
                diagnostic['input_recheck_count'] = 1
                ready, reason = (desktop_status or interactive_desktop_status)()
                diagnostic['desktop_ready'] = ready
                if not ready:
                    return unavailable(reason, 'desktop_unavailable')
    if field is None:
        if lookup.get('errors'):
            return unavailable('后台控件读取失败，本次无法确认输入控件；未输入、未发送', 'input_unavailable')
        if lookup.get('search_status')=='exhausted' and lookup.get('visited',0)<=3:
            return unavailable('本次仅获取到浅层窗口信息，可能是控件尚未加载，或微信没有开放聊天输入控件；当前只读证据不足以确认严格后台能力。不会切换窗口或模拟键鼠','accessibility_unavailable')
        return unavailable('没有可用输入控件；请确认目标聊天已打开。本次只读证据不足以确认严格后台能力','input_unavailable')
    try:
        current = chat_name(field, who)
        diagnostic['current_chat'] = current
        if not current or (who and current != who):
            return unavailable('请手动打开目标会话' + (f'「{who}」' if who else '') + '；不会自动切换','chat_not_open')
        value = field.GetValuePattern()
        if value is None or value.IsReadOnly:
            return unavailable('当前输入控件不支持后台写入', 'input_unavailable')
        if value.Value is None or str(value.Value):
            return unavailable('输入框已有草稿或无法回读；保留草稿，等待手动处理','draft')
        # 必须是同一窗口下明确的发送按钮，不能按坐标猜测或投递到任意窗口。
        button, invoke, problem = _send_button(root, submit, diagnostic)
        if problem:
            return unavailable(problem, 'accessibility_unavailable')
    except Exception as exc:
        return unavailable(f'控件读取失败（{type(exc).__name__}），稍后复查', 'input_unavailable')
    # 正常树查询和冷树刷新都可能跨越换窗/最小化/锁屏；成功出口必须同样复核。
    _, current, problem, code = _current_bound_window(access, account_path, row,
        desktop_status or interactive_desktop_status)
    diagnostic.update(current)
    if problem:
        return unavailable(problem, code)
    diagnostic.update(ready=True, input_available=True, delivery_verified=False,
        reason=('后台控件可读取，窗口投递为实验通道（未输入、未发送，未验证提交有效）'
                if submit == 'posted_enter' else '严格后台控件可读取（未输入、未发送，未验证提交有效，不代表已验证送达）'))
    return diagnostic, (field, value, button, invoke)


def _stop(info, code, reason):
    """失败出口统一落码：只写诊断，不改正文，也不做任何补发。"""
    info.update(ready=False, waiting_code=code)
    return False, reason


def send_background(job, phase, access=None, desktop_status=None):
    ok, reason = _send_background(job, phase, access, desktop_status)
    if not ok:
        diagnostic = dict(job.get('_diagnostic') or {}, ready=False, reason=reason)
        diagnostic.setdefault('waiting_code', 'send_failed')
        job['_diagnostic'] = diagnostic
    return ok, reason


def _send_background(job, phase, access=None, desktop_status=None):
    who, text = str(job.get('who') or ''), str(job.get('text') or '')
    if not valid_chat_name(who) or not text.strip():
        job['_diagnostic'] = dict(job.get('_diagnostic') or {}, ready=False,
            waiting_code='invalid_target', reason='后台目标名称或正文无效，本次未输入')
        return False, '后台目标名称或正文无效，本次未输入'
    desktop_status = desktop_status or interactive_desktop_status
    phase('window')
    submit = job.get('background_submit', 'invoke')
    access = access or NativeWindowAccess()
    info, controls = inspect_background(job.get('account_path', ''), who, access, desktop_status, submit)
    info['submission_state'] = 'not_submitted'
    job['_diagnostic'] = info
    if controls is None:
        return False, info['reason']
    field, value, button, invoke = controls
    # 三种"写前环境已变"分开落码：同一个笼统出口会把"没写入"报成发送失败。
    if not desktop_status()[0]:
        return _stop(info, 'desktop_unavailable', '后台输入前桌面已锁屏或断开，未写入正文')
    if chat_name(field, who) != who:
        return _stop(info, 'chat_not_open', '后台输入前会话已改变，未写入正文')
    if str(value.Value or ''):
        return _stop(info, 'draft', '后台输入前输入框已有草稿，未写入正文')
    phase('input')
    # 阶段记录可能阻塞，Qt也可能已重建输入框；旧field的标题和空值不能代表当前树。
    row, current, problem, code = _current_bound_window(access, job.get('account_path', ''), info, desktop_status)
    info.update(current)
    if problem:
        return _stop(info, code, '写入前' + problem + '，未写入正文')
    try:
        root = access.control(row['hwnd'])
        lookup = {}
        field = find_scoped(root, lambda c: c.ClassName == 'mmui::ChatInputField' or
            'chat_input_field' in str(c.AutomationId or '').split('.'), timeout=1, diagnostic=lookup)
        info['input_refresh_search'] = lookup
        if field is None or lookup.get('errors'):
            return _stop(info, 'input_unavailable', '写入前当前输入控件已改变，未写入正文')
        if chat_name(field, who) != who:
            return _stop(info, 'chat_not_open', '写入前会话已改变，未写入正文')
        value = field.GetValuePattern()
        if value is None or value.IsReadOnly:
            return _stop(info, 'input_unavailable', '写入前输入控件不支持后台写入，未覆盖用户输入')
        if value.Value != '':
            return _stop(info, 'draft', '写入前草稿已改变，未覆盖用户输入')
    except Exception as error:
        info['input_refresh_exception_type'] = type(error).__name__
        return _stop(info, 'input_unavailable', '写入前控件复核失败，未写入正文')
    _, current, problem, code = _current_bound_window(access, job.get('account_path', ''), info, desktop_status)
    info.update(current)
    if problem:
        return _stop(info, code, '写入前' + problem + '，未写入正文')
    if chat_name(field, who) != who:
        return _stop(info, 'chat_not_open', '写入前目标已改变，未覆盖用户输入')
    if value.Value != '':
        return _stop(info, 'draft', '写入前草稿已改变，未覆盖用户输入')
    info['input_controls_refreshed'] = True
    value.SetValue(text)
    phase('readback')
    expected = text.replace('\r\n', '\n')
    if not desktop_status()[0]:
        return _stop(info, 'desktop_unavailable', '后台提交前桌面状态改变，本次未提交；请检查输入框草稿')
    if submit == 'invoke':
        # SetValue 可能使 Qt 重建按钮甚至整个输入控件。重新读取时仍只认同一
        # 账号窗口、精确会话和完整正文；写后不先读取可能已失效的旧 COM 对象。
        try:
            row, current = select_window(access, job.get('account_path', ''))
            if (not same_window_identity(row, info) or current.get('matched_count') != 1
                    or row.get('minimized')):
                return _stop(info, 'account_unverified', '提交前账号窗口改变，本次未提交；请检查草稿')
            root = access.control(row['hwnd'])
            field = find_scoped(root, lambda c: c.ClassName == 'mmui::ChatInputField' or
                                'chat_input_field' in str(c.AutomationId or '').split('.'))
            if field is None or chat_name(field, who) != who:
                return _stop(info, 'chat_not_open', '后台提交前会话或状态改变，本次未提交；请检查输入框草稿')
            value = field.GetValuePattern()
            if value is None or value.IsReadOnly:
                return _stop(info, 'input_unavailable', '后台提交前输入控件不可写，本次未提交；请检查输入框草稿')
            if str(value.Value or '').replace('\r\n', '\n') != expected:
                return _stop(info, 'draft', '后台正文回读不一致，本次未提交；请检查输入框草稿')
            button, invoke, problem = _send_button(root, submit, info, 'button_search_refresh')
            if problem:
                return _stop(info, 'accessibility_unavailable', '提交前' + problem + '；本次未提交，保留草稿')
            info['controls_refreshed'] = True
        except Exception as exc:
            info['refresh_exception_type'] = type(exc).__name__
            return _stop(info, 'input_unavailable', '提交前控件复核失败，本次未提交；请检查草稿')
    else:
        if str(value.Value or '').replace('\r\n', '\n') != expected:
            return _stop(info, 'draft', '后台正文回读不一致，本次未提交；请检查输入框草稿')
        if chat_name(field, who) != who:
            return _stop(info, 'chat_not_open', '后台提交前会话或状态改变，本次未提交；请检查输入框草稿')
    if not desktop_status()[0]:
        return _stop(info, 'desktop_unavailable', '后台提交前桌面状态改变，本次未提交；请检查草稿')
    if not button.IsEnabled:
        return _stop(info, 'accessibility_unavailable', '后台提交前发送按钮不可用，本次未提交；请检查草稿')
    phase('send_key')
    _, current, problem, code = _current_bound_window(access, job.get('account_path', ''), info, desktop_status)
    info.update(current)
    if problem:
        return _stop(info, code, '提交前' + problem + '；本次未提交，保留草稿')
    if chat_name(field, who) != who:
        return _stop(info, 'chat_not_open', '提交前目标已改变，本次不重发，请检查草稿')
    if str(value.Value or '').replace('\r\n', '\n') != expected:
        return _stop(info, 'draft', '提交前正文已改变，本次不重发，请检查草稿')
    # Invoke抛异常也可能已经被微信接收，必须先标记未知，禁止任何前台兜底。
    info['submission_state'] = 'uncertain'
    info['submit_call_returned'] = False
    try:
        if submit == 'posted_enter':
            row, current = select_window(access, job.get('account_path', ''))
            if (not same_window_identity(row, info) or current.get('matched_count') != 1
                    or row['minimized']):
                info['submission_state'] = 'not_submitted'
                return _stop(info, 'account_unverified', '投递前账号窗口改变，本次未提交；请检查草稿')
            # 与Invoke互斥：只选择一个提交原语，不进行第二种发送尝试。
            if not access.post_enter(row['hwnd']):
                raise RuntimeError('window message rejected')
        else:
            result = invoke.Invoke()
            # 原始返回值仅作诊断，不能替代后续清空检查，也不据 False 重试。
            info['invoke_return'] = result if result is None or isinstance(result, (bool, int, float)) else 'other'
        info['submit_call_returned'] = True
        info['submission_state'] = 'submitted'
        phase('confirmation')
        # 旧的0.5秒在微信忙碌时过短。只读等待，绝不再次Invoke或用Enter补发。
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if chat_name(field, who) != who:
                break
            remaining = value.Value
            if remaining == '':
                info['submission_state'] = 'confirmed'
                return True, '后台调用已提交，输入框已确认清空'
            if remaining is None or str(remaining).replace('\r\n', '\n') != expected:
                break
            time.sleep(0.025)
    except Exception as exc:
        # 这里不记录驱动异常正文，避免控件内容混入日志。
        kind = ('confirmation_exception_type' if info['submit_call_returned'] else
                'invoke_exception_type' if submit == 'invoke' else 'post_submit_exception_type')
        info[kind] = type(exc).__name__
    reason = '已尝试后台调用，但未确认输入框清空，结果待核实，本次不重发'
    info.update(ready=False, submission_state='uncertain', waiting_code='submission_uncertain', reason=reason)
    return False, reason
