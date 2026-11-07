"""每个钩子单独提交结果；失败的插件不会污染后续插件输入。"""
import asyncio
import copy
import math


class PluginDispatch:
    # 竞标-仲裁只作用于**排他钩子**：它们的语义是「一个插件说话，其余闭嘴」。
    # on_ingest 返回字符串 = 立即回复并跳过模型；on_block 返回 True = 丢弃这条消息。
    # 其余钩子（on_observed / on_pre_ai / on_prepare / on_sent / on_post_reply / tick）
    # 都是累积或变换，必须全部执行 —— 拿它们仲裁等于让一半插件凭空消失。
    EXCLUSIVE_HOOKS = ('on_ingest', 'on_block')

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

    def _weight_of(self, plugin) -> int:
        """插件的竞标权重；没写或写坏了都按 0 处理（存量插件一个都不用改）。"""
        meta = getattr(plugin, 'META', None)
        value = getattr(meta, 'capability_weight', 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _bidders(self, hook):
        """按 (权重降序, 注册顺序) 排出这个排他钩子的竞标顺序。

        权重相同仍按注册顺序 —— 这条保证了不填权重的存量插件行为完全不变。
        tuple(self._plugins.values()) 的顺序就是注册顺序，sorted 是稳定排序。
        """
        candidates = [plugin for plugin in tuple(self._plugins.values())
                      if self._enabled(plugin)]
        return sorted(candidates, key=lambda plugin: -self._weight_of(plugin))

    def _attach_guard(self, plugin, runtime):
        """把这个插件的权限视图挂到 runtime 上，供插件自己 require() 用。

        每次调用前挂一次（而不是加载时挂一次）：runtime 是共享对象，
        插件 A 的视图留在上面会让插件 B 拿到 A 的权限。
        enforce 默认 False —— 44 个内置插件里有 7 个真的联网或读写文件，
        一刀切会让它们在升级当天全部被隔离（见 src/plugin_guard.py 的说明）。
        """
        try:
            from .plugin_guard import PermissionGuard
        except Exception:
            return
        meta = getattr(plugin, 'META', None)
        granted = getattr(meta, 'permissions', ()) or ()
        enforce = bool(getattr(self, 'permission_enforce', False))
        sink = None
        if isinstance(getattr(runtime, 'permission_audit', None), list):
            def sink(entry, _trace=runtime.permission_audit):
                try:
                    _trace.append(entry)
                    del _trace[:-50]
                except Exception:
                    pass
        try:
            runtime.permission_guard = PermissionGuard(
                self.plugin_id(plugin), granted, enforce=enforce, sink=sink)
        except Exception:
            pass

    def _record_bid(self, runtime, hook, winner, losers, reason):
        """把一次仲裁写进 runtime.bid_trace，供控制台与日志回答「为什么是它」。

        trace 缺失或写不进去都不能影响主链路：这是观测，不是业务。
        """
        # 没有败者就说明这次没发生仲裁（唯一候选，或前面几个自己没话说），
        # 记一条 losers=[] 只会让日志里堆满「什么都没发生」的噪声。
        if not losers:
            return
        trace = getattr(runtime, 'bid_trace', None)
        if not isinstance(trace, list):
            return
        try:
            trace.append({'hook': hook,
                          'winner': winner,
                          'weight': self._weight_of(self._plugins.get(winner)) if winner else 0,
                          'losers': losers,
                          'reason': reason})
            # 有界：长跑时 trace 只增不减会把内存吃满，只留最近 50 条。
            del trace[:-50]
        except Exception:
            pass

    def _switch_on(self, plugin, pid):
        if pid in getattr(self, '_removed', set()):
            return False
        settings = getattr(plugin, 'settings', None)
        if not isinstance(settings, dict):
            return bool(getattr(plugin, 'enabled', False))
        return bool(settings.get('enabled', getattr(plugin.META, 'enabled_by_default', False)))

    def run_on_ingest(self, message, runtime):
        """排他钩子：按权重从高到低问，第一个给出回复的赢，其余记进 trace。

        注意 losers 里只记**没被问到**的插件。已经问过但返回 None 的插件不算输，
        它们是「这次没话说」，跟「被权重挤掉」是两件事，混在一起会让用户以为
        自己的插件被禁用了。
        """
        bidders = self._bidders('on_ingest')
        losers = [self.plugin_id(plugin) for plugin in bidders]
        for plugin in bidders:
            pid = self.plugin_id(plugin)
            losers.remove(pid)
            with self.protected(plugin, 'on_ingest'):
                self._attach_guard(plugin, runtime)
                with self.local_changes(message, runtime=runtime) as (working, _):
                    result = self.bounded_sync(plugin.on_ingest, working, runtime,
                                               timeout=self.SYNC_TIMEOUT)
                    if result is not None and not isinstance(result, str):
                        raise TypeError('on_ingest must return str or None')
                    if result is not None:
                        working['_immediate_plugin'] = pid
                if result is not None:
                    self._record_bid(runtime, 'on_ingest', pid,
                                     [{'id': item, 'weight': self._weight_of(self._plugins.get(item))}
                                      for item in losers],
                                     'weight')
                    return result
        return None

    def run_on_observed(self, message, runtime):
        for plugin in tuple(self._plugins.values()):
            if self._enabled(plugin):
                with self.protected(plugin, 'on_observed'):
                    self._attach_guard(plugin, runtime)
                    with self.local_changes(message, runtime=runtime) as (working, _):
                        self.bounded_sync(plugin.on_observed, working, runtime,
                                          timeout=self.SYNC_TIMEOUT)

    def run_on_block(self, message, runtime):
        """排他钩子：按权重从高到低问，第一个返回 True 的赢。

        与 on_ingest 的区别：on_block 返回 False 是明确的「我看过了，不拦」，
        所以**必须继续问下一个** —— 不能因为高权重插件说了句不拦就整条放行。
        这里只在真的拦下时才停，权重只决定「谁的意见先被听取」。
        """
        bidders = self._bidders('on_block')
        losers = [self.plugin_id(plugin) for plugin in bidders]
        for plugin in bidders:
            pid = self.plugin_id(plugin)
            losers.remove(pid)
            with self.protected(plugin, 'on_block'):
                self._attach_guard(plugin, runtime)
                with self.local_changes(message, runtime=runtime) as (working, _):
                    blocked = self.bounded_sync(plugin.on_block, working, runtime,
                                                timeout=self.SYNC_TIMEOUT)
                    if blocked is not None and not isinstance(blocked, bool):
                        raise TypeError('on_block must return bool or None')
                if blocked:
                    self._record_bid(runtime, 'on_block', pid,
                                     [{'id': item, 'weight': self._weight_of(self._plugins.get(item))}
                                      for item in losers],
                                     'weight')
                    return True
        return bool(self.blocked_policies())

    def run_on_pre_ai(self, message, conversation, runtime):
        result = {'delay': 0.0, 'block': False, 'extra_system': '', 'on_ready': None,
                  'max_tokens': 0, 'extra_system_parts': []}
        for plugin in tuple(self._plugins.values()):
            if not self._enabled(plugin):
                continue
            with self.protected(plugin, 'on_pre_ai'):
                self._attach_guard(plugin, runtime)
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
                    self._attach_guard(plugin, runtime)
                    with self.local_changes(message, runtime=runtime) as (working, _):
                        await self.bounded(plugin.on_prepare, working, runtime, timeout=self.PREPARE_TIMEOUT)

    def run_on_sent(self, message, reply, runtime):
        for plugin in tuple(self._plugins.values()):
            if self._enabled(plugin):
                with self.protected(plugin, 'on_sent'):
                    self._attach_guard(plugin, runtime)
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
                self._attach_guard(plugin, runtime)
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
                    self._attach_guard(plugin, runtime)
                    await self.bounded(plugin.tick, runtime, timeout=self.TICK_TIMEOUT)
