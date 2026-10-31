"""只读DSH进程树与现有监听端口，协议验证后才作为候选，不猜固定端口。

普通权限（没有管理员）时 Windows 拿不到别的进程的端口归属：psutil.net_connections() 只会给
出 pid 为空的 LISTEN 行。这时退回「按 DSH 自己留下的线索端口逐个协议核验」——依然不猜：
只有真正回答 settings/describe + agentPresets/list 的端口才会被当成候选。
"""
import asyncio
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
import httpx

from .dsh_bridge import DshError, cookie_for, local_url

ROOT_NAMES={'deepseek harness.exe','deepseek harness','dsh.exe','dsh'}
DEFAULT_PORT=9175            # 桌面 DSH 网页界面的默认端口；只是候选之一，仍要协议核验
HINT_PORT_LIMIT=10           # 一次最多核验多少个线索端口，别把「自动查找」变成全网扫描
HINT_SCAN_FILES=40
HINT_FILE_BYTES=65536
PROBE_TIMEOUT=1.2
_PORT_HINT=re.compile(r'(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0):(\d{2,5})')


def snapshot():
    """进程列表与监听端口；端口归属读不到就退化成空列表，绝不因为权限把整条发现流程堵死。"""
    import psutil
    processes=[]
    for p in psutil.process_iter(['pid','ppid','name']):
        try: processes.append(dict(pid=p.info['pid'],parent=p.info['ppid'],name=p.info['name'] or ''))
        except (psutil.Error,OSError):continue
    ports=[]
    try:
        for c in psutil.net_connections(kind='tcp'):
            if c.status==psutil.CONN_LISTEN and c.pid and c.laddr:
                ports.append(dict(pid=c.pid,address=c.laddr.ip,port=c.laddr.port))
    except (psutil.Error,OSError):
        return processes,[]
    return processes,ports


def dsh_home(home=None):
    """DSH 的用户目录；DSH_HOME 优先，默认 ~/.dsh。"""
    if home is not None:return Path(home)
    env=str(os.environ.get('DSH_HOME') or '').strip()
    return Path(env) if env else Path.home()/'.dsh'


def hint_ports(saved='', home=None, limit=HINT_PORT_LIMIT):
    """从 DSH 自己的文件里找它用过的端口；只取端口号，文件内容一概不外传、不落日志。"""
    ports=[]
    def add(value):
        try: port=int(value)
        except (TypeError,ValueError): return
        if 1<=port<=65535 and port not in ports: ports.append(port)
    add(port_of(normalized(saved)))
    add(DEFAULT_PORT)
    root=dsh_home(home)
    files=[]
    try:
        if not root.is_dir(): return ports
        for path in root.rglob('*'):
            try:
                if not path.is_file(): continue
                if path.name.casefold().startswith('.credentials'): continue
                if path.suffix.casefold() not in ('.log','.json','.jsonl','.yaml','.yml','.txt'): continue
                files.append((path.stat().st_mtime,path))
            except OSError: continue
    except OSError:
        return ports
    files.sort(key=lambda row: row[0],reverse=True)
    for _,path in files[:HINT_SCAN_FILES]:
        try: data=path.read_bytes()[-HINT_FILE_BYTES:].decode('utf-8','replace')
        except OSError: continue
        for raw in _PORT_HINT.findall(data):
            add(raw)
            if len(ports)>=limit: return ports
    return ports


def candidates(processes,ports):
    owned={p['pid'] for p in processes if str(p.get('name','')).casefold() in ROOT_NAMES}
    roots=set(owned)
    # 有界展开父子关系，不把无关node或Electron当作DSH。
    for _ in range(12):
        more={p['pid'] for p in processes if p.get('parent') in owned}
        if more<=owned:break
        owned|=more
    bypid={p['pid']:p for p in processes}
    found=[];seen=set()
    for row in ports:
        if row.get('pid') not in owned or type(row.get('port')) is not int or not 1<=row['port']<=65535:continue
        try:addr=ipaddress.ip_address(row['address'])
        except ValueError:continue
        if not (addr.is_loopback or addr.is_unspecified):continue
        host='[::1]' if addr.version==6 else '127.0.0.1'
        url=f"http://{host}:{row['port']}"
        if url in seen:continue
        seen.add(url)
        found.append(dict(pid=row['pid'],process=bypid.get(row['pid'],{}).get('name','DSH'),base_url=url))
    return sorted(found,key=lambda r:(r['pid'] not in roots,r['pid'],r['base_url']))[:20]


def probe_rows(ports):
    """线索端口转成待核验行；pid=0 表示不是靠进程归属找到的。"""
    return [dict(pid=0,process='probe',base_url=f'http://127.0.0.1:{port}',source='probe') for port in ports]


def normalized(value):
    """已保存地址可能没带协议头或已失效；规范化失败就按原样比较，不影响发现流程。"""
    try:
        return local_url(value)
    except DshError:
        return str(value or '').strip()


def port_of(url):
    try:
        return int(urlsplit(url).port or 0)
    except ValueError:
        return 0


async def verify(service, rows, timeout=2):
    """并发做协议核验：只有真的回答 settings/describe 与 agentPresets/list 的才算 DSH。"""
    semaphore=asyncio.Semaphore(4)
    async def check(row):
        async with semaphore:
            try:
                base=row['base_url']
                cookie=cookie_for(base)
                async with httpx.AsyncClient(headers={'Cookie':cookie},timeout=timeout,trust_env=False,follow_redirects=False) as client:
                    await service.rpc(client,base,'settings/describe')
                    presets=await service.rpc(client,base,'agentPresets/list')
                entries=presets if isinstance(presets,list) else (presets or {}).get('presets',[])
                if not isinstance(entries,list):raise DshError('DSH人设列表格式不兼容')
                return dict(row,verified=True,preset_listed=any(isinstance(r,dict) and r.get('id')=='whale-wechat-chat' for r in entries))
            except (DshError,httpx.HTTPError,ValueError,TypeError,AttributeError):
                return dict(row,verified=False)
    return await asyncio.gather(*(check(row) for row in rows))


def classify(verified, saved, pid, process_count, checked_ports, home_present=None, probed=0):
    """按“端口漂移→端点可用→协议失败→端口不响→没有进程/没装/没在跑”顺序判定，先命中先返回。"""
    saved = normalized(saved)
    try:
        host = urlsplit(saved).hostname
    except ValueError:
        host = None
    if verified:
        if any(row['base_url'] == saved for row in verified):
            return ('ok', None, '端点可用，可以直接保存或连接',
                    f'已确认 {len(verified)} 个DSH本机端点；请选择并保存，不会自动启用或修改已有会话。')
        # 多个候选时先认同一台主机（127.0.0.1 与 ::1 是两个地址），再取端口最小者。
        target = sorted(verified, key=lambda row: (urlsplit(row['base_url']).hostname != host,
                                                   port_of(row['base_url']), row['base_url']))[0]['base_url']
        drift = {'from': saved, 'to': target, 'from_port': port_of(saved), 'to_port': port_of(target)}
        return ('drift', drift,
                f"端口已变更：{drift['from_port']} → {drift['to_port']}，请点「更新为 {drift['to_port']}」后重新探测",
                f'已确认 {len(verified)} 个DSH本机端点，但与已保存地址不一致；请更新为 {drift["to_port"]} 端口后重新探测。')
    if process_count and checked_ports:
        return ('protocol_failed', None,
                f'该端口不是 DSH 服务或需要登录（PID {pid}），请打开并登录桌面 DSH 后重试',
                f'已发现 DSH 进程，但核验的 {checked_ports} 个端口都不是可用的DSH服务；请打开并登录桌面 DSH 后重试。')
    if process_count:
        return ('port_unreachable', None,
                f'进程在（PID {pid}），但端口无响应，可能仍在启动，请等待后重试',
                '已发现 DSH 进程，但读取不到可核验的监听端口；可能仍在启动，请等待后重试。')
    if home_present is False:
        return ('dsh_missing', None,
                '这台电脑上没找到 DSH（没有 ~/.dsh 目录），请先安装桌面 DSH 并打开一次',
                '未发现 DSH 进程，也没有找到 DSH 的用户目录；请先安装桌面 DSH，打开它的网页界面并登录，再回来点「自动查找桌面DSH」。')
    if probed:
        return ('dsh_offline', None,
                f'这台电脑装过 DSH（有配置），但 {probed} 个候选端口都没有可用的 DSH 服务；请先打开桌面 DSH 并登录',
                f'已按 DSH 用户目录里的 {probed} 个线索端口逐个核验，都没有响应；请打开桌面 DSH（网页界面）并登录后重试，'
                '或把浏览器地址栏里的 http://127.0.0.1:端口 粘到「DSH 地址」。')
    return ('process_missing', None, '未找到 DSH 进程，请先启动桌面 DSH',
            '未找到 DSH 进程；请先启动桌面 DSH 后重新探测。')


async def discover(service, inventory=None, hints=None):
    """inventory 与 hints 只给测试注入；生产走进程树 + 线索端口。"""
    processes,ports=inventory if inventory is not None else await asyncio.to_thread(snapshot)
    rows=candidates(processes,ports)
    verified=[r for r in await verify(service,rows) if r['verified']]
    try:saved=str(service.settings().get('base_url',''))
    except (DshError,AttributeError,TypeError,ValueError):saved=''
    home_present=None;probed=0
    if inventory is None:
        home_present=await asyncio.to_thread(lambda: dsh_home().is_dir())
        if not verified:
            wanted=hints if hints is not None else await asyncio.to_thread(hint_ports,saved)
            known={port_of(row['base_url']) for row in rows}
            extra=[port for port in wanted if port not in known]
            if extra:
                probed=len(extra)
                verified=[r for r in await verify(service,probe_rows(extra),timeout=PROBE_TIMEOUT) if r['verified']]
    pids=sorted({p['pid'] for p in processes if str(p.get('name','')).casefold() in ROOT_NAMES})
    total=len(rows)+probed
    state,drift,hint,message=classify(verified,saved,rows[0]['pid'] if rows else (pids[0] if pids else ''),
        len(pids),total,home_present=home_present,probed=probed)
    return dict(success=True,candidates=verified,process_count=len(pids),
        checked_ports=total,unverified_ports=total-len(verified),message=message,
        state=state,saved_base_url=saved,drift=drift,hint=hint,
        probed_ports=probed,port_owner_known=any(row.get('pid') for row in ports))
