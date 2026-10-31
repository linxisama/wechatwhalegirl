"""Agent工作台本机API；外部Agent必须经现有bridge能力入口，不能直通此路由。"""
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .agent_control_api import require_bound
from .agent_authority import AuthorityError, authority
from .agent_workspace import WorkspaceError, workspace
from .agent_workbench import chat, extensions, invoke_extension, select_persona


class ChatBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(min_length=1,max_length=32)
    request_id: str = Field(min_length=8,max_length=100)
    text: str = Field(default='',max_length=4000)
    persona: str = Field(default='',max_length=48)
    attachments: list[str] = Field(default_factory=list,max_length=3)


class NoteBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=84)
    content: str = Field(max_length=65536)
    version: str = Field(max_length=64)


class MemoryBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1,max_length=1000)
    persona: str = Field(default='',max_length=48)


class ForgetBody(BaseModel):
    id: str = Field(min_length=1,max_length=32)


class OwnerBody(BaseModel):
    enabled: bool = Field(strict=True)


class DshBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    write: bool = Field(default=True,strict=True)


class ContextBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    history_turns: int = Field(default=10,ge=1,le=30,strict=True)
    max_chars: int = Field(default=8000,ge=1000,le=24000,strict=True)
    recall_limit: int = Field(default=3,ge=0,le=5,strict=True)


def add_routes(app):
    def area():
        require_bound(app.state.bot)
        return workspace(app.state.bot)

    @app.get('/api/agent/workspace/attachments')
    async def attachments():
        from .agent_attachments import AttachmentStore
        area()
        return {'success':True,'items':AttachmentStore(app.state.bot).list()}

    @app.post('/api/agent/workspace/attachments')
    async def upload_attachment(request: Request):
        import json
        from .agent_attachments import AttachmentStore, MAX_BYTES
        bot=app.state.bot;actor=require_bound(bot)
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>MAX_BYTES*4//3+4096: raise HTTPException(413,'单文件不得超过8MB')
        try:
            body=json.loads(raw)
            if not isinstance(body,dict) or set(body)!={'name','data_base64'}: raise ValueError()
        except (ValueError,UnicodeError): raise HTTPException(400,'需要name和data_base64字段') from None
        item=await AttachmentStore(bot).upload(body['name'],body['data_base64'],lambda:authority(bot).validate_actor(actor))
        return {'success':True,'item':item}

    @app.delete('/api/agent/workspace/attachments/{identity}')
    async def delete_attachment(identity: str):
        from .agent_attachments import AttachmentStore
        area();AttachmentStore(app.state.bot).delete(identity)
        return {'success':True}

    @app.get('/api/agent/workspace')
    async def info():
        from .agent_context import settings
        return {'success':True, **area().info(), 'owner_commands_enabled':
                bool(app.state.bot.config.get('agent.owner_commands_enabled',False)),
                'personas': app.state.bot.style.list_styles(), 'active_persona': app.state.bot.style.active_name,
                'context':settings(app.state.bot),'diary_auto_enabled':bool(app.state.bot.config.get('agent.diary.enabled',True))}

    @app.post('/api/agent/workspace/context')
    async def context(body: ContextBody):
        require_bound(app.state.bot)
        config=app.state.bot.config
        old=config.get('agent.context',{})
        config.set('agent.context',body.model_dump())
        try: config.save()
        except Exception:
            config.set('agent.context',old)
            raise
        return {'success':True,'context':body.model_dump()}

    @app.post('/api/agent/workspace/owner-channel')
    async def owner_channel(body: OwnerBody):
        actor = require_bound(app.state.bot)
        if body.enabled and actor['admin']['platform'] != 'wechat':
            raise HTTPException(400,'微信主人通道需要绑定微信稳定账号和主人wxid')
        app.state.bot.config.set('agent.owner_commands_enabled',body.enabled)
        app.state.bot.config.save()
        return {'success':True,'enabled':body.enabled}

    @app.get('/api/agent/workspace/files')
    async def files(): return {'success':True,'files':area().files()}

    @app.post('/api/agent/workspace/connector')
    async def connector():
        from .agent_mcp_connector import export_connector
        return {'success':True, **export_connector(area())}

    @app.post('/api/agent/dsh/provision')
    async def dsh_provision(body: DshBody):
        from .dsh_mcp_provision import provision
        area()
        return provision(app.state.bot,write=body.write)

    @app.get('/api/agent/workspace/note')
    async def read(name: str): return {'success':True, **area().read(name)}

    @app.post('/api/agent/workspace/note')
    async def write(body: NoteBody): return {'success':True, **area().write(body.name,body.content,body.version)}

    @app.get('/api/agent/workspace/memories')
    async def memories(persona: str='',query: str=''):
        found=area().memories(persona,query)
        return {'success':True,'items':found[:100],'total':len(found),'has_more':len(found)>100}

    @app.post('/api/agent/workspace/memories')
    async def remember(body: MemoryBody):
        bot=app.state.bot
        area()
        if body.persona: select_persona(bot,body.persona)
        return {'success':True,'item':area().remember(body.content,body.persona)}

    @app.post('/api/agent/workspace/forget')
    async def forget(body: ForgetBody): return {'success':True, **area().forget(body.id)}

    @app.post('/api/agent/workspace/diary')
    async def diary():
        actor=require_bound(app.state.bot)
        return await app.state.bot.diary_writer.generate(check_access=lambda:authority(app.state.bot).validate_actor(actor))

    @app.get('/api/agent/workspace/diary')
    async def read_diary(day: str=''):
        area()
        return app.state.bot.diary_writer.read(day or None)

    @app.post('/api/agent/workspace/diary-settings')
    async def diary_settings(body: OwnerBody):
        require_bound(app.state.bot)
        config=app.state.bot.config;old=config.get('agent.diary.enabled',True)
        config.set('agent.diary.enabled',body.enabled)
        try:config.save()
        except Exception:
            config.set('agent.diary.enabled',old)
            raise
        return {'success':True,'enabled':body.enabled}

    @app.get('/api/agent/history')
    async def history(session_id: str,persona: str=''):
        bot=app.state.bot
        return {'success':True,'messages':area().history(session_id,'*')}

    @app.post('/api/agent/chat')
    async def talk(body: ChatBody):
        bot=app.state.bot
        actor=require_bound(bot)
        return await chat(bot,**body.model_dump(),check_access=lambda:authority(bot).validate_actor(actor))

    @app.get('/api/agent/workspace/tools')
    async def tools():
        area()
        return {'success':True,'tools':extensions(app.state.bot)}

    @app.exception_handler(WorkspaceError)
    async def workspace_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'success':False,'detail':str(exc)},status_code=400)

    @app.exception_handler(AuthorityError)
    async def expired_authority(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'success':False,'detail':str(exc)},status_code=403)


def is_owner_command(bot,message):
    from .agent_workbench import owner_matches
    text = str(message.get('text') or '')
    return bool(bot.config.get('agent.owner_commands_enabled',False) and text.startswith('/agent ')
                and owner_matches(bot,message) and not bot.safety.status()['active'])


async def owner_command(bot,message):
    if not is_owner_command(bot,message): return None
    text = str(message.get('text') or '')
    # 账号/发送者由入站契约核对，不信任消息中自报的主人身份或session_id。
    rid = 'wechat-' + __import__('hashlib').sha256(str(message['id']).encode()).hexdigest()[:32]
    result = await chat(bot,'wechat-owner',rid,text[7:],source='wechat-owner')
    return result.get('reply') or result.get('message') or '本轮未完成，未重试。'
