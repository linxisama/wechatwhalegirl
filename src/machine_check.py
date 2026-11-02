"""机器体检（--machine-check）：把只读探测汇总成一份能直接发回来的文本报告。

为什么要有它：部分机器上截图工具抓不到内容（拿到全透明或全黑的空图），
让对方「发一张能看的截图」并不可靠；一份 txt 更好传，也更好比对。

只读模块：唯一的写操作是数据根可写性探测（写一个临时文件后立刻删掉）。
不写注册表、不改系统设置、不启动微信、不读聊天内容。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from . import install_guard, site_profile
from .runtime_identity import APP_VERSION

REPORT_NAME = 'machine-check.txt'
EXIT_OK = 0
EXIT_WRITE_FAILED = 2

# 界面和报告都按这个顺序读
ITEM_TITLES = {
    'scale': '屏幕缩放（DPI）',
    'screen': '屏幕分辨率',
    'wechat': '微信窗口/进程',
    'ocr': 'OCR 组件',
    'windows_sandbox': 'Windows 沙盒',
    'port': '控制台端口',
    'privilege': '权限（是否管理员）',
    'dsh': 'DSH 连接',
    'data_root': '数据目录',
}

STATUS_TEXT = {'ok': '正常', 'warn': '警告', 'unknown': '探测不出'}

VERDICTS = {
    'ok': '可以跑：九项自检全部正常',
    'warn': '能跑，但有警告项：坐标/OCR 精度可能受影响，先看建议',
    'blocked': '现在跑不起来：数据目录写不进去，先按建议处理',
    'unknown': '判断不了：有探测项拿不到结果，先看「探测不出」的那几项',
}


def _redact(text):
    """报告要能直接贴到 issue 里：把用户主目录换成 ~，别顺手带出用户名。"""
    value = str(text or '')
    try:
        home = str(Path.home())
    except Exception:
        home = ''
    candidates = (home, home.replace('\\', '/'), home.replace('/', '\\'))
    for candidate in candidates:
        if candidate and len(candidate) > 3:
            value = value.replace(candidate, '~')
    return value


def _text(value):
    return _redact(value if value not in (None, '') else '（未知）')


def verdict(report):
    """三态判断：数据目录写不进去最优先，其次「探测不出」，再次「警告」。"""
    data_root = report.get('data_root') or {}
    if data_root.get('writable') is False:
        return 'blocked'
    items = (report.get('site') or {}).get('items') or []
    if not items:  # 自检整段没跑起来，等于什么都没测
        return 'unknown'
    if any(str(item.get('status')) == 'unknown' for item in items):
        return 'unknown'
    if any(str(item.get('status')) == 'warn' for item in items):
        return 'warn'
    return 'ok'


def suggestions(report):
    """把「该做什么」压成几条人话，界面和报告共用。"""
    out = []
    data_root = report.get('data_root') or {}
    if data_root.get('writable') is False:
        out.append('数据目录写不进去：' + _text(data_root.get('reason')))
    elif data_root.get('fallback_used'):
        out.append('主数据目录写不进去，本轮已改用备用目录：' + _text(data_root.get('reason')))
    for item in (report.get('site') or {}).get('items') or []:
        if str(item.get('status')) == 'ok':
            continue
        title = ITEM_TITLES.get(str(item.get('key')), str(item.get('key')))
        out.append(f'{title}：{_text(item.get("detail"))}')
    residue = (report.get('install') or {}).get('residue') or {}
    if residue.get('count'):
        out.append(_text(residue.get('message')))
    if not out:
        out.append('没有需要处理的事项')
    return out


def _safe_call(worker, fallback):
    try:
        value = worker()
    except Exception as error:  # 体检本身绝不许因为某一项挂掉
        return dict(fallback, reason=f'探测出错（{type(error).__name__}: {error}）')
    return value if isinstance(value, dict) else dict(fallback)


def collect(*, site=None, install=None, root=None):
    """一次体检。任何一部分失败都降级成中文说明，绝不抛给调用方。"""
    started = time.monotonic()
    site_report = site if isinstance(site, dict) and site.get('items') is not None else None
    if site_report is None:
        site_report = _safe_call(site_profile.collect, {'ok': False, 'items': [], 'summary': '机器自检没跑起来'})
        if site_report.get('reason'):
            site_report['summary'] = f'机器自检没跑起来（{site_report['reason']}）'
    guard = install if isinstance(install, dict) else {}
    data_root = guard.get('data_root')
    if not isinstance(data_root, dict):
        data_root = _safe_call(lambda: install_guard.resolve_data_root(root),
                               {'writable': None, 'active': '', 'primary': '', 'reason': '数据目录探测失败'})
    marker = guard.get('marker')
    if not isinstance(marker, dict):
        marker = _safe_call(install_guard.read_install_marker, {'exists': False, 'note': '安装标记读不出来'})
    rollback = guard.get('rollback')
    if not isinstance(rollback, dict):
        rollback = _safe_call(install_guard.rollback_state, {'available': False, 'note': '回滚状态读不出来'})
    residue = guard.get('residue')
    if not isinstance(residue, dict):
        active = str(data_root.get('active') or data_root.get('primary') or '')
        residue = _safe_call(lambda: install_guard.residue_inventory(Path(active or '.')),
                             {'count': 0, 'message': '旧版本残留没统计出来'})
    report = {
        'app_version': APP_VERSION,
        'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'platform': dict(site_report.get('platform') or {}),
        'site': site_report,
        'data_root': data_root,
        'install': {'marker': marker, 'rollback': rollback, 'residue': residue},
    }
    report['verdict'] = verdict(report)
    report['ok'] = report['verdict'] in ('ok', 'warn')
    report['suggestions'] = suggestions(report)
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return report


def _rule(char='=', width=60):
    return char * width


def render(report):
    """纯文本报告：首屏就是结论，明细在后，原始数据附在最后。"""
    platform = report.get('platform') or {}
    site = report.get('site') or {}
    data_root = report.get('data_root') or {}
    install = report.get('install') or {}
    marker = install.get('marker') or {}
    rollback = install.get('rollback') or {}
    residue = install.get('residue') or {}
    verdict_key = str(report.get('verdict') or 'unknown')
    lines = [
        _rule(),
        '微信机器人 · 机器体检报告',
        _rule(),
        f'生成时间：{_text(report.get("generated_at"))}',
        f'控制台版本：{_text(report.get("app_version"))}　端口：{_text(platform.get("port"))}　'
        f'打包运行：{"是" if platform.get("frozen") else "否"}',
        f'系统：{_text(platform.get("os"))}　Python：{_text(platform.get("python"))}',
        '',
        f'结论：{verdict_key} —— {VERDICTS.get(verdict_key, VERDICTS["unknown"])}',
        f'摘要：{_text(site.get("summary"))}',
        '',
        '[九项自检]',
    ]
    items = site.get('items') or []
    if not items:
        lines.append('（自检没有返回任何项目）')
    for item in items:
        title = ITEM_TITLES.get(str(item.get('key')), str(item.get('key')))
        status = STATUS_TEXT.get(str(item.get('status')), '探测不出')
        lines.append(f'{status:<4} {title}：{_text(item.get("detail"))}')
    lines += ['', '[数据目录]']
    lines.append(f'主目录：{_text(data_root.get("primary"))}')
    lines.append(f'本轮使用：{_text(data_root.get("active"))}')
    lines.append('可写：{}　原子替换：{}　改用备用目录：{}'.format(
        _yes_no(data_root.get('writable')), _yes_no(data_root.get('atomic_replace')),
        _yes_no(data_root.get('fallback_used'))))
    if data_root.get('reason'):
        lines.append(f'说明：{_text(data_root.get("reason"))}')
    lines += ['', '[安装]']
    lines.append(f'安装目录：{_text(marker.get("install_dir") or rollback.get("install_dir"))}')
    lines.append(f'安装标记：{"有" if marker.get("exists") else "没有"}　'
                 f'版本：{_text(marker.get("version"))}　安装时间：{_text(marker.get("installed_at"))}')
    if marker.get('note'):
        lines.append(f'标记说明：{_text(marker.get("note"))}')
    lines.append(f'可回滚：{_yes_no(rollback.get("available"))}')
    if rollback.get('note'):
        lines.append(f'回滚说明：{_text(rollback.get("note"))}')
    lines += ['', '[旧版本残留]', _text(residue.get('message'))]
    lines += ['', '[需要你做的事]']
    for index, text in enumerate(report.get('suggestions') or [], start=1):
        lines.append(f'{index}. {_text(text)}')
    raw = json.dumps(_redact_tree(_plain(report)), ensure_ascii=False, indent=2, sort_keys=True)
    lines += ['', '[原始数据]', _redact(raw)]  # 原始段也要脱敏：整份报告是拿去贴 issue 的
    return '\n'.join(lines) + '\n'


def _yes_no(value):
    if value is True:
        return '是'
    if value is False:
        return '否'
    return '未确认'


def _redact_tree(value):
    """JSON 里反斜杠会被转义成双份，所以先按值脱敏再序列化。"""
    if isinstance(value, dict):
        return {str(key): _redact_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_tree(item) for item in value]
    if isinstance(value, str):
        return _redact(value)
    return value


def _plain(value):
    """只保留能 JSON 序列化的部分：报告里不许出现 Path / 异常对象。"""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def write_report(report, path):
    """写盘：UTF-8(BOM) + CRLF —— Win10 记事本双击打开不乱码、不断行。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render(report).replace('\r\n', '\n').replace('\n', '\r\n')
    target.write_bytes(text.encode('utf-8-sig'))
    return target


def _safe_print(text):
    try:
        print(text)
    except Exception:
        try:
            sys.stdout.write(str(text).encode('ascii', 'replace').decode('ascii') + '\n')
        except Exception:
            pass


def main(argv=None):
    """命令行入口：--machine-check [输出路径]。返回码只表示「报告写没写出来」。"""
    args = list(sys.argv[1:] if argv is None else argv)
    target = Path(args[0]) if args and str(args[0]).strip() else Path.cwd() / REPORT_NAME
    try:
        report = collect()
    except Exception as error:  # 兜底：collect 理论上不抛，真抛了也要留下报告
        report = {'app_version': APP_VERSION, 'verdict': 'unknown', 'ok': False,
                  'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
                  'platform': {}, 'site': {'items': [], 'summary': f'体检失败：{error}'},
                  'data_root': {}, 'install': {}, 'suggestions': [f'体检失败：{error}']}
    try:
        written = write_report(report, target)
    except Exception as error:
        _safe_print(f'报告写不出来：{error}')
        return EXIT_WRITE_FAILED
    _safe_print(f'结论：{report.get("verdict")}　报告：{written}')
    return EXIT_OK
