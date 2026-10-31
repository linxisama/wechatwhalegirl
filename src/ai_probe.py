"""按未保存的表单诊断模型，避免测试抢占机器人正在使用的客户端与锁。"""
import asyncio
import time

from pydantic import BaseModel, Field
from typing import Literal

from .ai_engine import AIEngine


class ProbeSettings(BaseModel):
    api_url: str = Field(default='', max_length=2048)
    api_key: str = Field(default='', max_length=4096)
    model: str = Field(default='', max_length=120)
    api_style: Literal['auto', 'openai', 'anthropic'] = 'auto'
    request_timeout: int | None = Field(default=None, ge=3, le=300)


async def probe(bot, settings, models_only=False):
    key = settings.api_key.strip() or bot.secret('ai.api_key')
    timeout = settings.request_timeout
    if timeout is None:
        timeout = max(3, min(300, int(bot.config.get('ai.timeout', 20) or 20)))
    engine = AIEngine(settings.api_url.strip(), key, model=settings.model.strip(),
                      api_style=settings.api_style, timeout=timeout, max_retries=1,
                      max_tokens=128)

    def scrub(value):
        text = str(value or '')
        return text.replace(key, '[密钥已隐藏]') if key else text

    problem = engine.configuration_error
    if not models_only:
        problem = problem or engine.model_id_problem(engine.model)
    if problem:
        return {'success': False, 'message': problem}
    model_engine = AIEngine(settings.api_url.strip(), key, model=engine.model,
                            api_style=settings.api_style, timeout=timeout, max_retries=1)

    async def models():
        started = time.monotonic()
        try:
            values = await asyncio.wait_for(model_engine.list_models(), timeout)
            error = scrub(model_engine.last_error) if not values else ''
        except asyncio.TimeoutError:
            values, error = [], f'读取模型列表超时（{timeout} 秒）；列表不可用不代表对话不可用'
        except Exception:
            values, error = [], '读取模型列表失败，请检查地址与网络'
        return values, error, round((time.monotonic()-started)*1000)

    async def chat():
        started = time.monotonic()
        preferences = bot.plugins._plugins.get('reply_preferences')
        options = preferences.model_options() if preferences else {}
        try:
            reply = await asyncio.wait_for(engine.chat(
                [{'role': 'user', 'content': 'Reply with OK only.'}], **options), timeout)
            error = scrub(engine.last_error) if not reply else ''
        except asyncio.TimeoutError:
            reply, error = '', f'对话请求超时（{timeout} 秒），请检查服务商状态或模型思考设置；未自动重试'
        except Exception:
            reply, error = '', '对话请求失败，请检查地址、密钥与模型'
        return scrub(reply)[:100], error or ('未收到回复' if not reply else ''), round((time.monotonic()-started)*1000)

    try:
        if models_only:
            values, error, elapsed = await models()
            return {'success': bool(values), 'models': values, 'message': error, 'models_ms': elapsed}
        # /models 有些服务商不提供；它不能挡住真正的最小对话测试。
        (values, list_error, list_ms), (reply, chat_error, chat_ms) = await asyncio.gather(models(), chat())
        listed = engine.model in values if values else None
        lines = [f"接口格式：{engine.api_style}", f"请求超时：{timeout} 秒（当前表单/配置）",
                 f"模型列表：{len(values)} 个模型 · {list_ms} ms" if values else '模型列表：'+list_error,
                 f"当前模型在列表中：{'是' if listed else '否'}" if listed is not None else '模型是否在列表中：未确认',
                 f"对话请求：{'成功' if reply else '失败'} · {chat_ms} ms",
                 '回复预览：'+reply if reply else '失败原因：'+chat_error]
        return {'success': bool(reply), 'message': '\n'.join(lines), 'models': values,
                'model_listed': listed, 'models_error': list_error, 'models_ms': list_ms,
                'chat_ms': chat_ms, 'preview': reply, 'api_style': engine.api_style,
                'request_timeout': timeout}
    finally:
        await asyncio.gather(engine.close(), model_engine.close())


def add_routes(app):
    @app.post('/api/ai/probe')
    async def diagnose(settings: ProbeSettings):
        return await probe(app.state.bot, settings)

    @app.post('/api/ai/models')
    async def list_draft_models(settings: ProbeSettings):
        return await probe(app.state.bot, settings, models_only=True)
