"""数据目录守卫：可写探测与回退、升级保数据自检、隔离通道残留盘点与显式清理。

这三件事都只在「换机器 / 覆盖升级 / 清理旧残留」这种低频但一错就很贵的时刻生效，
所以集中在一个模块里，工作方式也只有一条：先只读地判断、如实报告，再由人显式决定。

硬规则（动这个文件前先读）：
1. 回退只发生在「确实写不进去」的时候，而且绝不移动/删除主目录里的任何文件 ——
   旧数据留在原地，接口同时给出两处路径，用户不会以为自己几年的聊天记录消失了。
2. 改名（原子替换）失败不触发回退，只作为「可写但不能原子替换」如实报告；
   那一档由 persistence.atomic_text 就地重写兜底，详见 persistence.probe_directory。
3. 自检只读：不建库、不迁移、不修剪历史。
4. 清理残留通道目录必须显式二次确认，范围硬编码在 data/isolated_wechat 下的单个
   32 位通道编号目录，绝不递归删除它的父目录。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from . import persistence


# 探测整体墙钟上限：要求是「不得阻塞启动超过 3 秒」，这里取 2 秒，留 1 秒给上传和日志。
PROBE_SECONDS = 2.0
# 备用目录名固定，不随版本/机器变化：用户按提示就能找到，重装后也还是同一个。
FALLBACK_DIR_NAME = 'WhaleGirlWeChat-fallback'
CONFIG_NAMES = ('config.wechat.yaml', 'config.yaml')
DB_NAMES = ('data/wechat-only/messages.sqlite3', 'messages.sqlite3')
PLUGIN_SETTINGS = 'data/wechat-only/plugin_settings.json'
HISTORY_TABLE = 'conversation_log'
STYLE_DIRS = ('styles', 'styles/custom')

# ── 覆盖升级 / 回滚（1.3.51 批3）──────────────────────────────────────────────
EXE_NAME = 'WhaleGirlWeChat.exe'
PREV_EXE_NAME = 'WhaleGirlWeChat.prev.exe'
# 脚本名固定成一个：同名就覆盖，绝不生成第二个不同名字的回滚脚本。
# 名字也保持 ASCII —— 脚本正文必须全 ASCII，文件名跟着一起干净。
ROLLBACK_SCRIPT_NAME = 'rollback-prev.cmd'
# 生成脚本的二次确认口令：界面必须原样提交，差一个字就不生成。
ROLLBACK_CONFIRM = '已停止微信发送并愿意重启控制台'

# ── 旧版本残留（只统计，绝不删）────────────────────────────────────────────────
RESIDUE_LIMIT = 200        # 超过这么多条只报「至少 200 项」，不在界面上滚好几屏
RESIDUE_WALK_FILES = 500   # 残留目录内部最多数这么多个文件，免得一个大目录把自检拖死
RESIDUE_SAMPLES = 5        # 明细只留前几项，够用户认出来就行


def _now():
    return datetime.now().isoformat(timespec='seconds')


def _error_text(error):
    """把异常压成一句中文可读文本；WinError 才是用户在 Windows 上真正需要的东西。"""
    text = str(error) or type(error).__name__
    code = getattr(error, 'winerror', None)
    return text if code is None else f'{text}（WinError {code}）'


def _default_root():
    # 复用 site_profile 的同一套判定，避免守卫和 /api/env 报出两个不同的「数据目录」。
    from .site_profile import data_root
    return data_root()


def fallback_dirs(primary):
    """备用目录的固定顺序：同盘 -fallback 优先，其次用户目录。

    为什么固定顺序：可预测、可幂等，用户按提示找得到，换版本也不会跳来跳去。
    为什么不选 %TEMP%：临时目录会被清理工具删，配置和历史放那里等于埋雷。
    为什么不去搬旧数据：搬动/删除用户数据比「看起来干净」重要得多。
    """
    primary = Path(primary)
    home = Path.home()
    local = Path(os.environ.get('LOCALAPPDATA') or (home / 'AppData' / 'Local'))
    options = []
    if primary.parent == local:
        options.append(local / FALLBACK_DIR_NAME)
    else:
        options.append(primary.parent / (primary.name + '-fallback'))
    user_profile = Path(os.environ.get('USERPROFILE') or home)
    options.append(user_profile / 'WhaleGirlWeChat')
    unique = []
    for item in options:
        if item != primary and item not in unique:
            unique.append(item)
    return unique


# 探测线程登记表：超时之后 _run_with_deadline 起的线程还在数据根里写 .write-probe-*.tmp，
# 关停时得能等到它（见 join_startup_probe），否则紧接着删临时目录就会撞 WinError 32。
_PROBE_LOCK = threading.Lock()
_LIVE_PROBES = set()


def _run_with_deadline(worker, seconds):
    """带墙钟上限地跑一次探测；返回 (结果, 失败标记)。超时不等于不可写，绝不据此换目录。"""
    box = {}

    def run():
        try:
            box['value'] = worker()
        except BaseException as error:  # 探测自己炸了要能报告，而不是把线程和结果一起吞掉
            box['error'] = error

    thread = threading.Thread(target=run, daemon=True, name='data-root-probe')
    thread.start()
    with _PROBE_LOCK:
        _LIVE_PROBES.add(thread)
    thread.join(seconds)
    with _PROBE_LOCK:
        if not thread.is_alive():
            _LIVE_PROBES.discard(thread)
    if thread.is_alive():
        return None, 'timeout'
    if 'error' in box:
        return None, box['error']
    return box.get('value'), None


def _decide(primary, probe):
    check = probe or persistence.probe_directory
    report = dict(check(primary) or {})
    decision = {'primary': str(primary), 'active': str(primary), 'fallback_used': False,
                'writable': report.get('writable') is True,
                'atomic_replace': report.get('atomic_replace') is True,
                'reason': str(report.get('reason') or ''), 'candidates': [], 'checked_at': _now()}
    if decision['writable']:
        return decision
    primary_reason = decision['reason'] or '写入探测失败（原因未知）'
    for candidate in fallback_dirs(primary):
        try:
            outcome = dict(check(candidate) or {})
        except Exception as error:
            outcome = {'writable': False, 'reason': _error_text(error)}
        entry = {'path': str(candidate), 'writable': outcome.get('writable') is True,
                 'reason': str(outcome.get('reason') or '')}
        decision['candidates'].append(entry)
        if entry['writable']:
            decision.update(active=str(candidate), fallback_used=True,
                            reason=(f'主数据目录写不进去（{primary_reason}）；本轮改用备用目录 '
                                    f'{candidate}。原目录 {primary} 里的配置和聊天记录没有被移动或'
                                    '删除，修好权限后改回来即可'))
            return decision
    decision['reason'] = (f'主数据目录写不进去（{primary_reason}），备用目录也写不进去；'
                          f'请检查磁盘空间和安全软件，或把 {primary} 的权限改回可写后重启控制台')
    return decision


def resolve_data_root(primary=None, *, probe=None, seconds=PROBE_SECONDS):
    """探测主数据目录；确实写不进去时才选一个可写的备用目录。

    返回 primary/active/fallback_used/writable/atomic_replace/reason/candidates。
    seconds 是整轮探测的墙钟上限：超时按「未确认」处理，不切换目录（fail-safe）。
    """
    target = Path(primary) if primary is not None else _default_root()
    value, failure = _run_with_deadline(lambda: _decide(target, probe), seconds)
    if failure == 'timeout':
        return {'primary': str(target), 'active': str(target), 'fallback_used': False,
                'writable': None, 'atomic_replace': None, 'candidates': [], 'checked_at': _now(),
                'reason': f'数据目录可写性探测在 {seconds:g} 秒内没有结果；本轮按未确认处理，未切换目录'}
    if failure is not None:
        return {'primary': str(target), 'active': str(target), 'fallback_used': False,
                'writable': None, 'atomic_replace': None, 'candidates': [], 'checked_at': _now(),
                'reason': f'数据目录探测出错（{_error_text(failure)}）；未切换目录'}
    return value


def _check_config(root):
    for name in CONFIG_NAMES:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except OSError as error:
            return {'status': 'broken', 'path': name, 'value': None,
                    'message': f'配置文件读不出来（{_error_text(error)}）'}
        try:
            import yaml
            value = yaml.safe_load(text)
        except Exception as error:  # 解析失败必须如实报出来，不能吞成「没有配置」
            return {'status': 'broken', 'path': name, 'value': None,
                    'message': f'配置文件解析失败：{_error_text(error)}；可恢复同目录的 {name}.bak'}
        keys = len(value) if isinstance(value, (dict, list, tuple)) else 0
        return {'status': 'ok', 'path': name, 'value': keys,
                'message': f'配置可解析（{keys} 个顶层配置项）'}
    return {'status': 'missing', 'path': '', 'value': None,
            'message': '还没有配置文件（全新安装会自己生成；覆盖升级后缺它说明数据没跟过来）'}


def _check_history(root):
    for name in DB_NAMES:
        path = root / name
        if not path.is_file():
            continue
        try:
            # mode=ro：自检绝不能顺手建库或写 -wal，只读打开才可能真的只读。
            connection = sqlite3.connect('file:' + path.as_posix() + '?mode=ro',
                                         uri=True, timeout=2.0)
        except Exception as error:
            return {'status': 'broken', 'path': name, 'value': None,
                    'message': f'历史数据库打不开（{_error_text(error)}）'}
        try:
            rows = connection.execute('SELECT count(*) FROM ' + HISTORY_TABLE).fetchone()[0]
        except sqlite3.DatabaseError as error:
            # 「表还不存在」是全新建库后的正常形态；文件损坏才是真问题，必须报 broken 而不是 0 行。
            if 'no such table' in str(error).lower():
                return {'status': 'ok', 'path': name, 'value': 0,
                        'message': f'数据库里还没有 {HISTORY_TABLE} 表（还没收过消息）'}
            return {'status': 'broken', 'path': name, 'value': None,
                    'message': f'历史数据库读不出来（{_error_text(error)}）'}
        finally:
            connection.close()
        return {'status': 'ok', 'path': name, 'value': int(rows),
                'message': f'历史表 {HISTORY_TABLE} 有 {int(rows)} 行'}
    return {'status': 'missing', 'path': '', 'value': None,
            'message': '还没有历史数据库（全新安装正常；覆盖升级后缺它说明聊天记录没跟过来）'}


def _check_plugin_settings(root):
    path = root / PLUGIN_SETTINGS
    if not path.is_file():
        return {'status': 'missing', 'path': PLUGIN_SETTINGS, 'value': None,
                'message': '还没有插件设置文件（全新安装正常）'}
    try:
        import json
        value = json.loads(path.read_text(encoding='utf-8-sig') or '{}')
    except Exception as error:
        return {'status': 'broken', 'path': PLUGIN_SETTINGS, 'value': None,
                'message': f'插件设置解析失败：{_error_text(error)}'}
    count = len(value) if isinstance(value, (dict, list)) else 0
    return {'status': 'ok', 'path': PLUGIN_SETTINGS, 'value': count,
            'message': f'插件设置 {count} 条'}


def _check_personas(root):
    found = {}
    for name in STYLE_DIRS:
        folder = root / name
        try:
            found[name] = len(sorted(folder.glob('*.yaml'))) if folder.is_dir() else 0
        except OSError as error:
            return {'status': 'broken', 'path': name, 'value': None,
                    'message': f'人设目录读不出来（{_error_text(error)}）'}
    total = sum(found.values())
    missing = [name for name in STYLE_DIRS if not (root / name).is_dir()]
    status = 'ok' if total else 'missing'
    message = '人设文件 ' + str(total) + ' 个（' + '，'.join(f'{k} {v} 个' for k, v in found.items()) + '）'
    if missing:
        message += '；缺目录：' + '、'.join(missing)
    return {'status': status, 'path': '，'.join(STYLE_DIRS), 'value': total,
            'detail': found, 'message': message}


def channel_inventory(root):
    """只读盘点 data/isolated_wechat 下的通道目录：能核验的、核验不过的、不在范围的。"""
    root = Path(root)
    try:
        from . import sandbox_manager as manager
        base = manager._channel_root(root)
    except Exception as error:
        return {'root': '', 'exists': False, 'verified': [], 'unverified': [], 'ignored': [],
                'message': f'隔离通道目录不可用（{_error_text(error)}）'}
    report = {'root': str(base), 'exists': base.is_dir(), 'verified': [], 'unverified': [],
              'ignored': [], 'message': ''}
    if not report['exists']:
        report['message'] = '还没有隔离通道目录（没配对过隔离微信，正常）'
        return report
    try:
        entries = sorted(base.iterdir())
    except OSError as error:
        report['message'] = (f'隔离通道目录列不出来（{_error_text(error)}）；'
                             '可能需要管理员权限或修复目录权限')
        return report
    for path in entries:
        if not manager.CHANNEL_ID.fullmatch(path.name):
            report['ignored'].append({'name': path.name,
                                      'reason': '不是 32 位通道编号目录，不在盘点与清理范围'})
            continue
        try:
            is_dir = path.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            report['ignored'].append({'name': path.name, 'reason': '不是目录，不在清理范围'})
            continue
        row = {'channel_id': path.name, 'short_id': path.name[:12], 'path': str(path)}
        try:
            manager.channel_path(root, path.name)
        except Exception as error:
            row['reason'] = _error_text(error)
            row['suggestion'] = ('这是核验不过的残留目录：可以在界面里「清理残留通道」删掉它；'
                                 '如果提示权限不足，用「启动（管理员）」重开控制台再试')
            report['unverified'].append(row)
        else:
            report['verified'].append(row)
    report['message'] = (f'隔离通道：可核验 {len(report["verified"])} 个，'
                         f'核验不过 {len(report["unverified"])} 个')
    return report


def upgrade_self_check(root):
    """覆盖升级后的保数据自检：只读，不建库、不迁移、不修剪历史。"""
    root = Path(root)
    inventory = channel_inventory(root)
    items = {'config': _check_config(root), 'history': _check_history(root),
             'plugin_settings': _check_plugin_settings(root), 'personas': _check_personas(root),
             'isolated_channels': {
                 'status': 'ok', 'path': 'data/isolated_wechat',
                 'value': len(inventory.get('verified') or []),
                 'unverified': len(inventory.get('unverified') or []),
                 'message': inventory.get('message') or ''}}
    problems = [f'{name}：{item["message"]}' for name, item in items.items()
                if item['status'] == 'broken']
    notes = [f'{name}：{item["message"]}' for name, item in items.items()
             if item['status'] == 'missing']
    summary = ('升级数据自检：配置 {c} 项，历史 {h} 条，插件设置 {p} 条，人设 {s} 个，'
               '隔离通道 {e} 对（核验不过 {u} 个）').format(
        c=items['config']['value'] if items['config']['value'] is not None else '缺失',
        h=items['history']['value'] if items['history']['value'] is not None else '缺失',
        p=items['plugin_settings']['value'] if items['plugin_settings']['value'] is not None else '缺失',
        s=items['personas']['value'], e=items['isolated_channels']['value'],
        u=items['isolated_channels']['unverified'])
    return {'root': str(root), 'generated_at': _now(), 'ok': not problems, 'items': items,
            'problems': problems, 'notes': notes, 'message': summary}


def remove_unverified_channel(root, channel_id, confirm, *, remove=None):
    """显式删掉一个核验不过的残留通道目录（二次确认 + 范围硬编码）。"""
    from . import sandbox_manager as manager
    ident = str(channel_id or '')
    if not manager.CHANNEL_ID.fullmatch(ident):
        raise HTTPException(400, '通道编号无效：需要 32 位十六进制编号')
    # 二次确认：把通道编号再写一遍才算数，防止界面手一抖就把目录删了。
    if str(confirm or '') != ident:
        raise HTTPException(400, '清理残留通道需要二次确认：请把通道编号再填一遍'
                                 '（confirm 必须与 channel_id 一致）')
    try:
        base = manager._channel_root(root)
    except Exception as error:
        raise HTTPException(409, f'隔离通道目录不可用（{_error_text(error)}）') from None
    path = base / ident
    try:
        exists = path.is_dir()
    except OSError:
        exists = False
    if not exists:
        raise HTTPException(404, '该通道目录不存在，无需清理')
    if path.is_symlink():
        raise HTTPException(409, '该通道目录是链接，拒绝删除（可能指向别处）')
    try:
        manager.channel_path(root, ident)
    except Exception:
        pass  # 核验不过才允许删；能核验的一律拒绝，见下面的分支
    else:
        raise HTTPException(409, '该通道目录能通过核验，拒绝删除（可能是正在使用的通道）')
    try:
        if path.resolve().parent != base.resolve():
            raise HTTPException(409, '通道目录不在数据根目录下，拒绝删除')
    except OSError as error:
        raise HTTPException(409, f'通道目录路径解析失败（{_error_text(error)}）') from None
    action = remove or shutil.rmtree
    try:
        action(path)
    except PermissionError as error:
        raise HTTPException(409, f'系统拒绝删除该残留目录（{_error_text(error)}）：'
                                 '请用「启动（管理员）」重开控制台后重试') from None
    except OSError as error:
        raise HTTPException(409, f'残留目录删除失败（{_error_text(error)}）') from None
    logger.warning(f'已清理核验不过的隔离通道残留目录：{ident}')
    inventory = channel_inventory(root)
    return {'success': True, 'channel_id': ident, 'short_id': ident[:12],
            'message': '残留通道目录已删除；其它通道的配对与归档未受影响',
            'remaining_unverified': len(inventory.get('unverified') or [])}


# ── 回滚：只生成脚本，绝不自己执行 ─────────────────────────────────────────────
# 硬约束（本段所有代码的设计理由，改动前先读这三行）：
#   1. 控制台不能替换自己正在运行的 exe —— Windows 上正在运行的文件被独占，改名/覆盖必失败；
#   2. 所以后端只写一个 .cmd，由用户先关掉控制台、再自己双击执行；
#   3. 后端禁止调用 taskkill、禁止自己改名或覆盖 exe、禁止自动重启控制台。
def _default_install_dir():
    """安装目录：冻结态是 exe 旁边，源码态是工程根（与 site_profile.data_root 同一套口径）。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _file_facts(path):
    """一个文件的（字节数, 修改时间文本）；不存在或读不到都返回 (None, '')，绝不抛。"""
    try:
        if not path.is_file():
            return None, ''
        stat = path.stat()
    except OSError:
        return None, ''
    try:
        text = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds')
    except (OSError, OverflowError, ValueError):
        text = ''
    return int(stat.st_size), text


def rollback_state(install_dir=None):
    """只读检查安装目录里有没有上一版备份，决定界面能不能生成回滚脚本；绝不抛。"""
    folder = Path(install_dir) if install_dir is not None else _default_install_dir()
    state = {'install_dir': str(folder), 'available': False,
             'prev_path': str(folder / PREV_EXE_NAME), 'current_path': str(folder / EXE_NAME),
             'prev_size': None, 'current_size': None,
             'prev_mtime': '', 'current_mtime': '', 'note': ''}
    try:
        exists = folder.is_dir()
    except OSError as error:
        state['note'] = f'安装目录读不出来（{_error_text(error)}），没法确认有没有上一版备份'
        return state
    if not exists:
        state['note'] = (f'安装目录不存在（{folder}）；换过安装位置或用的是解压版时，'
                         '先确认控制台是从哪个目录启动的')
        return state
    prev_size, prev_mtime = _file_facts(folder / PREV_EXE_NAME)
    current_size, current_mtime = _file_facts(folder / EXE_NAME)
    state.update(prev_size=prev_size, prev_mtime=prev_mtime,
                 current_size=current_size, current_mtime=current_mtime)
    if prev_size is None:
        state['note'] = (f'这个安装目录里没有 {PREV_EXE_NAME}：还没做过覆盖升级，'
                         '或者升级前的旧版已经被删掉了；没有备份就没有可回滚的版本')
        return state
    state['available'] = True
    note = (f'有上一版备份 {PREV_EXE_NAME}（{prev_size} 字节，{prev_mtime or "时间未知"}）；'
            f'生成的回滚脚本会先结束控制台进程，再把它复制回 {EXE_NAME} 并重新启动')
    if current_size is None:
        note += f'；当前目录里没有 {EXE_NAME}，脚本会新建它'
    state['note'] = note
    return state


def _rollback_script_text(prev_token, current_token, generated=None):
    """回滚脚本正文：命令与提示全 ASCII（cmd 按系统代码页解析，非 ASCII 会变乱码命令）。"""
    lines = [
        '@echo off',
        'rem WhaleGirlWeChat rollback script (ASCII only on purpose).',
        f'rem Generated: {generated or _now()}',
        'setlocal',
        f'set "PREV={prev_token}"',
        f'set "CUR={current_token}"',
        'echo ============================================================',
        'echo  WhaleGirlWeChat rollback: copy previous exe over current',
        'echo ============================================================',
        'echo Close the console window before running this file.',
        'echo This script: 1) stops WhaleGirlWeChat.exe',
        'echo              2) copies WhaleGirlWeChat.prev.exe over WhaleGirlWeChat.exe',
        'echo              3) starts WhaleGirlWeChat.exe again',
        'echo.',
        'if not exist "%PREV%" goto missing',
        'taskkill /F /IM WhaleGirlWeChat.exe >nul 2>&1',
        'copy /Y "%PREV%" "%CUR%" >nul 2>&1',
        'if errorlevel 1 goto failed',
        'echo.',
        'echo Rollback done. Starting the previous version now...',
        'start "" "%CUR%"',
        'exit /b 0',
        ':missing',
        'echo.',
        'echo ERROR: backup file was not found, nothing was changed:',
        'echo   "%PREV%"',
        'pause',
        'exit /b 1',
        ':failed',
        'echo.',
        'echo ERROR: could not copy the backup over the console exe.',
        'echo   1) close every console window and run this file again',
        'echo   2) still failing: right click this file - Run as administrator',
        'echo The console exe was NOT changed.',
        'pause',
        'exit /b 1',
    ]
    return '\n'.join(lines) + '\n'


def _write_ascii_file(path, text):
    """按 ASCII + CRLF 落盘：.cmd 用系统代码页解析，任何非 ASCII 字节都可能变成乱码命令。"""
    text.encode('ascii')
    with open(Path(path), 'w', encoding='ascii', newline='\r\n') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def write_rollback_script(install_dir=None, remove=None):
    """生成回滚脚本（只写 .cmd；不执行、不改名、不覆盖 exe、不重启控制台，绝不抛）。

    返回 {success, script_path, written_to, mode, script_text, message}。
    mode='install_dir' 时脚本用 %~dp0 定位同目录的两个 exe；
    安装目录写不进去就退到数据根，mode='data_root'，脚本里改用绝对路径。
    """
    folder = Path(install_dir) if install_dir is not None else _default_install_dir()
    state = rollback_state(folder)
    result = {'success': False, 'script_path': '', 'written_to': '', 'mode': '',
              'script_text': '', 'state': state,
              'message': '没有可回滚的上一版备份，未生成脚本：' + str(state.get('note') or '')}
    if not state.get('available'):
        return result
    writer = remove or _write_ascii_file
    script_path = folder / ROLLBACK_SCRIPT_NAME
    text = _rollback_script_text('%~dp0' + PREV_EXE_NAME, '%~dp0' + EXE_NAME)
    try:
        writer(script_path, text)
    except (OSError, ValueError) as error:
        install_error = _error_text(error)
    else:
        result.update(success=True, script_path=str(script_path), written_to=str(folder),
                      mode='install_dir', script_text=text,
                      message=(f'回滚脚本已生成：{script_path}；先停止微信发送并关掉控制台窗口，'
                               '再双击这个脚本，它会结束控制台进程、复制回上一版并重新启动'))
        return result
    # 安装目录不可写（装在 Program Files 又没提权是常见情况）：退到数据根，脚本改绝对路径。
    prev_abs, current_abs = str(folder / PREV_EXE_NAME), str(folder / EXE_NAME)
    if not (prev_abs.isascii() and current_abs.isascii()):
        result['message'] = (f'安装目录写不进去（{install_error}），而且安装路径里有中文等非 ASCII 字符：'
                             '回滚脚本只能全 ASCII 才不乱码；请用「启动（管理员）」重开控制台后再生成一次')
        return result
    text_abs = _rollback_script_text(prev_abs, current_abs)
    try:
        fallback = Path(_default_root())
        fallback.mkdir(parents=True, exist_ok=True)
        fallback_path = fallback / ROLLBACK_SCRIPT_NAME
        writer(fallback_path, text_abs)
    except (OSError, ValueError) as error:
        result['message'] = (f'回滚脚本写不出来：安装目录和数据根都写不进去'
                             f'（{install_error}；{_error_text(error)}）；'
                             f'也可以手工把安装目录里的 {PREV_EXE_NAME} 复制成 {EXE_NAME}')
        return result
    result.update(success=True, script_path=str(fallback_path), written_to=str(fallback),
                  mode='data_root', script_text=text_abs,
                  message=(f'安装目录写不进去（{install_error}），回滚脚本已改存到数据根：'
                           f'{fallback_path}；先停止微信发送并关掉控制台窗口，再双击它'))
    return result


# ── 旧版本残留：只统计，不删 ───────────────────────────────────────────────────
def _residue_kind(name):
    """按文件名判断是不是旧版本残留；返回中文类别，不是残留就返回空串。"""
    lower = str(name or '').lower()
    # 隔离通道目录由 channel_inventory 单独盘点（核验不过的能一键清理），这里绝不重复计数。
    if lower == 'isolated_wechat':
        return ''
    if '.bak-hot-' in lower:
        return '热改备份'
    if lower.startswith('config.wechat.yaml.bak-') or lower.startswith('config.yaml.bak-'):
        return '旧配置备份'
    if lower.endswith('.bak'):
        return '备份'
    if lower.startswith('.tmp-') or lower.endswith('.tmp'):
        return '临时文件'
    return ''


def _residue_size(path):
    """一个残留项占的字节数：文件直接 stat，目录只在它自己里面数（有上限）。"""
    try:
        if not path.is_dir():
            return int(path.stat().st_size), False
    except OSError:
        return 0, False
    total = 0
    counted = 0
    capped = False
    try:
        for item in path.rglob('*'):
            if counted >= RESIDUE_WALK_FILES:
                capped = True
                break
            try:
                if item.is_file():
                    total += int(item.stat().st_size)
                    counted += 1
            except OSError:
                continue
    except OSError:
        return total, capped
    return total, capped


def residue_inventory(root, *, limit=RESIDUE_LIMIT):
    """只读盘点数据根一层的旧版本残留：只数、不删、不递归整个数据根，绝不抛。"""
    report = {'root': '', 'scanned': False, 'count': 0, 'bytes': 0, 'limit': int(limit),
              'truncated': False, 'capped': False, 'by_kind': {}, 'samples': [], 'message': ''}
    try:
        if root is None:
            report['message'] = '没有拿到数据目录，旧版本残留没统计'
            return report
        folder = Path(root)
        report['root'] = str(folder)
        try:
            if not folder.is_dir():
                report['message'] = f'数据目录还不存在（{folder}），没有残留可统计'
                return report
            entries = sorted(folder.iterdir(), key=lambda item: item.name.lower())
        except OSError as error:
            report['message'] = f'数据目录列不出来（{_error_text(error)}），旧版本残留没统计'
            return report
        report['scanned'] = True
        for path in entries:
            kind = _residue_kind(path.name)
            if not kind:
                continue
            size, capped = _residue_size(path)
            report['capped'] = report['capped'] or capped
            report['count'] += 1
            report['bytes'] += size
            report['by_kind'][kind] = report['by_kind'].get(kind, 0) + 1
            if len(report['samples']) < RESIDUE_SAMPLES:
                report['samples'].append({'name': path.name, 'kind': kind, 'bytes': size})
            if report['count'] >= int(limit):
                report['truncated'] = True
                break
        if not report['count']:
            report['message'] = '数据目录一层里没有旧版本残留（*.bak、*.tmp、.tmp-* 目录等）'
        else:
            head = (f'旧版本残留至少 {report["count"]} 项' if report['truncated']
                    else f'旧版本残留 {report["count"]} 项')
            report['message'] = (f'{head}，合计 {report["bytes"]} 字节'
                                 '（可手工删除，本工具不会自动删）')
        return report
    except Exception as error:
        # 兜底：任何意外都降级成一条中文说明，绝不让自检因为残留盘点整条挂掉。
        report['scanned'] = False
        report['message'] = f'旧版本残留没统计出来（{type(error).__name__}: {error}）'
        return report


def note_startup_probe(bot):
    """启动时后台探测数据目录：绝不拖慢启动，只有真出问题才留一条中文提示。"""
    def run():
        try:
            decision = resolve_data_root(getattr(bot, 'root', None))
        except Exception as error:
            logger.warning(f'数据目录可写探测失败（不影响启动）：{_error_text(error)}')
            return
        if decision.get('writable') is True and decision.get('atomic_replace') is not False:
            return
        text = str(decision.get('reason') or '数据目录写入状态异常')
        logger.warning(f'数据目录守卫：{text}')
        note = getattr(bot, 'note', None)
        if callable(note):
            try:
                note(f'数据目录：{text}')
            except Exception:
                pass

    # 线程挂到 bot 上：它正在数据根真实写 .write-probe-*.tmp，关停时 join_startup_probe
    # 要能等到它收尾（见 lifespan 里的调用；测试/升级清理紧接着会删临时目录）。
    thread = threading.Thread(target=run, daemon=True, name='data-root-probe')
    try:
        bot.data_root_probe_thread = thread
    except Exception:
        pass  # 挂不上（带 __slots__ 的替身）只是关停时等不到它，绝不因此不探测
    thread.start()


def _startup_probe_threads(bot):
    """关停时要等的线程：bot 上挂的那个，加上超时后仍在跑的内层探测线程。"""
    with _PROBE_LOCK:
        tracked = list(_LIVE_PROBES)
    current = threading.current_thread()
    unique = []
    for thread in [getattr(bot, 'data_root_probe_thread', None)] + tracked:
        if thread is not None and thread is not current and thread not in unique:
            unique.append(thread)
    return unique


def join_startup_probe(bot, seconds=PROBE_SECONDS + 0.5):
    """等启动探测线程收尾（关停路径专用）：超时返回 False，任何异常都不抛。

    为什么必须有它：探测线程会在数据根真实建 .write-probe-*.tmp 并写、fsync、改名。
    lifespan 关停后如果立刻删掉临时目录（覆盖升级换目录、测试清理都是这个动作），
    线程还攥着文件句柄就删不动，报 PermissionError: [WinError 32]。所以关停时先等它，
    等不到才放手 —— 宁可留一次未确认的探测，也不能让关停失败。
    返回 True 表示确实等到线程结束；没有线程、线程早就结束、或者超时仍活着都返回 False。
    """
    try:
        budget = max(0.0, float(seconds))
    except (TypeError, ValueError):
        budget = PROBE_SECONDS + 0.5
    waited = False
    try:
        deadline = time.monotonic() + budget
        threads = _startup_probe_threads(bot)
        for thread in threads:
            if not thread.is_alive():
                continue
            waited = True
            remaining = deadline - time.monotonic()
            if remaining > 0:
                thread.join(remaining)
        still_alive = [thread for thread in threads if thread.is_alive()]
        with _PROBE_LOCK:
            _LIVE_PROBES.difference_update({item for item in _LIVE_PROBES if not item.is_alive()})
    except Exception:
        return False
    return waited and not still_alive


class RemoveChannelBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    channel_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    confirm: str = Field(min_length=32, max_length=32)


class RollbackScriptBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # 故意留成可选：字段缺省也要走到下面那条 400 中文提示，而不是 FastAPI 默认的 422 英文校验。
    confirm: Optional[str] = None


def add_routes(app):
    """挂到控制台 HTTP 面：读接口只读，删接口走既有的管理员绑定校验。"""
    from .agent_control_api import require_bound

    def bot_root():
        return getattr(app.state.bot, 'root', None)

    @app.get('/api/install-guard/data-root')
    async def data_root_report():
        # 探测会做真实文件 IO，扔线程池别卡事件循环（上限由 PROBE_SECONDS 兜住）。
        return await asyncio.to_thread(resolve_data_root, bot_root())

    @app.get('/api/install-guard/self-check')
    async def self_check():
        require_bound(app.state.bot)
        return await asyncio.to_thread(upgrade_self_check, bot_root())

    @app.get('/api/install-guard/channels')
    async def channels():
        require_bound(app.state.bot)
        return await asyncio.to_thread(channel_inventory, bot_root())

    @app.post('/api/install-guard/channels/remove')
    async def remove_channel(body: RemoveChannelBody):
        require_bound(app.state.bot)
        return await asyncio.to_thread(remove_unverified_channel, bot_root(),
                                       body.channel_id, body.confirm)

    @app.get('/api/install-guard/rollback')
    async def rollback():
        # 只读：安装目录里有没有 .prev.exe、多大、什么时候，决定界面能不能生成回滚脚本。
        require_bound(app.state.bot)
        return await asyncio.to_thread(rollback_state)

    @app.post('/api/install-guard/rollback-script')
    async def rollback_script(body: RollbackScriptBody):
        # 只写脚本，绝不执行：控制台不能替换自己正在运行的 exe（见 rollback_state 上方的硬约束）。
        require_bound(app.state.bot)
        if str(body.confirm or '') != ROLLBACK_CONFIRM:
            raise HTTPException(400, f'生成回滚脚本需要二次确认：confirm 必须严格等于'
                                     f'「{ROLLBACK_CONFIRM}」（先停止微信发送，'
                                     '并接受脚本会关掉当前控制台窗口）')
        return await asyncio.to_thread(write_rollback_script)
