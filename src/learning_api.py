"""本机学习管理，所有操作与外发队列隔离。"""
from pydantic import BaseModel,Field
from .conversation_api import room


class LearningBulk(BaseModel):
    account:str=Field(max_length=256)
    action:str=Field(max_length=32)
    kind:str=Field(default='',max_length=10)
    target:str=Field(default='',max_length=256)


class LearningAction(BaseModel):
    account:str=Field(max_length=256)
    kind:str=Field(max_length=10)
    target:str=Field(max_length=256)
    action:str=Field(max_length=20)
    identity:str=Field(default='',max_length=64)


def add_routes(app):
    @app.get('/api/dialogue/learning')
    async def view(kind:str='group',target:str=''):
        bot=app.state.bot
        rooms=bot.conversations.rooms(bot)
        if target:
            room(kind,target);value=bot.learning.view(bot,kind,target)
        else:value=dict(window_count=0,window_limit=1000,items=[])
        return dict(success=True,account=bot.account(),rooms=rooms,**value)

    @app.get('/api/dialogue/learning/overview')
    async def overview():
        bot=app.state.bot
        return dict(success=True,account=bot.account(),
                    rooms=bot.learning.overview(bot),rooms_total=len(bot.conversations.rooms(bot)))

    @app.post('/api/dialogue/learning/bulk')
    async def bulk(req:LearningBulk):
        bot=app.state.bot
        if req.kind and req.target:room(req.kind,req.target)
        async with bot.lock:
            if req.account!=bot.account():return dict(success=False,message='账号已改变，请重新读取')
            try:
                with bot.db:
                    result=bot.learning.bulk(bot,req.action,req.kind or None,req.target or None)
            except ValueError as exc:return dict(success=False,message=str(exc))
        message=(f'已对 {result["rooms"]} 个会话补学 {result["learned"]} 条原文'
                 if req.action=='learn-all' else f'已批准 {result["approved"]} 条表达例句')
        return dict(success=True,message=message,**result)

    @app.post('/api/dialogue/learning')
    async def action(req:LearningAction):
        room(req.kind,req.target);bot=app.state.bot
        async with bot.lock:
            if req.account!=bot.account():return dict(success=False,message='账号已改变，请重新读取')
            try:
                with bot.db:
                    if req.action=='learn':
                        count=bot.learning.rebuild(bot,req.kind,req.target)
                        message=f'已从本会话最近 {count} 条记录提取本地学习内容，未调用模型、未修改人设'
                    else:
                        bot.learning.change(bot,req.kind,req.target,req.identity,req.action)
                        message='学习条目已更新'
            except ValueError as exc:return dict(success=False,message=str(exc))
        return dict(success=True,message=message)
