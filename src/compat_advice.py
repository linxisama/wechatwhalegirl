"""跨机器兼容性建议：这台机器缺什么、影响什么、怎么修。

纯只读：不改设置、不启动微信、不启停沙盒、不发 DSH 回合。
判定输入全部复用已有探测（site_profile.collect / install_guard.resolve_data_root /
upgrade_self_check / channel_inventory）加上 bot 现有的 DSH 设置（只读状态，不发起新探测），
这里只做「把机器特征翻译成人话 + 该点的按钮」，不复制任何一项判定逻辑。

每条建议固定字段 {key, severity, title, detail, fix, evidence}：
- severity 只有三档：blocking（现在就跑不起来）/ degraded（能跑但会退化）/ info（说明）。
- evidence 只放机器特征（版本号、DPI 缩放、端口、安装路径、磁盘余量），
  绝不出现用户名、wxid、聊天内容、会话昵称；路径一律走 site_profile._redact 的 ~ 脱敏。

每条判定各自独立 try：任何一条抛异常都降级成 severity='info' 并写明中文原因，
collect_advice() 绝不抛异常，接口绝不 500。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from . import install_guard, site_profile
from .runtime_identity import APP_VERSION, PORT

SEVERITIES = ('blocking', 'degraded', 'info')

# DSH 只有这两个阶段代表「配置层面已经不可用」；其余阶段是过程中的正常状态
DSH_BAD_STAGES = ('failed', 'preset_missing')

# 缺中文 OCR 包时给的安装路径，直接复用 site_profile 里那条能照着点的说明
_OCR_ZH_INSTALL = getattr(site_profile, '_OCR_ZH_INSTALL', '在 Windows 设置里安装「中文（简体）光学字符识别」')


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _text(value):
    """脱敏成一行的字符串：路径里的用户名换成 ~，None 不写成 "None"。"""
    if value is None:
        return ''
    redact = getattr(site_profile, '_redact', None)
    try:
        return redact(str(value)) if callable(redact) else str(value)
    except Exception:
        return str(value)


def _evidence(fields):
    """evidence 只留机器特征：标量原样保留，字符串统一脱敏。"""
    clean = {}
    for name, value in dict(fields or {}).items():
        if value is None or isinstance(value, (bool, int, float)):
            clean[str(name)] = value
        else:
            clean[str(name)] = _text(value)
    return clean


def _advice(key, severity, title, detail, fix, evidence=None):
    return {'key': key, 'severity': severity if severity in SEVERITIES else 'info',
            'title': _text(title), 'detail': _text(detail), 'fix': _text(fix),
            'evidence': _evidence(evidence)}


# ── 探测层：每个探测各自兜底，绝不把异常抛给判定层 ──────────────────────────────
def _run_probe(name, function, *args):
    try:
        result = function(*args)
    except Exception as exc:  # 探测层失败只影响这一条建议
        return {'probe_error': f'{type(exc).__name__}: {exc}'}
    return result if isinstance(result, dict) else {'probe_error': f'{name} 返回了非预期结构'}


def _probe_error(source):
    return str(source.get('probe_error')) if isinstance(source, dict) and source.get('probe_error') else ''


def dsh_snapshot(bot):
    """只读当前 DSH 设置与阶段：不发起探测、不发回合。没有后台对象就返回 None。"""
    bridge = getattr(bot, 'dsh', None)
    if bridge is None:
        return None
    try:
        raw = bridge.settings()
    except Exception as exc:
        return {'probe_error': f'{type(exc).__name__}: {exc}'}
    if not isinstance(raw, dict):
        return {'probe_error': 'DSH 设置返回了非预期结构'}
    settings = {name: raw.get(name) for name in
                ('enabled', 'base_url', 'model', 'provider', 'reasoning_effort', 'timeout')}
    return {'settings': settings, 'stage': str(getattr(bridge, 'stage', '') or ''),
            'last_error': str(getattr(bridge, 'last_error', '') or '')}


def _item_map(profile):
    items = profile.get('items') if isinstance(profile, dict) else None
    if not isinstance(items, list):
        return {}
    return {item.get('key'): item for item in items
            if isinstance(item, dict) and isinstance(item.get('key'), str)}


def _item_failed(item):
    return str((item or {}).get('status') or '') == 'unknown'


def _item_detail(item, fallback):
    return str((item or {}).get('detail') or fallback)


def _machine_facts(profile):
    """machine 级事实：版本号、端口、系统、DPI 缩放、磁盘余量。"""
    info = profile.get('platform') if isinstance(profile, dict) else None
    info = info if isinstance(info, dict) else {}
    facts = {'app_version': info.get('app_version') or APP_VERSION,
             'port': info.get('port') if info.get('port') is not None else int(PORT),
             'os': info.get('os') or '', 'frozen': info.get('frozen')}
    items = _item_map(profile)
    scale = (items.get('scale') or {}).get('data') or {}
    if scale.get('scale_percent') is not None:
        facts['scale_percent'] = scale['scale_percent']
    root = (items.get('data_root') or {}).get('data') or {}
    if root.get('free_gb') is not None:
        facts['free_gb'] = root['free_gb']
    if root.get('root'):
        facts['profile_root'] = root['root']
    return facts


# ── 1. 数据目录 ───────────────────────────────────────────────────────────────
def _advice_data_root(source, facts):
    writable = source.get('writable')
    atomic = source.get('atomic_replace')
    active = source.get('active') or source.get('primary') or facts.get('profile_root') or ''
    evidence = {'active_root': active, 'fallback_used': source.get('fallback_used'),
                'writable': writable, 'atomic_replace': atomic,
                'free_gb': facts.get('free_gb'), 'frozen': facts.get('frozen')}
    failed = _probe_error(source)
    if failed:
        return _advice('data_root', 'info', '数据目录没能判定',
                       f'可写性探测本身出错：{failed}', '重新点一次自检；一直失败再看后台日志',
                       evidence)
    if writable is None:
        return _advice('data_root', 'info', '数据目录可写性没有在时限内给结论',
                       f'{active} 的写入探测超时或出错（{source.get("reason") or "没有结论"}），'
                       '所以这一项既不算通过也不算失败',
                       '确认磁盘在线、目录没有被同步盘锁住，然后重新自检', evidence)
    if writable is not True:
        return _advice('data_root', 'blocking', '数据目录不可写，配置和聊天记录都存不下来',
                       f'{active} 写不进临时文件（{source.get("reason") or "写入被系统拒绝"}）；'
                       '换机器后这一档多是被安全软件、目录权限或只读盘挡住的',
                       '把数据目录换到当前用户可写的本地路径（例如 D:/WhaleGirlWeChat），'
                       '或给该目录当前用户完全控制权限后重开控制台', evidence)
    if atomic is False:
        return _advice('data_root', 'degraded', '能写，但原子替换被占用，写入退化为就地重写',
                       f'{active} 可以写，但"写临时文件再改名"这一步被占用'
                       f'（{source.get("reason") or "替换被拒绝"}）；就地重写时断电或崩溃可能留下半截配置',
                       '关掉正在同步该目录的网盘和杀毒实时扫描，或把数据目录换到本地磁盘后重新自检',
                       evidence)
    return _advice('data_root', 'info', '数据目录可写，原子替换正常',
                   f'{active} 可以写，改名替换也通过', '无需处理', evidence)


# ── 2. 微信版本 ───────────────────────────────────────────────────────────────
def _advice_wechat(source, facts):
    data = source.get('data') if isinstance(source.get('data'), dict) else {}
    evidence = {'wechat_running': data.get('running'), 'wechat_version': data.get('version') or '',
                'exe': data.get('exe') or '', 'scale_percent': facts.get('scale_percent'),
                'app_version': facts.get('app_version')}
    window = data.get('window') if isinstance(data.get('window'), dict) else None
    if window:
        evidence['window'] = f'{window.get("width")}x{window.get("height")}'
    if not source:
        return _advice('wechat', 'info', '微信状态没有探测到',
                       '没有拿到微信进程探测结果，这一项没判定', '先点一次兼容性自检', evidence)
    detail = _item_detail(source, '微信探测没有给出说明')
    if _item_failed(source):
        return _advice('wechat', 'info', '微信状态没测出来',
                       f'{detail}；这是探测本身失败，不代表微信有问题',
                       '稍后重新自检；一直失败就导出诊断包', evidence)
    if not data.get('running'):
        return _advice('wechat', 'degraded', '微信没在运行，装没装、版本对不对都没法核对',
                       f'{detail}；自检只读进程和文件版本，微信没开着就读不到版本号',
                       '手动打开微信并登录，再点一次兼容性自检；主链路要求微信 4.1.x', evidence)
    version = str(data.get('version') or '')
    if data.get('version_ok'):
        return _advice('wechat', 'info', '微信版本满足主链路',
                       f'读到微信 {version}，版本核验通过', '无需处理', evidence)
    if not version:
        return _advice('wechat', 'degraded', '微信在运行但读不到版本号',
                       f'{detail}；版本不明时不能假设发送接口可用，主链路按未验证处理',
                       '用微信安装目录里 Weixin.exe 的文件属性确认是 4.1.x，再重新自检', evidence)
    return _advice('wechat', 'degraded', f'微信 {version} 不在已验证范围',
                   f'读到微信 {version}；判断依据是主链路只核验过 4.1.x 这一支，'
                   '其它版本可能能发也可能发歪，所以按未验证处理',
                   '装回微信 4.1.x（官方稳定版）后重新自检；只想先试就用模拟对话面板验证链路',
                   evidence)


# ── 3. 系统 OCR ───────────────────────────────────────────────────────────────
def _advice_ocr(source, facts):
    data = source.get('data') if isinstance(source.get('data'), dict) else {}
    chinese = [str(tag) for tag in (data.get('chinese') or [])]
    evidence = {'ocr_total': data.get('total'), 'ocr_chinese': '、'.join(chinese) or '无',
                'os': facts.get('os')}
    if not source:
        return _advice('ocr', 'info', '系统 OCR 没有探测到', '没有拿到 OCR 探测结果，这一项没判定',
                       '先点一次兼容性自检', evidence)
    detail = _item_detail(source, 'OCR 探测没有给出说明')
    if _item_failed(source):
        return _advice('ocr', 'info', '系统 OCR 没测出来',
                       f'{detail}；这是探测本身失败，不代表缺语言包',
                       '重新自检；一直失败就在 Windows 设置的「可选功能」里确认已装中文 OCR', evidence)
    if chinese:
        return _advice('ocr', 'info', '系统 OCR 有中文语言包', detail, '无需处理', evidence)
    return _advice('ocr', 'degraded', '系统 OCR 缺中文语言包，发送会被拒绝',
                   f'{detail}；发送前要用 OCR 核验窗口标题，缺中文包时标题核验 fail-closed'
                   '（宁可拒绝也不发错窗口），所以发送会被拒绝',
                   f'在 {_OCR_ZH_INSTALL}，装完重开控制台再自检', evidence)


# ── 4. Windows 沙盒 ───────────────────────────────────────────────────────────
def _advice_sandbox(source, facts):
    data = source.get('data') if isinstance(source.get('data'), dict) else {}
    windows = ' '.join(part for part in (str(data.get('product_name') or ''),
                                         str(data.get('display_version') or '')) if part)
    evidence = {'windows': windows, 'edition_id': data.get('edition_id') or '',
                'build': data.get('build') or '',
                'sandbox_exe_exists': data.get('sandbox_exe_exists'),
                'sandbox_available': data.get('sandbox_available')}
    if not source:
        return _advice('windows_sandbox', 'info', 'Windows 沙盒没有探测到',
                       '没有拿到沙盒探测结果，这一项没判定', '先点一次兼容性自检', evidence)
    detail = _item_detail(source, '沙盒探测没有给出说明')
    if _item_failed(source):
        return _advice('windows_sandbox', 'info', 'Windows 沙盒状态没测出来',
                       f'{detail}；这是探测本身失败，不代表沙盒不可用',
                       '不影响主链路；要确认就在「Windows 功能」里看 Windows 沙盒是否勾选', evidence)
    if data.get('sandbox_available'):
        return _advice('windows_sandbox', 'info', '可以开 Windows 沙盒，隔离通道可用',
                       detail, '无需处理', evidence)
    return _advice('windows_sandbox', 'info', 'Windows 沙盒不可用，隔离通道跑不了',
                   f'{detail}；隔离通道是可选增强，缺失时本机直连仍然能收发，只是少一层隔离',
                   '不影响主链路；确实要用隔离后台就换专业版/企业版，'
                   '并在「Windows 功能」里勾上 Windows 沙盒', evidence)


# ── 5. DSH ───────────────────────────────────────────────────────────────────
def _advice_dsh(snapshot, facts):
    if not isinstance(snapshot, dict):
        return _advice('dsh', 'info', 'DSH 状态未知',
                       '这一项要读控制台后台里的现有设置，界面没连上后台就读不到',
                       '在控制台后台正常启动后再自检', {'app_version': facts.get('app_version')})
    failed = _probe_error(snapshot)
    settings = snapshot.get('settings') if isinstance(snapshot.get('settings'), dict) else {}
    stage = str(snapshot.get('stage') or '')
    last_error = str(snapshot.get('last_error') or '')
    evidence = {'dsh_enabled': settings.get('enabled'), 'dsh_stage': stage or '未知',
                'dsh_provider': settings.get('provider') or '',
                'dsh_model': settings.get('model') or '',
                'dsh_last_error': last_error[:200]}
    if failed:
        return _advice('dsh', 'info', 'DSH 设置读不出来',
                       f'读取 DSH 设置时出错：{failed}；这一项没判定', '重新自检；一直失败就导出诊断包',
                       evidence)
    if not settings.get('enabled'):
        return _advice('dsh', 'info', 'DSH 没启用，回复由本地插件生成',
                       f'当前 DSH 开关是关的（阶段 {stage or "未知"}），主聊走本地插件，属于正常配置',
                       '要用 DSH 主聊就在设置里填 base_url 和模型；不需要就保持关闭', evidence)
    if not str(settings.get('model') or ''):
        return _advice('dsh', 'degraded', 'DSH 已启用但没选到模型',
                       'DSH 开着但没有可用模型，主聊会退回本地插件回复；这是降级不是故障',
                       '在 DSH 设置里选一个可用模型（或重新拉一次模型列表）后保存', evidence)
    if stage in DSH_BAD_STAGES or last_error:
        return _advice('dsh', 'degraded', 'DSH 连不上，主聊会退回本地插件回复',
                       f'DSH 开着但当前阶段是 {stage or "未知"}'
                       f'{("，最近错误：" + last_error[:200]) if last_error else ""}；'
                       '1.3.46 起 DSH 异常会自动本地插件兜底，所以这是降级不是故障',
                       '检查 base_url 是否可访问、密钥是否还有效，然后在 DSH 页重新测试连接',
                       evidence)
    return _advice('dsh', 'info', 'DSH 已配置',
                   f'DSH 已启用，模型 {settings.get("model")}，当前阶段 {stage or "未知"}',
                   '无需处理', evidence)


# ── 6. 端口与版本一致性 ───────────────────────────────────────────────────────
def _advice_port(source, facts):
    data = source.get('data') if isinstance(source.get('data'), dict) else {}
    port = data.get('port') if data.get('port') is not None else facts.get('port')
    self_running = data.get('self_running')
    bindable = data.get('bindable')
    version = str(data.get('version') or '')
    mine = str(facts.get('app_version') or APP_VERSION)
    evidence = {'port': port, 'self_running': self_running, 'bindable': bindable,
                'runtime_version': version or '没读到', 'app_version': mine}
    if not source:
        return _advice('port', 'info', '端口没有探测到', '没有拿到端口探测结果，这一项没判定',
                       '先点一次兼容性自检', evidence)
    detail = _item_detail(source, '端口探测没有给出说明')
    if _item_failed(source):
        return _advice('port', 'info', '端口状态没测出来',
                       f'{detail}；这是探测本身失败，不代表端口被占用',
                       '重新自检；一直失败就在命令行确认 8091 的占用者', evidence)
    if self_running and version and version != mine:
        return _advice('port', 'degraded', '端口上跑的控制台版本和本进程不一致',
                       f'{port} 上的后台报的是 v{version}，当前进程是 v{mine}；'
                       '界面可能连到旧后台，看到的功能和实际后台对不上',
                       f'关掉旧的控制台进程，重启后确认 /api/runtime 报的是 v{mine}',
                       evidence)
    if self_running:
        return _advice('port', 'info', '端口被本控制台后台占用', detail, '无需处理', evidence)
    if bindable is False:
        return _advice('port', 'blocking', f'{port} 被别的进程占着，后台起不来',
                       f'{detail}；换机器后这一条通常是别的软件也用了 {port}',
                       f'用 netstat -ano | findstr :{port} 找到占用进程并关掉，或改用别的端口后重启控制台',
                       evidence)
    return _advice('port', 'info', f'{port} 空闲，后台可以监听', detail, '无需处理', evidence)


# ── 7. 隔离通道目录 ───────────────────────────────────────────────────────────
def _advice_channel(source, facts):
    unverified = [item for item in (source.get('unverified') or []) if isinstance(item, dict)]
    verified = [item for item in (source.get('verified') or []) if isinstance(item, dict)]
    ids = '、'.join(str(item.get('short_id') or item.get('channel_id') or '') for item in unverified)
    evidence = {'channel_root': source.get('root') or '', 'verified_count': len(verified),
                'unverified_count': len(unverified), 'unverified_ids': ids[:200]}
    failed = _probe_error(source)
    if failed:
        return _advice('isolated_channel', 'info', '隔离通道目录没能盘点',
                       f'盘点隔离通道目录时出错：{failed}', '重新自检；一直失败就导出诊断包', evidence)
    if not source.get('exists') and not unverified:
        return _advice('isolated_channel', 'info', '还没有隔离通道目录',
                       str(source.get('message') or '没配对过隔离微信，正常'),
                       '需要隔离后台时在配对流程里生成；不需要就不用管', evidence)
    if unverified:
        reasons = '；'.join(str(item.get('reason') or '核验不过') for item in unverified)[:200]
        return _advice('isolated_channel', 'degraded', '隔离通道目录核验不过',
                       f'{len(unverified)} 个通道目录核验不过（{reasons}）；'
                       '这些目录多半是上次配对中途断了，或者被别的程序改过',
                       '在控制台「环境与自检」里的「残留通道」区域选中编号，'
                       '再把编号手抄一遍做二次确认后清理；能核验通过的通道不会出现在清理列表里',
                       evidence)
    return _advice('isolated_channel', 'info', '隔离通道目录正常',
                   f'核验通过 {len(verified)} 个通道目录，没有残留', '无需处理', evidence)


# ── 8. 升级后数据对不上的项 ───────────────────────────────────────────────────
def _advice_upgrade(source, facts):
    problems = [str(text) for text in (source.get('problems') or [])]
    notes = [str(text) for text in (source.get('notes') or [])]
    evidence = {'data_root': source.get('root') or facts.get('profile_root') or '',
                'problems': '；'.join(problems)[:200], 'notes': '；'.join(notes)[:200]}
    failed = _probe_error(source)
    if failed:
        return _advice('upgrade_data', 'info', '升级自检没能完成',
                       f'升级数据自检时出错：{failed}', '重新自检；一直失败就导出诊断包', evidence)
    if problems:
        return _advice('upgrade_data', 'degraded', '升级后有几项数据对不上',
                       '；'.join(problems),
                       '先按提示把缺的文件补回来再升级；自检只读，不会移动或删除任何用户数据',
                       evidence)
    if notes:
        return _advice('upgrade_data', 'info', '升级自检有需要留意的项',
                       '；'.join(notes), '换机器前留意这些项，缺的多半是还没用过的功能', evidence)
    return _advice('upgrade_data', 'info', '升级自检通过',
                   str(source.get('message') or '配置、历史、人设、隔离通道都能对上'),
                   '无需处理', evidence)


def _advice_leftover(source, facts):
    """旧版本残留：只统计不删。干净且扫得动时返回 None（不占一条），没数据目录也不占一条。"""
    if not isinstance(source, dict):
        return None
    failed = _probe_error(source)
    kinds = source.get('by_kind') if isinstance(source.get('by_kind'), dict) else {}
    evidence = {'residue_root': source.get('root') or '', 'residue_count': source.get('count'),
                'residue_bytes': source.get('bytes'), 'residue_limit': source.get('limit'),
                'residue_kinds': '、'.join(f'{name} {count} 项'
                                          for name, count in kinds.items())[:200]}
    fix = '可手工删除，本工具不会自动删：先关掉控制台，再按文件名到数据目录里删'
    if failed:
        return _advice('leftover_residue', 'info', '旧版本残留没能统计',
                       f'统计残留时出错：{failed}', fix, evidence)
    if not source.get('scanned'):
        # 读不到 ≠ 干净：把中文原因原样摆出来，别让用户以为扫过且没残留。
        return _advice('leftover_residue', 'info', '旧版本残留没统计',
                       str(source.get('message') or '数据目录读不出来，残留没统计'), fix, evidence)
    count = source.get('count') or 0
    if not count:
        return None  # 干净：不产出一条噪声（否则每次自检都多一行「没有残留」）
    samples = source.get('samples') if isinstance(source.get('samples'), list) else []
    names = '、'.join(str(item.get('name') or '') for item in samples if isinstance(item, dict))
    evidence['residue_samples'] = names[:240]
    detail = str(source.get('message') or f'旧版本残留 {count} 项')
    if source.get('truncated'):
        detail += f'；只统计到 {source.get("limit")} 项就停了，实际更多'
    if source.get('capped'):
        detail += '；有残留目录里的文件太多，字节数只算了前一部分'
    return _advice('leftover_residue', 'info', '数据目录里有旧版本残留', detail, fix, evidence)


# ── 汇总 ─────────────────────────────────────────────────────────────────────
# 顺序固定：界面按这个顺序分组显示，测试也按这个顺序读；判定函数用名字取（照 site_profile 的写法）
_BUILDERS = (
    ('data_root', '_advice_data_root'),
    ('wechat', '_advice_wechat'),
    ('ocr', '_advice_ocr'),
    ('windows_sandbox', '_advice_sandbox'),
    ('dsh', '_advice_dsh'),
    ('port', '_advice_port'),
    ('isolated_channel', '_advice_channel'),
    ('upgrade_data', '_advice_upgrade'),
    ('leftover_residue', '_advice_leftover'),
)


def _build_one(key, builder, source, facts):
    """单条判定兜底：判定函数缺失或抛异常都只降级成 info，绝不让整函数抛。"""
    if not callable(builder):
        return _advice(key, 'info', '这一项没能判定', '这个版本的判定函数缺失，整条跳过',
                       '重新自检；还缺就说明安装文件不完整，需要重新安装控制台')
    try:
        return builder(source, facts)
    except Exception as exc:  # 判定层不许把异常抛出去，接口必须 200
        return _advice(key, 'info', '这一项没能判定',
                       f'判定过程出错：{type(exc).__name__}: {exc}',
                       '重新自检；反复失败就导出诊断包')


def _summary(items, counts):
    blocking, degraded = counts.get('blocking', 0), counts.get('degraded', 0)
    if blocking:
        return (f'这台机器现在跑不起来：{blocking} 项必须先解决，'
                f'另有 {degraded} 项降级；按每条的「怎么修」逐项处理')
    if degraded:
        return f'能跑，但有 {degraded} 项降级；搬机器前先按「怎么修」逐项看一眼'
    return f'{len(items)} 项检查没有发现问题，这台机器可以直接跑'


def collect_advice(root=None, *, dsh=None, bot=None, profile=None, data_root=None,
                   self_check=None, channels=None, residue=None):
    """一次兼容性建议。任何一项失败都只降级成 info，本函数绝不抛异常。"""
    sources = {
        'profile': profile if profile is not None else _run_probe('site_profile', site_profile.collect),
        'data_root': (data_root if data_root is not None
                      else _run_probe('resolve_data_root', install_guard.resolve_data_root, root)),
        'self_check': (self_check if self_check is not None
                       else _run_probe('upgrade_self_check', install_guard.upgrade_self_check, root)),
        'channels': (channels if channels is not None
                     else _run_probe('channel_inventory', install_guard.channel_inventory, root)),
        # 没有 root 就不扫：不传数据目录不等于「干净」，这条判定靠 None 表达「没扫」。
        'residue': (residue if residue is not None
                    else (None if root is None
                          else _run_probe('residue_inventory',
                                          install_guard.residue_inventory, root))),
    }
    if dsh is None and bot is not None:
        dsh = _run_probe('dsh_snapshot', dsh_snapshot, bot)
    items_by_key = _item_map(sources['profile'])
    facts = _machine_facts(sources['profile'])
    by_key = {'data_root': sources['data_root'], 'wechat': items_by_key.get('wechat'),
              'ocr': items_by_key.get('ocr'), 'windows_sandbox': items_by_key.get('windows_sandbox'),
              'dsh': dsh, 'port': items_by_key.get('port'),
              'isolated_channel': sources['channels'], 'upgrade_data': sources['self_check'],
              'leftover_residue': sources['residue']}
    items = []
    for key, name in _BUILDERS:
        source = by_key.get(key)
        # dsh 保留 None：没连到后台和「后台里 DSH 关着」是两件事；
        # leftover_residue 也保留 None：没扫过就不占一条（见 _advice_leftover）。
        if key not in ('dsh', 'leftover_residue'):
            source = source if isinstance(source, dict) else {}
        item = _build_one(key, globals().get(name), source, facts)
        if item is not None:  # 判定函数可以说「这台机器没有这一条」（残留干净时就是这样）
            items.append(item)
    counts = {severity: sum(1 for item in items if item['severity'] == severity)
              for severity in SEVERITIES}
    return {'ok': counts['blocking'] == 0, 'generated_at': _now(),
            'summary': _summary(items, counts), 'blocking_count': counts['blocking'],
            'degraded_count': counts['degraded'], 'info_count': counts['info'],
            'items': items}


def add_routes(app):
    """挂到控制台 HTTP 面：只读诊断接口。

    刻意不要求先绑定 Agent：全新安装（还没接 Agent）时用户同样要能看到
    「这台机器缺什么」，否则面板只会报一句「未绑定」。
    """

    @app.get('/api/compat/advice')
    async def compat_advice():
        # 探测要跑系统探测和文件 IO，扔线程池别卡事件循环（上限由 site_profile 预算兜住）。
        bot = app.state.bot
        return await asyncio.to_thread(collect_advice, getattr(bot, 'root', None), bot=bot)
