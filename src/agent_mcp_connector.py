"""可导出的标准库MCP连接器；凭据仅从外部Agent进程的环境读取。"""
import time

from .runtime_identity import PORT

CONNECTOR = r'''"""鲸鱼娘本地主人工作区MCP：Python3.10+，JSON-RPC newline stdio。"""
import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

SPECS = {
    'workspace_info': ('workspace.info', {}, []),
    'list_notes': ('workspace.files', {}, []),
    'read_note': ('workspace.read', {'name': {'type':'string'}}, ['name']),
    'write_note': ('workspace.write', {'name':{'type':'string'},'content':{'type':'string'},'version':{'type':'string'}}, ['name','content','version']),
    'search_memory': ('workspace.memories', {'query':{'type':'string'},'persona':{'type':'string'}}, []),
    'remember': ('workspace.remember', {'content':{'type':'string'},'persona':{'type':'string'}}, ['content']),
    'forget': ('workspace.forget', {'id':{'type':'string'}}, ['id']),
    'agent_chat': ('agent.chat', {'session_id':{'type':'string'},'text':{'type':'string'},'persona':{'type':'string'}}, ['session_id','text']),
    'chat_history': ('agent.history', {'session_id':{'type':'string'}}, ['session_id']),
    'conversation_search': ('archive.search', {'kind':{'type':'string','enum':['group','private']},'target':{'type':'string'},'q':{'type':'string'},'offset':{'type':'integer','minimum':0,'maximum':100000}}, ['kind','target']),
    'learning_overview': ('learning.overview', {}, []),
    'learning_search': ('learning.search', {'kind':{'type':'string','enum':['group','private']},'target':{'type':'string'}}, ['kind','target']),
    'learning_review': ('learning.review', {'account':{'type':'string'},'kind':{'type':'string','enum':['group','private']},'target':{'type':'string'},'action':{'type':'string','enum':['learn','approve','reject']},'identity':{'type':'string'}}, ['account','kind','target','action']),
}
READS = {'workspace.info','workspace.files','workspace.read','workspace.memories','agent.history','archive.search','learning.overview','learning.search'}
# 控制台动作目录与执行，走 /api/agent/control/*，与bridge动作表不同一套形状。
CONTROL = {
    'console_catalog': ('console.catalog（读控制台动作目录，只读）', {}, []),
    'console_execute': ('console.execute（执行一个控制台动作，写动作需要稳定request_id）',
        {'action':{'type':'string'},'params':{'type':'object'},'query':{'type':'object'},'body':{'type':'object'}}, ['action']),
}
SPECS.update(CONTROL)


def call(name, args):
    if name in CONTROL: return control(name, args)
    if name not in SPECS or not isinstance(args,dict): raise ValueError('未知工具或参数格式无效')
    action, fields, required = SPECS[name]
    if set(args)-set(fields) or not set(required)<=set(args):
        raise ValueError('工具参数缺失或类型无效')
    if any((type(v) is not int or not 0<=v<=100000) if fields[k]['type']=='integer' else not isinstance(v,str) for k,v in args.items()):
        raise ValueError('工具参数类型或范围无效')
    if any('enum' in fields[k] and v not in fields[k]['enum'] for k,v in args.items()):
        raise ValueError('工具参数不在允许范围内')
    base,token=endpoint()
    rid=uuid.uuid4().hex
    body=dict(args)
    if action=='agent.chat':body['request_id']=rid
    payload={'action':action,'request_id':rid,('query' if action in READS else 'body'):body}
    return send(base,token,'POST','/api/agent/bridge/execute',payload)


DEFAULT_PORT=8091


def console_port():
    # 与后台同一条规则：环境变量优先，写错的值回落到默认端口。
    try:
        port=int(os.environ.get('WECHAT_CONSOLE_PORT') or DEFAULT_PORT)
    except (TypeError,ValueError):
        return DEFAULT_PORT
    return port if 0<port<=65535 else DEFAULT_PORT


def endpoint():
    base=os.environ.get('WHALE_BOT_URL','http://127.0.0.1:%d'%console_port())
    parsed=urllib.parse.urlsplit(base)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username or parsed.password or not parsed.port:
        raise ValueError('WHALE_BOT_URL只能是带端口的本机http地址')
    token=os.environ.get('WHALE_AGENT_TOKEN','')
    if not token: raise ValueError('请在外部Agent的MCP环境中配置WHALE_AGENT_TOKEN')
    return base.rstrip('/'),token


def send(base, token, method, path, payload=None, *, timeout=75):
    data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
    request=urllib.request.Request(base+path,data=data,method=method,
        headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    try:
        # 本机请求不走系统代理，不跟随重定向，不自动重试写入。
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=timeout) as response:
            raw=response.read(2*1024*1024+1)
        if len(raw)>2*1024*1024:raise ValueError('接口返回过大')
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise ValueError('接口拒绝请求 HTTP '+str(exc.code)+'，请检查绑定及凭据权限') from None
    except urllib.error.URLError:
        raise ValueError('本机控制台不可用；没有自动重试') from None


def control(name, args):
    base,token=endpoint()
    if name=='console_catalog':
        if not isinstance(args,dict) or args: raise ValueError('工具参数缺失或类型无效')
        return send(base,token,'GET','/api/agent/control/catalog')
    if (not isinstance(args,dict) or set(args)-{'action','params','query','body'}
            or not isinstance(args.get('action'),str) or not args['action']):
        raise ValueError('工具参数缺失或类型无效')
    params,query,body=(args.get(k,{}) for k in ('params','query','body'))
    if any(not isinstance(v,dict) for v in (params,query,body)): raise ValueError('params、query、body必须为对象')
    # 控制面只接受path槽位的字符串标识，query值只能是标量，这里先收紧再发。
    if (any(not isinstance(k,str) or not isinstance(v,str) or not v or len(v)>100 for k,v in params.items())
            or any(not isinstance(k,str) or len(k)>80 or type(v) not in (str,int,bool,float) for k,v in query.items())):
        raise ValueError('操作目标标识或查询参数类型无效')
    payload={'action':args['action'],'params':params,'query':query,'body':body,'request_id':uuid.uuid4().hex}
    return send(base,token,'POST','/api/agent/control/execute',payload)


def announce():
    # 启动时尽力上报一次：DSH每次重启端口都会变，控制台据此重新发现地址并落盘。
    def post():
        try:
            base,token=endpoint()
            # 走通用控制通道；3秒超时、只发这一次，失败一律静默（stdout是JSON-RPC通道，不能写）。
            send(base,token,'POST','/api/agent/control/execute',
                 {'action':'dsh.announce','params':{},'query':{},'body':{},'request_id':uuid.uuid4().hex},timeout=3)
        except Exception:
            return
    thread=threading.Thread(target=post,daemon=True)
    thread.start()
    return thread


def handle(message):
    method=message.get('method')
    if method=='initialize':
        version=(message.get('params') or {}).get('protocolVersion','2024-11-05')
        return {'protocolVersion':version,'capabilities':{'tools':{}},'serverInfo':{'name':'whale-owner-workspace','version':'2'}}
    if method=='ping':return {}
    if method=='tools/list':
        return {'tools':[{'name':name,'description':'主人工作区 '+action+'；权限由宿主检查，写入不自动重试',
                'inputSchema':{'type':'object','properties':fields,'required':required,'additionalProperties':False}}
                for name,(action,fields,required) in SPECS.items()]}
    if method=='tools/call':
        params=message.get('params') or {}
        try:
            result=call(params.get('name'),params.get('arguments',{}))
            return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}],'isError':not result.get('success',False)}
        except Exception as exc:
            text=str(exc) if isinstance(exc,ValueError) else '连接器未完成：'+type(exc).__name__
            return {'content':[{'type':'text','text':text}],'isError':True}
    raise ValueError('不支持的MCP方法')


def main():
    for line in sys.stdin:
        message = None
        if len(line)>256*1024:continue
        try:
            message=json.loads(line)
            if not isinstance(message,dict) or 'id' not in message:continue
            result=handle(message)
            response={'jsonrpc':'2.0','id':message['id'],'result':result}
        except Exception:
            response={'jsonrpc':'2.0','id':message.get('id') if isinstance(message,dict) else None,
                      'error':{'code':-32600,'message':'MCP请求无效'}}
        print(json.dumps(response,ensure_ascii=False),flush=True)


if __name__=='__main__':
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    announce()
    main()
'''


# 判断「这是控制台自己生成的连接器」只看内容特征，不看文件名：
# 首行是控制台模板首行，且两个特征函数都在，才能安全原地升级。
# 首行故意写成历史字面量而不是从 CONNECTOR 推导：模板以后改了首行，
# 真机上那些升级前的旧文件还得认得出来。
CONNECTOR_SIGNATURE = (
    '"""鲸鱼娘本地主人工作区MCP：Python3.10+，JSON-RPC newline stdio。"""',
    'def endpoint()',
    'def control(name, args)',
)


def generated_by_console(text):
    """内容特征命中三个条件才返回 True；命中即视为可安全升级的旧连接器。"""
    lines=str(text or '').splitlines()
    if not lines or lines[0].rstrip()!=CONNECTOR_SIGNATURE[0]:
        return False
    return all(marker in text for marker in CONNECTOR_SIGNATURE[1:])


def _backup_file(path, raw):
    """在连接器同目录落一份原始字节备份，返回备份文件名。

    写不进去就向上抛：调用点必须因此放弃覆盖，绝不能出现两处都丢的窗口。
    """
    stamp=time.strftime('%Y%m%d-%H%M%S',time.localtime())
    base=path.name+'.bak-'+stamp
    target,index=path.with_name(base),2
    while target.exists():
        target,index=path.with_name(base+'-'+str(index)),index+1
    target.write_bytes(raw)
    return target.name


def place(path, text):
    """写接入文件：内容一致不写；控制台生成的旧连接器先备份成功再原地覆盖。

    返回 {'path','backup','changed','message'}；被用户改过的其它文件仍按原样抛错。
    """
    from pathlib import Path
    from .agent_workspace import AgentWorkspace, WorkspaceError
    from .persistence import atomic_text
    path=Path(path)
    AgentWorkspace._safe(path)
    if not path.exists():
        atomic_text(path,text,backup=False)
        return {'path':path,'backup':'','changed':True,'message':'已生成接入文件：'+path.name}
    raw=path.read_bytes()
    if raw==text.encode('utf-8'):
        # 幂等短路：不写、不改 mtime，避免每次预览都动一遍磁盘。
        return {'path':path,'backup':'','changed':False,'message':'接入文件已是最新，没有改动：'+path.name}
    try:
        old=raw.decode('utf-8')
    except UnicodeDecodeError:
        old=''
    if not generated_by_console(old):
        raise WorkspaceError('接入文件已被本地修改，为保留修改没有覆盖：'+path.name)
    try:
        backup=_backup_file(path,raw)
    except OSError as error:
        # 备份没落盘就绝不覆盖：宁可这次不升级，也不能抹掉用户机器上唯一那份。
        raise WorkspaceError('备份旧接入文件失败，为保留修改没有覆盖：'+path.name
                             +'（'+type(error).__name__+'）') from None
    atomic_text(path,text,backup=False)
    return {'path':path,'backup':backup,'changed':True,'message':'已更新，原文件备份为 '+backup}


def place_connector(path):
    """把DSH连接器写到指定路径：内容一致不写，控制台生成的旧版先备份再原地升级。"""
    return place(path, CONNECTOR)


def refresh_existing(path):
    """启动时尽力把真机上已存在的连接器刷成当前模板；绝不新建文件、绝不抛错。

    与 place() 的差别只有一处：这是无人值守的启动路径，任何失败都必须安静收场，
    连 place() 那份「用户改过就抛错」的行为也降级成返回值。
    返回 {'changed','backup','reason'}，reason ∈ missing/current/upgraded/foreign/blocked/error。
    """
    from pathlib import Path
    from .agent_workspace import WorkspaceError
    try:
        path=Path(path)
        if not path.is_file():
            # 没有就不建：用户机器上可能挂着自建连接器，凭空生成只会多一份迷惑。
            return {'changed': False, 'backup': '', 'reason': 'missing'}
        raw=path.read_bytes()
        if raw==CONNECTOR.encode('utf-8'):
            # 幂等短路，连 mtime 都不动，避免每次启动都白脏一次磁盘。
            return {'changed': False, 'backup': '', 'reason': 'current'}
        try:
            old=raw.decode('utf-8')
        except UnicodeDecodeError:
            old=''
        if not generated_by_console(old):
            return {'changed': False, 'backup': '', 'reason': 'foreign'}
        try:
            placed=place(path, CONNECTOR)
        except WorkspaceError:
            # 备份没落盘或路径不安全时 place() 拒绝覆盖；启动路径同样保持原样、绝不抛错。
            return {'changed': False, 'backup': '', 'reason': 'blocked'}
        return {'changed': bool(placed['changed']), 'backup': placed['backup'], 'reason': 'upgraded'}
    except Exception:
        # 启动路径的最后一道网：读盘、比较之外的任何意外都不许把控制台带崩。
        return {'changed': False, 'backup': '', 'reason': 'error'}


def export_connector(area):
    import json
    folder=area.folder().parent
    # 版本文件独立输出，升级不覆盖用户已经编辑的连接器与外部Agent配置。
    script=folder/'whale_workspace_mcp_v2.py'
    config=folder/'mcp.v2.example.json'
    example={'mcpServers':{'whale-owner':{'command':'python','args':[str(script)],
        'env':{'WHALE_BOT_URL':f'http://127.0.0.1:{PORT}','WHALE_AGENT_TOKEN':'请在外部Agent环境填写接入凭据'}}}}
    placed=place(script,CONNECTOR)
    place(config,json.dumps(example,ensure_ascii=False,indent=2))
    # 升级过旧连接器就把「已更新，原文件备份为 …」摆在最前面，界面/接口直接看得到。
    head=placed['message']+'\n' if placed['backup'] else ''
    return {'path':str(folder),'script':str(script),'config':str(config),'backup':placed['backup'],
            'message':head+f'已生成MCP v2：{script}\n配置示例：{config}\n需要Python3.10+；在DeepSeek Harness的MCP配置中使用此示例，WHALE_AGENT_TOKEN填入管理员页签发的凭据。查询要“读记忆”，整理审阅要“写记忆”。未自动写入凭据或修改DSH配置。'}
