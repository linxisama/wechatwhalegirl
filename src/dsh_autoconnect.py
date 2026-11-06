"""控制台↔DSH 零配置连接：连接器刷新、用户级插件行、状态与守护线程。

纯标准库（不 import Qt / FastAPI）；配置文件路径、provision、connector 全部可注入。
对外函数一律返回 dict：错误带 reason 码与中文 message，绝不把裸异常抛给调用方。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

from . import agent_mcp_connector
from . import dsh_mcp_provision
from .persistence import atomic_text
from .runtime_identity import PORT

DEFAULT_BASE_URL = 'http://127.0.0.1:8091'
ROW_ID = 'mcp-whale-wechat'
SERVER_NAME = 'whale_wechat'
TOOL_TIMEOUT_MS = 75000
REGISTRY_ID = 'agent-preset-registry'
PRESET_ID = 'whale-workbench'
PRESET_NAME = '鲸鱼娘工作台'
CONNECTOR_VERSION = '2'
WATCH_ATTR = 'dsh_autoconnect_watch'
USER_START = '# >>> whale-wechat-default v1（微信控制台自动生成；删除本段即移除所有新会话的 whale_wechat 工具）'
USER_END = '# <<< whale-wechat-default v1'
# 只看顶层（0～4 空格缩进）的同一行：预设内部的 mcp-whale-wechat 缩进更深，不算用户手写。
FOREIGN_ROW = re.compile(r'^ {0,4}-?[ \t]*id:[ \t]*' + re.escape(ROW_ID) + r'[ \t]*$', re.MULTILINE)
# 凭据可能被写成 'xxx'，也可能是手写/旧版本的裸值，两种都要认。
TOKEN_LINE = re.compile(r"WHALE_AGENT_TOKEN:[ \t]*(?:'(?P<quoted>[^']*)'|(?P<plain>[^\s#]+))")


def _quoted(value):
    text = str(value)
    if any(ch in text for ch in ('\n', '\r')):
        raise ValueError('配置值不能包含换行')
    return "'" + text.replace("'", "''") + "'"


def _plain(value):
    text = str(value or '').strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1]
    return text


def _stamp(now=None):
    try:
        value = float(now() if callable(now) else now)
    except (TypeError, ValueError):
        value = time.time()
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(value))


def _iso(value):
    if value is None:
        return None
    try:
        return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(float(value)))
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value) or None


def project_root(runtime=None, root=None):
    if root is not None:
        return Path(root)
    value = getattr(runtime, 'root', None)
    if value:
        return Path(value)
    return Path(__file__).resolve().parent.parent


def patch_file(patch=None):
    """DSH 桌面配置文件路径：显式传入 > dsh_mcp_provision 的解析规则 > 用户目录默认值。"""
    if patch is not None:
        return Path(patch)
    try:
        return Path(dsh_mcp_provision.dsh_patch())
    except Exception:
        return Path.home() / '.dsh' / 'profiles' / 'desktop' / dsh_mcp_provision.PATCH_NAME


def connector_file(runtime=None, root=None):
    return project_root(runtime, root) / 'data' / 'agent_workspace' / 'whale_dsh_mcp.py'


def console_url(runtime=None):
    port = getattr(runtime, 'port', None)
    if type(port) is int and 1 <= port <= 65535:
        return 'http://127.0.0.1:' + str(port)
    return 'http://127.0.0.1:' + str(PORT)


def base_url(runtime=None):
    """从 runtime 设置读 dsh.base_url；读不到一律回落到本机 8091。"""
    try:
        settings = getattr(getattr(runtime, 'dsh', None), 'settings', None)
        value = settings() if callable(settings) else None
        text = str((value or {}).get('base_url') or '').strip()
        return text or DEFAULT_BASE_URL
    except Exception:
        return DEFAULT_BASE_URL


def last_announce_at(runtime=None):
    try:
        value = getattr(getattr(runtime, 'dsh', None), 'last_announce', None)
        if isinstance(value, dict):
            return _iso(value.get('at'))
    except Exception:
        pass
    return None


def build_user_block(python, script, url, token):
    """用户级（顶层）MCP 插件行：与 mcp-snowluma 同形，所有新会话都会继承。"""
    lines = [
        USER_START,
        '- insert:',
        '    - id: ' + ROW_ID,
        "      name: '@deepseek-ai/dsh-mcp-client'",
        '      config:',
        '        serverName: ' + SERVER_NAME,
        '        transport: stdio',
        '        command: ' + _quoted(python),
        '        args:',
        '          - ' + _quoted(script),
        '        env:',
        '          WHALE_BOT_URL: ' + _quoted(url),
        '          WHALE_AGENT_TOKEN: ' + _quoted(token),
        "          PYTHONUTF8: '1'",
        '        toolCallTimeoutMs: ' + str(TOOL_TIMEOUT_MS),
        USER_END,
    ]
    return '\n'.join(lines)


def read_registry(text):
    """读 agent-preset-registry 条目的 default / selectedDefault（决定新会话默认预设）。"""
    result = {'present': False, 'default': '', 'selected': ''}
    lines = str(text or '').splitlines()
    head = -1
    for index, line in enumerate(lines):
        if line.strip() == '- id: ' + REGISTRY_ID:
            head = index
            break
    if head < 0:
        return result
    result['present'] = True
    for line in lines[head + 1:]:
        if line.startswith('- '):
            break
        stripped = line.strip()
        if stripped.startswith('selectedDefault:'):
            result['selected'] = _plain(stripped.split(':', 1)[1])
        elif stripped.startswith('default:'):
            result['default'] = _plain(stripped.split(':', 1)[1])
    return result


def _segment(text, start, end):
    head = str(text or '').find(start)
    if head < 0:
        return ''
    tail = text.find(end, head)
    if tail < 0:
        return ''
    stop = text.find('\n', tail)
    return text[head:] if stop < 0 else text[head:stop]


def block_segment(text):
    return _segment(text, dsh_mcp_provision.START, dsh_mcp_provision.END)


def user_segment(text):
    return _segment(text, USER_START, USER_END)


def block_current(text, python, script, url):
    """补丁里的 MCP 块是否与当前解释器/脚本/地址一致（一致就不再签发凭据）。"""
    segment = block_segment(text)
    if not segment:
        return False
    return all(_quoted(item) in segment for item in (python, str(script), url))


def token_in(text):
    match = TOKEN_LINE.search(str(text or ''))
    if not match:
        return ''
    value = match.group('quoted')
    return _plain(value if value is not None else match.group('plain') or '')


def unmanaged_block(text):
    """手写（无标记行）的 whale-workbench 预设段：返回 (起始行号, 段文本)，没有就是 (0, '')。"""
    source = str(text or '')
    lines = source.splitlines()
    head = source.find(dsh_mcp_provision.START)
    tail = source.find(dsh_mcp_provision.END, head) if head >= 0 else -1
    offset = 0
    for index, line in enumerate(lines):
        start_of_line, offset = offset, offset + len(line) + 1
        if line.strip() != '- id: preset-' + PRESET_ID:
            continue
        if head >= 0 and tail >= 0 and head <= start_of_line <= tail:
            continue  # 标记段内的预设是控制台自己在管，不算手写
        stop = len(lines)
        for later in range(index + 1, len(lines)):
            nxt = lines[later]
            if nxt[:1] not in ('', ' ', '\t') and nxt.lstrip().startswith('- '):
                stop = later
                break
        return index + 1, '\n'.join(lines[index:stop])
    return 0, ''


def unmanaged_state(text, python, script, url):
    """无标记的等价预设：current（内容一致，别再写一遍）/ stale（内容不同，绝不覆盖）。"""
    line, body = unmanaged_block(text)
    if not body:
        return 0, 'absent'
    if ('id: ' + ROW_ID) in body and all(str(item) in body for item in (python, str(script), url)):
        return line, 'current'
    return line, 'stale'


def block_state(text, python, script, url):
    """工作台预设是否已可用：带标记的块优先，其次认手写的等价段。"""
    if block_current(text, python, script, url):
        return 'current'
    return unmanaged_state(text, python, script, url)[1]


def user_row_present(text):
    segment = user_segment(text)
    if not segment:
        return False
    return ('id: ' + ROW_ID) in segment


def _backup_file(path, raw):
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime())
    base = path.name + '.bak-' + stamp
    target, index = path.with_name(base), 2
    while target.exists():
        target, index = path.with_name(base + '-' + str(index)), index + 1
    target.write_bytes(raw)
    return target.name


def write_user_block(patch_path, block, *, start=USER_START, end=USER_END):
    """幂等写入用户级插件行：有标记原地替换、没有就追加；用户手写的同 id 行不碰。"""
    path = Path(patch_path)
    if not path.is_file():
        return {'changed': False, 'backup': '', 'reason': 'missing'}
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return {'changed': False, 'backup': '', 'reason': 'encoding'}
    newline = '\r\n' if '\r\n' in text else '\n'
    body = str(block).strip('\n')
    if newline != '\n':
        body = body.replace('\n', newline)
    head = text.find(start)
    if head >= 0:
        tail = text.find(end, head)
        if tail < 0:
            return {'changed': False, 'backup': '', 'reason': 'unterminated'}
        stop = text.find('\n', tail)
        stop = len(text) if stop < 0 else stop + 1
        updated = text[:head] + body + newline + text[stop:]
    elif FOREIGN_ROW.search(text):
        return {'changed': False, 'backup': '', 'reason': 'foreign'}
    else:
        prefix = '' if not text or text.endswith('\n') else newline
        updated = text + prefix + body + newline
    if updated == text:
        return {'changed': False, 'backup': '', 'reason': 'current'}
    backup = _backup_file(path, raw)
    atomic_text(path, updated, backup=False)
    return {'changed': True, 'backup': backup, 'reason': 'written'}


def _connector_step(connector, path):
    refresh = getattr(connector, 'refresh_existing', None)
    if not callable(refresh):
        return {'name': 'connector', 'state': 'failed', 'detail': '连接器模块不可用，未刷新接入脚本'}
    try:
        result = refresh(str(path)) or {}
        reason = str(result.get('reason') or '')
        if reason == 'current':
            return {'name': 'connector', 'state': 'current', 'detail': '接入脚本已是最新：' + Path(path).name}
        if reason == 'upgraded':
            return {'name': 'connector', 'state': 'upgraded',
                    'detail': '接入脚本已升级，原文件备份为 ' + str(result.get('backup') or '（无）')}
        if reason == 'foreign':
            return {'name': 'connector', 'state': 'foreign',
                    'detail': '接入脚本被本地改过，保留原样没有覆盖：' + Path(path).name}
        if reason == 'blocked':
            return {'name': 'connector', 'state': 'blocked',
                    'detail': '接入脚本备份失败，为保留本地修改没有覆盖：' + Path(path).name}
        if reason == 'missing':
            place = getattr(connector, 'place_connector', None)
            if not callable(place):
                return {'name': 'connector', 'state': 'failed', 'detail': '接入脚本缺失且无法生成'}
            placed = place(str(path)) or {}
            return {'name': 'connector', 'state': 'generated',
                    'detail': '已生成接入脚本：' + Path(path).name}
        return {'name': 'connector', 'state': 'failed', 'detail': '接入脚本刷新返回未知原因：' + (reason or '（空）')}
    except Exception as error:
        return {'name': 'connector', 'state': 'failed',
                'detail': '刷新接入脚本失败（' + type(error).__name__ + '），其余步骤照常继续'}


def _provision_step(provision, runtime, patch_path, python, script, url):
    text = ''
    try:
        if Path(patch_path).is_file():
            text = Path(patch_path).read_text('utf-8')
    except (OSError, UnicodeDecodeError):
        text = ''
    if block_current(text, python, script, url):
        return {'name': 'mcp-block', 'state': 'current', 'detail': 'DSH 配置里的 MCP 块已是最新：' + str(patch_path)}, text
    line, unmanaged = unmanaged_state(text, python, script, url)
    if unmanaged == 'current':
        return {'name': 'mcp-block', 'state': 'current',
                'detail': '第 %d 行已有等价的手写工作台预设（没有标记行），没有重复写入' % line}, text
    if unmanaged == 'stale':
        return {'name': 'mcp-block', 'state': 'foreign',
                'detail': '第 %d 行的手写工作台预设与本次解释器/脚本/地址不一致，没有自动改动；'
                          '请先手工删掉那一段再点「一键修复连接」' % line}, text
    if not Path(patch_path).is_file():
        # 配置还没出现（DSH 没装或没启动过）就先不签发凭据，免得白占一张票。
        return {'name': 'mcp-block', 'state': 'pending',
                'detail': '未找到 DSH 桌面配置文件：' + str(patch_path) + '（先安装并启动一次 DeepSeek Harness）'}, text
    if not callable(provision):
        return {'name': 'mcp-block', 'state': 'failed', 'detail': 'provision 不可用，未写 MCP 块'}, text
    try:
        result = provision(runtime) or {}
    except Exception as error:
        return {'name': 'mcp-block', 'state': 'failed',
                'detail': '写入 MCP 块失败（' + type(error).__name__ + '）：' + str(error)}, text
    if not result.get('success'):
        return {'name': 'mcp-block', 'state': 'failed',
                'detail': '写入 MCP 块未完成：' + str(result.get('message') or '未知原因')}, text
    try:
        text = Path(patch_path).read_text('utf-8')
    except (OSError, UnicodeDecodeError):
        text = ''
    if result.get('backup'):
        return {'name': 'mcp-block', 'state': 'written',
                'detail': '已写入 MCP 块，原配置备份为 ' + str(result.get('backup'))}, text
    return {'name': 'mcp-block', 'state': 'current', 'detail': 'DSH 配置已是最新，没有改动'}, text


def _default_step(patch_path, text, python, script, url, token, registry=None):
    """让新会话默认看得见 whale_wechat：默认预设已是工作台就不再叠一条；否则写用户级插件行。"""
    registry = registry or {}
    if registry.get('selected') == PRESET_ID:
        return {'name': 'new-session-default', 'state': 'current',
                'detail': '新会话默认预设已是「' + PRESET_NAME + '」，无需再加用户级插件行'}
    if not token:
        return {'name': 'new-session-default', 'state': 'failed',
                'detail': '没有可用凭据，未生成用户级插件行'}
    block = build_user_block(python, script, url, token)
    try:
        placed = write_user_block(patch_path, block)
    except Exception as error:
        return {'name': 'new-session-default', 'state': 'failed',
                'detail': '写入用户级插件行失败（' + type(error).__name__ + '），DSH 配置保持原样'}
    reason = str(placed.get('reason') or '')
    if reason == 'written':
        return {'name': 'new-session-default', 'state': 'written',
                'detail': '已写入用户级 MCP 插件行（原配置备份为 ' + str(placed.get('backup') or '（无）') + '）'}
    if reason == 'current':
        return {'name': 'new-session-default', 'state': 'current', 'detail': '用户级 MCP 插件行已是最新'}
    if reason == 'foreign':
        return {'name': 'new-session-default', 'state': 'foreign',
                'detail': 'DSH 配置里已有手写的 ' + ROW_ID + ' 行，按你的写法保留、不重复添加'}
    if reason == 'missing':
        return {'name': 'new-session-default', 'state': 'pending',
                'detail': '未找到 DSH 桌面配置文件，暂不写用户级插件行：' + str(patch_path)}
    if reason == 'unterminated':
        return {'name': 'new-session-default', 'state': 'failed',
                'detail': '配置里的 ' + USER_START + ' 缺少结束标记，请先手动修正该段'}
    if reason == 'encoding':
        return {'name': 'new-session-default', 'state': 'failed',
                'detail': 'DSH 配置不是 UTF-8 文本，为免损坏没有改动'}
    return {'name': 'new-session-default', 'state': 'failed', 'detail': '写入用户级插件行返回未知结果'}


def ensure(runtime=None, provision=None, connector=None, now=None, *, patch=None, root=None, python=None):
    """幂等三步：刷新连接器 → 写 MCP 块 → 让新会话默认看得见。每步失败只记录不抛。"""
    steps = []
    try:
        patch_path = patch_file(patch)
        script = connector_file(runtime, root)
        if python is None:
            python, _reason = dsh_mcp_provision.python_executable(project_root(runtime, root))
        url = console_url(runtime)
        steps.append(_connector_step(connector if connector is not None else agent_mcp_connector, script))
        block_step, text = _provision_step(provision if provision is not None else dsh_mcp_provision.provision,
                                           runtime, patch_path, python or '', script, url)
        steps.append(block_step)
        token = token_in(block_segment(text)) or token_in(user_segment(text)) or token_in(text)
        registry = read_registry(text)
        steps.append(_default_step(patch_path, text, python or '', script, url, token, registry))
    except Exception as error:
        # 兜底：任何没料到的意外也只变成一条失败步骤，不把裸异常抛给接口。
        steps.append({'name': 'ensure', 'state': 'failed',
                      'detail': '准备连接步骤时出现意外错误（' + type(error).__name__ + '）'})
    states = [step['state'] for step in steps]
    failed = [step for step in steps if step['state'] == 'failed']
    critical = [step for step in failed if step['name'] in ('connector', 'mcp-block')]
    if critical:
        state = 'failed'
    elif failed:
        state = 'partial'
    elif all(value in ('current', 'upgraded', 'generated', 'written') for value in states):
        state = 'ready'
    else:
        state = 'partial'
    return {'ok': state == 'ready', 'state': state, 'steps': steps, 'at': _stamp(now),
            'message': _ensure_message(state, steps)}


def _ensure_message(state, steps):
    if state == 'ready':
        return 'DSH 连接已就绪：新会话可直接使用 whale_wechat 工具'
    if state == 'failed':
        broken = next((step for step in steps if step['state'] == 'failed'), {})
        return 'DSH 连接未完成：' + str(broken.get('detail') or '未知原因')
    pending = [step['detail'] for step in steps if step['state'] not in ('current', 'upgraded', 'generated', 'written')]
    return 'DSH 连接部分完成：' + ('；'.join(pending) or '还有步骤没到位') + '（不重启 DSH 时配置不会生效）'


def status(runtime=None, *, patch=None, root=None, connector=None, python=None):
    """一眼看清：连不上是缺连接器、缺配置，还是缺「新会话默认可见」。"""
    try:
        patch_path = patch_file(patch)
        script = connector_file(runtime, root)
        port_url = console_url(runtime)
        module = connector or agent_mcp_connector
        template = str(getattr(module, 'CONNECTOR', ''))
        issues = []
        raw = b''
        try:
            if Path(script).is_file():
                raw = Path(script).read_bytes()
        except OSError:
            raw = b''
        digest = hashlib.sha256(raw).hexdigest() if raw else ''
        if not raw:
            version, issues = '', ['connector_missing']
        elif raw == template.encode('utf-8'):
            version = CONNECTOR_VERSION
        else:
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                text = ''
            version = 'unknown'
            issues.append('connector_foreign' if not module.generated_by_console(text) else 'connector_outdated')
        text = ''
        patch_exists = Path(patch_path).is_file()
        if patch_exists:
            try:
                text = Path(patch_path).read_text('utf-8')
            except (OSError, UnicodeDecodeError):
                return _status_failure(patch_path, 'patch_unreadable',
                                       'DSH 配置文件读不出来（' + str(patch_path) + '），请检查权限后重试')
        registry = read_registry(text)
        if not patch_exists:
            issues.append('patch_missing')
        if patch_exists:
            expected_python = python
            if expected_python is None:
                expected_python, _py_reason = dsh_mcp_provision.python_executable(project_root(runtime, root))
            known = block_state(text, expected_python or '', script, port_url)
            if known == 'absent':
                issues.append('block_missing')
            elif known == 'stale':
                issues.append('block_unmanaged')
        if not user_row_present(text) and registry.get('selected') != PRESET_ID:
            issues.append('default_missing')
        if 'connector_foreign' in issues:
            state = 'error'
        elif not issues:
            state = 'ready'
        else:
            state = 'pending'
        actions = [
            {'key': 'repair', 'label': '一键修复连接', 'method': 'POST', 'path': '/api/dsh/connect/repair'},
            {'key': 'watch', 'label': '开启自动守护', 'method': 'POST', 'path': '/api/dsh/connect/watch',
             'body': {'enabled': True}},
        ]
        code = _issue_code(issues, state)
        return {'state': state,
                'base_url': base_url(runtime),
                'connector': {'path': str(script), 'version': version, 'sha256': digest},
                'preset': str(registry.get('selected') or registry.get('default') or ''),
                'default_preset_set': bool(user_row_present(text) or registry.get('selected') == PRESET_ID),
                'last_announce_at': last_announce_at(runtime),
                'hint': _hint(state, issues, patch_path),
                'actions': actions,
                'patch': str(patch_path),
                'port_url': port_url,
                'issues': issues,
                'code': code,
                'code_text': _code_text(code),
                'code_hint': _code_hint(code),
                'message': _status_message(state)}
    except Exception as error:
        return _status_failure(patch_file(patch), 'internal',
                               '读取 DSH 连接状态失败（' + type(error).__name__ + '），请重试')


# issue → 连接码：面板只认码，不再从 issues 字符串里猜。顺序即优先级。
_ISSUE_CODES = (
    ('patch_unreadable', 'dsh_config_missing'),
    ('patch_missing', 'dsh_config_missing'),
    ('connector_foreign', 'dsh_setup_incomplete'),
    ('connector_outdated', 'dsh_setup_incomplete'),
    ('connector_missing', 'dsh_setup_incomplete'),
    ('block_unmanaged', 'dsh_setup_incomplete'),
    ('block_missing', 'dsh_setup_incomplete'),
    ('default_missing', 'dsh_setup_incomplete'),
)


def _issue_code(issues, state):
    """按优先级取第一个命中；都就绪时给 dsh_ok。码表在 dsh_discovery 里，延迟导入保持本模块轻量。"""
    if state == 'ready':
        return 'dsh_ok'
    for issue, code in _ISSUE_CODES:
        if issue in issues:
            return code
    return ''


def _code_text(code):
    if not code:
        return ''
    from .dsh_discovery import describe_code
    return describe_code(code)


def _code_hint(code):
    if not code:
        return ''
    from .dsh_discovery import hint_for_code
    return hint_for_code(code)


def _status_message(state):
    if state == 'ready':
        return 'DSH 连接已就绪：新建会话即可使用 whale_wechat 工具'
    if state == 'error':
        return 'DSH 连接需要人工确认：接入脚本被本地改过，控制台不会覆盖你的改动'
    return 'DSH 连接还没就绪：按 hint 里的动作执行一次即可'


def _hint(state, issues, patch_path):
    if state == 'ready':
        return '新建会话即可使用；DSH 重启后若端口变了，控制台会自动重新发现（守护开启时）'
    if 'connector_foreign' in issues:
        return ('接入脚本 ' + '被本地改过，控制台不会覆盖；请点「一键修复连接」查看明细，'
                '或手动把 data/agent_workspace/whale_dsh_mcp.py 换回控制台模板')
    if 'patch_missing' in issues:
        return '没找到 ' + str(patch_path) + '：先安装并启动一次 DeepSeek Harness，再点「一键修复连接」'
    if 'block_unmanaged' in issues:
        return ('DSH 配置里有手写的工作台预设，但解释器/脚本/地址与控制台不一致；请先手工删掉那一段，'
                '再点「一键修复连接」（控制台不会自动覆盖你的写法）')
    if 'block_missing' in issues or 'default_missing' in issues:
        return '点「一键修复连接」写入 MCP 配置，然后重启一次 DeepSeek Harness（配置只在启动时读取）'
    return '点「一键修复连接」补齐缺失步骤'


def _status_failure(patch_path, reason, message):
    # 读配置失败与读状态炸了不是一回事：前者让用户修文件，后者让用户重试。
    code = 'dsh_config_missing' if reason == 'patch_unreadable' else 'dsh_state_unreadable'
    return {'state': 'error', 'base_url': DEFAULT_BASE_URL,
            'code': code, 'code_text': _code_text(code), 'code_hint': _code_hint(code),
            'connector': {'path': '', 'version': '', 'sha256': ''},
            'preset': '', 'default_preset_set': False, 'last_announce_at': None,
            'hint': '请检查控制台日志后重试', 'actions': [], 'patch': str(patch_path),
            'issues': [reason], 'reason': reason, 'message': message}


def watch(runtime, interval=30.0, stop_event=None, *, status_fn=None, backoff=(1.0, 60.0)):
    """后台守护：周期跑 status()，异常按 1→2→4…（上限 60s）退避；只用局部状态。"""
    try:
        step = float(interval)
    except (TypeError, ValueError):
        step = 30.0
    if not step > 0:
        step = 30.0
    base, cap = 1.0, 60.0
    try:
        base = float(backoff[0])
        cap = float(backoff[1])
    except (TypeError, ValueError, IndexError, KeyError):
        base, cap = 1.0, 60.0
    if base <= 0:
        base = 0.001
    if cap < base:
        cap = base
    stop = stop_event if stop_event is not None else threading.Event()
    probe = status_fn if callable(status_fn) else (lambda: status(runtime))
    cycles, errors, unhealthy, last, delay = 0, 0, 0, None, base
    while not stop.is_set():
        try:
            value = probe()
            cycles += 1
            if isinstance(value, dict):
                last = value
            healthy = _watch_healthy(last)
            if healthy:
                delay = base
                stop.wait(step)
            else:
                # 「连不上」不是异常：状态函数照常返回 dict，过去这里直接按正常节奏空转，
                # 于是 DSH 关着的时候守护仍然每 30 秒刷一遍日志和磁盘。按同一套退避慢下来。
                unhealthy += 1
                stop.wait(min(delay, cap))
                delay = min(delay * 2, cap)
        except Exception:
            # 读状态本身炸了：不退出守护，只按退避慢下来，避免刷屏和打爆磁盘。
            errors += 1
            stop.wait(min(delay, cap))
            delay = min(delay * 2, cap)
    return {'cycles': cycles, 'errors': errors, 'unhealthy': unhealthy, 'interval': step,
            'stopped': True, 'last_delay': delay, 'cap': cap,
            'last_state': (last or {}).get('state') if isinstance(last, dict) else None}


# 这些状态说明链路是通的；其余（error/pending/未检测/没码）都要退避，别把「没连上」当正常。
_HEALTHY_STATES = frozenset({'ready', 'ok'})


def _watch_healthy(value):
    """只有明确的就绪状态才算健康：读不到状态时按不健康处理，宁可慢一点。"""
    if not isinstance(value, dict):
        return False
    return str(value.get('state') or '') in _HEALTHY_STATES


def connect_instructions():
    """给用户的可复制中文说明：3～5 行，照做即可。"""
    lines = [
        '1. 打开 DeepSeek Harness 点「新建会话」：控制台已把 whale_wechat 的 MCP 插件写进 DSH 配置，不用手动找地址或改配置。',
        '2. 工具没出现时，回控制台点「一键修复连接」，再重启一次 DeepSeek Harness（配置只在启动时读取）。',
        '3. 到新会话里发这句话验证：「请用 whale_wechat 的 workspace_info 工具看一下我的工作区」——能返回工作区信息就是连上了。',
        '4. 想让它自己盯着端口漂移：在控制台打开「自动守护」，DSH 重启后会自动恢复，多个会话各用各的，不互相串。',
    ]
    return '\n'.join(lines)
