"""仅修复首次通道共享之前的明确拒绝；归档旧 ACL，不接管或重试未知映射。"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

from .private_directory import create_private_directory
from .runtime_identity import APP_VERSION

REPAIRABLE_REASON = '共享程序在首次通道映射前被拒绝，可显式修复目录并保留原配对'
BLOCKED_REASON = '目录修复尚未确认完成；保留归档和日志，不能重复映射或启动'
FILES = frozenset(('pairing.json', 'session.json', 'recovery.json', 'program-mappings.json'))
FOLDERS = frozenset(('requests', 'responses', 'consumed', 'events', 'control', 'ocr'))
STAGES = frozenset(('preflight', 'repair_staging', 'repair_archiving', 'repair_installing',
    'mapping_program', 'mapping_channel', 'channel_mapped', 'launching', 'verifying',
    'committing', 'ready', 'failed'))


def _manager():
    # manager 只在入口导入本模块，避免协议及 GUI 初始化时形成循环。
    from . import sandbox_manager
    return sandbox_manager


def _journal_root(root):
    m = _manager()
    return m._reject_links(Path(root) / 'data' / 'isolated_wechat_repairs')


def _journal_path(root, ident):
    m = _manager()
    if not m.CHANNEL_ID.fullmatch(str(ident or '')):
        raise m.SandboxError('目录修复通道编号无效')
    return m._reject_links(_journal_root(root) / ident / 'journal.json')


def _journal(root, ident):
    m = _manager()
    path = _journal_path(root, ident)
    # 目录先于 journal 创建；这段崩溃空隙同样必须视为未完成，不能当成无记录。
    if not path.parent.exists():
        return None
    value = m._read_small(path, limit=64 * 1024)
    if (type(value.get('version')) is not int or value['version'] != 1
            or value.get('channel_id') != ident
            or not isinstance(value.get('stage'), str) or value['stage'] not in STAGES
            or m._sandbox_id(value.get('sandbox_id')) != value.get('sandbox_id')
            or not isinstance(value.get('program_key'), str)
            or not m.PROGRAM_KEY.fullmatch(value['program_key'])
            or not isinstance(value.get('app_version'), str)
            or not re.fullmatch(r'\d+\.\d+\.\d+', value['app_version'])
            or type(value.get('created')) not in (int, float)
            or not math.isfinite(value['created'])
            or type(value.get('rpc_requests')) is not int or value['rpc_requests'] != 0
            or value.get('initial_failed_operation') != 'share_program'
            or value.get('initial_hresult') != '0x80070005'
            or type(value.get('initial_returncode')) is not int or value['initial_returncode'] != 2147942405
            or not isinstance(value.get('current_operation'), str)
            or value['current_operation'] not in m.RECOVERY_OPERATIONS
            or not isinstance(value.get('original_sha256'), dict)
            or set(value['original_sha256']) != FILES
            or any(not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                for digest in value['original_sha256'].values())
            or not isinstance(value.get('archive_name'), str)
            or not re.fullmatch(r'\.acl-archive-' + ident + r'-[0-9a-f]{32}', value['archive_name'])):
        raise m.SandboxError(BLOCKED_REASON)
    return value


def check_channel(root, ident):
    """启用/连接入口不能穿过尚未原子交接的同一路径。"""
    m = _manager()
    try:
        record = _journal(root, ident)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        raise m.SandboxError(BLOCKED_REASON) from None
    if record is not None and record['stage'] != 'ready':
        raise m.SandboxError(BLOCKED_REASON)
    if record is not None:
        canonical = m._reject_links(m._channel_root(root) / ident)
        try:
            session = m._read_small(canonical / 'session.json')
            if (session.get('stage') != 'prepared' or session.get('channel_id') != ident
                    or session.get('sandbox_id') != record['sandbox_id']):
                raise m.SandboxError(BLOCKED_REASON)
            m._session_launch_key(session)
            m._session_channel_key(session)
        except (OSError, ValueError, RuntimeError):
            raise m.SandboxError(BLOCKED_REASON) from None


def check_pending(root):
    """全局保守阻断：损坏或缺少 canonical 的日志不能使 prepare 另造身份。"""
    m = _manager()
    folder = _journal_root(root)
    if not folder.exists():
        return
    if not folder.is_dir():
        raise m.SandboxError(BLOCKED_REASON)
    for path in folder.iterdir():
        m._reject_links(path)
        if not m.CHANNEL_ID.fullmatch(path.name) or not path.is_dir():
            raise m.SandboxError(BLOCKED_REASON)
        check_channel(root, path.name)


def status_rows(root, running):
    """缺 canonical 时仍展示外部崩溃守卫；不披露原始日志或目录。"""
    m = _manager()
    folder = _journal_root(root)
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.iterdir()):
        if not m.CHANNEL_ID.fullmatch(path.name):
            continue
        try:
            record = _journal(root, path.name)
            if record['stage'] == 'ready':
                try:
                    check_channel(root, path.name)
                except (OSError, ValueError, RuntimeError):
                    pass
                else:
                    continue
            value = {'channel_id': path.name, 'short_id': path.name[:12],
                'sandbox_id': record['sandbox_id'], 'app_version': record['app_version'],
                'stage': 'failed', 'agent_online': False, 'wechat_online': False,
                'reason': BLOCKED_REASON}
            value.update(diagnostic(root, path.name, {'stage': 'failed'}))
            rows.append(value)
        except (OSError, ValueError, RuntimeError, TypeError, KeyError):
            # 无法验证实例编号就不制造可选项；check_pending 仍阻止一切新映射。
            continue
    return rows


def diagnostic(root, ident, session):
    """外部 guard 的后续失败比保留的旧 recovery 优先显示。"""
    m = _manager()
    record = _journal(root, ident)
    if record is None:
        return None
    ready = record['stage'] == 'ready' and session.get('stage') == 'prepared'
    value = {'recovery_state': 'ready' if ready else 'blocked',
        'recovery_reason': '配对恢复已经完成' if ready else BLOCKED_REASON,
        'recovery_stage': record['stage'], 'initial_failed_operation': 'share_program',
        'initial_hresult': '0x80070005', 'initial_returncode': 2147942405}
    operation = record.get('failed_operation', record.get('current_operation'))
    if isinstance(operation, str) and operation in m.RECOVERY_OPERATIONS:
        value['recovery_failed_operation'] = operation
    failure = record.get('failure')
    if isinstance(failure, dict):
        if type(failure.get('returncode')) is int:
            value['recovery_returncode'] = failure['returncode']
        code = failure.get('hresult')
        if isinstance(code, str) and re.fullmatch(r'0x[0-9A-F]{8}', code):
            value['recovery_hresult'] = code
    return value


def _denied_before_channel(value):
    failure = value.get('failure')
    return (value.get('stage') == 'failed' and value.get('failed_operation') == 'share_program'
        and 'channel_mapping' not in value and 'launch_started' not in value
        and isinstance(failure, dict) and failure.get('operation') == 'share'
        and failure.get('hresult') == '0x80070005'
        and type(failure.get('returncode')) is int and failure['returncode'] == 2147942405)


def _snapshot(path):
    m = _manager()
    result = {}
    for item in path.iterdir():
        m._reject_links(item)
        if item.name in FILES and item.is_file():
            with item.open('rb') as stream:
                raw = stream.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise m.SandboxError('目录修复元数据超过界限，原目录保留')
            result[item.name] = raw
        elif item.name in FOLDERS and item.is_dir() and not any(item.iterdir()):
            continue
        else:
            raise m.SandboxError('目录修复发现业务、消费或未知文件，原目录保留')
    if set(result) != FILES:
        raise m.SandboxError('目录修复原始证据不完整，原目录保留')
    return result


def eligible(root, path, original):
    """状态页不散列旧程序树；原始受限元数据、无业务事实仍须完整一致。"""
    m = _manager()
    if _journal_path(root, path.name).parent.exists():
        raise m.SandboxError(BLOCKED_REASON)
    record = m._read_small(m._reject_links(path / 'recovery.json'), limit=64 * 1024)
    if (not _denied_before_channel(original) or not _denied_before_channel(record)
            or type(original.get('version')) is not int or original['version'] != 1
            or type(record.get('version')) is not int or record['version'] != 1
            or original.get('channel_id') != path.name or record.get('channel_id') != path.name
            or record.get('sandbox_id') != original.get('sandbox_id')
            or record.get('original_session') != original
            or type(record.get('rpc_requests')) is not int or record['rpc_requests'] != 0
            or record.get('current_operation') != 'share_program'
            or record.get('original_service_evidence') != 'no_service_no_business'
            or record.get('app_version') == APP_VERSION):
        raise m.SandboxError('此失败配对不能按首次共享拒绝修复，原证据保留')
    m._session_launch_key(original)
    m._session_channel_key(original)
    m._sandbox_id(original.get('sandbox_id'))
    key = record.get('program_key')
    registry = m._read_program_registry(path, original['sandbox_id'])
    mapping = registry['mappings'].get(key)
    if (not isinstance(key, str) or not m.PROGRAM_KEY.fullmatch(key)
            or not mapping or mapping['stage'] != 'uncertain'
            or mapping['app_version'] != record.get('app_version')):
        raise m.SandboxError('原程序共享证据不符，未修复目录')
    snapshot = _snapshot(path)
    return record, registry, snapshot


def _guest_preflight(sandbox_id, ident, program_key):
    """固定只读命令；没有通道时不能借用未核验回执或把密钥放入命令。"""
    m = _manager()
    derived = m._build_guest_channel(ident)
    app = m._build_guest_app(program_key)
    code = r'''
# WHALE_ACL_REPAIR_PREFLIGHT
$ErrorActionPreference='Stop'
if ($env:USERNAME -cne 'WDAGUtilityAccount') { exit 2 }
$rows=@(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
if (-not $rows.Count -or @($rows | Where-Object { -not $_.Name }).Count) { exit 3 }
$agents=@($rows | Where-Object {
 $_.Name -ieq 'WhaleGirlWeChat.exe' -or
 ($_.CommandLine -and $_.CommandLine -match '(?i)--(?:sandbox-agent(?:-launch)?|wechat-send-worker)(?:\s|$)')
})
if ($agents.Count -ne 0) { exit 4 }
if ((Test-Path -LiteralPath 'C:\WhaleChannel' -ErrorAction Stop) -or
    (Test-Path -LiteralPath '__DERIVED__' -ErrorAction Stop) -or
    (Test-Path -LiteralPath '__APP__' -ErrorAction Stop)) { exit 5 }
exit 0
'''.replace('__DERIVED__', derived).replace('__APP__', app)
    encoded = base64.b64encode(code.encode('utf-16le')).decode('ascii')
    command = (r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe '
        '-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand ' + encoded)
    value = m._run_wsb(['exec', '--id', sandbox_id, '--run-as', 'ExistingLogin', '--command', command], timeout=25)
    if type(value.get('ExitCode')) is not int or value['ExitCode'] != 0:
        raise m.SandboxError('首次通道共享前的只读证据不足，未归档或映射', operation='exec',
            guest_exit_code=value.get('ExitCode') if type(value.get('ExitCode')) is int else None)


def _same_snapshot(path, expected):
    if _snapshot(path) != expected:
        raise _manager().SandboxError('目录修复期间原始证据改变，未继续映射或启动')


def repair(root, app_dir, sandbox_id, path, original, pairing):
    """调用方持有生命周期与跨进程锁；此函数从不启用通道或发送。"""
    m = _manager()
    from .isolated_wechat.protocol import load_pairing
    check_pending(root)
    record, registry, snapshot = eligible(root, path, original)
    app_dir, key = m._prepare_program(root, app_dir)
    if key in registry['mappings'] or key in (original.get('program_key'), record['program_key']):
        raise m.SandboxError('目录修复必须使用此前从未映射的新构建，未重复共享')
    m._session_probe(sandbox_id)
    _guest_preflight(sandbox_id, path.name, key)
    _same_snapshot(path, snapshot)
    repairs = _journal_root(root)
    if not repairs.exists():
        create_private_directory(repairs)
    m._reject_links(repairs)
    journal_dir = m._reject_links(repairs / path.name)
    create_private_directory(journal_dir)
    journal_path = journal_dir / 'journal.json'
    nonce = uuid.uuid4().hex
    archive = m._reject_links(path.parent / ('.acl-archive-' + path.name + '-' + nonce))
    stage = m._reject_links(path.parent / ('.acl-stage-' + path.name + '-' + nonce))
    if archive.exists() or stage.exists() or archive.parent != path.parent or stage.parent != path.parent:
        raise m.SandboxError('目录修复临时路径边界异常，未归档原目录')
    journal = {'version': 1, 'channel_id': path.name, 'sandbox_id': sandbox_id,
        'app_version': APP_VERSION, 'program_key': key, 'archive_name': archive.name,
        'created': time.time(), 'stage': 'preflight', 'current_operation': 'repair_preflight',
        'rpc_requests': 0, 'initial_failed_operation': 'share_program',
        'initial_hresult': '0x80070005', 'initial_returncode': 2147942405,
        'original_sha256': {name: hashlib.sha256(raw).hexdigest() for name, raw in snapshot.items()}}
    with journal_path.open('x', encoding='utf-8') as stream:
        json.dump(journal, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    operation = 'stage_channel'
    new_pairing = None
    try:
        journal.update(stage='repair_staging', current_operation=operation)
        m._write_session(journal_path, journal)
        create_private_directory(stage)
        for name, raw in snapshot.items():
            with (stage / name).open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        staged_pairing = load_pairing(stage)
        if dict(staged_pairing) != dict(pairing):
            raise m.SandboxError('目录修复配对身份不符，未归档原目录')
        _same_snapshot(stage, snapshot)
        _same_snapshot(path, snapshot)
        operation = 'archive_channel'
        journal.update(stage='repair_archiving', current_operation=operation)
        m._write_session(journal_path, journal)
        path.rename(archive)
        operation = 'install_channel'
        journal.update(stage='repair_installing', current_operation=operation)
        m._write_session(journal_path, journal)
        stage.rename(path)
        _same_snapshot(archive, snapshot)
        _same_snapshot(path, snapshot)
        new_pairing = load_pairing(path)
        operation = 'share_program'
        journal.update(stage='mapping_program', current_operation=operation)
        m._write_session(journal_path, journal)
        m._share_build_program(path, sandbox_id, app_dir, key)
        operation = 'share_channel'
        journal.update(stage='mapping_channel', current_operation=operation, channel_mapping='uncertain')
        m._write_session(journal_path, journal)
        m._run_wsb(['share', '--id', sandbox_id, '--host-path', str(path),
            '--sandbox-path', m._build_guest_channel(path.name), '--allow-write'])
        journal.update(stage='channel_mapped', channel_mapping='mapped')
        m._write_session(journal_path, journal)
        operation = 'process_probe'
        journal['process_probe'] = m._recovery_process_probe(new_pairing, sandbox_id)
        m._recovery_idle_channel(new_pairing)
        if (path / 'service.json').exists():
            raise m.SandboxError('目录修复期间代理状态已改变，未启动代理')
        operation = 'launch'
        started = time.time()
        journal.update(stage='launching', current_operation=operation, launch_started=started)
        m._write_session(journal_path, journal)
        m._launch(sandbox_id, program_key=key, channel_id=path.name)
        operation = 'verify_heartbeat'
        journal.update(stage='verifying', current_operation=operation)
        m._write_session(journal_path, journal)
        m._wait_upgrade_agent(new_pairing, '', key, minimum_heartbeat=started)
        operation = 'commit_session'
        _same_snapshot(archive, snapshot)
        for name in ('pairing.json', 'session.json', 'recovery.json'):
            if (path / name).read_bytes() != snapshot[name]:
                raise m.SandboxError('目录修复原始身份或恢复证据改变，未登记成功')
        journal.update(stage='committing', current_operation=operation)
        m._write_session(journal_path, journal)
        updated = dict(original, app_version=APP_VERSION, program_key=key,
            program_mapping='by_build', channel_mapping='by_channel', stage='prepared', repaired_at=time.time())
        m._write_session(path / 'session.json', updated)
        # 外部守卫最后提交；写入失败时 prepared 也不能绕过 guard 被激活。
        journal.update(stage='ready', current_operation='complete', completed=time.time())
        m._write_session(journal_path, journal)
    except BaseException as error:
        if 'launch_started' in journal and new_pairing is not None:
            m._cancel_upgrade_candidate(new_pairing, '', key)
        journal.update(stage='failed', failed_operation=operation, failure=m._error_scalars(error))
        try:
            m._write_session(journal_path, journal)
        except (OSError, ValueError, RuntimeError):
            pass
        raise
    return {'success': True, 'channel_id': path.name, 'sandbox_id': sandbox_id,
        'stage': 'agent_ready', 'message': '目录访问已修复，原配对及归档保留；微信登录保留，机器人保持停止，未发送消息'}
