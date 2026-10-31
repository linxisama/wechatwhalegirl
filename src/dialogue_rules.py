"""对话页可编辑的规则白名单，不提供主动外发或发送安全参数。"""
GROUPS={
    'trigger':{'label':'回复触发与名单','help':'群聊仍需 @ 或命中唤醒词；名单限制同样作用于固定回复。群内用户规则是忽略名单，建议填写稳定 wxid。',
        'plugins':{'keyword_trigger':None,'private_replies':None,'group_monitor':None,'mention_gate':None}},
    'followup':{'label':'连续跟聊','help':'上下文轮数默认10，字符预算优先保留较近记录。跟聊窗口和句数按账号、人设、会话、发送者隔离；未点名回复概率独立设置，默认0%，非零仍遵守名单、仅@策略与限流。',
        'plugins':{'short_followup':None,'chat_controls':['enabled','cooldownSeconds','perMinute','ambient','historyTurns','contextChars','replyProbability']}},
    'isolation':{'label':'人设隔离','help':'开启时个人记忆按人设隔离，旧无标记记忆归默认鲸鱼娘；关闭后同一人可共享已有各人设记忆。手动记忆和模型对话历史始终按人设隔离，切换不会删除数据。',
        'plugins':{'conversation_memory':['enabled','personaIsolation','recallLimit','maxEntries']}},
    'preferences':{'label':'跨会话偏好','help':'跨会话仅共享同账号、同发送者的个人事实，不共享群聊原文；与 QQ 版一致，私聊偏好默认不用于群聊，需另行允许。自动记录需两条独立消息确认，也可人工确认。',
        'plugins':{'conversation_memory':['enabled','crossChatPreferences','privateToGroup','autoRemember','autoPrefixes']}},
}


def snapshot(bot):
    plugins={p['id']:p for p in bot.plugins.list_plugins()}
    groups={}
    for key,group in GROUPS.items():
        fields=[]
        for pid,allowed in group['plugins'].items():
            # 市场插件（如 mention_gate）在冻结版里不存在：跳过而不是 KeyError，
            # 否则安装包自检会因为一个可选插件直接崩溃（实测踩过）。
            plugin=plugins.get(pid)
            if plugin is None:continue
            keys=allowed or list(plugin['schema'])
            fields.append(dict(id=pid,name=plugin['name'],schema={k:plugin['schema'][k] for k in keys},
                               settings={k:plugin['settings'].get(k,plugin['defaults'].get(k)) for k in keys}))
        groups[key]=dict(label=group['label'],help=group['help'],plugins=fields)
    return {'success':True,'groups':groups}


def validate_updates(bot,body):
    if not isinstance(body,dict) or not isinstance(body.get('group'),str) or body['group'] not in GROUPS:
        raise ValueError('请选择有效规则分组')
    plugins=body.get('plugins')
    if not isinstance(plugins,dict) or not plugins:
        raise ValueError('请填写规则')
    allowed=GROUPS[body['group']]['plugins']
    for pid,values in plugins.items():
        if pid not in allowed or not isinstance(values,dict):
            raise ValueError('此页面不能修改插件：'+str(pid))
        plugin=bot.plugins._plugins.get(pid)
        if plugin is None:
            raise ValueError('该插件未安装（市场插件需先下载）：'+str(pid))
        keys=allowed[pid] or plugin.META.config_schema
        if set(values)-set(keys):
            raise ValueError('规则包含不允许修改的字段')
        bot.plugins.validated_settings(pid,values)
    return plugins
