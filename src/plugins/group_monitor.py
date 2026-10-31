"""群监控范围由宿主应用到数据库监听和发送前校验。"""
from . import PluginBase, PluginMeta, FieldSchema
from ..text_matching import configured_terms


class GroupMonitor(PluginBase):
    META = PluginMeta(id='group_monitor', name='群聊监控管理', category='management',
        description='增删监控群，支持只监控不回复。可沿用设置页白名单，或改用此处名单；指定名单为空表示不监听任何群。群消息仍须被 @ 或命中触发词才回复。',
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用群监控策略'),
            'mode': FieldSchema(type='select', label='监控范围', options=[
                {'value':'settings','label':'沿用设置页群白名单'},
                {'value':'selected','label':'只监控下方名单'}, {'value':'off','label':'关闭群监控'}]),
            'groups': FieldSchema(type='string', label='监控群名单', multiline=True,
                description='每行一个群 username（推荐 xxx@chatroom）或完整群名，也可用逗号分隔。'),
            'replyEnabled': FieldSchema(type='bool', label='允许回复监控群',
                description='关闭时只保留本地消息观察，固定关键词和 AI 都不回复。'),
        }, default_settings={'enabled':True,'mode':'settings','groups':'','replyEnabled':True})

    def groups(self):
        return list(dict.fromkeys(v.strip() for v in configured_terms(self.get('groups','')) if v.strip()))
