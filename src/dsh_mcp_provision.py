"""一键接入DSH：签发凭据、生成本机MCP连接器、写入DSH桌面配置。

配置只维护带标记的一段；标记之外的字节原样保留，首次修改前先备份。
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .agent_authority import CAPABILITIES, AuthorityError, authority
from .agent_mcp_connector import place_connector
from .persistence import atomic_text
from .runtime_identity import PORT

START = '# >>> whale-wechat-workbench'
END = '# <<< whale-wechat-workbench'
BLOCK_ID = 'preset-whale-workbench'
PRESET_ID = 'whale-workbench'
PRESET_NAME = '鲸鱼娘工作台'
SERVER_NAME = 'whale_wechat'
PEER = 'dsh-app'
DAYS = 30
TIMEOUT_MS = 75000
PATCH_NAME = 'cordis.patch.yml'
DEFAULT_URL = f'http://127.0.0.1:{PORT}'
PREVIEW_TOKEN = '<WHALE_AGENT_TOKEN>'


class ProvisionError(ValueError):
    pass


def dsh_patch():
    """DSH桌面配置文件路径；WHALE_DSH_PATCH仅供测试指向临时目录。"""
    override = os.environ.get('WHALE_DSH_PATCH', '').strip()
    if override:
        return Path(override)
    home = os.environ.get('USERPROFILE') or Path.home()
    return Path(home) / '.dsh' / 'profiles' / 'desktop' / PATCH_NAME


def python_executable(root):
    """按工程venv、PATH里的python.exe、py.exe顺序取解释器；找不到则fail-closed。"""
    venv = Path(root) / '.venv-wechat' / 'Scripts' / 'python.exe'
    if venv.is_file():
        return str(venv), None
    for name in ('python.exe', 'PYTHON.EXE', 'py.exe', 'PY.EXE'):
        try:
            found = shutil.which(name)
        except OSError:
            found = None
        if found and Path(found).is_file():
            return found, None
    return None, '未找到可用的 Python 3.10+ 解释器，请先安装 Python 或把本工程 venv 放到 .venv-wechat'


def _quoted(value):
    text = str(value)
    if any(ch in text for ch in ('\n', '\r')):
        raise ProvisionError('配置值不能包含换行')
    # YAML单引号内反斜杠不转义，路径原样写；单引号按YAML规则写成两个。
    return "'" + text.replace("'", "''") + "'"


def build_block(python, script, token, url=DEFAULT_URL, *, server_name=SERVER_NAME, timeout_ms=TIMEOUT_MS):
    if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 600000:
        raise ProvisionError('工具调用超时须为1000～600000毫秒的整数')
    # 工具只挂在本工程自己的预设上，不动用户已有的顶层插件条目。
    lines = [
        START + ' v1（微信控制台自动生成；删除本段即移除该预设）',
        '- insert:',
        '    - id: ' + BLOCK_ID,
        "      name: '@deepseek-ai/dsh-agent-preset'",
        '      config:',
        '        id: ' + PRESET_ID,
        '        name: ' + _quoted(PRESET_NAME),
        '        description: ' + _quoted('通过 whale_wechat 的 MCP 工具读写微信控制台工作区/记忆/学习，并执行控制台动作'),
        '        plugins:',
        '          - id: mcp-whale-wechat',
        "            name: '@deepseek-ai/dsh-mcp-client'",
        '            config:',
        '              serverName: ' + server_name,
        '              transport: stdio',
        '              command: ' + _quoted(python),
        '              args:',
        '                - ' + _quoted(script),
        '              env:',
        '                WHALE_BOT_URL: ' + _quoted(url),
        '                WHALE_AGENT_TOKEN: ' + _quoted(token),
        "                PYTHONUTF8: '1'",
        '              toolCallTimeoutMs: ' + str(timeout_ms),
        END + ' v1',
    ]
    return '\n'.join(lines)


def _backup(path, raw):
    base = path.with_name(path.name + '.bak-' + time.strftime('%Y%m%d%H%M%S'))
    candidate, index = base, 2
    while candidate.exists() and candidate.read_bytes() != raw:
        candidate = base.with_name(base.name + '-' + str(index))
        index += 1
    candidate.write_bytes(raw)
    return str(candidate)


def write_block(patch_path, block):
    """幂等写入：有标记就原地替换，没有就追加到末尾；标记之外的字节不动。"""
    path = Path(patch_path)
    if not path.is_file():
        raise ProvisionError('未找到 DSH 桌面配置文件：' + str(path))
    raw = path.read_bytes()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    body = str(block).strip('\n').encode('utf-8')
    if newline != b'\n':
        body = body.replace(b'\n', newline)
    start = raw.find(START.encode('utf-8'))
    if start >= 0:
        end = raw.find(END.encode('utf-8'), start)
        if end < 0:
            raise ProvisionError('DSH配置文件里的 ' + START + ' 没有对应的结束标记，请先手动修正该段')
        # 结束标记那一行整行属于本段，避免把标记行尾的内容（版本号）留在文件里。
        stop = raw.find(b'\n', end)
        end = len(raw) if stop < 0 else stop + 1
        updated = raw[:start] + body + newline + raw[end:]
    else:
        prefix = b'' if not raw or raw.endswith(b'\n') else newline
        updated = raw + prefix + body + newline
    if updated == raw:
        return {'written': True, 'backup': '', 'changed': False}
    try:
        text = updated.decode('utf-8')
    except UnicodeDecodeError:
        raise ProvisionError('DSH配置文件不是UTF-8文本，为免损坏没有改动') from None
    backup = _backup(path, raw)
    atomic_text(path, text, backup=False)
    return {'written': True, 'backup': backup, 'changed': True}


def console_url(bot):
    port = getattr(bot, 'port', None)
    return f'http://127.0.0.1:{port}' if type(port) is int and 1 <= port <= 65535 else DEFAULT_URL


def _drop(service, credential_id):
    try:
        service.revoke(credential_id)
    except (AuthorityError, OSError, ValueError):
        pass


def _fail(message, patch=''):
    return {'success': False, 'server_name': SERVER_NAME, 'token': '', 'script': '', 'patch': str(patch),
            'written': False, 'backup': '', 'connector_backup': '', 'block': '', 'message': str(message)}


def provision(bot, *, write=True):
    """签发凭据并生成连接器；write=True时写入DSH桌面配置。异常一律变成中文message。"""
    try:
        return _provision(bot, write=write)
    except Exception as exc:
        return _fail('接入未完成：' + (str(exc) or type(exc).__name__))


def _provision(bot, *, write):
    root = Path(bot.root)
    python, reason = python_executable(root)
    if not python:
        # 没有可用的解释器就不签发凭据：写了也连不上，只会白占一个凭据名额。
        return _fail(reason)
    patch = dsh_patch()
    script = root / 'data' / 'agent_workspace' / 'whale_dsh_mcp.py'
    if not write:
        # 预览绝不签发也不撤销：签发出来的凭据没有落盘入口，只会把在用的那条顶成废票。
        try:
            authority(bot).local_actor()
            placed = place_connector(script)
            block = build_block(python, script, PREVIEW_TOKEN, console_url(bot))
        except (AuthorityError, OSError, ValueError) as exc:
            return _fail(str(exc) or '生成配置块失败', patch)
        # 旧连接器被就地升级时，先把备份文件名摆到最前面，别让用户以为凭空丢了一份文件。
        head = placed['message'] + '\n' if placed['backup'] else ''
        return {'success': True, 'server_name': SERVER_NAME, 'token': '', 'script': str(script),
                'patch': str(patch), 'written': False, 'backup': '', 'connector_backup': placed['backup'],
                'block': block,
                'message': head + '只生成了可复制配置块，没有写任何文件，也没有签发或撤销凭据；'
                                  '真正写入时才签发新凭据（同时撤销同名接入端的旧凭据）。'}
    try:
        service = authority(bot)
        status = service.status()
        for row in status.get('credentials') or []:
            if row.get('peer') == PEER:
                service.revoke(row.get('id'))
        credential = service.issue(PEER, sorted(CAPABILITIES), DAYS)
    except (AuthorityError, OSError, ValueError) as exc:
        return _fail(str(exc) or '签发凭据失败', patch)
    service = authority(bot)
    try:
        placed = place_connector(script)
        block = build_block(python, script, credential['token'], console_url(bot))
    except Exception:
        _drop(service, credential['id'])
        raise
    written, backup, note = False, '', '只生成了可复制配置块，没有写任何文件'
    if write:
        if not patch.is_file():
            note = '未找到 DSH 桌面配置文件，已生成可复制配置块'
        else:
            try:
                patched = write_block(patch, block)
                written, backup = True, patched['backup']
                note = (f'已写入 DSH 配置：{patch}（备份 {backup}）' if patched['changed']
                        else f'DSH 配置已是最新，没有改动：{patch}')
            except (ProvisionError, OSError) as exc:
                note = '写入 DSH 配置失败：' + str(exc)
    head = placed['message'] + '\n' if placed['backup'] else ''
    message = (head + f'已为 DSH 签发凭据（全部五项能力，{DAYS} 天）：{script}\n{note}\n'
               f'重启 DeepSeek Harness 后，在会话里选择预设「{PRESET_NAME}」即可使用。'
               '凭据等同于本机控制权，别贴到公共地方。')
    return {'success': True, 'server_name': SERVER_NAME, 'token': credential['token'], 'script': str(script),
            'patch': str(patch), 'written': written, 'backup': backup, 'connector_backup': placed['backup'],
            'block': block, 'message': message}
