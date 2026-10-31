"""每个钩子单独提交结果；失败的插件不会污染后续插件输入。"""
import asyncio
import copy
import math


class PluginDispatch:
    PREPARE_TIMEOUT = 75.0
    POST_TIMEOUT = 5.0
    TICK_TIMEOUT = 60.0
    READY_TIMEOUT = 10.0
    # 同步钩子（on_ingest/on_observed/on_block/on_pre_ai/on_sent）的有界执行上限。
    # 它们在收消息的主路径上同步跑，卡住一次就是整条链路堵死，所以给得比异步钩子更紧。
    SYNC_TIMEOUT = 8.0
    # 视觉识别与生成：一个是本地模型 OCR，一个是把请求转发给插件的整轮生成，都比钩子贵。
    DESCRIBE_TIMEOUT = 30.0
    GENERATE_TIMEOUT = 60.0

    def _enabled(self, plugin):
        # 判的是**隔离线**而不是"有没有故障记录"：一次超时只记账，不该把插件永久关掉。
        pid = self.plugin_id(plugin)
        if self._blocked(pid):
            return False
        with self.protected(plugin, 'enabled'):
            if self.is_quarantined(pid):
                # 插件自己的 enabled 一看到故障记录就返回 False（见 src/plugins/__init__.py
                # 的同名 property），但没到隔离线的插件还得能被再叫一次，所以这里按设置本身
                # 判断——逻辑与该 property 一致，只跳过"有故障就关"这一条。
                return self._switch_on(plugin, pid)
            return bool(plugin.enabled)

    def _switch_on(self, plugin, pid):
        if pid in getattr(self, '_removed', set()):
            return False
        settings = getattr(plugin, 'settings', None)
        if not isinstance(settings, dict):
            return bool(getattr(plugin, 'enabled', False))
        return bool(settings.get('enabled', getattr(plugin.META, 'enabled_by_default', False)))

    def run_on_ingest(self, message, runtime):
        for plugin in tuple(self._plugins.values()):
            if not self._enabled(plugin):
                continue
            with self.protected(plugin, 'on_ingest'):
                with self.local_changes(message, runtime=runtime) as (working, _):
                    result = self.bounded_sync(plugin.on_ingest, working, runtime,
                                               timeout=self.SYNC_TIMEOUT)
                    if result is not None and not isinstance(result, str):
                        raise TypeError('on_ingest must return str or None')
                    if result is not None:
                        working['_immediate_plugin'] = self.plugin_id(plugin)
                if result is not None:
                    return result
        return None

    def run_on_observed(self, message, runtime):
        for plugin in tuple(self._plugins.values()):
            if self._enabled(plugin):
                with self.protected(plugin, 'on_observed'):
                    with self.local_changes(message, runtime=runtime) as (working, _):
                        self.bounded_sync(plugin.on_observed, working, runtime,
                                          timeout=self.SYNC_TIMEOUT)

    def run_on_block(self, message, runtime):
        for plugin in tuple(self._plugins.values()):
            if self._enabled(plugin):
                with self.protected(plugin, 'on_block'):
                    with self.local_changes(message, runtime=runtime) as (working, _):
                        blocked = self.bounded_sync(plugin.on_block, working, runtime,
                                                    timeout=self.SYNC_TIMEOUT)
                        if blocked is not None and not isinstance(blocked, bool):
                            raise TypeError('on_block must return bool or None')
                    if blocked:
                        return True
        return bool(self.blocked_policies())

    def run_on_pre_ai(self, message, conversation, runtime):
        result = {'delay': 0.0, 'block': False, 'extra_system': '', 'on_ready': None,
                  'max_tokens': 0, 'extra_system_parts': []}
        for plugin in tuple(self._plugins.values()):
            if not self._enabled(plugin):
                continue
            with self.protected(plugin, 'on_pre_ai'):
                with self.local_changes(message, conversation, runtime) as (working, turns):
                    hint = self.bounded_sync(plugin.on_pre_ai, working, turns, runtime,
                                             timeout=self.SYNC_TIMEOUT)
                    if not hint:
                        continue
                    if not isinstance(hint, dict):
                        raise TypeError('on_pre_ai must return dict or None')
                    update = dict(result)
                    delay = float(hint.get('delay') or 0)
                    if not math.isfinite(delay) or delay < 0 or delay > 60:
                        raise ValueError('invalid plugin delay')
                    update['delay'] = max(update['delay'], delay)
                    update['block'] = update['block'] or bool(hint.get('block'))
                    if hint.get('extra_system'):
                        if not isinstance(hint['extra_system'], str):
                            raise TypeError('extra_system must be str')
                        update['extra_system'] += '\n' + hint['extra_system']
                        # 纯增量：拼接后的字符串语义一个字都不变，另外记下每段归谁，
                        # 注入预算日志才说得出"哪个插件占了多少字"（dict 浅拷贝共享列表，
                        # 这里必须换新列表，失败的插件才不会污染上一轮快照）
                        update['extra_system_parts'] = update['extra_system_parts'] + [
                            (self.plugin_id(plugin), hint['extra_system'])]
                    cap = hint.get('max_tokens')
                    if isinstance(cap, (int, float)) and not isinstance(cap, bool) and int(cap) > 0:
                        update['max_tokens'] = min(update['max_tokens'], int(cap)) if update['max_tokens'] else int(cap)
                    if callable(hint.get('on_ready')) and update['on_ready'] is None:
                        async def ready(action=hint['on_ready'], owner=plugin):
                            if self._enabled(owner):
                                with self.protected(owner, 'on_ready'):
                                    await self.bounded(action, timeout=self.READY_TIMEOUT)
                        update['on_ready'] = ready
                result = update
        if self.blocked_policies():
            result['block'] = True
        return result

    async def describe_images(self, message, runtime):
        plugin = self._plugins.get('image_understanding')
        if plugin is not None and not self.is_quarantined('image_understanding'):
            with self.protected(plugin, 'describe'):
                with self.local_changes(message, runtime=runtime) as (working, _):
                    await self.bounded(plugin.describe, working, runtime,
                                       timeout=self.DESCRIBE_TIMEOUT)
                return
        if any(isinstance(item, dict) and item.get('type') in {'image', 'emotion'} for item in message.get('media', [])):
            message.pop('vision_description', None)
            message['vision_status'] = '失败：图片识别插件未加载或已隔离'

    async def run_on_prepare(self, message, runtime):
        for plugin in tuple(self._plugins.values()):
            if self._enabled(plugin):
                with self.protected(plugin, 'on_prepare'):
                    with self.local_changes(message, runtime=runtime) as (working, _):
                        await self.bounded(plugin.on_prepare, working, runtime, timeout=self.PREPARE_TIMEOUT)

    def run_on_sent(self, message, reply, runtime):
        for plugin in tuple(self._plugins.values()):
            if self._enabled(plugin):
                with self.protected(plugin, 'on_sent'):
                    with self.local_changes(message, runtime=runtime) as (working, _):
                        self.bounded_sync(plugin.on_sent, working, reply, runtime,
                                          timeout=self.SYNC_TIMEOUT)

    async def generate_reply(self, engine, conversation, **kwargs):
        controls = self._plugins.get('chat_controls')
        if controls and self._enabled(controls):
            class ModelCalls:
                # 模型网络错误属于后端故障，不能把转发请求的插件永久隔离。
                error = None

                def __getattr__(self, name):
                    return getattr(engine, name)

                async def chat(self, *args, **options):
                    try:
                        return await engine.chat(*args, **options)
                    except Exception as exc:
                        self.error = exc
                        raise

            calls = ModelCalls()
            try:
                # 有界执行：调用点（src/wechat_app.py 的 ai_lock 内）也正在等这一句，
                # 插件自己的 generate 卡死时 wait_for 到点取消，取消让 async with 正常退出，
                # 模型锁不会被一个失控插件永久占住；超时按普通故障记一笔，本轮直接放弃生成。
                return await asyncio.wait_for(controls.generate(calls, conversation, **kwargs),
                                              self.GENERATE_TIMEOUT)
            except (Exception, SystemExit) as exc:
                if exc is calls.error:
                    raise
                self._fault(self.plugin_id(controls), 'generate', exc)
        if self.blocked_policies():
            return ''
        return await engine.chat(conversation, **kwargs)

    async def run_on_post_reply(self, message, reply, runtime):
        for plugin in tuple(self._plugins.values()):
            if not self._enabled(plugin):
                continue
            with self.protected(plugin, 'on_post_reply'):
                with self.local_changes(message, runtime=runtime) as (working, _):
                    value = await self.bounded(plugin.on_post_reply, working, reply, runtime, timeout=self.POST_TIMEOUT)
                    new_reply = reply
                    if isinstance(value, tuple) and len(value) == 2:
                        text, meta = value
                        if not isinstance(text, str) or not isinstance(meta, dict):
                            raise TypeError('invalid reply metadata')
                        merged = copy.deepcopy(getattr(runtime, 'reply_meta', None) or {})
                        for key, item in meta.items():
                            if key in {'media', 'mentions'} and item:
                                if not isinstance(item, list):
                                    raise TypeError('media and mentions must be lists')
                                merged[key] = list(merged.get(key) or []) + item
                            else:
                                merged[key] = item
                        runtime.reply_meta = merged
                        new_reply = text
                    elif isinstance(value, str):
                        new_reply = value
                    elif value is not None:
                        raise TypeError('invalid post reply result')
                reply = new_reply
        return reply

    async def run_tick(self, runtime):
        for plugin in tuple(self._plugins.values()):
            if self._enabled(plugin):
                with self.protected(plugin, 'tick'):
                    await self.bounded(plugin.tick, runtime, timeout=self.TICK_TIMEOUT)
