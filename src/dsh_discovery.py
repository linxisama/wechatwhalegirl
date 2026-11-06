"""只读DSH进程树与现有监听端口，协议验证后才作为候选，不猜固定端口。

普通权限（没有管理员）时 Windows 拿不到别的进程的端口归属：psutil.net_connections() 只会给
出 pid 为空的 LISTEN 行。这时退回「按 DSH 自己留下的线索端口逐个协议核验」——依然不猜：
只有真正回答 settings/describe + agentPresets/list 的端口才会被当成候选。
"""
import asyncio
import ipaddress
import os
import re
import threading
import time
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
PROBE_CONCURRENCY=4          # 同时核验几个端口；再大只会让本机 DSH 排队，不会更快
DISCOVERY_TTL=5.0            # 一次发现的结果在这几秒内复用：面板连点、守护轮询不该重复扫盘
DISCOVERY_FAIL_TTL=1.0       # 失败结论只留 1 秒：刚启动的 DSH 不该被一份过期结论钉死
DISCOVERY_BUDGET=12.0        # 单次发现的总预算，必须小于 /api/dsh/discover 的 15 秒硬超时
_PORT_HINT=re.compile(r'(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0):(\d{2,5})')

# 连接失败码 → 中文短句 / 可照做的下一步，与 src/send_history.py 的 CODE_TEXT/CODE_HINT 同口径。
# 拆成两张表是因为「是什么」和「怎么办」在界面上分行显示：只有码，用户看不懂。
CODE_TEXT = {
    'dsh_ok': '连接正常',
    'dsh_unreachable': '连不上 DSH 服务（端口没有响应或连接被拒绝）',
    'dsh_auth_failed': 'DSH 登录态或本机凭据失效',
    'dsh_timeout': 'DSH 在限定时间内没有回应',
    'dsh_version_mismatch': 'DSH 版本与本程序不兼容',
    'dsh_endpoint_moved': 'DSH 端口已变更',
    'dsh_not_running': '桌面 DSH 没在运行或还没装',
    'dsh_preset_missing': '缺少专用无工具人设',
    'dsh_config_missing': '没找到 DSH 桌面配置文件',
    'dsh_setup_incomplete': '接入配置还没补齐（缺 MCP 行或新会话默认预设）',
    'dsh_state_unreadable': '读不出 DSH 连接状态',
}
CODE_HINT = {
    'dsh_ok': '',
    'dsh_unreachable': '先打开桌面 DSH 并登录，再点「自动查找桌面DSH」；仍不行就把浏览器地址栏里的 http://127.0.0.1:端口 填进「DSH 地址」',
    'dsh_auth_failed': '在桌面 DSH 里重新登录一次，再回控制台点「只读检测DSH」；控制台不会替你保存或修改密钥',
    'dsh_timeout': '等几秒重试一次；一直超时说明 DSH 正在启动或卡住了，重启桌面 DSH 后再点「只读检测DSH」',
    'dsh_version_mismatch': '把桌面 DSH 与本控制台都升到最新版；版本对不上时协议核验必然失败，重试没有用',
    'dsh_endpoint_moved': '点横幅上的「更新为 XXXX」把地址改成新端口，然后重新检测',
    'dsh_not_running': '先启动桌面 DSH（网页界面）并登录，再回来点「自动查找桌面DSH」',
    'dsh_preset_missing': '点「安装专用人设（先备份）」，然后手动重启一次 DSH（配置只在启动时读取）',
    'dsh_config_missing': '先安装并启动一次 DeepSeek Harness，让它生成桌面配置文件后再点「一键修复连接」',
    'dsh_setup_incomplete': '点「一键修复连接」补齐缺失步骤，然后重启一次 DeepSeek Harness（配置只在启动时读取）',
    'dsh_state_unreadable': '稍等几秒再点「刷新」；一直读不出来就重启控制台（配置没动，不会丢东西）',
}
_STATE_CODE = {
    'ok': 'dsh_ok', 'drift': 'dsh_endpoint_moved', 'protocol_failed': 'dsh_version_mismatch',
    'port_unreachable': 'dsh_unreachable', 'dsh_missing': 'dsh_not_running',
    'dsh_offline': 'dsh_not_running', 'process_missing': 'dsh_not_running',
    'timeout': 'dsh_timeout', 'auth_failed': 'dsh_auth_failed',
}
# 桥抛出来的中文里没有码，只能按关键片段归类；顺序即优先级，先命中先返回。
# 不可达排在最前：桥的原文是「无法连接 DSH 地址（连接被拒绝或超时）」，两句里都带「超时」，
# 先判超时会把「端口没开」说成「DSH 没回应」，让用户去重启一个根本没启动的程序。
_MESSAGE_CODES = (
    ('dsh_unreachable', ('无法连接', '连接被拒绝', 'connect', 'refused', 'unreachable')),
    ('dsh_auth_failed', ('登录', '凭据', '鉴权', '认证', '401', '403', 'unauthorized', 'forbidden')),
    ('dsh_version_mismatch', ('协议', '信封', '不兼容', '版本')),
    ('dsh_preset_missing', ('人设',)),
    ('dsh_timeout', ('超时', 'timed out', 'timeout')),
)


def describe_code(code):
    """码 → 中文短句；未知码返回空串（界面只显示码本身，不重复一遍码）。"""
    return CODE_TEXT.get(str(code or ''), '')


def hint_for_code(code):
    """码 → 可照做的中文提示；没有就返回空串。"""
    return CODE_HINT.get(str(code or ''), '')


def code_for_state(state):
    """发现状态 → 连接码；未知状态给空串，不硬套一个错的码。"""
    return _STATE_CODE.get(str(state or ''), '')


def code_for_message(text):
    """从桥的中文失败原因里归一个码；认不出来返回空串，由调用方决定兜底。"""
    body = str(text or '').casefold()
    if not body:
        return ''
    for code, marks in _MESSAGE_CODES:
        if any(mark in body for mark in marks):
            return code
    return ''


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


class ProbeClients:
    """一次发现里复用同一个连接；地址换了先把旧客户端关掉，别把 socket 留给下一次重连。

    httpx.AsyncClient 自己带连接池，但池的生命周期必须有人负责：每次重连都新建而不关旧的，
    长时间开着守护就会攒下一堆没人关的 socket。这里把「换地址」做成显式的 reconnect。
    """

    def __init__(self, timeout=2):
        self.timeout=timeout
        self._clients={}

    def acquire(self, base):
        client=self._clients.get(base)
        if client is not None and not client.is_closed:
            return client
        if client is not None:
            self._clients.pop(base,None)
        client=httpx.AsyncClient(headers={'Cookie':cookie_for(base)},timeout=self.timeout,
                                 trust_env=False,follow_redirects=False)
        self._clients[base]=client
        return client

    async def reconnect(self, base):
        """断线重连：先关旧客户端再建新的，旧连接池不会随重试一起泄漏。"""
        await self.close(base)
        return self.acquire(base)

    async def close(self, base):
        client=self._clients.pop(base,None)
        if client is None:
            return False
        try:
            await client.aclose()
        except (httpx.HTTPError,OSError,RuntimeError):
            pass
        return True

    async def aclose(self):
        bases=list(self._clients)
        for base in bases:
            await self.close(base)

    def opened(self):
        return sorted(base for base,client in self._clients.items() if not client.is_closed)


async def verify(service, rows, timeout=2, clients=None):
    """并发做协议核验：只有真的回答 settings/describe 与 agentPresets/list 的才算 DSH。"""
    semaphore=asyncio.Semaphore(PROBE_CONCURRENCY)
    pool=clients if clients is not None else ProbeClients(timeout)
    async def check(row):
        async with semaphore:
            try:
                base=row['base_url']
                client=pool.acquire(base)
                await service.rpc(client,base,'settings/describe')
                presets=await service.rpc(client,base,'agentPresets/list')
                entries=presets if isinstance(presets,list) else (presets or {}).get('presets',[])
                if not isinstance(entries,list):raise DshError('DSH人设列表格式不兼容')
                return dict(row,verified=True,preset_listed=any(isinstance(r,dict) and r.get('id')=='whale-wechat-chat' for r in entries))
            except (DshError,httpx.HTTPError,ValueError,TypeError,AttributeError):
                return dict(row,verified=False)
    try:
        return await asyncio.gather(*(check(row) for row in rows))
    finally:
        if clients is None:
            await pool.aclose()


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


class DiscoveryCache:
    """发现结果缓存 + 单飞。

    面板连点、守护每 30 秒轮询、上报失败后的兜底发现，过去各扫一遍进程树和 ~/.dsh；
    端口探测还是每行一个新 httpx 客户端。这里给结果加 5 秒复用，并保证同一时刻只有一轮
    真正在扫：后来者等前一轮的结果，而不是再起一套线程和连接。
    失败结果只留很短时间——刚启动的 DSH 不能被一份过期结论钉死。
    """

    def __init__(self, ttl=DISCOVERY_TTL, fail_ttl=DISCOVERY_FAIL_TTL):
        self.ttl = float(ttl)
        self.fail_ttl = float(fail_ttl)
        self.clock = time.monotonic
        self._guard = threading.Lock()
        self._entry = None
        self._busy = None
        self._busy_deadline = None
        self.runs = 0

    def begin(self, key, budget=None):
        """('hit', 结果) / ('lead', 本轮令牌) / ('wait', 别人的令牌)。"""
        with self._guard:
            entry = self._entry
            if entry is not None and entry['key'] == key and entry['expires'] > self.clock():
                return 'hit', dict(entry['value'])
            if self._busy is not None:
                return 'wait', self._busy
            token = threading.Event()
            self._busy = token
            # 预算只算一次：后来者按剩余时间等，不会各自再等一整份。
            self._busy_deadline = None if budget is None else self.clock() + float(budget)
            self.runs += 1
            return 'lead', token

    def remaining(self):
        """当前这一轮还剩几秒可等；没有在扫或没给预算时返回 None。"""
        with self._guard:
            if self._busy is None or self._busy_deadline is None:
                return None
            return self._busy_deadline - self.clock()

    def peek(self, key):
        with self._guard:
            entry = self._entry
            if entry is not None and entry['key'] == key and entry['expires'] > self.clock():
                return dict(entry['value'])
        return None

    def finish(self, key, value, token):
        healthy = str(value.get('state') or '') in ('ok', 'drift')
        with self._guard:
            self._entry = {'key': key, 'value': dict(value),
                           'expires': self.clock() + (self.ttl if healthy else self.fail_ttl)}
            if self._busy is token:
                self._busy = None
                self._busy_deadline = None
        token.set()

    def abort(self, token):
        with self._guard:
            if self._busy is token:
                self._busy = None
                self._busy_deadline = None
        token.set()

    def clear(self):
        with self._guard:
            self._entry = None
            self._busy = None
            self._busy_deadline = None


CACHE = DiscoveryCache()


def _cache_key(service):
    """缓存键只看「用户配的地址」：换地址就是换目标，必须重新核验。"""
    try:
        return normalized(str(service.settings().get('base_url', '')))
    except (DshError, AttributeError, TypeError, ValueError):
        return ''


def _degraded(state, hint, message, saved='', code=''):
    """发现没跑完时的兜底形状：字段与正常结果一致，界面不必写第二套分支。"""
    code = code or code_for_state(state)
    return dict(success=True, candidates=[], process_count=0, checked_ports=0, unverified_ports=0,
                state=state, saved_base_url=saved, drift=None, hint=hint, message=message,
                probed_ports=0, port_owner_known=False, code=code,
                code_text=describe_code(code), code_hint=hint_for_code(code), cached=False)


def _with_code(value):
    """给结果补上码与两句中文：界面只认这三样，不再靠字符串猜。"""
    code = code_for_state(value.get('state'))
    value['code'] = code
    value['code_text'] = describe_code(code)
    value['code_hint'] = hint_for_code(code)
    return value


async def _discover_once(service, inventory, hints):
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
    return _with_code(dict(success=True,candidates=verified,process_count=len(pids),
        checked_ports=total,unverified_ports=total-len(verified),message=message,
        state=state,saved_base_url=saved,drift=drift,hint=hint,
        probed_ports=probed,port_owner_known=any(row.get('pid') for row in ports),cached=False))


async def discover(service, inventory=None, hints=None, budget=None):
    """inventory 与 hints 只给测试注入；生产走进程树 + 线索端口。

    只有生产路径（两者都不注入）才走缓存与单飞：测试注入的是「这一轮的输入」，必须当次生效，
    拿缓存糊过去等于让断言验一份别人的结果。
    """
    started = time.monotonic()
    limit = DISCOVERY_BUDGET if budget is None else float(budget)
    if inventory is not None or hints is not None:
        try:
            async with asyncio.timeout(limit):
                value = await _discover_once(service, inventory, hints)
        except TimeoutError:
            return _degraded('timeout', CODE_HINT['dsh_timeout'], 'DSH自动查找超时，未完成端口核验', '')
        value['elapsed_ms'] = int((time.monotonic() - started) * 1000)
        return value

    key = _cache_key(service)
    kind, token = CACHE.begin(key, budget=limit)
    if kind == 'hit':
        value = dict(token)
        value['cached'] = True
        value['elapsed_ms'] = int((time.monotonic() - started) * 1000)
        return value
    if kind == 'wait':
        # 已有一轮在扫：等它的结论，不另起线程重复扫一遍。等的是它的剩余预算而不是
        # 再等一整份——两份叠加就顶穿了 /api/dsh/discover 的 15 秒硬超时。
        left = CACHE.remaining()
        await asyncio.to_thread(token.wait, limit if left is None else max(0.0, min(limit, left)))
        shared = CACHE.peek(key)
        if shared is not None:
            shared['cached'] = True
            shared['elapsed_ms'] = int((time.monotonic() - started) * 1000)
            return shared
        return _degraded('timeout', CODE_HINT['dsh_timeout'], 'DSH自动查找超时，未完成端口核验', key)
    try:
        async with asyncio.timeout(limit):
            value = await _discover_once(service, inventory, hints)
    except TimeoutError:
        CACHE.abort(token)
        return _degraded('timeout', CODE_HINT['dsh_timeout'], 'DSH自动查找超时，未完成端口核验', key)
    except BaseException:
        CACHE.abort(token)
        raise
    CACHE.finish(key, value, token)
    value = dict(value)
    value['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    return value
