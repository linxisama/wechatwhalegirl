"""用户制定规则、编辑记忆，并查询当前账号可持久化的处理结果。"""
import asyncio
import json
import time
import uuid

from .chat_state import person_of,safe_fact

STATES={'pending':'等待处理','processing':'正在处理','sending':'发送中','sent':'已发送','failed':'处理失败','uncertain':'发送结果待核实',
        'expired':'已过期','merged':'已合并','cancelled':'已取消','blocked':'规则阻止','observed':'仅观察','skipped':'未进入队列'}
STAGES={'prepare':'合并/准备回复','waiting_desktop':'等待解锁 Windows / 恢复远程桌面','waiting_wechat_input':'等待微信提供输入控件（可点测试微信复查）','waiting_ai':'等待模型','model':'模型生成','post_reply':'整理回复','sending':'发送回复','idle':'空闲'}


def ensure_schema(bot):
    bot.db.executescript('''
    CREATE TABLE IF NOT EXISTS persona_notes(id TEXT PRIMARY KEY,persona TEXT,content TEXT,enabled INTEGER);
    CREATE TABLE IF NOT EXISTS dialogue_decisions(
      mode TEXT,account TEXT,mid TEXT,target TEXT,kind TEXT,sender_id TEXT,sender TEXT,target_name TEXT,
      text TEXT,created REAL,state TEXT,reason TEXT,PRIMARY KEY(mode,account,mid,target));
    CREATE INDEX IF NOT EXISTS dialogue_recent ON dialogue_decisions(mode,account,created);
    ''')
    bot.db.commit()


def decision(bot,message,state,reason):
    bot.remember_account()
    bot.db.execute('INSERT INTO dialogue_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(mode,account,mid,target) DO UPDATE SET state=excluded.state,reason=excluded.reason,text=excluded.text',
        (bot.mode,bot.management_account(),str(message['id']),message['target'],message['kind'],person_of(message),
         str(message.get('sender') or ''),str(message.get('target_name') or ''),str(message.get('text') or '')[:2000],
         time.time(),state,reason))
    bot.db.commit()


def notes(bot):
    return [{'id':key,'content':content,'enabled':bool(enabled)} for key,content,enabled in bot.db.execute(
        'SELECT id,content,enabled FROM persona_notes WHERE persona=? ORDER BY rowid',(bot.style.active_name,))]


def diagnostics(bot):
    result={}
    for row in bot.db.execute('SELECT mid,target,kind,sender_id,sender,target_name,text,created,state,reason '
        'FROM dialogue_decisions WHERE mode=? AND account=? ORDER BY created DESC LIMIT 200',
        (bot.mode,bot.management_account())):
        mid,target,kind,sender_id,sender,name,text,created,state,reason=row
        result[(target,str(mid))]=dict(id=mid,target=target,kind=kind,sender_id=sender_id,sender=sender,
                                     target_name=name,text=text,time=created,state=state,error=reason,reply='')
    for raw,state,error,reply,created in bot.db.execute("SELECT payload,state,error,reply,created FROM inbox WHERE mode=? "
        "AND (json_extract(payload,'$._management_account')=? OR "
        "(json_extract(payload,'$._management_account') IS NULL AND account=?)) ORDER BY created DESC LIMIT 200",
        (bot.mode,bot.management_account(),bot.account())):
        message=json.loads(raw);key=(message.get('target'),str(message.get('id')))
        result[key]=dict(result.get(key,{**message,'time':created}),state=state,error=error or '',reply=reply or '',queued_at=created)
    for value in bot.recent[-100:]:
        if value.get('account_key',bot.management_account())==bot.management_account():
            key=(value.get('target'),str(value.get('id')))
            result.setdefault(key,dict(value,state='observed',error='仅观察：未进入回复队列',reply=''))
    for value in result.values():
        state=value['state']
        if state in {'pending','processing'}:
            if not bot.running:
                reason='机器人未启动，历史待处理记录保留；本页不会重发消息'
            elif not bot.transport.online:
                reason='微信离线，等待恢复连接；过期消息不会迟到重发'
            elif str(bot.pipeline.get('message_id'))==str(value.get('id')) and bot.pipeline.get('stage')!='idle':
                reason='正在'+STAGES.get(bot.pipeline['stage'],bot.pipeline['stage'])
            else:
                reason='排队等待前序消息完成' if bot.pipeline.get('stage')!='idle' else '等待后台工作循环处理'
            value['error']=reason
        if state=='sent' and value.get('reply')=='[插件屏蔽]':
            value.update(state='blocked',reply='',error='旧版记录：模型调用前被插件阻止，并未发送')
        value['state_label']=STATES.get(value['state'],value['state'])
        value['wait_seconds']=max(0,int(time.time()-float(value.get('queued_at') or value.get('time') or time.time())))
    return sorted(result.values(),key=lambda v:float(v.get('time') or 0),reverse=True)[:200]


def snapshot(bot,query=''):
    memory=bot.plugins._plugins['conversation_memory']
    facts=memory.visible_facts(bot,query=query)
    prefix=bot.mode+'|'+bot.account()+'|'
    people={};subjects=[]
    for scope,subject,name,count in bot.db.execute('SELECT scope,subject,max(name),count(*) FROM chat_ambient '
        'WHERE subject<>? AND substr(scope,1,?)=? GROUP BY scope,subject',('',len(prefix),prefix)):
        row=people.setdefault(subject,dict(id=subject,name=name,messages=0,facts=[]))
        row['messages']+=count
        subjects.append(dict(subject=subject,scope=scope,name=name))
    for value in facts:
        person=people.setdefault(value['subject'],dict(id=value['subject'],name=value['subject'],messages=0,facts=[]))
        person['facts'].append(('已确认：' if value['status']=='confirmed' else '待复证：')+value['content'])
        source_scope=value['scope'].split('|persona:')[0]
        if source_scope[len(prefix):].startswith(('private:','group:')) and not any(v['subject']==value['subject'] and v['scope']==source_scope for v in subjects):
            subjects.append(dict(subject=value['subject'],scope=source_scope,name=person['name']))
    return {'success':True,'persona':bot.style.active_name,'notes':notes(bot),
        'people':sorted(people.values(),key=lambda v:v['messages'],reverse=True)[:200],
        'subjects':subjects[:200],'facts':facts[:500],'diagnostics':diagnostics(bot),
        'account':bot.account(),'offline':not bot.bridge.online,
        'autoRemember':memory.get('autoRemember',True),'memoryEnabled':memory.enabled,
        'personaIsolation':memory.get('personaIsolation',True),'crossChatPreferences':memory.get('crossChatPreferences',False)}


def _persona(bot,body):
    from fastapi import HTTPException
    if body.get('persona',bot.style.active_name)!=bot.style.active_name:
        raise HTTPException(409,'当前人设已变化，请刷新后再操作，原输入已保留')


def add_routes(app):
    from fastapi import Request,HTTPException
    from .dialogue_rules import snapshot as rules_snapshot,validate_updates

    @app.get('/api/dialogue')
    async def dialogue(q:str=''):
        return snapshot(app.state.bot,q[:300])

    @app.get('/api/dialogue/rules')
    async def rules():
        return rules_snapshot(app.state.bot)

    @app.post('/api/dialogue/rules')
    async def save_rules(request:Request):
        body=await request.json();bot=app.state.bot
        try:
            async with bot.lock:
                updates=validate_updates(bot,body)
                before=bot.plugins.get_settings('group_monitor')
                bot.plugins.update_many(updates)
                bot.cancel_disallowed_pending()
                monitor_ok=True
                if 'group_monitor' in updates and before!=bot.plugins.get_settings('group_monitor'):
                    try:
                        await bot.refresh_monitoring()
                    except Exception:
                        monitor_ok=False
            return {'success':True,'monitor_ok':monitor_ok,'message':'规则已保存并生效；重启后保留'+
                ('' if monitor_ok else '。监听重建失败，请在控制台重新连接微信')}
        except (ValueError,KeyError) as exc:
            raise HTTPException(400,str(exc)) from None
        except OSError:
            raise HTTPException(400,'规则保存失败，请检查磁盘空间和用户目录权限；原规则未更改') from None

    @app.post('/api/dialogue/memory/maintain')
    async def maintain_memory():
        """整理记忆：合并近重复、清理过期 superseded 与长期单证据候选（只动本地库）。"""
        bot = app.state.bot
        from .memory_maintenance import maintain
        async with bot.lock:
            result = await asyncio.to_thread(maintain, bot.db)
        return {"success": True, "message":
                f"合并 {result['merged']} 条近重复，清理 {result['expired']} 条过期、"
                f"{result['dropped']} 条长期未复证的候选", **result}

    @app.post('/api/dialogue/notes')
    async def update_note(request:Request):
        body=await request.json();bot=app.state.bot
        if not isinstance(body,dict):
            raise HTTPException(400,'请输入记忆操作')
        _persona(bot,body)
        key=body.get('id')
        old=bot.db.execute('SELECT content,enabled FROM persona_notes WHERE id=? AND persona=?',
                            (str(key),bot.style.active_name)).fetchone() if key else None
        if key and not old:
            raise HTTPException(404,'这条手动记忆不存在或不属于当前人设')
        if body.get('delete') is True and key:
            bot.db.execute('DELETE FROM persona_notes WHERE id=? AND persona=?',(key,bot.style.active_name))
        else:
            content=body.get('content',old[0] if old else '')
            enabled=body.get('enabled',bool(old[1]) if old else True)
            if not isinstance(content,str) or not 1<=len(content.strip())<=500:
                raise HTTPException(400,'手动记忆请输入 1–500 个字')
            if not isinstance(enabled,bool):
                raise HTTPException(400,'记忆开关必须为布尔值')
            if key:
                bot.db.execute('UPDATE persona_notes SET content=?,enabled=? WHERE id=? AND persona=?',
                    (content.strip(),int(enabled),key,bot.style.active_name))
            else:
                count=bot.db.execute('SELECT count(*) FROM persona_notes WHERE persona=?',(bot.style.active_name,)).fetchone()[0]
                if count>=100:
                    raise HTTPException(400,'每个人设最多保留 100 条手动记忆')
                bot.db.execute('INSERT INTO persona_notes VALUES(?,?,?,?)',(uuid.uuid4().hex,bot.style.active_name,content.strip(),int(enabled)))
        bot.db.commit()
        return {'success':True,'message':'手动记忆已保存，启用长期记忆插件时下一轮生效'}

    @app.post('/api/dialogue/facts')
    async def update_fact(request:Request):
        body=await request.json();bot=app.state.bot
        if not isinstance(body,dict):
            raise HTTPException(400,'请输入个人记忆操作')
        _persona(bot,body)
        memory=bot.plugins._plugins['conversation_memory'];key=body.get('id');action=body.get('action')
        found=next((v for v in memory.visible_facts(bot) if v['id']==key),None) if key else None
        if key and not found:
            raise HTTPException(404,'这条个人记忆不存在或不在当前账号、人设范围')
        if key and action=='delete':
            bot.db.execute('DELETE FROM chat_memories WHERE id=?',(key,))
        elif key and action=='confirm':
            bot.db.execute("UPDATE chat_memories SET status='confirmed',updated=? WHERE id=?",(time.time(),key))
        elif action in ('add','edit'):
            if not isinstance(body.get('content'),str):
                raise HTTPException(400,'个人记忆内容必须为文字')
            try:
                content=safe_fact(body.get('content'))
            except ValueError as exc:
                raise HTTPException(400,str(exc)) from None
            if action=='edit' and found:
                bot.db.execute("UPDATE chat_memories SET content=?,status='confirmed',updated=? WHERE id=?",(content,time.time(),key))
            elif action=='add' and not key:
                subject=body.get('subject');scope=body.get('scope')
                candidates=snapshot(bot)['subjects']
                if not any(v['subject']==subject and v['scope']==scope for v in candidates):
                    raise HTTPException(400,'请选择当前账号已观察到的会话和联系人')
                base=scope.removeprefix(bot.mode+'|'+bot.account()+'|')
                kind,target=base.split(':',1)
                message={'kind':kind,'target':target,'sender_id':subject}
                bot.chat_state.remember(memory.memory_scope(message,bot),subject,content,'local:'+uuid.uuid4().hex,True,int(memory.get('maxEntries',500)))
            else:
                raise HTTPException(400,'请选择要修改的记忆')
        else:
            raise HTTPException(400,'请选择添加、修改、确认或删除')
        bot.db.commit()
        return {'success':True,'message':'个人记忆已保存；已确认内容将在规则允许的下一轮引用'}
