"""绑定身份之外再验证密码；密码消息在任何本地记录前消费并脱敏。"""
import hashlib
import hmac
import json
import secrets
import time
from .persistence import atomic_text
from .admin_password import password_error


class AdminSessions:
    def __init__(self,bot):
        self.bot=bot
        self.path=bot.root/'data/wechat-only/admin_security.json'
        self.pending={}
        self.authenticated={}
        self.failures={}
        self.redact_until={}
        self.credential={}
        if self.path.exists():
            try:
                self.credential=json.loads(self.path.read_text('utf-8'))
                if not all(self.credential.get(k) for k in ('salt','digest')):
                    raise ValueError('invalid credential')
            except Exception:
                # 损坏的密码文件不能被当作未设置密码，降级为免密。
                self.credential={'blocked':True}

    def set_password(self,password):
        error=password_error(password)
        if error:
            raise ValueError(error)
        salt=secrets.token_hex(16)
        digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),300000).hex()
        credential={'salt':salt,'digest':digest}
        atomic_text(self.path,json.dumps(credential),backup=False)
        self.credential=credential
        self.invalidate()
        self.failures.clear()

    def invalidate(self):
        self.redact_until.update(self.pending)
        self.pending.clear()
        self.authenticated.clear()

    def identity(self,message):
        return self.bot.management_account(),message.get('target')

    def logged_in(self,message):
        return time.monotonic()<self.authenticated.get(self.identity(message),0)

    def handles(self,message):
        if (message.get('kind')=='private' and message.get('target')==message.get('sender_id') and
                (self.identity(message) in self.pending or time.monotonic()<self.redact_until.get(self.identity(message),0))):
            return True
        if not self.bot.plugins._plugins['robot_admin'].is_admin(message,self.bot):
            return False
        key=self.identity(message)
        text=message.get('text','').strip()
        if key in self.pending:
            return True
        from .plugins.robot_admin import parse_command
        return bool(parse_command(text)) or text in ('进入管理员模式','退出管理员模式') or text.startswith('机器人 ')

    def handle(self,message):
        bot=self.bot
        key=self.identity(message)
        raw_text=message.get('text','')
        text=raw_text.strip()
        now=time.monotonic()
        message['_robot_admin']=True
        if now<self.redact_until.pop(key,0):
            message['text']='[管理员验证消息已隐藏]'
            return '登录请求已失效，请重新进入管理员模式。'
        if key in self.pending:
            expires=self.pending.pop(key)
            message['text']='[管理员验证消息已隐藏]'
            if not bot.plugins._plugins['robot_admin'].is_admin(message,bot):
                return '管理员权限已变更，请在本机管理页重新确认绑定。'
            if now>expires:
                return '验证已超时，请重新发送“进入管理员模式”。'
            count,locked=self.failures.get(key,(0,0))
            if now<locked:
                return '验证失败次数过多，请五分钟后重试。'
            if locked and now>=locked:
                count=0
            credential=self.credential
            if not credential or credential.get('blocked'):
                return '请先在本机机器人管理页重新设置管理员密码。'
            got=hashlib.pbkdf2_hmac('sha256',raw_text.encode(),bytes.fromhex(credential['salt']),300000).hex()
            if not hmac.compare_digest(got,credential['digest']):
                count+=1
                self.failures[key]=(count,now+300 if count>=5 else 0)
                return '密码不正确，请重新进入管理员模式验证。'
            self.failures.pop(key,None)
            self.authenticated[key]=now+1800
            return '已进入管理员模式，有效期三十分钟。可发送：机器人 帮助。'
        if text=='进入管理员模式':
            if not self.credential or self.credential.get('blocked'):
                return '请先在本机机器人管理页设置管理员密码。'
            if now<self.failures.get(key,(0,0))[1]:
                return '验证已锁定，请五分钟后重试。'
            self.pending[key]=now+60
            return '请在一分钟内发送管理员密码；本机不记录密码正文。'
        if text=='退出管理员模式':
            self.authenticated.pop(key,None)
            return '已退出管理员模式。'
        if self.credential and not self.logged_in(message):
            return '请先发送“进入管理员模式”并验证密码。'
        from .plugins.robot_admin import parse_command
        if parse_command(text) and parse_command(text)!='帮助':
            return bot.plugins._plugins['robot_admin'].on_ingest(message,bot)
        if not self.logged_in(message):
            return '此操作需要设置密码并进入管理员模式。'
        command=text.removeprefix('机器人').strip()
        if command.startswith('切换人设 '):
            name=command[5:].strip()
            bot.activate_persona(name)
            return '已切换并保存人设：'+name
        if command in ('人设列表','查看人设'):
            return '\n'.join(v['name']+'：'+v['display_name'] for v in bot.style.list_styles())[:1800]
        if command.startswith(('开启插件 ','关闭插件 ')):
            enabled=command.startswith('开启插件 ')
            pid=command[5:].strip()
            if pid in ('robot_admin','group_monitor'):
                return '管理员通道和群监听范围请在本机插件页修改。'
            if pid not in bot.plugins._plugins:
                return '未知插件，请发送“机器人 插件”查看标识。'
            bot.plugins.update_settings(pid,{'enabled':enabled})
            bot.cancel_disallowed_pending()
            return ('已开启插件：' if enabled else '已关闭插件：')+pid
        if command in ('聊天记录','回复记录','最近回复'):
            from .plugins.robot_admin import clean_log
            rows=bot.db.execute("SELECT payload,reply,state FROM inbox WHERE mode=? AND account=? "
                "AND json_extract(payload,'$._management_account')=? ORDER BY created DESC LIMIT 8",
                (bot.mode,bot.account(),bot.management_account())).fetchall()
            return '本账号最近记录：\n'+'\n'.join(
                f"{state} · {clean_log(json.loads(raw).get('sender',''))}：{clean_log(json.loads(raw).get('text',''))}\n回复：{clean_log(reply)}"
                for raw,reply,state in rows)[:1800]
        return '管理员指令：机器人 状态/连接/队列/日志/人设/人设列表/插件/聊天记录/暂停回复/恢复回复；机器人 切换人设 标识；机器人 开启插件 标识；机器人 关闭插件 标识；退出管理员模式。'
