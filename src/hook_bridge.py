"""可选的进程内协议通道；只调用已核验本机接口，不安装或加载 DLL。"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .send_outcome import SendOutcome
from .wechat_bridge import WeChatBridge
from .wechat_safety import SafetyHold, service_target

HOOK_VERSION = '4.1.10.27'
KNOWN_DLL_SHA256 = '23948e7d933ff40735ddc0cc516641fd9f935d67ffdec476bb8db61b79940f29'
MAX_RESPONSE_BYTES = 256 * 1024
_ACCOUNT = re.compile(r'wxid_[A-Za-z0-9_]{1,120}\Z')
_TARGET = re.compile(r'(?:wxid_[A-Za-z0-9_]{1,120}|[A-Za-z0-9_]{1,128}@chatroom|filehelper)\Z')


class HookUnavailable(ValueError):
    """只含可展示的原因，不携带 HTTP 正文、消息正文或令牌。"""


def loopback_url(value, *, callback=False):
    """不用 DNS、代理或重定向，避免本机配置被变成任意网络请求。"""
    if not isinstance(value, str) or len(value) > 1024:
        raise HookUnavailable('本机协议地址无效')
    try:
        parts = urlsplit(value)
        address = ipaddress.ip_address(parts.hostname or '')
        port = parts.port
    except ValueError:
        raise HookUnavailable('本机协议地址必须使用明确的回环 IP 和端口') from None
    if (parts.scheme != 'http' or not address.is_loopback or not port
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or any(ord(c) < 33 for c in value)):
        raise HookUnavailable('本机协议只允许无代理的回环 HTTP 地址')
    if callback:
        if not parts.path.startswith('/') or len(parts.path) < 20:
            raise HookUnavailable('回调必须使用独立的随机路径')
    elif parts.path not in ('', '/'):
        raise HookUnavailable('本机协议基址不能附加路径')
    return value.rstrip('/')


class HardenedHookBridge(WeChatBridge):
    """固定账号、固定一个会话，提交与服务器记录确认分别记录。"""

    text_only = True

    def __init__(self, data_dir, settings=None, note=None, *, client=None,
                 identity_probe=None, receipt_verifier=None, journal=None):
        super().__init__(data_dir=data_dir, backend='auto', note=note)
        self.backend = self.requested_backend = 'hook'
        self.settings = dict(settings or {})
        self._client = client
        self._owns_client = client is None
        self._identity_probe = identity_probe
        self._receipt = receipt_verifier
        self._journal = journal
        self._journal_path = getattr(journal, 'path', self.data_dir / 'hook-state.sqlite3')
        self._binding = None
        self._identity_checked_at = 0.0
        self._listening = False
        self._cancel = threading.Event()
        self._cancel.set()
        self._lifecycle_lock = threading.RLock()
        self._generation = 0
        self._receive_generation = 0
        self._callback_url = ''
        self.degraded = '协议后台仅支持已固定会话的文字；发送必须核验新增自身记录'

    def _options(self):
        if self.settings.get('enabled') is not True:
            raise HookUnavailable('进程内协议通道未启用')
        api = loopback_url(self.settings.get('api_url', 'http://127.0.0.1:30001'))
        account = self.settings.get('account', '')
        target = self.settings.get('target', '')
        account_path = self.settings.get('account_path', '')
        if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
            raise HookUnavailable('请绑定明确的微信稳定账号 ID')
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise HookUnavailable('请固定一个微信会话稳定 ID')
        if (not isinstance(account_path, str) or not account_path
                or not Path(account_path).is_absolute() or '..' in Path(account_path).parts):
            raise HookUnavailable('请绑定当前账号的本机数据目录')
        return api, account, target, str(Path(account_path).absolute())

    def _http(self):
        if self._client is None:
            self._client = httpx.Client(trust_env=False, follow_redirects=False)
        return self._client

    def _post(self, action, payload, timeout=4):
        if action not in ('GetSelfProfile', 'set_callback', 'SendTextMsg'):
            raise HookUnavailable('此协议动作未开放')
        api, _, _, _ = self._options()
        try:
            with self._http().stream('POST', api + '/' + action, json=payload,
                                     timeout=timeout, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise HookUnavailable('本机协议请求未成功')
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise HookUnavailable('本机协议响应超过限制')
            result = json.loads(body)
            if not isinstance(result, dict):
                raise HookUnavailable('本机协议响应格式无效')
            return result
        except HookUnavailable:
            raise
        except Exception as error:
            raise HookUnavailable('本机协议请求失败（' + type(error).__name__ + '）') from None

    def _probe(self):
        from .hook_identity import APPROVED_PROVIDER_SHA256
        api, account, _, account_path = self._options()
        probe = self._identity_probe
        if probe is None:
            from .hook_identity import probe_hook_identity
            probe = probe_hook_identity
        info = probe(api, account_path, account)
        if not isinstance(info, dict) or info.get('verified') is not True:
            raise HookUnavailable('无法独立核验微信进程与账号')
        pid, created = info.get('pid'), info.get('create_time')
        if (type(pid) is not int or pid <= 0 or type(created) not in (int, float)
                or not math.isfinite(created) or created <= 0
                or info.get('account') != account or info.get('version') != HOOK_VERSION
                or info.get('dll_sha256') not in APPROVED_PROVIDER_SHA256
                or info.get('loopback_listener') is not True
                or info.get('account_path') != account_path):
            raise HookUnavailable('微信版本、接口监听范围或账号绑定未通过核验')
        binding = (pid, created, account, account_path)
        if self._binding is not None and binding != self._binding:
            raise HookUnavailable('微信进程或账号已变化，请停止并重新检查通道')
        profile = self._post('GetSelfProfile', {})
        # 此 DLL 的 profile 曾返回错误账号；不以配置覆盖真实不一致值。
        if profile.get('wxid') != account:
            raise HookUnavailable('微信接口返回的账号与已绑定账号不一致')
        self._identity_checked_at = time.time()
        return binding

    def _hold(self):
        return SafetyHold(self.data_dir.parent / 'wechat_safety_hold.json').status().get('active') is True

    def _environment(self, ready, code='', reason=''):
        value = {'ready': bool(ready), 'waiting_code': code, 'reason': reason,
                 'execution_environment': 'hook', 'current_chat_only': True,
                 'text_only': True, 'delivery_verified': False}
        self.send_environment = value
        return dict(value)

    def _refuse(self, reason, code='send_failed', *, reconnect=False):
        environment = self._environment(False, code, reason)
        environment.update(submission_state='not_submitted', send_key=False)
        self.send_environment = environment
        self.send_fail += 1
        self.last_error = reason
        self.last_send_at = time.time()
        if reconnect:
            self.consecutive_fail += 1
        return SendOutcome(False, reason, environment)

    def connect(self):
        with self._lock:
            self.stop_receiving()
            self.cancel_pending_sends()
            try:
                if self._hold():
                    raise HookUnavailable('账号安全暂停，协议通道未连接')
                _, account, _, account_path = self._options()
                self._binding = None
                binding = self._probe()
                if self._journal is None:
                    from .hook_gateway import HookJournal
                    self._journal = HookJournal(self._journal_path, account)
                if getattr(self._journal, 'account', account) != account:
                    raise HookUnavailable('本机入站记录属于其他账号，通道未连接')
                self._binding = binding
                self.self_id = self.account_name = account
                self.account_dir = Path(account_path).name
                self._db_dir_hint = ''
                self.online = True
                self.connected_at = time.time()
                self.last_error = ''
                return True, '本机协议与账号已核验；请启动已固定会话的监听'
            except Exception as error:
                self.online = False
                self.last_error = str(error) if isinstance(error, HookUnavailable) else '协议通道检查失败（' + type(error).__name__ + '）'
                self._environment(False, 'hook_unavailable', self.last_error)
                return False, self.last_error

    def set_callback_url(self, callback_url):
        self._callback_url = loopback_url(callback_url, callback=True)

    def start_listening(self, callback):
        with self._lifecycle_lock:
            self._receive_generation += 1
            generation = self._receive_generation
            self._listening = False
        try:
            if not self.online or self._hold():
                raise HookUnavailable('协议通道尚未连接或账号处于安全暂停')
            self._probe()
            endpoint = self._callback_url or self.settings.get('callback_url', '')
            endpoint = loopback_url(endpoint, callback=True)
            result = self._post('set_callback', {'callback_url': endpoint})
            if type(result.get('ret')) is not int or result['ret'] != 0:
                raise HookUnavailable('微信回调地址未设置成功')
            if result.get('callback') != endpoint:
                raise HookUnavailable('微信接口未确认本次随机回调地址')
            with self._lifecycle_lock:
                if generation != self._receive_generation or not self.online or self._hold():
                    raise HookUnavailable('监听准备期间通道已停止，本次未恢复监听')
                # 每次连接仅归属一个宿主监听者，旧闭包不能越积越多。
                self._callbacks = [callback]
                self._listening = True
            self.replay_pending()
            return True, '已监听固定会话的文字消息'
        except Exception as error:
            message = str(error) if isinstance(error, HookUnavailable) else '启动协议监听失败'
            with self._lifecycle_lock:
                if generation == self._receive_generation:
                    self._listening = False
                    self.last_error = message
            return False, message

    def ingest_raw(self, payload):
        """父 API 先核对随机路径及回环来源，再在线程中调用此入口。"""
        with self._lifecycle_lock:
            generation = self._receive_generation
            if not self._listening or not self.online or self._hold():
                return False
        try:
            self._probe()
            with self._lifecycle_lock:
                if (generation != self._receive_generation or not self._listening
                        or not self.online or self._hold()):
                    return False
            _, account, target, _ = self._options()
            from .hook_gateway import normalize_callback
            raw = normalize_callback(payload, account=account)
            if raw is None:
                return False
            from .wechat_safety import security_notice
            if security_notice(raw):
                # 回调交给宿主事件循环之前先阻止外发，不能等待下一次轮询。
                SafetyHold(self.data_dir.parent / 'wechat_safety_hold.json').activate('hook-callback')
                self.cancel_pending_sends()
                self._listening = self.online = False
            if raw.get('username') != target:
                # 官方安全通知仍须交给宿主的持久暂停逻辑，不能被会话范围挡掉。
                if not security_notice(raw):
                    return False
            if not self._journal.record_inbound(raw):
                return False
            for callback in list(self._callbacks):
                callback(dict(raw))
            # 回调只负责投递到宿主事件循环；宿主实际完成持久入队后再 ack。
            # 提前确认会在“回调已返回、SQLite 尚未写入”之间丢失事件。
            self.last_message_at = time.time()
            self.recv_count += 1
            return True
        except Exception as error:
            with self._lifecycle_lock:
                if generation != self._receive_generation:
                    return False
            self.cancel_pending_sends()
            self._listening = self.online = False
            self.last_error = str(error) if isinstance(error, HookUnavailable) else '协议入站保存失败（' + type(error).__name__ + '）'
            self._environment(False, 'hook_ingress_failed', self.last_error)
            return False

    def ack_inbound(self, message_id):
        if self._journal is not None:
            self._journal.ack_inbound(message_id)

    def replay_pending(self, limit=100):
        """恢复尚未被宿主持久接收的记录；宿主去重后显式确认。"""
        if not self._listening or not self.online or self._journal is None or self._hold():
            return 0
        pending = getattr(self._journal, 'pending_inbound', None)
        if not callable(pending):
            return 0
        from .wechat_safety import security_notice
        target = self.settings.get('target')
        count = 0
        for raw in pending(limit=limit, target=target):
            if not self._listening or not self.online:
                break
            if raw.get('username') != target and not security_notice(raw):
                continue
            for callback in list(self._callbacks):
                callback(dict(raw))
            count += 1
        return count

    def stop_receiving(self):
        with self._lifecycle_lock:
            self._receive_generation += 1
            self._listening = False

    def stop_listening(self):
        self.stop_receiving()
        self.cancel_pending_sends()
        self._environment(False, 'stopped', '协议通道已停止')

    def cancel_pending_sends(self):
        with self._lifecycle_lock:
            self._generation += 1
            self._cancel.set()

    def _send_active(self, generation):
        with self._lifecycle_lock:
            return generation == self._generation and not self._cancel.is_set() and self.online and not self._hold()

    def resume_sends(self):
        if self._hold():
            raise HookUnavailable('账号安全暂停，发送未恢复')
        with self._lifecycle_lock:
            self._cancel.clear()

    def disconnect(self):
        self.stop_listening()
        self.online = False
        self._binding = None
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None
        if self._receipt is not None:
            close = getattr(self._receipt, 'close', None)
            if callable(close):
                close()
        if self._journal is not None:
            close = getattr(self._journal, 'close', None)
            if callable(close):
                close()
                self._journal = None

    def inspect_send_environment(self, who=''):
        try:
            if not self.online or self._hold():
                raise HookUnavailable('协议通道尚未连接或账号处于安全暂停')
            _, _, target, _ = self._options()
            if who and str(who) != target:
                return self._environment(False, 'target_not_open', '协议通道只允许已固定的一个会话')
            self._probe()
            if self._receipt is None:
                return self._environment(False, 'receipt_unavailable', '尚无可靠的新增自身消息记录核验，本轮不发送')
            return self._environment(True, '', '账号和固定会话已核验；无需微信窗口前台')
        except Exception as error:
            text = str(error) if isinstance(error, HookUnavailable) else '协议发送环境未通过检查'
            return self._environment(False, 'hook_unavailable', text)

    def _chat_name(self, target, target_name=''):
        _, _, fixed, _ = self._options()
        if target != fixed:
            raise ValueError('协议通道只回复已固定会话')
        return fixed

    def resolve_target_name(self, target):
        return str(target) if str(target) == self.settings.get('target') else ''

    def send_files(self, target, paths, target_name='', as_image=False):
        return self._refuse('协议后台本轮只支持文字，图片或文件未发送', 'unsupported_media')

    def download_media(self, chat, local_id, msg_type, shard=''):
        return None

    def send_text(self, target, text, target_name='', at=None):
        request_id = uuid.uuid4().hex
        posted = False
        with self._send_lock:
            try:
                with self._lifecycle_lock:
                    generation = self._generation
                if not self._send_active(generation):
                    return self._refuse('机器人已停止、未连接或账号安全暂停，本条未发送', 'stopped')
                _, account, fixed, _ = self._options()
                if target != fixed or service_target({'target': target}):
                    return self._refuse('目标不在固定会话范围内，本条未发送', 'target_not_open')
                if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                    return self._refuse('文字为空或超过发送上限，本条未发送', 'invalid_text')
                text = text.replace('\r\n', '\n').replace('\r', '\n')
                self._probe()
                if self._receipt is None:
                    return self._refuse('新增自身消息记录核验尚未可用，本条未发送', 'receipt_unavailable')
                baseline = self._receipt.prepare(account, target, text)
                if not self._send_active(generation):
                    return self._refuse('准备期间机器人停止，本条未发送', 'stopped')
                # 基线读取可能等待磁盘；写入前必须再次核对进程和账号。
                self._probe()
                if not self._journal.begin_send(request_id, target, text):
                    return self._refuse('本次请求已经消费，不重发', 'request_consumed')
                if not self._send_active(generation):
                    self._journal.finish_send(request_id, 'not_submitted')
                    return self._refuse('提交前机器人停止，本条未发送', 'stopped')
                posted = True
                result = self._post('SendTextMsg', {'wxidorgid': target, 'msg': text}, timeout=8)
                if type(result.get('ret')) is not int or result['ret'] != 0:
                    raise HookUnavailable('微信发送接口未确认提交，结果待核实，不重发')
                self._journal.finish_send(request_id, 'submitted')
                receipt = self._receipt.verify(baseline)
                expected_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
                if (isinstance(receipt, dict) and receipt.get('verified') is True
                        and receipt.get('account') == account and receipt.get('target') == target
                        and receipt.get('body_sha256') == expected_hash
                        and receipt.get('account_matches') is True
                        and type(receipt.get('new_self_rows')) is int and receipt['new_self_rows'] == 1
                        and type(receipt.get('server_id')) is int and receipt['server_id'] > 0):
                    proof = dict(receipt, new_outgoing_record_verified=True)
                    self._journal.finish_send(request_id, 'confirmed', receipt=proof)
                    self.send_ok += 1
                    self.last_send_at = time.time()
                    with self._lifecycle_lock:
                        # 记录的确已发出，但等待回执期间可能已经停止/超时。
                        # 同一锁内复核代次并发布状态，晚回执不能复活发送就绪。
                        active = self._send_active(generation)
                        reason = ('已核验新增自身消息记录及服务器编号' if active else
                                  '已核验本条新增自身消息记录；通道已停止或超时，未恢复发送')
                        if active:
                            self.consecutive_fail = 0
                        environment = self._environment(active, '' if active else 'late_confirmation', reason)
                        environment.update(submission_state='confirmed', send_key=True, delivery_verified=True)
                        self.send_environment = environment
                    return SendOutcome(True, reason, dict(environment,
                        request_id=request_id, server_id_observed=True))
                raise HookUnavailable('接口已返回，但未确认唯一新增自身消息记录，结果待核实，不重发')
            except Exception as error:
                message = str(error) if isinstance(error, HookUnavailable) else '协议发送检查失败（' + type(error).__name__ + '）'
                if posted:
                    try:
                        self._journal.finish_send(request_id, 'uncertain')
                    except Exception:
                        pass
                    self.send_fail += 1
                    self.consecutive_fail += 1
                    self.last_send_at = time.time()
                    self.last_error = message
                    self._environment(False, 'submission_uncertain', message)
                    return SendOutcome(False, message, {'submission_state': 'uncertain', 'send_key': True,
                        'delivery_verified': False, 'request_id': request_id})
                return self._refuse(message, 'hook_unavailable', reconnect=True)

    def mark_send_timeout(self, reason='发送超时'):
        self.cancel_pending_sends()
        super().mark_send_timeout(reason)
        environment = self._environment(False, 'submission_uncertain', reason)
        environment.update(submission_state='uncertain', delivery_verified=False)
        self.send_environment = environment

    def status(self):
        result = super().status()
        result.update(backend='hook', execution_environment='hook', current_chat_only=True,
            text_only=True, listening=self._listening,
            read_state='listening' if self.online and self._listening else 'connected' if self.online else 'disconnected',
            client_login_state='verified' if self.online else 'unknown',
            identity_checked_at=self._identity_checked_at, scope='pinned_single_chat')
        return result
