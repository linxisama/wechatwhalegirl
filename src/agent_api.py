"""控制台 Agent 的本机接口（1.3.0 P0-4）。

路由只做参数校验与 JSON 返回，业务在 `wechat_agent` / `agent_sessions`。
**这里没有任何发送消息的接口**：Agent 只能改配置、插件开关、人设和本地记忆。
"""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .agent_scheduler import AgentScheduler, SchedulerError
from .agent_skills import SkillError, SkillStore
from .agent_sessions import AgentSessions
from .wechat_agent import AgentError, answer, apply_card, plan, undo_card
from .agent_control_api import require_bound


class SessionBody(BaseModel):
    title: str = Field(default='新会话', max_length=60)


class PlanBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=500)


class SkillBody(BaseModel):
    skill: dict
    overwrite: bool = False


class SkillToggle(BaseModel):
    skill_id: str = Field(min_length=1, max_length=48)
    enabled: bool | None = None


class ScheduleBody(BaseModel):
    name: str = Field(default='新任务', max_length=40)
    action: str = Field(default='connection_check', max_length=32)
    every_minutes: int = Field(default=60)
    enabled: bool = True


class ScheduleAction(BaseModel):
    task_id: str = Field(min_length=1, max_length=32)


class CardBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=32)
    card_id: str = Field(min_length=1, max_length=32)


def add_routes(app):
    def skills(bot) -> SkillStore:
        store = getattr(bot, 'agent_skills', None)
        if store is None:
            store = SkillStore(bot.root / 'data' / 'agent_skills.json')
            bot.agent_skills = store
        return store

    def scheduler(bot) -> AgentScheduler:
        instance = getattr(bot, 'agent_scheduler', None)
        if instance is None:
            instance = AgentScheduler(bot.root / 'data')
            bot.agent_scheduler = instance
        return instance

    def store(bot) -> AgentSessions:
        from .agent_authority import authority
        from .agent_workspace import workspace
        state = authority(bot).status()
        scope = workspace(bot).identity()[0] if state.get('bound') else 'legacy'
        sessions = getattr(bot, 'agent_sessions', None)
        if sessions is None or getattr(bot,'_agent_session_scope',None) != scope:
            path = (workspace(bot).folder().parent / 'sessions.json') if scope != 'legacy' else bot.root / 'data' / 'agent_sessions.json'
            sessions = AgentSessions(path)
            bot.agent_sessions = sessions
            bot._agent_session_scope = scope
        return sessions

    @app.get('/api/agent/sessions')
    async def list_sessions():
        bot = app.state.bot
        return {'success': True, 'sessions': store(bot).list_sessions()}

    @app.post('/api/agent/sessions')
    async def create_session(body: SessionBody):
        bot = app.state.bot
        return {'success': True, 'session': store(bot).create(body.title)}

    @app.get('/api/agent/sessions/{session_id}')
    async def get_session(session_id: str):
        bot = app.state.bot
        session = store(bot).get(session_id)
        if session is None:
            raise HTTPException(404, '会话不存在')
        return {'success': True, 'session': session}

    @app.delete('/api/agent/sessions/{session_id}')
    async def delete_session(session_id: str):
        bot = app.state.bot
        return {'success': store(bot).delete(session_id)}

    @app.post('/api/agent/plan')
    async def make_plan(body: PlanBody):
        bot = app.state.bot
        sessions = store(bot)
        session = sessions.get(body.session_id)
        if session is None:
            raise HTTPException(404, '会话不存在')
        try:
            result = plan(bot, body.text)
        except AgentError as exc:
            raise HTTPException(400, str(exc)) from None
        if all(action.get('action') == 'answer' for action in result.get('actions', [])):
            from .wechat_agent import answer
            text = body.text.strip()
            reply = ('你好，我是控制台助手。可以帮你查看状态，或生成修改回复长度、插件和人设的方案。'
                     if text in ('你好', '您好', '嗨', 'hello', 'hi') else
                     '我是鲸鱼娘控制台助手，用于查看和管理本机机器人；这里的回复不会发到微信。'
                     if any(word in text for word in ('你是谁', '你叫什么', '你能做什么')) else
                     answer(bot) if any(word in text for word in ('状态', '运行', '模型', '记忆')) else
                     '当前还不能把这句话可靠地转成操作。可以试试“回复太长了，改成40字”“关闭图片必回”，或询问当前状态。')
            result.update(reply=reply, state='answered')
        card = sessions.add_card(body.session_id, result)
        return {'success': True, 'card': card}

    @app.post('/api/agent/apply')
    async def apply(body: CardBody):
        bot = app.state.bot
        require_bound(bot)
        sessions = store(bot)
        session = sessions.get(body.session_id)
        if session is None:
            raise HTTPException(404, '会话不存在')
        card = next((c for c in session.get('cards') or [] if c.get('id') == body.card_id), None)
        if card is None:
            raise HTTPException(404, '卡片不存在')
        try:
            return {'success': True, 'card': apply_card(bot, session, card, sessions)}
        except AgentError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post('/api/agent/undo')
    async def undo(body: CardBody):
        bot = app.state.bot
        require_bound(bot)
        sessions = store(bot)
        session = sessions.get(body.session_id)
        if session is None:
            raise HTTPException(404, '会话不存在')
        card = next((c for c in session.get('cards') or [] if c.get('id') == body.card_id), None)
        if card is None:
            raise HTTPException(404, '卡片不存在')
        try:
            return {'success': True, 'card': undo_card(bot, session, card, sessions)}
        except AgentError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get('/api/agent/skills')
    async def list_skills():
        return {'success': True, 'skills': skills(app.state.bot).skills()}

    @app.post('/api/agent/skills/preview')
    async def preview_skill(body: SkillBody):
        try:
            return {'success': True, **skills(app.state.bot).preview(body.skill)}
        except SkillError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post('/api/agent/skills/import')
    async def import_skill(body: SkillBody):
        require_bound(app.state.bot)
        try:
            return {'success': True, 'skill': skills(app.state.bot).import_skill(body.skill, body.overwrite)}
        except SkillError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post('/api/agent/skills/toggle')
    async def toggle_skill(body: SkillToggle):
        require_bound(app.state.bot)
        try:
            return {'success': True, 'skill': skills(app.state.bot).toggle(body.skill_id, body.enabled)}
        except SkillError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.delete('/api/agent/skills/{skill_id}')
    async def delete_skill(skill_id: str):
        require_bound(app.state.bot)
        return {'success': skills(app.state.bot).delete(skill_id)}

    @app.get('/api/agent/schedules')
    async def list_schedules():
        return {'success': True, 'tasks': scheduler(app.state.bot).tasks()}

    @app.post('/api/agent/schedules')
    async def create_schedule(body: ScheduleBody):
        require_bound(app.state.bot)
        try:
            task = scheduler(app.state.bot).add(body.name, body.action,
                                                body.every_minutes, body.enabled)
        except SchedulerError as exc:
            raise HTTPException(400, str(exc)) from None
        return {'success': True, 'task': task}

    @app.post('/api/agent/schedules/run')
    async def run_schedule(body: ScheduleAction):
        bot = app.state.bot
        require_bound(bot)
        task = next((t for t in scheduler(bot).tasks() if t['id'] == body.task_id), None)
        if task is None:
            raise HTTPException(404, '任务不存在')
        try:
            result = scheduler(bot).run(bot, task['action'])
        except (SchedulerError, Exception) as exc:
            raise HTTPException(400, str(exc)) from None
        scheduler(bot).update(task['id'], last_run=__import__('time').time(), last_result=result[:200])
        return {'success': True, 'result': result}

    @app.post('/api/agent/schedules/toggle')
    async def toggle_schedule(body: ScheduleAction):
        bot = app.state.bot
        require_bound(bot)
        task = next((t for t in scheduler(bot).tasks() if t['id'] == body.task_id), None)
        if task is None:
            raise HTTPException(404, '任务不存在')
        return {'success': True, 'task': scheduler(bot).update(body.task_id,
                                                              enabled=not task.get('enabled'))}

    @app.delete('/api/agent/schedules/{task_id}')
    async def delete_schedule(task_id: str):
        require_bound(app.state.bot)
        return {'success': scheduler(app.state.bot).delete(task_id)}

    @app.post('/api/agent/answer')
    async def ask_status(body: SessionBody | None = None):
        bot = app.state.bot
        return {'success': True, 'text': answer(bot)}
