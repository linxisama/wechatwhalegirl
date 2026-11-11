"""宿主微信桥替身：只经配对目录访问客体，保留宿主模型/记忆/策略。"""
from __future__ import annotations

import base64
import json
import hashlib
import logging
import threading
import time
import uuid
from pathlib import Path

from ..logging_setup import RUNTIME_LOGGER_NAME
from ..runtime_identity import APP_VERSION
from ..send_outcome import SendOutcome, not_submitted
from ..wechat_bridge import WeChatBridge
from .protocol import (MAX_SEND_FILE_BYTES, MAX_SEND_FILES, SEND_FILES_BUDGET, ProtocolError,
                       checked_path, live_service, load_pairing, read_document, request_digest,
                       write_document)
from .request_slots import publish_request_mirror


# 客体每次 poll 都要读写共享目录并解密消息库；单次预算过紧会让正常入站撞上
# 周期性的「超时即拆线」。放宽单次预算与间隔，并给「变慢」留出退避与故障预算。
POLL_TIMEOUT = 10.0
POLL_INTERVAL = 1.0
POLL_RETRY_DELAY = 0.5
POLL_RETRY_MAX_DELAY = 2.0
POLL_TIMEOUT_BUDGET = 60.0


# 客体侧抽帧要解码整段视频再回传三帧 base64，比轮询慢得多，但仍远小于发送时限。
VIDEO_FRAME_TIMEOUT = 20.0

# 发送类动作共用的失效语义：旧任务不能再投递，没人接手时还要尝试撤回。
SENDING_ACTIONS = frozenset(('send_text', 'send_files'))

# 客体要把 base64 落成临时文件再交给微信，比纯文字慢；仍受协议 30 秒期限约束。
SEND_FILES_TIMEOUT = 24.0


# 客人端随响应带回的耗时键（缺键即不打印，老客人端不带 timings 也照常工作）。
SEND_TIMING_KEYS = ('total', 'receive', 'agent', 'gui_ready', 'press', 'confirm', 'write')


# 「准备监听」是一次握手：慢机器上客人端可能正在读微信库，单次 5 秒会先到点。
# 这里不改单次预算（把 5 拍成 30 只会让卡死更难发现），而是给整轮准备一个总预算，
# 预算没用完就再试一次；两次都拿不到照旧回落 False —— fail-closed 语义不变：
# 宁可如实报告「未启动」，也不假装在听。
LISTEN_ATTEMPT_TIMEOUT = 5.0
LISTEN_PREPARE_BUDGET = 12.0
LISTEN_ATTEMPTS = 2


def _files_payload(paths, as_image):
    """把待发文件读成 base64：整批一起校验，任一项不合格就整批不发。"""
    items = [str(item or '') for item in list(paths or ())]
    if not items:
        return {'ok': False, 'message': '没有要发送的图片或文件，本次未发送'}
    if len(items) > MAX_SEND_FILES:
        return {'ok': False, 'message': f'一次最多发送 {MAX_SEND_FILES} 个图片或文件，本次未发送'}
    total, files = 0, []
    for raw in items:
        source = Path(raw)
        try:
            data = source.read_bytes()
        except OSError:
            return {'ok': False, 'message': '图片或文件读取失败，本次未发送：' + (source.name or '未知文件')}
        if not data:
            return {'ok': False, 'message': '图片或文件是空的，本次未发送：' + source.name}
        if len(data) > MAX_SEND_FILE_BYTES:
            return {'ok': False,
                    'message': f'单个图片或文件必须在 {MAX_SEND_FILE_BYTES >> 20}MB 以内，'
                               f'本次未发送：{source.name}'}
        total += len(data)
        if total > SEND_FILES_BUDGET:
            return {'ok': False,
                    'message': f'图片或文件合计超过 {SEND_FILES_BUDGET >> 20}MB，本次未发送'}
        files.append({'name': source.name[:120], 'image': bool(as_image),
                      'data': base64.b64encode(data).decode('ascii')})
    return {'ok': True, 'files': files}

class RpcError(RuntimeError):
    def __init__(self, message, *, submitted=False, diagnostic=None):
        super().__init__(message)
        self.diagnostic = {'submission_state': 'uncertain' if submitted else 'not_submitted'}
        if (isinstance(diagnostic, dict) and diagnostic.get('submission_state')
                in ('not_submitted', 'submitted', 'confirmed', 'uncertain')):
            self.diagnostic = dict(diagnostic)


class RpcBusy(RpcError):
    """另一个串行请求尚未结束；轮询可以稍后继续，但发送不能重投。"""


class RpcTimeout(RpcError):
    """请求未在期内完成：通道可能只是变慢，轮询可退避重试；发送语义不变。"""


class SandboxBridge(WeChatBridge):
    # 1.3.113 起通道能送图片/文件了（send_files + 客体 files_v1 能力），
    # 不再是「只发文字」的替身；旧客人端由 send_files 的能力预检拦下。
    text_only = False

    def __init__(self, data_dir, channel_dir, note=None, **kwargs):
        super().__init__(data_dir=data_dir, backend='auto', note=note)
        self.channel_dir = Path(channel_dir)
        self.backend = 'sandbox'
        self.requested_backend = 'sandbox'
        self._rpc_lock = threading.Lock()
        self._remote_status = {}
        self._epoch = None
        self._pairing = None
        self._relay = None
        self._listening = False
        self._name_cache = {}
        self._event_ack = 0
        self._event_lock = threading.RLock()
        self._event_seen = 0
        self._event_pending = {}
        self._event_completed = set()
        self._event_dispatched = set()
        self._receive_generation = 0
        self._ack_file = self.data_dir / 'isolated_event_ack.json'

    def _settings(self):
        return {'configured_account': self.configured_account, 'target_groups': list(self.target_groups),
                'listen_private': bool(self.listen_private), 'listen_groups': bool(self.listen_groups)}

    def _service(self):
        pairing = self._pairing or load_pairing(self.channel_dir)
        service = live_service(pairing)
        if service.get('app_version') != APP_VERSION:
            raise ProtocolError('隔离代理版本与控制台不一致，请重新准备隔离环境')
        if self._epoch is not None and service['epoch'] != self._epoch:
            raise ProtocolError('隔离会话已更换，请重新配对；不会恢复旧发送')
        return pairing, service

    def _apply_status(self, status):
        if not isinstance(status, dict):
            return
        identity = str(status.get('self_id') or '')
        if self.self_id and identity != self.self_id:
            with self._names_lock:
                self._name_cache.clear()
        self._remote_status = dict(status)
        self.online = status.get('online') is True
        self.self_id = identity
        self.account_name = str(status.get('account') or '')
        self._cache_name(self.self_id, self.account_name)
        self.account_dir = str(status.get('account_dir') or '')
        self._db_dir_hint = ''  # 宿主不能拿客体路径去反查自己的微信进程。
        self.send_environment = dict(status.get('send_environment') or {})
        self.last_error = str(status.get('error') or '')
        if type(status.get('consecutive_fail')) is int and status['consecutive_fail'] >= 0:
            self.consecutive_fail = status['consecutive_fail']
        self.degraded = '隔离后台仅支持当前已打开会话的消息回复'

    def _rpc(self, action, args=None, *, timeout=5, send_epoch=None):
        sending = action in SENDING_ACTIONS
        if sending and send_epoch is None:
            send_epoch = getattr(self, '_send_epoch', 0)
        def current_send():
            return (send_epoch == getattr(self, '_send_epoch', 0)
                    and not getattr(self, '_sends_stopped', False))
        if sending and not current_send():
            raise RpcError('旧发送任务已停止，本次未投递请求')
        started = time.monotonic()
        deadline = started + timeout
        if not self._rpc_lock.acquire(timeout=max(.01, timeout)):
            raise RpcBusy('隔离通道正忙，本次未投递请求')
        request_path = response_path = None
        digest = None
        posted = False
        completed = False
        try:
            if sending and not current_send():
                raise RpcError('旧发送任务已停止，本次未投递请求')
            pairing, service = self._service()
            epoch = service['epoch']
            if self._epoch is None:
                self._epoch = epoch
            if len(list((pairing.root / 'requests').glob('*.json'))) >= 100:
                raise RpcError('隔离请求队列已满，本次未投递')
            remaining = timeout - (time.monotonic() - started)
            if remaining <= .05:
                raise RpcError('隔离请求等待已超时，本次未投递')
            request_id = uuid.uuid4().hex
            request_path = pairing.root / 'requests' / (request_id + '.json')
            response_path = pairing.root / 'responses' / (request_id + '.json')
            now = time.time()
            capabilities = service.get('capabilities')
            mirrored = isinstance(capabilities, list) and 'request_mirror_v1' in capabilities
            request = {'version': pairing['version'], 'channel_id': pairing['channel_id'],
                       'epoch': epoch, 'request_id': request_id,
                       'action': action, 'args': dict(args or {}), 'created': now,
                       'deadline': now + min(remaining, 29.0)}
            digest = request_digest(request)
            with self._lock:
                # RPC 排队及身份读取都可能跨越停止；最后核验与落盘串行，
                # 避免旧请求在取消控制之后才变为客体可见。
                if sending and not current_send():
                    raise RpcError('旧发送任务已停止，本次未投递请求')
                if time.monotonic() >= deadline:
                    raise RpcError('隔离请求等待已超时，本次未投递')
                # 写入/flush 报错时客体也可能已读到完整请求，保守视为已投递。
                posted = True
                write_document(request_path, pairing, request, exclusive=True)
                # 新 UUID 在最小化沙盒可能被负缓存数秒；预建槽传递同一请求。
                # 两份同 ID/摘要/期限，只发布一次，不在失败时改 ID 或重发。
                if mirrored:
                    if time.monotonic() >= deadline:
                        raise RpcError('隔离请求发布已超时，本次不重试', submitted=sending)
                    publish_request_mirror(pairing.root / 'control/request.json', pairing,
                        request, epoch=epoch, digest=digest, deadline=deadline)
            while time.monotonic() < deadline:
                try:
                    result = read_document(response_path, pairing)
                except FileNotFoundError:
                    time.sleep(.025)
                    continue
                if result.get('request_id') != request_id or result.get('epoch') != epoch:
                    raise ProtocolError('隔离响应身份不匹配')
                if mirrored and result.get('request_digest') != digest:
                    raise ProtocolError('隔离响应请求摘要不匹配')
                # 响应到达时仍核验当前epoch，防止新会话回填旧私有结果。
                _, current = self._service()
                if current['epoch'] != epoch:
                    raise ProtocolError('隔离会话已经改变')
                if time.monotonic() >= deadline:
                    raise RpcTimeout('隔离响应核验超时，本次不重试', submitted=sending)
                value = result.get('value')
                if not isinstance(value, dict):
                    raise ProtocolError('隔离响应格式无效')
                if not result.get('ok'):
                    raise RpcError(str(value.get('message') or '隔离请求失败'), submitted=action == 'send_text',
                                   diagnostic=value.get('diagnostic'))
                completed = True
                return value
            raise RpcTimeout('隔离请求超时，本次不重试', submitted=sending)
        except RpcError:
            raise
        except (OSError, ProtocolError, ValueError) as error:
            # 共享目录的偶发读写失败可退避重试；协议/身份不符仍立即失败，
            # 不能把「客体协议错误」藏进轮询重试里。
            kind = RpcError if isinstance(error, ProtocolError) else RpcTimeout
            raise kind('隔离通道不可用（' + type(error).__name__ + '）',
                       submitted=posted and sending) from None
        finally:
            if posted and not completed and sending:
                with self._lock:
                    if current_send():
                        self._control('cancel')
            if request_path is not None and digest is not None:
                # 冲突证据不能因本次 RPC 结束而被宿主清掉。
                for path in (request_path, response_path):
                    try:
                        value = read_document(path, pairing)
                        matches = (request_digest(value) == digest if path == request_path
                                   else (not mirrored or value.get('request_digest') == digest))
                        if (matches and value.get('epoch') == epoch
                                and value.get('request_id') == request_id):
                            checked_path(path, pairing).unlink(missing_ok=True)
                    except (OSError, ProtocolError):
                        pass
            self._rpc_lock.release()

    def _control(self, action):
        try:
            pairing, service = self._service()
            now = time.time()
            write_document(pairing.root / 'control' / (action + '.json'), pairing,
                {'epoch': service['epoch'], 'action': action, 'request_id': uuid.uuid4().hex,
                 'created': now, 'deadline': now + 30})
        except (OSError, ProtocolError):
            pass

    def connect(self):
        try:
            self._pairing, service = self._service()
            if self._epoch is None:
                self._epoch = service['epoch']
            if self._relay is None:
                from .ocr_relay import HostOCRRelay
                self._relay = HostOCRRelay(self.channel_dir)
                self._relay.start()
            # 首次只读解密消息库的真机冷连接可超过 50 秒。只延长响应等待，
            # _rpc 仍限制请求必须在 29 秒内开始，不重投且不改变发送时限。
            self.note('正在只读连接隔离微信消息库，首次加载可能较慢（最长等待 85 秒）')
            value = self._rpc('connect', self._settings(), timeout=85)
            self._apply_status(value.get('status'))
            if not value.get('ok'):
                if self._relay is not None:
                    self._relay.close()
                    self._relay = None
                return False, str(value.get('message') or '隔离微信读取连接失败')
            self.connected_at = time.time()
            self._load_ack()
            return True, '隔离微信读取已连接；仅当前已打开会话可发送'
        except (RpcError, OSError, ProtocolError) as error:
            self.online = False
            self.last_error = str(error)
            if self._relay is not None:
                self._relay.close()
                self._relay = None
            return False, self.last_error

    def disconnect(self):
        self.stop_listening()
        try:
            self._rpc('disconnect', timeout=3)
        except RpcError:
            pass
        self.online = False
        if self._relay is not None:
            self._relay.close()
            self._relay = None

    def _load_ack(self):
        ack = 0
        try:
            value = json.loads(self._ack_file.read_text('utf-8'))
            if (value.get('channel_id') == self._pairing['channel_id'] and value.get('epoch') == self._epoch
                    and type(value.get('ack')) is int and value['ack'] >= 0):
                ack = value['ack']
        except (OSError, ValueError, AttributeError):
            pass
        with self._event_lock:
            self._event_ack = self._event_seen = ack
            self._event_pending.clear()
            self._event_completed.clear()
            self._event_dispatched.clear()

    def _save_ack(self, ack):
        from ..persistence import atomic_text
        atomic_text(self._ack_file, json.dumps({'channel_id': self._pairing['channel_id'],
                    'epoch': self._epoch, 'ack': ack}))

    def ack_inbound(self, event_id):
        """宿主持久接收入站后确认；仅允许连续、已落盘的水位删除客体事件。"""
        if type(event_id) is not int or event_id <= 0:
            raise ProtocolError('隔离入站确认身份无效')
        with self._event_lock:
            if self._poll_stop.is_set():
                return  # 停止后的迟到完成不能删除客体事件；下次监听重新投递核验。
            if event_id <= self._event_ack:
                return
            if event_id not in self._event_pending:
                raise ProtocolError('隔离入站记录尚未投递，不能提前确认')
            completed = self._event_completed | {event_id}
            ack = self._event_ack
            while ack + 1 in completed and ack + 1 in self._event_dispatched:
                ack += 1
            if ack > self._event_ack:
                # 写盘失败时不推进内存水位，下一次 poll 仍要求客体保留原事件。
                self._save_ack(ack)
            self._event_completed = {sequence for sequence in completed if sequence > ack}
            self._event_pending = {sequence: digest for sequence, digest in self._event_pending.items()
                                   if sequence > ack}
            self._event_dispatched = {sequence for sequence in self._event_dispatched if sequence > ack}
            self._event_ack = ack

    def _receive_event(self, event, stop_event):
        if not isinstance(event, dict):
            raise ProtocolError('隔离事件格式无效')
        sequence, raw = event.get('event_id'), event.get('message')
        if type(sequence) is not int or sequence <= 0 or not isinstance(raw, dict):
            raise ProtocolError('隔离事件格式无效')
        digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                separators=(',', ':')).encode('utf-8')).digest()
        with self._event_lock:
            if sequence <= self._event_ack:
                return
            if sequence in self._event_pending:
                if self._event_pending[sequence] != digest:
                    raise ProtocolError('同一隔离事件身份对应不同内容，已停止接收')
                return
            if sequence != self._event_seen + 1:
                raise ProtocolError('隔离事件水位不连续，停止接收以免漏消息')
            if len(self._event_pending) >= 30:
                raise ProtocolError('隔离未确认事件超出单批上限，停止接收以免丢弃旧记录')
            if stop_event.is_set():
                return
            callbacks = list(self._callbacks)
            if not callbacks:
                raise ProtocolError('隔离入站缺少宿主持久接收者')
            self._event_pending[sequence] = digest
            self._event_seen = sequence
        for key, label in ((raw.get('username'), raw.get('target_name')),
                           (raw.get('sender_username'), raw.get('sender_name'))):
            self._cache_name(key, label)
        delivered = dict(raw, _sandbox_event_id=sequence)
        asynchronous = False
        for callback in callbacks:
            if stop_event.is_set():
                return
            # 旧同步 callback 的 None 返回值仍表示调用完成；只有严格 False
            # 表示已投递到其他线程/事件循环，须等 ack_inbound 持久确认。
            asynchronous = callback(dict(delivered)) is False or asynchronous
        with self._event_lock:
            if stop_event.is_set():
                return  # callback 内停止/重启时，旧轮不能给新轮发布确认与收件计数。
            self.last_message_at = time.time()
            self.recv_count += 1
            self._event_dispatched.add(sequence)
            early_ack = sequence in self._event_completed
        # 异步消费者可能在 callback 返回前就完成入库并确认。仍需等本轮
        # 兼容同步接收者全部成功返回，才能发布这份连续确认水位。
        if not asynchronous or early_ack:
            self.ack_inbound(sequence)

    def start_listening(self, callback):
        with self._event_lock:
            if (self._listening and not self._poll_stop.is_set()
                    and self._poll_thread is not None and self._poll_thread.is_alive()):
                if callback not in self._callbacks:
                    self._callbacks.append(callback)
                return True, '隔离监听已经运行'
            self._poll_stop.set()
            self._listening = False
            self._receive_generation += 1
            generation = self._receive_generation
        try:
            value = self._listen_rpc_with_budget()
            if not value.get('ok'):
                return False, str(value.get('message') or '隔离监听未启动')
        except RpcError as error:
            return False, str(error)
        with self._event_lock:
            # RPC 可等待前一条请求；期间停止或新一轮准备已生效时，旧完成不能复活轮询。
            if generation != self._receive_generation:
                return False, '隔离监听准备期间已停止，本轮未启动'
            # stop/start 不一定经过 connect；未确认事件由客体重新投递，
            # 不得被上一轮内存 pending 或失效宿主闭包永久抑制。
            self._event_seen = self._event_ack
            self._event_pending.clear()
            self._event_completed.clear()
            self._event_dispatched.clear()
            self._callbacks = [callback]
            self._poll_stop = threading.Event()
            self._listening = True
            self._poll_thread = threading.Thread(target=self._poll_remote, args=(self._poll_stop, generation),
                                                daemon=True, name='isolated-wechat-events')
            self._poll_thread.start()
        return True, '隔离数据库只读监听已启动'

    def _listen_rpc_with_budget(self):
        """在总预算内重试「准备监听」这一条 RPC，并把失败原因留在异常文本里。

        只重试明确「请求没有落地」的超时/忙：重投不会变成第二次监听（客人端这条
        管理请求是幂等的）。其它 RpcError 可能已经执行过，重试不安全，直接上抛。
        """
        deadline = time.monotonic() + LISTEN_PREPARE_BUDGET
        attempt = 0
        last = None
        while attempt < LISTEN_ATTEMPTS:
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                break
            attempt += 1
            try:
                return self._rpc('start_listening', self._settings(),
                                 timeout=min(LISTEN_ATTEMPT_TIMEOUT, remaining))
            except (RpcTimeout, RpcBusy) as error:
                last = error
                left = max(deadline - time.monotonic(), 0.0)
                logging.getLogger(RUNTIME_LOGGER_NAME).warning(
                    f'隔离监听准备第 {attempt} 次未完成：{error}；剩余预算 {left:.1f} 秒，'
                    + ('再试一次' if attempt < LISTEN_ATTEMPTS else '不再重试'))
        if last is not None:
            raise RpcTimeout(
                f'{last}（已用 {LISTEN_PREPARE_BUDGET:g} 秒预算准备 {attempt} 次仍未启动）',
                submitted=False, diagnostic=getattr(last, 'diagnostic', None))
        raise RpcTimeout('隔离监听准备没有可用时间预算，本轮未启动', submitted=False)

    def _poll_remote(self, stop_event, generation=None):
        worker = threading.current_thread()
        with self._event_lock:
            generation = self._receive_generation if generation is None else generation

        def owns_generation():
            return (self._poll_stop is stop_event and self._receive_generation == generation
                    and self._poll_thread is worker)

        def current():
            return owns_generation() and not stop_event.is_set()

        retrying = 0
        timeout_deadline = None
        try:
            while not stop_event.is_set():
                try:
                    with self._event_lock:
                        if not current():
                            return
                        ack = self._event_ack
                    result = self._rpc('poll', {'ack': ack}, timeout=POLL_TIMEOUT)
                    # RPC 等待期间可以停止/重启；来源复核与状态回填必须同锁完成。
                    with self._event_lock:
                        if not current():
                            return
                        remote_status = result.get('status')
                        if not isinstance(remote_status, dict) or type(remote_status.get('listening')) is not bool:
                            raise ProtocolError('客体消息监听状态未核验，停止接收并等待重连')
                        self._apply_status(remote_status)
                        events = result.get('events', [])
                        if not isinstance(events, list) or len(events) > 30:
                            raise ProtocolError('隔离事件批次无效')
                    for event in events:
                        with self._event_lock:
                            if not current():
                                return
                        self._receive_event(event, stop_event)
                    # 同次已签名 poll 才证明客体收件活性；连接时的旧状态不能沿用。
                    # 本批已持久接收事件先交给原 ACK 路径，收件线程停止不丢已收事件。
                    with self._event_lock:
                        if not current():
                            return
                        if remote_status['listening'] is False:
                            raise ProtocolError('客体消息监听已停止，等待重连')
                except RpcBusy:
                    # 尚未投递 poll；同一代仍可继续，不重放任何发送请求。
                    with self._event_lock:
                        if not current():
                            return
                    stop_event.wait(.1)
                    continue
                except RpcTimeout as error:
                    # 单次变慢不等于通道损坏：保持 online 与监听代次，退避重试；
                    # 只有连续超时吃满故障预算，才回到原来的 fail-closed 拆线路径。
                    with self._event_lock:
                        if not current():
                            return
                        retrying += 1
                        now = time.monotonic()
                        if timeout_deadline is None:
                            timeout_deadline = now + POLL_TIMEOUT_BUDGET
                        slow = (f'隔离通道响应变慢，正在重试（第 {retrying} 次）'
                                if retrying == 1 else None)
                        expired = now >= timeout_deadline
                        if expired:
                            self.last_error = str(error)
                            self.online = False
                            self._listening = False
                            stopped = '隔离监听已停止：' + self.last_error
                    if slow:
                        self.note(slow)
                    if expired:
                        self.note(stopped)
                        break
                    stop_event.wait(min(POLL_RETRY_DELAY * retrying, POLL_RETRY_MAX_DELAY))
                    continue
                except Exception as error:
                    with self._event_lock:
                        if not current():
                            return
                        self.last_error = (str(error) if isinstance(error, (RpcError, ProtocolError))
                                           else '隔离消息回调失败（' + type(error).__name__ + '）')
                        self.online = False
                        self._listening = False
                        message = '隔离监听已停止：' + self.last_error
                    self.note(message)
                    break
                if retrying:
                    # 整场超时风暴只报一次恢复，恢复正常后重试计数与预算清零。
                    retrying, timeout_deadline = 0, None
                    self.note('隔离通道已恢复')
                stop_event.wait(POLL_INTERVAL)
        finally:
            with self._event_lock:
                if owns_generation():
                    self._listening = False

    def stop_receiving(self):
        with self._event_lock:
            self._receive_generation += 1
            self._poll_stop.set()
            if hasattr(self, '_runtime_receive_epoch'):
                self._runtime_receive_epoch = None

    def stop_listening(self):
        self.stop_receiving()
        self.cancel_pending_sends()
        try:
            self._rpc('stop_listening', timeout=3)
        except RpcError:
            pass
        thread, self._poll_thread = self._poll_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(1)
        self._listening = False

    def cancel_pending_sends(self):
        with self._lock:
            self._send_epoch = getattr(self, '_send_epoch', 0) + 1
            self._sends_stopped = True
            self._control('cancel')

    def resume_sends(self):
        epoch = getattr(self, '_send_epoch', 0)
        value = self._rpc('resume_sends', timeout=3)
        if not value.get('ok'):
            raise RuntimeError(str(value.get('message') or '隔离发送未恢复'))
        with self._lock:
            if epoch != getattr(self, '_send_epoch', 0):
                raise RuntimeError('隔离发送恢复已取消，未启用新发送')
            self._sends_stopped = False

    def _log_send_timings(self, timings):
        """一行 INFO 记下客人端耗时归因；没有 timings 或日志坏了都静默跳过。"""
        try:
            if not isinstance(timings, dict):
                return
            parts = [f'{key}_ms={timings[key]}' for key in SEND_TIMING_KEYS
                     if isinstance(timings.get(key), (int, float))]
            if parts:
                logging.getLogger(RUNTIME_LOGGER_NAME).info('send timing guest ' + ' '.join(parts))
        except BaseException:
            return

    def send_text(self, target, text, target_name='', at=None):
        epoch = getattr(self, '_send_epoch', 0)
        if getattr(self, '_sends_stopped', False):
            return not_submitted('隔离发送已停止，本次未发送')
        from ..wechat_safety import SafetyHold, service_target
        if (service_target({'target': target}) or SafetyHold(self.data_dir.parent / 'wechat_safety_hold.json').status().get('active')):
            return not_submitted('官方/系统账号或安全暂停，隔离通道未发送')
        if not self.online or not str(text or '').strip():
            return not_submitted('隔离微信未连接或正文为空，本次未发送')
        with self._send_lock:
            if epoch != getattr(self, '_send_epoch', 0) or getattr(self, '_sends_stopped', False):
                return not_submitted('旧隔离发送任务已停止，本次未发送')
            try:
                value = self._rpc('send_text', {'target': str(target), 'text': str(text),
                    'target_name': str(target_name or ''), 'at': list(at or [])}, timeout=24,
                    send_epoch=epoch)
                self._log_send_timings(value.get('timings'))
                self._apply_status(value.get('status'))
                outcome = SendOutcome(bool(value.get('ok')), str(value.get('message') or '隔离发送未完成'),
                                      value.get('diagnostic') or {})
            except RpcError as error:
                outcome = SendOutcome(False, str(error), error.diagnostic)
                if (not isinstance(error, RpcBusy)
                        and error.diagnostic.get('submission_state') != 'not_submitted'):
                    self.consecutive_fail += 1
            if outcome[0]:
                self.send_ok += 1
                self.consecutive_fail = 0
            else:
                self.send_fail += 1
                self.last_error = outcome[1]
            self.last_send_at = time.time()
            return outcome

    def supports_files(self):
        """客人端是否声明 files_v1：旧代理没这一行就不发，免得整批落空。"""
        try:
            capabilities = self._service()[1].get('capabilities')
        except (OSError, ProtocolError):
            return False
        return isinstance(capabilities, list) and 'files_v1' in capabilities

    def send_files(self, target, paths, target_name='', as_image=False):
        """经隔离通道发图片/文件：内容随签名请求送进沙盒，再由客体用微信发出去。"""
        epoch = getattr(self, '_send_epoch', 0)
        if getattr(self, '_sends_stopped', False):
            return not_submitted('隔离发送已停止，本次未发送')
        from ..wechat_safety import SafetyHold, service_target
        if (service_target({'target': target})
                or SafetyHold(self.data_dir.parent / 'wechat_safety_hold.json').status().get('active')):
            return not_submitted('官方/系统账号或安全暂停，隔离通道未发送')
        if not self.online:
            return not_submitted('隔离微信未连接，图片或文件未发送')
        if not self.supports_files():
            return not_submitted('隔离后台版本过旧，不支持发送图片或文件，本次未发送')
        payload = _files_payload(paths, as_image)
        if not payload['ok']:
            return not_submitted(str(payload.get('message')))
        with self._send_lock:
            if epoch != getattr(self, '_send_epoch', 0) or getattr(self, '_sends_stopped', False):
                return not_submitted('旧隔离发送任务已停止，本次未发送')
            try:
                value = self._rpc('send_files', {'target': str(target), 'files': payload['files'],
                    'target_name': str(target_name or ''), 'as_image': bool(as_image)},
                    timeout=SEND_FILES_TIMEOUT, send_epoch=epoch)
                self._log_send_timings(value.get('timings'))
                self._apply_status(value.get('status'))
                outcome = SendOutcome(bool(value.get('ok')), str(value.get('message') or '隔离发送未完成'),
                                      value.get('diagnostic') or {})
            except RpcError as error:
                outcome = SendOutcome(False, str(error), error.diagnostic)
                if (not isinstance(error, RpcBusy)
                        and error.diagnostic.get('submission_state') != 'not_submitted'):
                    self.consecutive_fail += 1
            if outcome[0]:
                self.send_ok += 1
                self.consecutive_fail = 0
            else:
                self.send_fail += 1
                self.last_error = outcome[1]
            self.last_send_at = time.time()
            return outcome

    def download_media(self, chat, local_id, msg_type, shard=''):
        return None

    def video_frames(self, chat, local_id, shard='', want=3, max_side=768):
        """让客体就地抽帧并回传 data URL：沙盒里宿主下不了媒体，视频只能由客人端解码。"""
        try:
            value = self._rpc('video_frames', {
                'chat': str(chat or ''), 'local_id': '' if local_id is None else str(local_id),
                'shard': str(shard or ''), 'want': int(want), 'max_side': int(max_side)},
                timeout=VIDEO_FRAME_TIMEOUT)
            frames = [frame for frame in (value.get('frames') or [])
                      if isinstance(frame, str) and frame.startswith('data:image/')]
            if not frames:
                return {'ok': False, 'frames': [],
                        'message': str(value.get('message') or '隔离后台没有取到视频画面')}
            return {'ok': True, 'frames': frames, 'message': ''}
        except Exception:
            return {'ok': False, 'message': '隔离后台视频抽帧失败或超时', 'frames': []}

    def _cache_name(self, username, label):
        if (not isinstance(username, str) or not 1 <= len(username) <= 256
                or not isinstance(label, str) or not label.strip() or len(label) > 256
                or any(ord(char) < 32 for char in username + label)):
            return
        with self._names_lock:
            if username not in self._name_cache and len(self._name_cache) >= 5000:
                self._name_cache.pop(next(iter(self._name_cache)))
            self._name_cache[username] = label

    def display_name(self, username, fallback=''):
        # 归一化与 ingest 在宿主事件循环同步调用此方法；只读已接收的显示缓存，
        # 不可等待正被定位/发送占用的 RPC 锁。发送名称另经客体权威核验。
        with self._names_lock:
            value = self._name_cache.get(str(username))
        if value:
            return value
        return fallback or username

    def resolve_target_name(self, target):
        target = str(target or '').strip()
        if not target:
            return ''
        from ..wechat_send_worker import valid_chat_name
        value = self.display_name(target)
        # unknown 沿用原桥的“尚未解析”语义：进队列后由 _wait_for_desktop
        # 在线程里执行 _chat_name；不能把通道繁忙误判成目标不存在。
        return value if value != target and valid_chat_name(value) else 'unknown'

    def _chat_name(self, target, target_name=''):
        # 名称与重名检查必须由客体当前账号执行；不能借宿主的联系人缓存。
        try:
            value = self._rpc('resolve_chat_name', {'target': str(target),
                              'target_name': str(target_name or '')}, timeout=3).get('value')
        except RpcError as error:
            raise ValueError(str(error)) from None
        from ..wechat_send_worker import valid_chat_name
        if not isinstance(value, str) or not valid_chat_name(value):
            raise ValueError('隔离账号未能确认目标会话名称，本次未操作微信')
        return value

    def resolve_session(self, text):
        try:
            value = self._rpc('resolve_session', {'text': str(text)}, timeout=2).get('value')
            return value if isinstance(value, str) and value else text
        except RpcError:
            return text

    def member_names(self, chat):
        try:
            value = self._rpc('member_names', {'text': str(chat)}, timeout=2).get('value')
            return value[:1000] if isinstance(value, list) else []
        except RpcError:
            return []

    def inspect_send_environment(self, who=''):
        try:
            # 客体只读 worker 自身最多等 24 秒；宿主须留出启动及共享目录
            # 响应余量，避免先超时后的清理取消把仍在检查的 worker 杀掉。
            value = self._rpc('inspect_environment', {'who': str(who or '')}, timeout=35)
            self.send_environment = dict(value.get('environment') or {})
        except RpcError as error:
            self.send_environment = {'ready': False, 'waiting_code': 'isolated_unavailable', 'reason': str(error)}
        self.send_environment.update(execution_environment='sandbox', current_chat_only=True)
        return dict(self.send_environment)

    def is_receiving(self):
        """本地接收代次的活性；健康守护不能通过阻塞 RPC 或旧 service 判定。"""
        with self._event_lock:
            return bool(self.online and self._listening and not self._poll_stop.is_set()
                        and self._poll_thread is not None and self._poll_thread.is_alive())

    def status(self):
        capabilities = None
        try:
            _, service = self._service()
            available = True
            guest_status = service.get('status') or {}
            capabilities = service.get('capabilities')
        except (OSError, ProtocolError):
            available, guest_status = False, {}
        with self._event_lock:
            # 先使连接失效，避免基类快照复制上次检查的 ready=True。
            if not available:
                self.online = False
            value = super().status()
            if not available:
                value.update(online=False, send_state='disconnected',
                             send_code='isolated_unavailable', send_reason='隔离代理未运行或会话已过期')
            listening = bool(value['online'] and self.is_receiving())
            value.update(backend='sandbox', native_backend=str(self._remote_status.get('backend') or ''),
                         execution_environment='sandbox', current_chat_only=True,
                         # 只有客人端真的声明了 files_v1 才算能送文件；旧代理照旧只发文字。
                         text_only=not (isinstance(capabilities, list) and 'files_v1' in capabilities),
                         isolated_agent_available=available, guest_safety=guest_status.get('safety', {}),
                         scope='current_open_chat', listening=listening,
                         read_state='listening' if listening else 'connected' if value['online'] else 'disconnected')
            return value
