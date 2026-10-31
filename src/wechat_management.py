"""当前账号的本地会话概况及管理员绑定，不读其他账号、不发消息。"""
import json
import time


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS management_sessions(
        mode TEXT, account TEXT, kind TEXT, target TEXT, name TEXT, verified INTEGER,
        received INTEGER, last_seen REAL, PRIMARY KEY(mode,account,kind,target))''')
    db.execute('CREATE INDEX IF NOT EXISTS management_recent ON management_sessions(last_seen)')
    db.commit()


def observe(bot, message, inserted=True):
    verified = int(message.get('kind') == 'private' and message.get('target') == message.get('sender_id'))
    bot.db.execute('''INSERT INTO management_sessions VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(mode,account,kind,target) DO UPDATE SET name=excluded.name,
        verified=max(verified,excluded.verified),received=received+excluded.received,last_seen=excluded.last_seen''',
        (bot.mode, bot.management_account(), message['kind'], message['target'],
         message.get('target_name') or message['target'], verified, int(inserted), time.time()))
    if time.monotonic() - getattr(bot,'_management_trim',-300) >= 300:
        bot.db.execute('DELETE FROM management_sessions WHERE rowid NOT IN '
                       '(SELECT rowid FROM management_sessions ORDER BY last_seen DESC LIMIT 1000)')
        bot._management_trim = time.monotonic()
    bot.db.commit()


def sessions(bot):
    counts = {}
    for raw, state in bot.db.execute("SELECT payload,state FROM inbox WHERE mode=? AND account=? "
                                   "AND json_extract(payload,'$._management_account')=?",
                                   (bot.mode,bot.account(),bot.management_account())):
        message = json.loads(raw)
        key = (message['kind'],message['target'])
        item = counts.setdefault(key,{})
        item[state] = item.get(state,0) + 1
    rows = bot.db.execute('SELECT kind,target,name,verified,received,last_seen FROM management_sessions '
                          'WHERE mode=? AND account=? ORDER BY last_seen DESC LIMIT 200', (bot.mode,bot.management_account())).fetchall()
    admin = bot.plugins._plugins['robot_admin']
    owners = admin.owners(bot.management_account())
    groups = bot._configured_groups()
    result = []
    for kind,target,name,verified,received,last_seen in rows:
        allowed = bot.reply_allowed({'kind':kind,'target':target,'target_name':name,'sender_id':target})
        result.append(dict(kind=kind,target=target,name=name,verified=bool(verified),received=received,
            last_seen=last_seen,counts=counts.get((kind,target),{}),owner=target in owners,
            reply_allowed=allowed,monitored=(bot.group_monitoring_enabled() and (not groups or target in groups or name in groups)) if kind=='group' else bool(bot.config.get('wechat_only.listen_private',True))))
    credential=getattr(getattr(bot,'admin_sessions',None),'credential',{})
    return {'success':True,'account':bot.account(),'sessions':result,'owners':owners,
            'password_state':'invalid' if credential.get('blocked') else ('set' if credential else 'unset'),
            'reply_paused':bool(admin.get('paused',False))}


def bind_owner(bot, target, bind=True, manual=False):
    admin = bot.plugins._plugins['robot_admin']
    bindings = json.loads(admin.get('ownerAccounts','{}'))
    owners = admin.owners(bot.management_account())
    if bind:
        if manual and not bot.self_id:
            raise ValueError('请先启动或测试微信连接，确认机器人账号后再直接绑定管理员')
        row = bot.db.execute('SELECT verified FROM management_sessions WHERE mode=? AND account=? AND kind=? AND target=?',
                             (bot.mode,bot.management_account(),'private',target)).fetchone()
        import re
        if manual and not re.fullmatch(r'[A-Za-z0-9_.-]{3,80}',target):
            raise ValueError('请输入稳定微信标识，例如 wxid_xxx，不能用昵称或群号')
        if not manual and (not row or not row[0]):
            raise ValueError('只能绑定当前账号实际收到过私聊且身份可验证的会话')
        owners = list(dict.fromkeys(owners + [target]))
    else:
        owners = [v for v in owners if v != target]
    bindings[bot.management_account()] = owners
    admin._binding = True
    try:
        bot.plugins.update_settings('robot_admin',{'ownerAccounts':json.dumps(bindings,ensure_ascii=False),
            'ownerIds':'\n'.join(dict.fromkeys(v for items in bindings.values() for v in items))})
    finally:
        admin._binding = False
    bot.cancel_disallowed_pending()
    if hasattr(bot,'admin_sessions'):
        bot.admin_sessions.invalidate()
    bot.note('已绑定本账号私聊管理员' if bind else '已解除本账号私聊管理员')
    return {'success':True,'message':'管理员已绑定；请在插件配置中启用私聊管理员指令。' if bind else '管理员已解除绑定。'}
