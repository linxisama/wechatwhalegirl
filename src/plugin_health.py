"""宿主侧隔离：插件失败不能依靠另一个插件来救场。"""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import threading
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from pathlib import Path
from .runtime_identity import APP_VERSION
from . import plugin_audit

POLICY_PLUGINS = frozenset({'robot_admin', 'private_replies', 'keyword_trigger',
    'group_monitor', 'chat_controls', 'mention_gate', 'addressee_guard'})
_message_source = ContextVar('plugin_message_source', default=None)

# 同一插件同一阶段**连续**失败到这一次才隔离。一次超时、一次网络抖动不等于插件坏了：
# 一条故障记录就永久禁用，会把"偶发"变成"永久"，用户只能靠 retry_failed 全量清零。
FAILURE_LIMIT = 3
# 连续失败计数存在 plugin_faults.json 里的保留键下。旧文件没有这个键→读成空计数；
# 新版文件多出来的这个键会被 _init_health 的 id 校验过滤掉，旧版照旧能读。
STRIKE_KEY = '__strikes__'
# 加载阶段出错时连对象都没建出来，没有"下次再调"这回事，仍是一次即隔离。
LOAD_FATAL_STAGES = frozenset({'load', 'import', 'metadata', 'constructor',
                               'post_load', 'source_check', 'settings'})


def live_message(message):
    pair = _message_source.get()
    return pair[1] if pair is not None and pair[0] is message else message


class PluginExitError(Exception):
    """把插件协程的SystemExit转换为任务内普通异常，避免退出整个事件循环。"""


class PluginHealth:
    FAILURE_LIMIT = FAILURE_LIMIT   # 前端/接口读得到隔离线，不用再抄一个数字

    def _init_health(self):
        self._fault_path = self.data_dir / 'plugin_faults.json'
        self._plugin_ids = {}
        self._faults = {}
        self._strikes = {}
        try:
            data = json.loads(self._fault_path.read_text('utf-8'))
            if isinstance(data, dict):
                self._faults = {k: v for k, v in data.items() if isinstance(k, str)
                    and isinstance(v, dict) and v.get('id') == k
                    and all(isinstance(v.get(field), str) for field in
                            ('fingerprint', 'stage', 'error_type', 'message'))}
                self._strikes = self._load_strikes(data.get(STRIKE_KEY))
        except (OSError, ValueError):
            self._faults = {}
        # 计数只在还有故障记录时才说明问题；记录被撤销后计数一并作废，免得旧数字变成"迟到的一刀"。
        for pid in [pid for pid in self._strikes if pid not in self._faults]:
            self._strikes.pop(pid, None)

    @staticmethod
    def _load_strikes(raw):
        """{插件: {阶段: 连续失败次数}}；形状不对的项一律丢弃，坏文件不让启动崩。"""
        if not isinstance(raw, dict):
            return {}
        counts = {}
        for pid, stages in raw.items():
            if not isinstance(pid, str) or not isinstance(stages, dict):
                continue
            kept = {stage: value for stage, value in stages.items()
                    if isinstance(stage, str) and isinstance(value, int)
                    and not isinstance(value, bool) and value >= 0}
            if kept:
                counts[pid] = kept
        return counts

    def _fingerprint(self, pid):
        # 自定义插件更新后自动复查；冻结内置模块随程序版本重新复查。
        try:
            path = self.custom_plugin_path(pid)
            payload = path.read_bytes() if path.is_file() and path.stat().st_size <= 512*1024 else b''
        except (OSError, AttributeError):
            payload = b''
        return hashlib.sha256(APP_VERSION.encode() + b'\0' + payload).hexdigest()

    def _save_faults(self):
        if not hasattr(self, '_fault_path'):
            return
        payload = dict(getattr(self, '_faults', {}))
        payload[STRIKE_KEY] = getattr(self, '_strikes', {})
        try:
            from .persistence import atomic_text
            atomic_text(self._fault_path, json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception:
            # 磁盘不可写时仍保留内存隔离，不能让诊断落盘反过来拖垮启动。
            print('[plugins] 故障记录无法保存，本次内存隔离仍生效')

    @staticmethod
    def _frames(exc):
        """只记栈帧（文件:行:函数名），**不记**异常消息与源码行。

        为什么不用 traceback.format_exception：它会把异常文本和出错那一行源码一起写进
        plugin_faults.json，插件抛的私钥/口令就跟着落盘了（test_plugin_isolation_197
        专门断言 PRIVATE_KEY 不得出现在故障记录里）。栈帧足够定位到哪一行。
        """
        rows = traceback.extract_tb(getattr(exc, '__traceback__', None))
        text = ''.join('  File "%s", line %s, in %s\n' % (row.filename, row.lineno, row.name)
                       for row in rows)
        return text[:4000]

    def _fault(self, pid, stage, exc):
        pid = str(pid)
        if not hasattr(self, '_faults'):
            self._faults = {}
        if not hasattr(self, '_strikes'):
            self._strikes = {}
        kind = 'SystemExit' if isinstance(exc, (SystemExit, PluginExitError)) else type(exc).__name__
        stages = self._strikes.setdefault(pid, {})
        stages[stage] = stages.get(stage, 0) + 1
        failures = stages[stage]
        quarantined = self._blocked(pid)
        note = (f'已隔离：{stage}（{kind}）' if quarantined
                else f'故障 {failures}/{FAILURE_LIMIT}：{stage}（{kind}）')
        self._faults[pid] = {'id': pid, 'stage': stage, 'error_type': kind,
            'time': time.time(), 'fingerprint': self._fingerprint(pid),
            'failures': failures, 'timed_out': isinstance(exc, TimeoutError),
            'quarantined': quarantined, 'traceback': self._frames(exc),
            'message': f'{note}，其他正常插件继续运行'}
        self._save_faults()
        print(f'[plugins] {pid} {stage} failed ({kind}); '
              + ('quarantined' if quarantined else f'failures {failures}/{FAILURE_LIMIT}'))

    def _fault_free(self, pid, stage):
        """跑成功一次就清零该插件的连续失败计数（计数归 0，记录留着）。

        记录不能跟着删：health() 是"这个插件出过什么事"的账本，一次正常返回
        不该让事故凭空消失（tests/test_plugin_hook_signatures.py:257 就是靠它
        核对 on_block 签名错位的）。能否再被调用由 _blocked 判，不看记录在不在。
        计数条目保留成 0 而不是删掉：删掉会让 _blocked 把"有记录没计数"当成
        旧版文件的已隔离记录，隔离线就再也走不回来了。

        'enabled' 只是读设置判断开关，不是插件真的跑了一遍，不能算成功——
        否则每次派发前的开关检查都会把计数抹掉，隔离线永远走不到。
        """
        if stage == 'enabled':
            return
        pid = str(pid)
        counts = getattr(self, '_strikes', {}).get(pid)
        if not counts or counts.get(stage, 0) == 0:
            return
        counts[stage] = 0
        self._save_faults()

    def _blocked(self, pid):
        """隔离线：加载期故障立即隔离，钩子故障同阶段连续 FAILURE_LIMIT 次才隔离。"""
        pid = str(pid)
        counts = getattr(self, '_strikes', {}).get(pid) or {}
        if any(value >= FAILURE_LIMIT for value in counts.values()):
            return True
        fault = getattr(self, '_faults', {}).get(pid)
        if not fault:
            return False
        if fault.get('stage') in LOAD_FATAL_STAGES:
            return True
        # 旧版故障文件里没有连续计数：那份记录的语义就是"已隔离"，照旧算到线。
        return not counts

    def is_quarantined(self, pid):
        return str(pid) in getattr(self, '_faults', {})

    def _skip_fault(self, pid):
        fault = self._faults.get(pid)
        if fault and self._blocked(pid) and fault['fingerprint'] == self._fingerprint(pid):
            return True
        if fault:
            # 没到隔离线（或插件文件改过）：这次重试，计数留着，失败会继续往上加。
            self._faults.pop(pid, None)
            self._save_faults()
        return False

    def plugin_id(self, plugin):
        known = getattr(self, '_plugin_ids', {}).get(id(plugin))
        if known is not None:
            return known
        try:
            return str(plugin.META.id)
        except (Exception, SystemExit):
            return 'unknown'

    def health(self):
        return [copy.deepcopy(value) for value in getattr(self, '_faults', {}).values()]

    def blocked_policies(self):
        """策略插件是否让本轮"不许直连模型"。

        语义是**有故障记录即阻断**，刻意比 _blocked 的 3 次线更保守：
        plugin_dispatch.generate_reply / describe_images 用它 fail-closed，
        策略插件（robot_admin 暂停线、chat_controls 分段与节奏）偶发一次失败时
        绕过策略直接调模型，等于把闸门放开。没到线的记录会在下一次派发的
        _skip_fault 里被摘掉并重试，所以不是永久禁用。
        """
        return sorted(POLICY_PLUGINS & getattr(self, '_faults', {}).keys())

    def retry_failed(self):
        self._faults.clear()
        self._strikes.clear()
        self._save_faults()
        self.reload()
        return self.health()

    @contextmanager
    def protected(self, plugin, stage):
        pid = self.plugin_id(plugin)
        try:
            yield
        except (Exception, SystemExit) as exc:
            self._fault(pid, stage, exc)
        else:
            self._fault_free(pid, stage)

    @contextmanager
    def local_changes(self, message=None, conversation=None, runtime=None):
        before = copy.deepcopy(message) if message is not None else None
        working = copy.deepcopy(before)
        turn_memo = {}
        turns = copy.deepcopy(conversation, turn_memo) if conversation is not None else None
        originals = {id(turn_memo[id(row)]): row for row in conversation
                     if isinstance(row, dict)} if conversation is not None else {}
        meta = copy.deepcopy(getattr(runtime, 'reply_meta', None)) if runtime is not None else None
        token = _message_source.set((working, message))
        try:
            yield working, turns
        except BaseException:
            if runtime is not None:
                runtime.reply_meta = meta
            raise
        else:
            if message is not None:
                # 只提交该插件改过的键，避免覆盖同时完成的视觉识别结果。
                for key in before.keys() - working.keys():
                    message.pop(key, None)
                for key, value in working.items():
                    if key not in before or value != before[key]:
                        message[key] = value
            if conversation is not None:
                # 当前轮可能仍被运行时持有，后续识图注释必须写回同一个对象。
                committed = []
                for row in turns:
                    original = originals.get(id(row))
                    if original is not None:
                        if original != row:
                            original.clear()
                            original.update(row)
                        row = original
                    committed.append(row)
                conversation[:] = committed
        finally:
            _message_source.reset(token)

    def _audit_call(self, method, started, exc):
        """一次插件调入记一行审计；旁路观测，坏了也不许影响主流程。"""
        try:
            owner = getattr(method, '__self__', None)
            plugin_audit.record(
                self.plugin_id(owner) if owner is not None else 'unknown',
                plugin_audit.hook_label(method),
                exc is None,
                (time.perf_counter() - started) * 1000.0,
                error=exc,
            )
        except Exception:
            pass

    async def bounded(self, method, *args, timeout):
        started = time.perf_counter()
        try:
            result = await self._bounded_call(method, args, timeout)
        except BaseException as exc:
            self._audit_call(method, started, exc)
            raise
        self._audit_call(method, started, None)
        return result

    async def _bounded_call(self, method, args, timeout):
        async def invoke():
            try:
                return await method(*args)
            except SystemExit:
                raise PluginExitError() from None
        # 用 asyncio.timeout 而不是 asyncio.wait_for：wait_for 是 asyncio 模块上的公开
        # 属性，既有测试会 patch 它来核对"图片理解用的是插件自己配置的超时"（
        # tests/test_regressions_149.py:102），外层再走一次 wait_for 会让那次 capture
        # 多记一条。上下文管理器不受该 patch 影响，超时同样抛 TimeoutError。
        async with asyncio.timeout(timeout):
            return await invoke()

    def bounded_sync(self, method, *args, timeout):
        """同步钩子的有界执行：丢进守护线程，到点主流程不再等它。

        为什么不用 asyncio.to_thread + wait_for：这几个入口本身是同步函数
        （run_on_ingest / run_on_observed / run_on_block / run_on_pre_ai / run_on_sent
        由同步代码直接调用），没有可以 await 的协程上下文。

        线程**杀不掉**：超时只是主流程放弃等待，被放弃的钩子仍在后台继续跑，
        它对 self 字段、state_file 的写入无法回滚（能回滚的只有调用方
        local_changes 里的 message/conversation/reply_meta）。用守护线程是为了让
        这种挂死不会在退出时把进程拖住——join(timeout) 一返回主线程就走。
        """
        started = time.perf_counter()
        outcome = {}
        context = copy_context()   # 带上 _message_source，插件里 live_message() 语义不变

        def invoke():
            try:
                outcome['value'] = context.run(method, *args)
            except BaseException as exc:   # SystemExit 也要带回主线程统一记账
                outcome['error'] = exc

        worker = threading.Thread(target=invoke, daemon=True, name='plugin-hook')
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            error = TimeoutError(f'plugin hook {timeout}s timeout')
            self._audit_call(method, started, error)
            raise error
        error = outcome.get('error')
        if error is not None:
            self._audit_call(method, started, error)
            raise error    # 原异常对象带着插件线程的栈帧，protected 仍按原类型记账
        self._audit_call(method, started, None)
        return outcome.get('value')
