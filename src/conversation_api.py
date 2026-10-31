"""对话库的本机 HTTP 管理接口；操作永远不进入发送队列。"""
import json
from fastapi import HTTPException, Request


def room(kind, target):
    if kind not in ('group','private') or not isinstance(target,str) or not 1<=len(target)<=256:
        raise HTTPException(400,'请先选择一个群聊或私聊会话')


def add_routes(app):
    @app.get('/api/dialogue/archive')
    async def archive(kind: str='group', target: str='', q: str='', offset: int=0):
        bot=app.state.bot
        rooms=bot.conversations.rooms(bot)
        for value in rooms:
            value['name']=bot.bridge.display_name(value['target'])
        if not target:
            return dict(success=True,rooms=rooms,records=[],total=0,account=bot.account())
        room(kind,target)
        records,total=bot.conversations.records(bot,kind,target,query=q[:200],offset=max(0,offset),limit=100)
        return dict(success=True,rooms=rooms,records=records,total=total,offset=max(0,offset),account=bot.account())

    @app.get('/api/dialogue/archive/export')
    async def export(kind: str='group', target: str=''):
        room(kind,target)
        bot=app.state.bot
        return dict(success=True,bundle=bot.conversations.export(bot,kind,target))

    async def body(request):
        raw=await request.body()
        if len(raw)>64*1024*1024:
            raise HTTPException(400,'导入文件超过 64MB')
        try:
            data=json.loads(raw)
        except (ValueError,UnicodeError):
            raise HTTPException(400,'JSON 格式无效') from None
        if not isinstance(data,dict):
            raise HTTPException(400,'请求内容应为 JSON 对象')
        room(data.get('kind'),data.get('target'))
        return data

    @app.post('/api/dialogue/archive/import')
    async def restore(request: Request):
        data=await body(request);bot=app.state.bot
        async with bot.lock:
            if data.get('account')!=bot.account():
                raise HTTPException(409,'微信账号已改变，请重新读取记录')
            try:
                count=bot.conversations.restore(bot,data['kind'],data['target'],data.get('bundle'),data.get('mode','merge'))
            except ValueError as exc:
                raise HTTPException(400,str(exc)) from None
        return dict(success=True,message=f'已导入 {count} 条记录；重复项自动合并，不发送历史消息')

    @app.post('/api/dialogue/archive/delete')
    async def delete(request: Request):
        data=await body(request);bot=app.state.bot
        async with bot.lock:
            if data.get('account')!=bot.account():
                raise HTTPException(409,'微信账号已改变，请重新读取记录')
            if data.get('clear') is True:
                bot.conversations.clear(bot,data['kind'],data['target'])
            elif isinstance(data.get('key'),str):
                bot.conversations.delete(bot,data['kind'],data['target'],data['key'])
            else:
                raise HTTPException(400,'请选择记录或明确清空当前会话')
        return dict(success=True,message='已删除选定的本地对话记忆；原始运行日志保留，不会补发')
