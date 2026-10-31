"""私聊策略必须在秒回插件之前执行，避免固定回复绕过名单。"""
from . import PluginBase, PluginMeta, FieldSchema
from ..text_matching import configured_terms


class PrivateReplies(PluginBase):
    META = PluginMeta(id='private_replies', name='私聊回复管理', category='management',
        description='管理哪些私聊可以收到回复。支持全部、仅允许名单、排除名单和关闭自动回复；名单支持会话标识或备注名，推荐稳定的 wxid。管理员通道独立保留。',
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用私聊策略'),
            'mode': FieldSchema(type='select', label='回复范围', options=[
                {'value':'all','label':'全部私聊'}, {'value':'allow','label':'只回复名单内'},
                {'value':'deny','label':'不回复名单内'}, {'value':'off','label':'关闭私聊自动回复'}]),
            'contacts': FieldSchema(type='string', label='私聊名单', multiline=True,
                description='每行一个会话标识或备注名，也可用逗号分隔。允许名单为空时不回复任何普通私聊。可在插件页“会话监控与管理员”查看实际标识。'),
            'stableIdsOnly': FieldSchema(type='bool',label='仅按会话标识匹配名单',description='推荐开启并填写wxid，避免同名联系人获得回复；空允许名单不放行任何人。'),
        }, default_settings={'enabled':True,'mode':'all','contacts':'','stableIdsOnly':False})

    def allows(self, message):
        if not self.enabled or message.get('kind') != 'private':
            return True
        mode = self.get('mode', 'all')
        contacts = {v.strip() for v in configured_terms(self.get('contacts','')) if v.strip()}
        from ..conversation_access import matches
        matched = matches(message,contacts,self.get('stableIdsOnly',False))
        return mode == 'all' or (mode == 'allow' and matched) or (mode == 'deny' and not matched)
