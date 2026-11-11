"""Windows 沙盒功能的启用编排：探测 / 提权启用 / 重启确认 / 创建隔离通道。

为什么单独成模块：Windows 沙盒是「可选功能」——家庭版没有，启用要管理员权限，
启用后必须重启才生效，而控制台平时以普通权限运行。用户看到的应当只是
「点一下 → 在 UAC 弹窗点“是” → 重启 → 再点」，而不是手抄 dism 命令。

本模块只用标准库，不 import Qt / FastAPI；所有外部命令都经过可注入的 runner，
提权经过可注入的 elevation，单元测试全部用替身——不会真的调用 dism、不会弹 UAC、
也不会改动本机的功能状态。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .site_profile import _resolve_wsb_exe

FEATURE_NAME = 'Containers-DisposableClientVM'
FEATURE_LABEL = 'Windows 沙盒'
# 只有这些版本带「Windows 沙盒」可选功能；Core（家庭版）永远没有。
SUPPORTED_EDITIONS = frozenset((
    'professional', 'professionaln', 'enterprise', 'enterprisen',
    'education', 'educationn', 'iotenterprise', 'iotenterprises'))
# DISM 用 3010 表示「成功但需要重启」，不能当成失败。
REBOOT_EXIT_CODES = frozenset((3010,))
DISM = 'dism.exe'
REGISTRY_PATH = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion'
DEFAULT_TIMEOUT = 900.0
PROBE_TIMEOUT = 60.0
# dism /get-featureinfo 需要管理员权限，普通用户只会拿到 740；WMI 的 Win32_OptionalFeature
# 同一份状态普通人也能读，所以它是 dism 读不到时的第二条路。
CIM_TIMEOUT = 30.0
CIM_SCRIPT = ('(Get-CimInstance -ClassName Win32_OptionalFeature -Filter "Name=\'' + FEATURE_NAME +
              '\'").InstallState')
# InstallState 枚举：1 已启用、2 已禁用、3 不存在、4 未知。
INSTALL_STATES = {1: 'Enabled', 2: 'Disabled', 3: 'Absent'}
LOG_DIR_NAME = 'sandbox-provision'
RECORD_NAME = 'last-enable.json'
EXIT_MARKER = '[whale-dism-exit]'
# UAC 发起的提权进程拿不到 pid，用「最近一次请求时间」判断它还在不在跑。
ELEVATION_FRESH_SECONDS = 600.0

_EDITION_LABELS = {
    'professional': '专业版', 'professionaln': '专业版 N',
    'enterprise': '企业版', 'enterprisen': '企业版 N',
    'education': '教育版', 'educationn': '教育版 N',
    'iotenterprise': 'IoT 企业版', 'iotenterprises': 'IoT 企业版 LTSC',
    'core': '家庭版', 'corecountryspecific': '家庭中文版',
    'coresinglelanguage': '家庭单语言版',
    'professionalcountryspecific': '专业中文版',
    'serverstandard': '服务器标准版', 'serverdatacenter': '服务器数据中心版',
}

_EXIT_MESSAGES = {
    0: '启用成功（0）',
    50: '此系统不支持这个操作（50）：常见于家庭版或服务器精简版',
    87: '参数错误（87）：功能名或系统版本与命令不匹配',
    740: '需要管理员权限（740）：提权没有成功，请在 UAC 弹窗点“是”，或右键“以管理员身份运行”控制台',
    '80070005': '拒绝访问（0x80070005）：当前进程不是管理员',
    '800F0805': '找不到该功能包（0x800f0805）：系统里没有 Containers-DisposableClientVM，'
                '常见于家庭版、被精简的镜像或被组策略移除',
    '800F0906': '需要联网下载安装文件但失败了（0x800f0906）：检查网络或代理后重试',
    '800F081F': '找不到安装源文件（0x800f081f）：联网重试，或用系统 ISO 以 /Source 指定源',
    '800F0954': '组策略 / WSUS 拦住了功能安装（0x800f0954）：请让管理员放行“可选功能”安装',
    '800F0922': '系统保留分区空间不足或组件存储损坏（0x800f0922）：清理磁盘后重试',
}


def _decode_bytes(raw):
    """dism 在中文系统上按控制台代码页输出，UTF-8 不成时退回 GBK。"""
    if isinstance(raw, str):
        return raw
    if not raw:
        return ''
    for codec in ('utf-8', 'gbk'):
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', 'replace')


def _default_runner(cmd, timeout=DEFAULT_TIMEOUT):
    """默认 runner：真实执行外部命令，失败也返回 dict，绝不抛给调用方。"""
    try:
        completed = subprocess.run([str(part) for part in cmd], capture_output=True,
                                   timeout=timeout, check=False,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return {'returncode': -1, 'stdout': '', 'stderr': type(error).__name__ + ': ' + str(error)}
    return {'returncode': int(completed.returncode or 0),
            'stdout': _decode_bytes(completed.stdout),
            'stderr': _decode_bytes(completed.stderr)}


def _call_runner(runner, cmd, timeout):
    """把任意 runner 的返回归一成 {returncode, stdout, stderr}，异常也收成结果。"""
    call = runner if callable(runner) else _default_runner
    try:
        raw = call([str(part) for part in cmd], timeout)
    except Exception as error:  # 替身或系统调用炸了都不能冒到界面上
        return {'returncode': -1, 'stdout': '', 'stderr': type(error).__name__ + ': ' + str(error)}
    if not isinstance(raw, dict):
        return {'returncode': -1, 'stdout': '', 'stderr': 'runner 返回格式异常'}
    try:
        code = int(raw.get('returncode', -1))
    except (TypeError, ValueError):
        code = -1
    return {'returncode': code,
            'stdout': _decode_bytes(raw.get('stdout') or ''),
            'stderr': _decode_bytes(raw.get('stderr') or '')}


def _default_registry_reader():
    """读 EditionID；非 Windows 或读不到时返回空字典。"""
    values = {}
    try:
        import winreg
    except ImportError:
        return values
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_PATH, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            for name in ('EditionID', 'ProductName'):
                try:
                    values[name] = str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    continue
    except OSError:
        return values
    return values


def _read_edition(registry_reader):
    """registry_reader 可以是返回 dict 的可调用对象、dict 或字符串；坏了也只是空字符串。"""
    reader = registry_reader if registry_reader is not None else _default_registry_reader
    try:
        raw = reader() if callable(reader) else reader
    except Exception:
        return ''
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for name in ('EditionID', 'edition_id', 'EditionId'):
            value = raw.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ''


def _edition_label(edition):
    key = str(edition or '').casefold()
    if not key:
        return '当前系统'
    return _EDITION_LABELS.get(key, str(edition))


def _sandbox_exe_path():
    root = os.environ.get('SystemRoot') or r'C:\Windows'
    return Path(root) / 'System32' / 'WindowsSandbox.exe'


def _sandbox_app_exists():
    try:
        return _sandbox_exe_path().is_file()
    except OSError:
        return False


def _wsb_exe_path():
    """Windows 沙盒的命令行工具 wsb.exe。

    注意：Windows 10（19045 实测）根本没有 wsb.exe，只有 WindowsSandbox.exe；
    wsb.exe 是较新版本才带的命令行入口。1.3.75 及以前这里探的是 WindowsSandbox.exe，
    于是 Windows 10 上面板显示「沙盒已就绪、可以去建通道」，真去建通道却报找不到命令行。
    1.3.78 起与 site_profile 共用同一个解析器（System32 → WindowsApps 别名 → PATH），
    保证「兼容性自检」与「沙盒面板」对同一台机器给出同一个结论。
    """
    try:
        path, exists, _source = _resolve_wsb_exe()
        if exists:
            return Path(path)
    except Exception:
        pass
    root = os.environ.get('SystemRoot') or r'C:\Windows'
    return Path(root) / 'System32' / 'wsb.exe'


def _wsb_exe_exists():
    """只认命令行 wsb.exe；有 WindowsSandbox.exe 但没 wsb.exe 的系统算「没有命令行」。"""
    try:
        return bool(_resolve_wsb_exe()[1])
    except Exception:
        return False


def _parse_feature_state(text):
    """解析 get-featureinfo 的 State 行，中英文都认；认不出返回空串。"""
    if not text:
        return ''
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.casefold()
        if not (low.startswith('state') or line.startswith('状态')):
            continue
        value = re.split('[:：]', line, maxsplit=1)[-1].strip().casefold()
        if any(token in value for token in ('disabled', '已禁用', '未启用', '禁用')):
            return 'Disabled'
        if any(token in value for token in ('enabled', '已启用', '启用')):
            return 'Enabled'
        if any(token in value for token in ('absent', '不存在', '已删除')):
            return 'Absent'
    return ''


def _looks_absent(text, returncode):
    """dism 找不到功能名时不会给 State 行，只给 0x800f0805 或一句“功能名称未知”。"""
    if returncode == 0 or not text:
        return False
    low = str(text).casefold()
    return ('0x800f0805' in low or '功能名称未知' in text or 'feature name is unknown' in low
            or 'the feature name is unknown' in low)


def _powershell_path():
    """绝对路径：PATH 里没有 powershell 时裸名 spawn 会直接 WinError 2。"""
    root = Path(os.environ.get('SystemRoot') or r'C:\Windows')
    exe = root / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    return str(exe) if exe.is_file() else 'powershell.exe'


def _install_state_label(value):
    """InstallState 可能是 int、字符串数字或空；认不出返回空串，绝不猜。"""
    try:
        code = int(str(value).strip())
    except (TypeError, ValueError):
        return ''
    return INSTALL_STATES.get(code, '')


def _parse_install_state(text):
    """解析 Win32_OptionalFeature 输出：既认 ConvertTo-Json 的字典，也认裸的 InstallState 数字。"""
    raw = str(text or '').strip()
    if not raw:
        return ''
    if raw.startswith('{') or raw.startswith('['):
        try:
            value = json.loads(raw)
        except ValueError:
            return ''
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if isinstance(row, dict) and str(row.get('Name') or '') == FEATURE_NAME:
                return _install_state_label(row.get('InstallState'))
        return ''
    for line in raw.splitlines():
        label = _install_state_label(line)
        if label:
            return label
    return ''


def _cim_feature_state(runner):
    """dism 读不到时的兜底：WMI 查询同一条可选功能，普通权限就能读。"""
    cmd = [_powershell_path(), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
           '-Command', CIM_SCRIPT]
    result = _call_runner(runner, cmd, CIM_TIMEOUT)
    out = result.get('stdout', '') or ''
    text = out + chr(10) + (result.get('stderr', '') or '')
    state = _parse_install_state(out)
    if (not state and not out.strip() and not (result.get('stderr') or '').strip()
            and result.get('returncode', -1) == 0):
        # 查询没报错但没有这条功能：机器上确实没有这个可选功能（家庭版、精简镜像）。
        state = 'Absent'
    return {'state': state, 'returncode': result.get('returncode', -1), 'text': text}


def _probe_feature(runner):
    cmd = [DISM, '/online', '/get-featureinfo', '/featurename:' + FEATURE_NAME]
    result = _call_runner(runner, cmd, PROBE_TIMEOUT)
    text = (result.get('stdout', '') or '') + chr(10) + (result.get('stderr', '') or '')
    state = _parse_feature_state(text)
    if not state and _looks_absent(text, result.get('returncode', -1)):
        state = 'Absent'
    source = 'dism' if state else ''
    if not state:
        cim = _cim_feature_state(runner)
        text += chr(10) + cim['text']
        state = cim['state']
        source = 'cim' if state else ''
    return {'state': state, 'returncode': result.get('returncode', -1), 'text': text,
            'source': source}


def _result(state, supported, edition, feature, wsb_exe, needs_reboot, reason, message, hint,
            next_step):
    # wsb_exe 是命令行工具 wsb.exe（建通道要用）；sandbox_app 是 WindowsSandbox.exe（开窗口要用）。
    # 两个键分开报，界面才不会在只有 WindowsSandbox.exe 的 Windows 10 上说「已就绪」。
    return {'state': state, 'supported': bool(supported), 'edition': str(edition or ''),
            'feature': feature, 'wsb_exe': bool(wsb_exe), 'sandbox_app': _sandbox_app_exists(),
            'needs_reboot': bool(needs_reboot),
            'reason': reason, 'message': message, 'hint': hint, 'next_step': next_step}


def _container_broken(runner=None, store=None):
    """容器管理器服务起不来（Win10 精简版常见）：服务不在 RUNNING，且组件存储缺 BaseImages。

    只在「服务确实查到了、状态不是 RUNNING」且「组件存储缺失」同时成立时才判损坏，
    免得把一台从没开过沙盒的健康机器误判成坏机器、挡住用户点“启用”。
    """
    try:
        from . import sandbox_doctor
        info = store if isinstance(store, dict) else sandbox_doctor.container_store_info()
        result = _call_runner(runner, ['sc', 'query', 'CmService'], 10)
        state = sandbox_doctor._sc_state(result.get('stdout') or '')
    except Exception:
        return False
    if not state or state == 'RUNNING':
        return False
    return info.get('base_images') is False or info.get('portable_base_layer') is False


def _container_broken_hint():
    return ('这台机器上的「容器管理器服务」（CmService）起不来，Windows 沙盒本身启动失败，'
            '控制台再怎么建通道也用不了。这是系统组件损坏，不是控制台的问题。'
            '可以这样继续：① 用本机模式（微信照旧在桌面运行，功能完整，只是不隔离）；'
            '② 想修系统：先跑 sfc /scannow 和 DISM /Online /Cleanup-Image /RestoreHealth，'
            '修不好就只能重装系统或用官方 ISO 升级；'
            '③ 想要隔离通道：换一台 Windows 11（24H2 及以上）机器，点“重新检测”就会变成可建通道。')

def _no_cli_hint():
    return ('自动建隔离通道要调用 wsb.exe，这台机器没有，所以通道功能暂时用不了。'
            '可以这样继续：① 用本机模式（微信照旧在桌面运行，不隔离，功能完整）；'
            '② 手动打开 Windows 沙盒、在里面登录微信，再把共享目录当传输通道（控制台不自动接管）；'
            '③ 换一台 Windows 11 或带 wsb.exe 的机器，点“重新检测”就会变成可建通道。')


def _unsupported_hint():
    return ('替代方案：① 升级到 Windows 专业版/企业版/教育版后重试；'
            '② 继续用本机模式（微信在桌面运行，不隔离）；'
            '③ 自己用 VMware / VirtualBox 开虚拟机跑微信，本控制台暂不接管虚拟机。')


def detect(runner=None, registry_reader=None, last_record=None):
    """探测 Windows 沙盒功能状态；任何异常都收敛成 unknown + 可执行建议，绝不外抛。"""
    try:
        return _detect(runner, registry_reader, last_record)
    except Exception as error:
        return _result('unknown', True, '', '', _wsb_exe_exists(), False, 'probe_exception',
                       '探测 Windows 沙盒状态时出现意外错误（' + type(error).__name__ + '），已按未知处理',
                       '可以照常点“启用 Windows 沙盒”：已启用时 DISM 不会重复安装，只是确认一次。',
                       'enable')


def _detect(runner, registry_reader, last_record):
    edition = _read_edition(registry_reader)
    key = edition.casefold()
    supported_edition = (key in SUPPORTED_EDITIONS) if key else None
    wsb_exe = _wsb_exe_exists()
    needs_reboot = (isinstance(last_record, dict) and last_record.get('state') == 'pending_reboot'
                    and not _rebooted_since(last_record))
    if supported_edition is False:
        return _result('unsupported', False, edition, '', wsb_exe, needs_reboot,
                       'unsupported_edition',
                       _edition_label(edition) + '不支持 Windows 沙盒：这个功能只在专业版、企业版、教育版提供',
                       _unsupported_hint(), 'none')
    probe = _probe_feature(runner)
    feature = probe['state']
    if feature == 'Enabled':
        if _container_broken(runner):
            # Win10 19045 精简版实测：功能显示已启用，但 CmService 卡在 START_PENDING，
            # 组件存储缺 BaseImages/PortableBaseLayer，沙盒窗口根本起不来。
            return _result('broken', True, edition, 'Enabled', wsb_exe, needs_reboot,
                           'container_service_broken',
                           _edition_label(edition) + '上 Windows 沙盒功能显示已启用，'
                           '但容器管理器服务起不来，沙盒启动会失败',
                           _container_broken_hint(), 'manual')
        if not wsb_exe:
            # Windows 10 19045 实测：功能开着、WindowsSandbox.exe 也在，但根本没有 wsb.exe。
            # 这时不能说「已就绪、去建通道」，否则用户点下去只会看到「找不到命令行」。
            return _result('no_cli', True, edition, 'Enabled', wsb_exe, needs_reboot, 'no_cli',
                           _edition_label(edition) + '上 Windows 沙盒功能已启用，'
                           '但这台机器没有 wsb.exe 命令行工具（Windows 10 只带 WindowsSandbox.exe）',
                           _no_cli_hint(), 'manual')
        next_step = 'launch'
        message = _edition_label(edition) + '上 Windows 沙盒功能已启用'
        hint = ('功能已生效：先打开 Windows 沙盒并在其中登录微信，再点“创建隔离通道”。'
                if not needs_reboot else
                '功能已启用；建议重启电脑一次再建通道（更稳），也可以先直接点“创建隔离通道”试试。')
        return _result('enabled', True, edition, 'Enabled', wsb_exe, needs_reboot, 'enabled',
                       message, hint, next_step)
    if feature == 'Disabled':
        message = _edition_label(edition) + '上找到了 Windows 沙盒功能，但它是关闭状态'
        hint = ('点“启用 Windows 沙盒”，在管理员授权窗口点“是”；启用完成后需要重启电脑才生效。')
        next_step = 'enable'
        return _result('disabled', True, edition, 'Disabled', wsb_exe, needs_reboot, 'disabled',
                       message, hint, next_step)
    if feature == 'Absent':
        message = '本机镜像里没有 Windows 沙盒组件（DISM 找不到 ' + FEATURE_NAME + '）'
        hint = ('点“启用 Windows 沙盒”让系统尝试补装；若报 0x800f0805 说明镜像被精简，'
                '请用官方 ISO 修复或改用本机模式。')
        next_step = 'enable'
        return _result('absent', True, edition, 'Absent', wsb_exe, needs_reboot, 'feature_absent',
                       message, hint, next_step)
    # 两条路都读不到（dism 没提权返回 740，WMI 也没给出结果）才落到这里。
    supported = True if supported_edition is None else bool(supported_edition)
    if needs_reboot:
        # 已经提权跑过 dism 且退出码是 3010：状态读不到只是因为还没重启，别再让用户白点一次“启用”。
        return _result('unknown', supported, edition, '', wsb_exe, True, 'dism_unavailable',
                       '已提交启用 Windows 沙盒，正在等重启生效（当前读不到功能状态）',
                       '重启电脑后再点“我已重启，重新检测”；重启前不需要再点“启用”。', 'enable')
    message = '无法确认 Windows 沙盒功能状态（DISM 需要管理员权限，WMI 查询也没有给出结果）'
    parts = ['可以照常点“启用 Windows 沙盒”：已启用时 DISM 不会重复安装，只会再确认一次。']
    if not wsb_exe:
        parts.append('另外没找到 WindowsSandbox.exe，可能是家庭版或被精简的系统镜像。')
    return _result('unknown', supported, edition, '', wsb_exe, needs_reboot, 'dism_unavailable',
                   message, ''.join(parts), 'enable')


def _explain_exit_code(code):
    """把退出码翻成中文；同时给出原始十进制与十六进制，方便用户报障。"""
    try:
        value = int(code)
    except (TypeError, ValueError):
        value = -1
    unsigned = value & 0xFFFFFFFF
    key = hex(unsigned).upper().replace('0X', '')
    text = _EXIT_MESSAGES.get(value) or _EXIT_MESSAGES.get(key)
    if text is None:
        text = '启用失败（返回码 ' + str(value) + '）：请把这条日志发给维护者，或在管理员命令行手动执行启用命令'
    return text + '（原始返回码 ' + str(value) + ' / 0x' + format(unsigned, '08X') + '）'


REBOOT_GRACE_SECONDS = 90.0


def _system_boot_time():
    """返回本次开机时刻（epoch 秒）；读不到返回 None。"""
    try:
        import ctypes
        millis = ctypes.windll.kernel32.GetTickCount64()
        return time.time() - float(millis) / 1000.0
    except Exception:
        return None


def _rebooted_since(record):
    """记录写着“待重启”，但电脑在写记录之后已经重启过，这条就不再算数。"""
    if not isinstance(record, dict) or record.get('state') != 'pending_reboot':
        return False
    try:
        updated = float(record.get('updated') or 0)
    except (TypeError, ValueError):
        return False
    if updated <= 0:
        return False
    boot = _system_boot_time()
    if boot is None:
        return False
    return updated + REBOOT_GRACE_SECONDS < boot


def _stamp():
    return time.strftime('%Y%m%d-%H%M%S')


def _default_log_dir():
    return Path(__file__).resolve().parent.parent / 'logs' / LOG_DIR_NAME


def _resolve_log_dir(log_dir):
    folder = Path(log_dir) if log_dir else _default_log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _read_record(path):
    try:
        with Path(path).open('r', encoding='utf-8') as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_record(path, payload):
    """哨兵文件：写失败不影响主流程，只是下次会少一点幂等依据。"""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(str(target) + '.tmp')
        with temp.open('w', encoding='utf-8', newline='') as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        os.replace(temp, target)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _tail_text(path, limit=200000):
    with Path(path).open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        raw = stream.read()
    return _decode_bytes(raw)


def check_elevation_result(log_path):
    """读提权进程写下的日志，判断成功 / 需要重启 / 失败；日志还没生成也给出可操作提示。"""
    try:
        if not log_path:
            return {'ok': False, 'state': 'pending', 'reason': 'log_missing', 'exit_code': None,
                    'log_path': '', 'message': '还没有启用日志：请先点“启用 Windows 沙盒”。'}
        path = Path(str(log_path))
        if not path.is_file():
            return {'ok': False, 'state': 'pending', 'reason': 'log_missing', 'exit_code': None,
                    'log_path': str(path),
                    'message': '还没看到提权日志：管理员授权窗口可能还没点“是”，或被取消；'
                               '请重新点“启用 Windows 沙盒”。'}
        text = _tail_text(path)
    except (OSError, ValueError) as error:
        return {'ok': False, 'state': 'pending', 'reason': 'log_unreadable', 'exit_code': None,
                'log_path': str(log_path or ''), 'message': '读不到启用日志（' + type(error).__name__ + '），请重试。'}
    found = re.search(r'\[whale-dism-exit\]\s*(-?\d+)', text)
    if not found:
        return {'ok': False, 'state': 'pending', 'reason': 'no_result', 'exit_code': None,
                'log_path': str(path),
                'message': '提权进程似乎还在运行（日志里还没有结束标记），请等几秒再点“核对结果”。'}
    try:
        code = int(found.group(1))
    except (TypeError, ValueError):
        code = -1
    if code == 0:
        return {'ok': True, 'state': 'enabled', 'exit_code': 0, 'needs_reboot': False,
                'log_path': str(path),
                'message': 'Windows 沙盒功能已启用成功，可以直接创建隔离通道。'}
    if code in REBOOT_EXIT_CODES:
        return {'ok': True, 'state': 'pending_reboot', 'exit_code': code, 'needs_reboot': True,
                'log_path': str(path),
                'message': '启用成功，但需要重启电脑才生效：重启后再点“我已重启，重新检测”。'}
    return {'ok': False, 'state': 'failed', 'exit_code': code, 'needs_reboot': False,
            'log_path': str(path), 'message': _explain_exit_code(code)}


class ShellElevation:
    """默认提权实现：判断是否已提权，并用 ShellExecuteW('runas') 拉起提权进程。"""

    def is_elevated(self):
        try:
            from . import elevation as elevation_module
            return bool(elevation_module.is_elevated())
        except Exception:
            return False

    def start(self, executable, arguments, workdir=''):
        """返回 {started, code, message}；窗口隐藏，避免 DISM 黑框在桌面上闪。"""
        try:
            import ctypes
            code = int(ctypes.windll.shell32.ShellExecuteW(None, 'runas', str(executable),
                                                           str(arguments), str(workdir or os.getcwd()), 0) or 0)
        except Exception as error:
            return {'started': False, 'code': -1, 'message': '提权请求异常：' + type(error).__name__}
        if code <= 32:
            return {'started': False, 'code': code,
                    'message': '提权请求没有成功（返回码 ' + str(code) + '）：可能在 UAC 弹窗点了“否”'}
        return {'started': True, 'code': code, 'message': '已请求管理员权限'}


DEFAULT_ELEVATION = ShellElevation()


def _is_elevated(elevation):
    provider = elevation if elevation is not None else DEFAULT_ELEVATION
    checker = getattr(provider, 'is_elevated', None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return False


def _start_elevated(elevation, executable, arguments, workdir):
    provider = elevation if elevation is not None else DEFAULT_ELEVATION
    starter = getattr(provider, 'start', None)
    if not callable(starter):
        starter = DEFAULT_ELEVATION.start
    try:
        outcome = starter(executable, arguments, workdir)
    except Exception as error:
        return {'started': False, 'code': -1, 'message': '提权请求异常：' + type(error).__name__}
    if not isinstance(outcome, dict):
        return {'started': False, 'code': -1, 'message': '提权实现返回格式异常'}
    try:
        code = int(outcome.get('code', -1))
    except (TypeError, ValueError):
        code = -1
    return {'started': bool(outcome.get('started')), 'code': code,
            'message': str(outcome.get('message') or '')}


def _enable_command():
    return [DISM, '/online', '/enable-feature', '/featurename:' + FEATURE_NAME, '/all', '/norestart']


def _elevated_batch(log_path):
    """提权进程不回传 stdout，所以让它把 dism 输出和退出码写进日志。"""
    dism = str(Path(os.environ.get('SystemRoot') or r'C:\Windows') / 'System32' / 'dism.exe')
    lines = ['@echo off',
             'chcp 65001>nul',
             '"' + dism + '" /online /enable-feature /featurename:' + FEATURE_NAME + ' /all /norestart >> "' + str(log_path) + '" 2>&1',
             'echo ' + EXIT_MARKER + ' %ERRORLEVEL% >> "' + str(log_path) + '"']
    return chr(13).join(lines) + chr(13) + chr(10)


def _write_batch(path, log_path):
    with Path(path).open('w', encoding='utf-8', newline='') as stream:
        stream.write(_elevated_batch(log_path))


def _pending_elevation(record, record_path, timeout):
    """上一次提权还在跑就别再拉一次；已经出结果就直接把它当成这次的结果。"""
    if not isinstance(record, dict) or record.get('state') != 'elevation_started':
        return None
    log_path = str(record.get('log_path') or '')
    checked = check_elevation_result(log_path)
    if checked['state'] in ('enabled', 'pending_reboot'):
        _write_record(record_path, {'state': checked['state'], 'updated': time.time(),
                                    'log_path': log_path, 'exit_code': checked.get('exit_code'),
                                    'source': 'elevation'})
        return {'state': checked['state'], 'already': True, 'feature': 'Enabled',
                'needs_reboot': checked['state'] == 'pending_reboot',
                'exit_code': checked.get('exit_code'), 'log_path': log_path,
                'message': checked['message']}
    if checked['state'] == 'failed':
        return None  # 上次确实失败了：允许用户重试
    try:
        started_at = float(record.get('started_at') or 0)
    except (TypeError, ValueError):
        started_at = 0.0
    window = min(max(float(timeout), 0.0) or DEFAULT_TIMEOUT, ELEVATION_FRESH_SECONDS)
    if started_at and (time.time() - started_at) < window:
        return {'state': 'elevation_started', 'already': True, 'log_path': log_path,
                'message': '上一次“启用”请求还在进行：请在管理员授权窗口点“是”，'
                           '等日志出现结果再点“核对结果”；不要重复点击。'}
    return None


def _elevated_result(outcome, record_path, log_path):
    code = int(outcome.get('returncode', -1))
    if code == 0:
        _write_record(record_path, {'state': 'enabled', 'updated': time.time(),
                                    'log_path': str(log_path or ''), 'exit_code': 0,
                                    'source': 'elevated'})
        return {'state': 'enabled', 'already': False, 'feature': 'Enabled', 'needs_reboot': False,
                'exit_code': 0, 'log_path': str(log_path or ''),
                'message': 'Windows 沙盒功能已启用成功，可以直接创建隔离通道。'}
    if code in REBOOT_EXIT_CODES:
        _write_record(record_path, {'state': 'pending_reboot', 'updated': time.time(),
                                    'log_path': str(log_path or ''), 'exit_code': code,
                                    'source': 'elevated'})
        return {'state': 'pending_reboot', 'already': False, 'feature': 'Enabled',
                'needs_reboot': True, 'exit_code': code, 'log_path': str(log_path or ''),
                'message': '启用成功，但需要重启电脑才生效（3010）：重启后再点“我已重启，重新检测”。'}
    message = _explain_exit_code(code)
    _write_record(record_path, {'state': 'failed', 'updated': time.time(),
                                'log_path': str(log_path or ''), 'exit_code': code,
                                'source': 'elevated'})
    return {'state': 'failed', 'already': False, 'reason': 'enable_failed', 'exit_code': code,
            'needs_reboot': False, 'log_path': str(log_path or ''), 'message': message}


def enable(runner=None, elevation=None, log_dir=None, timeout=900):
    """启用 Windows 沙盒：已启用直接返回；未提权走 UAC；已提权直接跑 dism。全程幂等。"""
    try:
        return _enable(runner, elevation, log_dir, timeout)
    except Exception as error:
        return {'state': 'failed', 'already': False, 'reason': 'internal',
                'message': '启用流程出现意外错误（' + type(error).__name__ + '），未做任何改动，请重试'}


def _enable(runner, elevation, log_dir, timeout):
    folder = _resolve_log_dir(log_dir)
    record_path = folder / RECORD_NAME
    record = _read_record(record_path)
    probe = detect(runner=runner, last_record=record)
    last_log = str(record.get('log_path') or '')
    if probe['state'] == 'enabled':
        if probe.get('needs_reboot'):
            return {'state': 'pending_reboot', 'already': True, 'feature': 'Enabled',
                    'needs_reboot': True, 'log_path': last_log,
                    'message': '功能已启用，但仍在等待重启生效：请重启电脑后再点“我已重启，重新检测”。'}
        _write_record(record_path, {'state': 'enabled', 'updated': time.time(),
                                    'feature': 'Enabled', 'edition': probe.get('edition', ''),
                                    'source': 'detect'})
        return {'state': 'enabled', 'already': True, 'feature': 'Enabled', 'needs_reboot': False,
                'log_path': last_log,
                'message': 'Windows 沙盒功能已是启用状态，不需要重复启用；可以直接创建隔离通道。'}
    if probe['state'] == 'unsupported':
        return {'state': 'unsupported', 'already': False, 'reason': 'unsupported_edition',
                'needs_reboot': False, 'log_path': '',
                'message': probe['message'], 'hint': probe['hint']}
    pending = _pending_elevation(record, record_path, timeout)
    if pending is not None:
        return pending
    if _is_elevated(elevation):
        outcome = _call_runner(runner, _enable_command(), timeout)
        return _elevated_result(outcome, record_path, '')
    stamp = _stamp()
    log_path = folder / ('enable-sandbox-' + stamp + '.log')
    batch_path = folder / ('enable-sandbox-' + stamp + '.cmd')
    try:
        _write_batch(batch_path, log_path)
    except OSError as error:
        return {'state': 'failed', 'already': False, 'reason': 'log_unwritable',
                'log_path': str(log_path),
                'message': '写不了提权脚本（' + type(error).__name__ + '）：请检查日志目录是否可写'}
    comspec = os.environ.get('ComSpec') or r'C:\Windows\System32\cmd.exe'
    arguments = '/c ""' + str(batch_path) + '""'
    outcome = _start_elevated(elevation, comspec, arguments, str(folder))
    if not outcome['started']:
        return {'state': 'failed', 'already': False, 'reason': 'elevation_denied',
                'exit_code': outcome.get('code'), 'needs_reboot': False, 'log_path': str(log_path),
                'message': outcome['message'] + '；也可以右键“以管理员身份运行”控制台后重试'}
    _write_record(record_path, {'state': 'elevation_started', 'pid': os.getpid(),
                                'started_at': time.time(), 'log_path': str(log_path),
                                'batch_path': str(batch_path), 'timeout': float(timeout),
                                'feature': FEATURE_NAME, 'source': 'uac'})
    return {'state': 'elevation_started', 'already': False, 'log_path': str(log_path),
            'batch_path': str(batch_path),
            'message': '已弹出管理员授权窗口：点“是”后等它跑完，再点“核对结果”。启用成功通常还需要重启电脑。'}


def _last_enable_view(record):
    if not isinstance(record, dict) or not record:
        return {}
    view = {'state': str(record.get('state') or ''), 'log_path': str(record.get('log_path') or '')}
    for name in ('exit_code', 'updated', 'started_at'):
        value = record.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            view[name] = value
    return view


def status(runner=None, registry_reader=None, log_dir=None):
    """把探测结果和上次启用记录合成一句话状态，并给出下一步该按哪个按钮。"""
    try:
        folder = _resolve_log_dir(log_dir)
        record = _read_record(folder / RECORD_NAME)
        probe = detect(runner=runner, registry_reader=registry_reader, last_record=record)
    except Exception as error:
        return {'ok': False, 'state': 'unknown', 'supported': True, 'edition': '', 'feature': '',
                'wsb_exe': False, 'needs_reboot': False, 'reason': 'status_exception',
                'next_step': 'enable', 'log_path': '', 'last_enable': {},
                'message': '读取沙盒状态时出现意外错误（' + type(error).__name__ + '），已按未知处理',
                'hint': '可以照常点“启用 Windows 沙盒”，DISM 对已启用的机器不会重复安装。'}
    needs_reboot = bool(probe.get('needs_reboot'))
    state = probe['state']
    wsb_exe = bool(probe.get('wsb_exe'))
    if state == 'enabled':
        next_step = 'create_channel'
    elif state == 'unsupported':
        next_step = 'none'
    elif not wsb_exe and state in ('disabled', 'absent'):
        # 1.3.82：Windows 10 实测只带 WindowsSandbox.exe、没有 wsb.exe 命令行。这种机器就算把
        # 功能启用也建不了隔离通道，再引导用户去点“启用”只是白折腾一次重启。
        next_step = 'manual'
    else:
        next_step = 'enable'
    message = probe['message']
    hint = probe['hint']
    if state in ('disabled', 'absent') and not wsb_exe:
        edition = str(probe.get('edition') or '')
        where = '这台机器（' + edition + '）' if edition else '这台机器'
        message = (where + '没有 wsb.exe 命令行（Windows 10 只带 WindowsSandbox.exe），'
                   '自动建隔离通道用不了，启用沙盒也解决不了；请直接用本机模式')
        hint = _no_cli_hint()
    if needs_reboot and state != 'unsupported':
        message += '；建议重启电脑一次后再用，也可以先点“创建隔离通道”试试'
    return {'ok': True, 'state': state, 'supported': probe['supported'],
            'edition': probe['edition'], 'feature': probe['feature'], 'wsb_exe': probe['wsb_exe'],
            'needs_reboot': needs_reboot, 'reason': probe['reason'], 'next_step': next_step,
            'message': message, 'hint': hint, 'log_path': str(record.get('log_path') or ''),
            'last_enable': _last_enable_view(record)}


def rollback_text():
    """给用户一段可直接复制到管理员命令行执行的禁用命令与说明。"""
    return (
        '关闭 Windows 沙盒功能（需要管理员权限，在“以管理员身份运行”的命令提示符里执行）：' + chr(10) +
        DISM + ' /online /disable-feature /featurename:' + FEATURE_NAME + ' /norestart' + chr(10) + chr(10) +
        '说明：' + chr(10) +
        '1. /norestart 表示不自动重启；禁用同样要重启一次才彻底生效。' + chr(10) +
        '2. 禁用前请先在控制台把微信切回本机模式，并关闭沙盒窗口。' + chr(10) +
        '3. 禁用不会删除已经建好的隔离通道目录，重新启用后可以继续用。' + chr(10) +
        '4. 家庭版本来就没有这个功能，执行会报 0x800f0805，属于正常现象。')


def _default_manager():
    from . import sandbox_manager
    return sandbox_manager


def _resolve_root(runtime):
    root = getattr(runtime, 'root', None)
    if root:
        return Path(root)
    return Path(__file__).resolve().parent.parent / 'data'


def _resolve_app_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent / 'dist/WhaleGirlWeChat'


def _pick_sandbox(module, runtime):
    """优先用控制台记录过的沙盒 id，否则取当前唯一在跑的沙盒。"""
    config = getattr(runtime, 'config', None)
    try:
        configured = str(config.get('wechat.sandbox_id', '') or '') if isinstance(config, dict) else ''
    except Exception:
        configured = ''
    if configured:
        return configured
    lister = getattr(module, 'list_sandboxes', None)
    if not callable(lister):
        return ''
    try:
        rows = lister() or []
    except Exception:
        return ''
    for row in rows:
        ident = row.get('id') if isinstance(row, dict) else None
        if ident:
            return str(ident)
    return ''


def _normalise_prepare(result, sandbox_id):
    if not isinstance(result, dict):
        return {'ok': False, 'state': 'failed', 'reason': 'internal', 'sandbox_id': sandbox_id,
                'channel_id': '', 'stage': '', 'message': '准备通道的返回格式异常（不是字典）'}
    channel_id = str(result.get('channel_id') or '')
    ok = bool(result.get('success', bool(channel_id)))
    message = str(result.get('message') or '')
    if not message:
        message = ('隔离通道已准备；请启用此通道后再启动机器人' if ok else '隔离通道没有准备完成，请重试')
    return {'ok': ok, 'state': 'ready' if ok else 'failed',
            'sandbox_id': str(result.get('sandbox_id') or sandbox_id),
            'channel_id': channel_id, 'stage': str(result.get('stage') or ''),
            'message': message, 'reason': '' if ok else 'prepare_failed'}


def create_channel(runtime_or_none=None, manager=None, runner=None):
    """创建隔离通道：先确认功能已启用，再调用 sandbox_manager 的准备流程。"""
    try:
        return _create_channel(runtime_or_none, manager, runner)
    except Exception as error:
        return {'ok': False, 'state': 'failed', 'reason': 'internal', 'sandbox_id': '',
                'channel_id': '', 'stage': '',
                'message': '创建隔离通道时出现意外错误（' + type(error).__name__ + '），未做任何改动'}


def _create_channel(runtime_or_none, manager, runner):
    state = status(runner=runner)
    if state['state'] == 'unsupported':
        return {'ok': False, 'state': 'unsupported', 'reason': 'unsupported_edition',
                'sandbox_id': '', 'channel_id': '', 'stage': '', 'message': state['message']}
    if state.get('needs_reboot'):
        return {'ok': False, 'state': 'needs_reboot', 'reason': 'needs_reboot',
                'sandbox_id': '', 'channel_id': '', 'stage': '',
                'message': 'Windows 沙盒功能已启用但需要重启电脑才生效：重启后再点“创建隔离通道”'}
    if state['state'] != 'enabled':
        if not state.get('wsb_exe'):
            # 没有 wsb.exe 的机器（Windows 10 19045 实测）根本没有自动建通道的能力，
            # 让用户去“启用沙盒再重启”只会再来一次失望。
            edition = str(state.get('edition') or '')
            where = '这台机器（' + edition + '）' if edition else '这台机器'
            return {'ok': False, 'state': 'no_cli', 'reason': 'no_cli',
                    'sandbox_id': '', 'channel_id': '', 'stage': '',
                    'message': where + '没有 wsb.exe 命令行（Windows 10 只带 WindowsSandbox.exe），'
                               '建不了隔离通道；请改用本机模式'}
        return {'ok': False, 'state': 'needs_enable', 'reason': 'needs_enable',
                'sandbox_id': '', 'channel_id': '', 'stage': '',
                'message': '请先启用 Windows 沙盒功能，并重启电脑后再创建隔离通道'}
    module = manager if manager is not None else _default_manager()
    prepare = getattr(module, 'prepare_sandbox', None) or getattr(module, 'prepare', None)
    if not callable(prepare):
        return {'ok': False, 'state': 'failed', 'reason': 'internal', 'sandbox_id': '',
                'channel_id': '', 'stage': '', 'message': '隔离通道模块没有提供准备接口，无法创建通道'}
    sandbox_id = _pick_sandbox(module, runtime_or_none)
    if not sandbox_id:
        return {'ok': False, 'state': 'no_sandbox', 'reason': 'no_sandbox', 'sandbox_id': '',
                'channel_id': '', 'stage': '',
                'message': '没有检测到正在运行的 Windows 沙盒：请先打开沙盒并在其中登录微信，再回来创建通道'}
    result = prepare(_resolve_root(runtime_or_none), _resolve_app_dir(), sandbox_id)
    return _normalise_prepare(result, sandbox_id)


__all__ = ['FEATURE_NAME', 'SUPPORTED_EDITIONS', 'REBOOT_EXIT_CODES', 'detect', 'enable',
           'check_elevation_result', 'status', 'rollback_text', 'create_channel',
           'ShellElevation', 'DEFAULT_ELEVATION']
