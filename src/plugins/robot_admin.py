"""仅管理机器人；身份由本机绑定的实际私聊会话决定。"""
import re
import json
from . import PluginBase, PluginMeta, FieldSchema
from ..runtime_identity import APP_VERSION


def parse_command(text):
    match = re.fullmatch(r'/?机器人\s*(状态|连接|队列|日志|人设|插件|暂停回复|恢复回复|帮助)', str(text or '').strip())
    return match[1] if match else None


def clean_log(text):
    text = re.sub(r'(?i)\bBearer\s+[^\s,，;；]+', 'Bearer [已隐藏]', str(text))
    text = re.sub(r'sk-[A-Za-z0-9_-]{8,}', '[密钥已隐藏]', str(text))
    text = re.sub(r'(?i)(api[_ -]?key|authorization|password|secret|token)\s*[:=]\s*[^\s,，;；]+', r'\1=[已隐藏]', text)
    text = re.sub(r'https?://\S+', '[接口地址已隐藏]', text)
    return text[:180]


class RobotAdmin(PluginBase):
    META = PluginMeta(id='robot_admin', name='机器人私聊管理员', category='management', enabled_by_default=False,
        description='在插件页“会话监控与管理员”绑定实际私聊联系人后，私聊发送“机器人 状态/连接/队列/日志/人设/插件/暂停回复/恢复回复”。仅管理本机器人。',
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用私聊管理员指令'),
            'ownerIds': FieldSchema(type='string', label='已绑定管理员', editor='admin_owners', multiline=True,
                description='只读。在插件页“会话监控与管理员”选择实际收到消息的私聊联系人绑定或解除。'),
            'ownerAccounts': FieldSchema(type='string', label='管理员账号隔离记录', editor='internal'),
            'paused': FieldSchema(type='bool', label='暂停全部自动回复', description='管理员查询仍可使用；恢复后不补发积压消息。'),
            'maxPerHour': FieldSchema(type='int', label='每位管理员每小时最多指令数',min=1,max=100),
        }, default_settings={'enabled':False,'ownerIds':'','ownerAccounts':'{}','paused':False,'maxPerHour':20})

    def validate_settings(self, settings):
        if any(settings.get(k,'') != self.get(k,'') for k in ('ownerIds','ownerAccounts')) and not getattr(self,'_binding',False):
            raise ValueError('请从“会话监控与管理员”绑定实际收到过私聊的联系人')

    def owners(self, account):
        try:
            owners = json.loads(self.get('ownerAccounts','{}')).get(account,[])
            return [v for v in owners if isinstance(v,str)] if isinstance(owners,list) else []
        except (ValueError,AttributeError):
            return []

    def is_admin(self, message, runtime):
        target = str(message.get('target') or '')
        return (self.enabled and message.get('kind') == 'private'
                and target == str(message.get('sender_id') or '')
                and target in self.owners(runtime.management_account()))

    def on_ingest(self, message, runtime):
        command = parse_command(message.get('text'))
        if not command or not self.is_admin(message,runtime):
            return None
        import time
        count = runtime.db.execute("SELECT count(*) FROM inbox WHERE mode=? AND account=? AND created>? "
            "AND json_extract(payload,'$._robot_admin')=1 AND json_extract(payload,'$.target')=?",
            (runtime.mode,runtime.account(),time.time()-3600,message['target'])).fetchone()[0]
        if count >= int(self.get('maxPerHour',20)):
            runtime.note('管理员指令达到每小时上限，请在本机插件页管理')
            return ''
        message['_robot_admin'] = True
        if command in ('暂停回复','恢复回复'):
            paused = command == '暂停回复'
            self._manager.update_settings(self.META.id, {'paused':paused})
            runtime.cancel_disallowed_pending()
            runtime.note('管理员已暂停自动回复' if paused else '管理员已恢复自动回复')
            return '已暂停自动回复，管理员通道保留。已经开始发送的消息无法撤回。' if paused else '已恢复自动回复，不补发暂停期间消息。'
        if command == '状态':
            return f"鲸鱼娘 {APP_VERSION}\n微信：{'在线' if runtime.transport.online else '离线'}\n自动回复：{'已暂停' if self.get('paused') else '正常'}\n模型：{runtime.ai.model}\n当前阶段：{runtime.pipeline['stage']}"
        if command == '连接':
            return f"后端：{runtime.bridge.backend}\n微信：{'在线' if runtime.transport.online else '离线'}\n私聊监听：{'开启' if runtime.config.get('wechat_only.listen_private',True) else '关闭'}"
        if command == '队列':
            rows = runtime.db.execute('SELECT state,count(*) FROM inbox WHERE mode=? AND account=? GROUP BY state', (runtime.mode,runtime.account())).fetchall()
            return '本账号消息队列：\n' + ('\n'.join(f'{state}：{count}' for state,count in rows) or '暂无消息')
        if command == '日志':
            return '最近运行记录：\n' + ('\n'.join(clean_log(v['text']) for v in runtime.events[-6:]) or '暂无记录')
        if command == '人设':
            return '当前人设：' + str(runtime.style.get().display_name)
        if command == '插件':
            return '机器人插件：\n' + '\n'.join(('✓ ' if p.enabled else '○ ') + p.META.id+'：'+p.META.name for p in runtime.plugins._plugins.values())[:1700]
        return '支持：机器人 状态/连接/队列/日志/人设/插件/暂停回复/恢复回复。管理员需在本机插件页绑定。'
