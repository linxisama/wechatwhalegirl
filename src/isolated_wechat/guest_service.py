"""沙盒内的串行收发代理：不运行模型、不启动微信、不自动切会话。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from ..runtime_identity import APP_VERSION
from ..wechat_bridge import WeChatBridge
from ..wechat_safety import SafetyHold, security_notice
from .protocol import (ACTIONS, MAX_DOCUMENT_BYTES, MAX_EVENT_BYTES, MAX_SEND_FILE_BYTES,
                       MAX_SEND_FILES, SEND_FILES_BUDGET, UUID32, ProtocolError, checked_path,
                       load_pairing, read_document, request_digest, signed, validate_control,
                       validate_request, write_document)

MAX_EVENTS = 500
MAX_CONSUMED = 20000
MAX_REQUESTS_SEEN = 20000


def require_sandbox_user():
    if os.name != 'nt':
        raise ProtocolError('隔离代理只能在 Windows Sandbox 内运行')
    import ctypes
    from ctypes import wintypes
    value, size = ctypes.create_unicode_buffer(512), wintypes.DWORD(512)
    if (not ctypes.windll.advapi32.GetUserNameW(value, ctypes.byref(size))
            or value.value != 'WDAGUtilityAccount'):
        raise ProtocolError('隔离代理只能由 Windows Sandbox 用户运行')


SEND_TIMING_KEYS = ('total', 'receive', 'agent', 'gui_ready', 'press', 'confirm', 'write')

# 回执是单个 JSON 文档（上限 4MB）：三帧 base64 画面还要给签名与信封留余量。
FRAME_PAYLOAD_BUDGET = MAX_DOCUMENT_BYTES - 256 * 1024

_FILE_NAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_file_name(name):
    """客人端落盘用的文件名：去掉 Windows 不认的字符，绝不带出任何路径。"""
    value = _FILE_NAME_BAD.sub('_', str(name or '')).strip(' .')
    return value[:100] or 'file'


def _frames_size(frames) -> int:
    return len(json.dumps({'ok': True, 'frames': list(frames), 'message': ''},
                          ensure_ascii=False).encode('utf-8'))


def _send_timings(received, started, finished, extra=None) -> dict:
    """客人端耗时归因（毫秒）：只上报取得到的键；纯观测，不参与任何判定。"""
    timings = {'total': round((finished - received) * 1000, 3),
               'receive': round((started - received) * 1000, 3),
               'agent': round((finished - started) * 1000, 3)}
    for key, value in (extra or {}).items():
        if key in SEND_TIMING_KEYS and key not in timings and isinstance(value, (int, float)):
            timings[key] = round(float(value), 3)
    return timings


class GuestService:
    def __init__(self, channel_dir, data_root=None, *, bridge_factory=None):
        self.pairing = load_pairing(channel_dir)
        self.root = self.pairing.root
        self.epoch = uuid.uuid4().hex
        from .launcher import runtime_program_key
        self.program_key = runtime_program_key()
        # Sender 子进程继承此受核验目录；只有 OCR 小裁剪在此请求宿主本机识别。
        os.environ['WHALE_ISOLATED_CHANNEL'] = str(self.root)
        if data_root is None:
            data_root = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'WhaleGirlWeChatGuest'
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.bridge = (bridge_factory or WeChatBridge)(data_dir=self.data_root / 'data/wechat-only',
                                                       backend='auto', note=lambda _text: None)
        self.bridge.background_send = False
        self.bridge.send_compat = True
        self.bridge.auto_open_chat = False
        self.bridge.hot_activate_uia = False
        self._stop = threading.Event()
        self._state_lock = threading.RLock()
        self._event_lock = threading.RLock()
        self._control_lock = threading.Lock()
        self._sequence = 0
        self._status = {}
        self._client_touch = time.time()
        self._busy_deadline = 0.0
        self._controls_seen = set()
        self._overflow = False
        self._heartbeat_at = 0.0
        self._requests_seen = {}
        self._validation_wall = time.time()
        self._prepare_control_slots()
        self._refresh_status()

    def _prepare_control_slots(self):
        # Windows Sandbox 共享目录会缓存“不存在”的查询，首次取消可能晚数秒。
        # 先建立签名但不可执行的槽；独占创建保留已到达的真实取消/停止指令。
        for action in ('cancel', 'stop'):
            try:
                write_document(self.root / 'control' / (action + '.json'), self.pairing,
                    {'epoch': self.epoch, 'action': 'pending', 'pending_for': action}, exclusive=True)
            except FileExistsError:
                pass
        # 本 epoch 在首个心跳前建立既有名称，绕过共享目录对新 UUID 的负缓存。
        # 旧槽必须失效；不能保留前一代理的请求并宣布新能力。
        write_document(self.root / 'control/request.json', self.pairing,
                       {'epoch': self.epoch, 'action': 'pending', 'pending_for': 'request'})

    def _refresh_status(self):
        value = dict(self.bridge.status())
        value.update(execution_environment='sandbox', scope='current_open_chat', text_only=False,
                     safety=SafetyHold(self.data_root / 'data/wechat_safety_hold.json').status())
        if self._overflow:
            value.update(online=False, error='隔离入站队列已满，已停止收发；请停止并重新配对检查记录')
        with self._state_lock:
            self._status = value

    def heartbeat(self, active=True):
        with self._state_lock:
            status = dict(self._status)
        write_document(self.root / 'service.json', self.pairing,
                       {'epoch': self.epoch, 'app_version': APP_VERSION, 'active': bool(active),
                        'program_key': self.program_key,
                        'heartbeat': time.time(), 'status': status,
                        'capabilities': ['current_chat_text', 'files_v1', 'request_mirror_v1']})

    def _on_message(self, raw):
        if not isinstance(raw, dict):
            return
        if security_notice(raw):
            SafetyHold(self.data_root / 'data/wechat_safety_hold.json').activate()
            self.bridge.cancel_pending_sends()
            self.bridge.stop_receiving()
        with self._event_lock:
            files = list((self.root / 'events').glob('*.json'))
            payload = {'epoch': self.epoch, 'event_id': self._sequence + 1, 'message': raw}
            try:
                size = len(json.dumps(signed(self.pairing, payload), ensure_ascii=False).encode('utf-8'))
            except (TypeError, ValueError, ProtocolError):
                size = MAX_EVENT_BYTES + 1
            if len(files) >= MAX_EVENTS or size > MAX_EVENT_BYTES:
                self._overflow = True
                self.bridge.cancel_pending_sends()
                self.bridge.stop_receiving()
                return
            sequence = self._sequence + 1
            path = self.root / 'events' / f'{self.epoch}-{sequence:012d}.json'
            try:
                write_document(path, self.pairing, payload, exclusive=True)
            except (OSError, ProtocolError):
                # Listener 已读过此条原消息，落盘失败不能静默继续并越过它。
                self._overflow = True
                self.bridge.cancel_pending_sends()
                self.bridge.stop_receiving()
                return
            self._sequence = sequence

    def _poll_events(self, ack):
        if type(ack) is not int or not 0 <= ack <= self._sequence:
            raise ProtocolError('隔离入站确认水位无效')
        with self._event_lock:
            result = []
            for path in sorted((self.root / 'events').glob(self.epoch + '-*.json')):
                value = read_document(path, self.pairing, MAX_EVENT_BYTES)
                sequence = value.get('event_id')
                if value.get('epoch') != self.epoch or type(sequence) is not int:
                    raise ProtocolError('隔离入站事件身份不符')
                if sequence <= ack:
                    checked_path(path, self.pairing).unlink(missing_ok=True)
                elif len(result) < 30:
                    result.append({'event_id': sequence, 'message': value.get('message')})
            return result

    def _settings(self, args):
        groups = args.get('target_groups', [])
        if (not isinstance(groups, list) or len(groups) > 500
                or any(not isinstance(group, str) or len(group) > 256 for group in groups)):
            raise ProtocolError('隔离监听名单无效')
        account = args.get('configured_account', '')
        if not isinstance(account, str) or len(account) > 256:
            raise ProtocolError('隔离账号选择无效')
        self.bridge.configured_account = account
        self.bridge.target_groups = list(groups)
        self.bridge.listen_private = args.get('listen_private') is True
        self.bridge.listen_groups = args.get('listen_groups') is True
        self.bridge.background_send = False
        self.bridge.auto_open_chat = False
        self.bridge.hot_activate_uia = False

    def dispatch(self, action, args):
        if action not in ACTIONS:
            raise ProtocolError('隔离动作不受支持')
        if action == 'connect':
            if self._overflow:
                return {'ok': False, 'message': '隔离消息队列已满，请停止并重新配对检查'}
            self._settings(args)
            ok, detail = self.bridge.connect()
            return {'ok': bool(ok), 'message': str(detail), 'status': self.bridge.status()}
        if action == 'disconnect':
            self.bridge.cancel_pending_sends()
            self.bridge.disconnect()
            return {'ok': True, 'message': '隔离微信读取已断开'}
        if action == 'start_listening':
            if self._overflow:
                return {'ok': False, 'message': '隔离入站记录未完整落盘，未恢复监听'}
            self._settings(args)
            ok, detail = self.bridge.start_listening(self._on_message)
            return {'ok': bool(ok), 'message': str(detail)}
        if action == 'stop_listening':
            self.bridge.cancel_pending_sends()
            self.bridge.stop_listening()
            return {'ok': True}
        if action == 'resume_sends':
            if self._overflow:
                return {'ok': False, 'message': '隔离入站记录未完整落盘，未恢复发送'}
            if SafetyHold(self.data_root / 'data/wechat_safety_hold.json').status().get('active'):
                return {'ok': False, 'message': '隔离微信账号处于安全暂停'}
            with self._control_lock:
                try:
                    pending = read_document(self.root / 'control/cancel.json', self.pairing, 8192)
                    if validate_control(pending, self.epoch, 'cancel'):
                        self._controls_seen.add(pending['request_id'])
                except (OSError, ProtocolError):
                    pass
                self.bridge.resume_sends()
            return {'ok': True}
        if action == 'poll':
            return {'ok': True, 'events': self._poll_events(args.get('ack', 0)), 'status': self.bridge.status()}
        if action == 'inspect_environment':
            who = args.get('who', '')
            if not isinstance(who, str) or len(who) > 256:
                raise ProtocolError('隔离会话名无效')
            return {'ok': True, 'environment': self.bridge.inspect_send_environment(who)}
        if action == 'resolve_chat_name':
            target, name = args.get('target'), args.get('target_name', '')
            if (not isinstance(target, str) or not 1 <= len(target) <= 256
                    or not isinstance(name, str) or len(name) > 256):
                raise ProtocolError('隔离会话名称参数无效')
            return {'ok': True, 'value': self.bridge._chat_name(target, name)}
        if action == 'send_text':
            # 只记时刻：参数、顺序、超时与判定逻辑一个都不改。
            received = time.perf_counter()
            if self._overflow:
                return {'ok': False, 'message': '隔离入站记录未完整落盘，未发送',
                        'diagnostic': {'submission_state': 'not_submitted'}}
            target, text, name = args.get('target'), args.get('text'), args.get('target_name', '')
            at = args.get('at') or []
            if (not isinstance(target, str) or not 1 <= len(target) <= 256
                    or not isinstance(text, str) or not 1 <= len(text) <= 12000
                    or not isinstance(name, str) or len(name) > 256 or not isinstance(at, list)
                    or len(at) > 10 or any(not isinstance(value, str) or len(value) > 128 for value in at)):
                raise ProtocolError('隔离文字发送参数无效')
            started = time.perf_counter()
            result = self.bridge.send_text(target, text, name, at)
            finished = time.perf_counter()
            diagnostic = dict(getattr(result, 'diagnostic', {}))
            return {'ok': bool(result[0]), 'message': str(result[1]),
                    'diagnostic': diagnostic, 'status': self.bridge.status(),
                    'timings': _send_timings(received, started, finished,
                                             diagnostic.get('timings'))}
        if action == 'send_files':
            return self._send_files(args)
        if action == 'video_frames':
            # 客体侧抽帧：沙盒后端下不了媒体（宿主 download_media 是 fail-closed 空桩），
            # 视频帧只能由客人端就地解码后随回执送回去。
            chat, local_id = args.get('chat'), args.get('local_id')
            shard, want, max_side = args.get('shard', ''), args.get('want', 3), args.get('max_side', 768)
            if (not isinstance(chat, str) or not 1 <= len(chat) <= 256
                    or type(local_id) not in (str, int) or not str(local_id)
                    or not isinstance(shard, str) or len(shard) > 256
                    or type(want) is not int or not 1 <= want <= 3
                    or type(max_side) is not int or not 256 <= max_side <= 1024):
                return {'ok': False, 'message': '隔离视频抽帧参数无效', 'frames': []}
            # WeChatBridge 没有 download_video：分片与保存目录都收在
            # download_media(chat, local_id, 'video', shard) 里，内部再调
            # MediaDownloader.download_video(chat, local_id, save_dir=…, shard=…)。
            try:
                path = self.bridge.download_media(chat, local_id, 'video', shard)
            except Exception:
                path = None
            local = None
            try:
                local = Path(str(path)) if path else None
            except (TypeError, ValueError):
                local = None
            if local is None or not local.is_file():
                return {'ok': False, 'message': '视频没有落到本机，未抽帧', 'frames': []}
            reason = ''
            try:
                from ..video_frames import frames_for_video
                frames, reason = frames_for_video(str(local), want=want, max_side=max_side)
            except Exception:
                frames = []
            frames = [frame for frame in (frames or [])
                      if isinstance(frame, str) and frame.startswith('data:image/')]
            if not frames:
                return {'ok': False, 'message': reason or '视频抽帧失败', 'frames': []}
            # 超上限的回执写不出去会让整条代理循环崩掉：先按预算裁帧。
            while len(frames) > 1 and _frames_size(frames) > FRAME_PAYLOAD_BUDGET:
                frames.pop()
            if _frames_size(frames) > FRAME_PAYLOAD_BUDGET:
                return {'ok': False, 'message': '视频帧过大，未能送回宿主', 'frames': []}
            return {'ok': True, 'frames': frames, 'message': ''}
        if action in ('display_name', 'resolve_session', 'member_names'):
            text = args.get('text', '')
            if not isinstance(text, str) or len(text) > 256:
                raise ProtocolError('隔离会话标识无效')
            if action == 'display_name':
                value = self.bridge.display_name(text)
            elif action == 'resolve_session':
                value = self.bridge.resolve_session(text)
            else:
                value = self.bridge.member_names(text)
            return {'ok': True, 'value': value}
        raise ProtocolError('隔离动作不受支持')

    def _send_files(self, args):
        """宿主只送得进 base64：先落成客人端自己的文件，再交给微信发出去。"""
        received = time.perf_counter()
        if self._overflow:
            return {'ok': False, 'message': '隔离入站记录未完整落盘，未发送',
                    'diagnostic': {'submission_state': 'not_submitted'}}
        target, name = args.get('target'), args.get('target_name', '')
        files, as_image = args.get('files'), args.get('as_image') is True
        if (not isinstance(target, str) or not 1 <= len(target) <= 256
                or not isinstance(name, str) or len(name) > 256
                or not isinstance(files, list) or not 1 <= len(files) <= MAX_SEND_FILES):
            raise ProtocolError('隔离图片或文件发送参数无效')
        total, decoded = 0, []
        for item in files:
            if not isinstance(item, dict):
                raise ProtocolError('隔离图片或文件发送参数无效')
            raw_name, encoded = item.get('name', ''), item.get('data')
            if (not isinstance(raw_name, str) or len(raw_name) > 120
                    or not isinstance(encoded, str) or not encoded):
                raise ProtocolError('隔离图片或文件发送参数无效')
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                raise ProtocolError('隔离图片或文件内容无效')
            if not data or len(data) > MAX_SEND_FILE_BYTES:
                raise ProtocolError('隔离图片或文件大小无效')
            total += len(data)
            if total > SEND_FILES_BUDGET:
                raise ProtocolError('隔离图片或文件合计超出上限')
            decoded.append((raw_name, data))
        folder = self.data_root / 'data' / 'isolated-send' / uuid.uuid4().hex
        try:
            folder.mkdir(parents=True, exist_ok=True)
            paths = []
            for index, (raw_name, data) in enumerate(decoded):
                path = folder / f'{index + 1}-{_safe_file_name(raw_name)}'
                path.write_bytes(data)
                paths.append(str(path))
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            return {'ok': False, 'message': '隔离通道无法暂存图片或文件，本次未发送',
                    'diagnostic': {'submission_state': 'not_submitted'}}
        started = time.perf_counter()
        try:
            result = self.bridge.send_files(target, paths, name, as_image)
        finally:
            # 发完即删：客人端磁盘是要留给微信自己的，不留中转目录。
            shutil.rmtree(folder, ignore_errors=True)
        finished = time.perf_counter()
        diagnostic = dict(getattr(result, 'diagnostic', {}))
        return {'ok': bool(result[0]), 'message': str(result[1]),
                'diagnostic': diagnostic, 'status': self.bridge.status(),
                'timings': _send_timings(received, started, finished,
                                         diagnostic.get('timings'))}
    def _request_now(self):
        # 已过期身份不能在墙钟回拨后重新变成有效请求。
        self._validation_wall = max(self._validation_wall, time.time())
        return self._validation_wall

    def _prune_requests_seen(self):
        now, monotonic = self._request_now(), time.monotonic()
        for identity, entry in list(self._requests_seen.items()):
            if now > entry['deadline'] and monotonic - entry['seen_at'] >= 35:
                del self._requests_seen[identity]

    def _remove_matching_request(self, path, digest):
        # 镜像先可见时原件可缺失；清理不能误删随后出现的冲突载荷。
        try:
            current = read_document(path, self.pairing)
            if request_digest(current) == digest:
                checked_path(path, self.pairing).unlink(missing_ok=True)
        except (OSError, ProtocolError):
            pass

    def process_once(self):
        self._prune_requests_seen()
        try:
            request = read_document(self.root / 'control/request.json', self.pairing)
            validate_request(request, self.epoch, now=self._request_now())
        except (OSError, ProtocolError):
            pass
        else:
            path = self.root / 'requests' / (request['request_id'] + '.json')
            if self._process_request(path, request):
                return True
        # 陈旧镜像只跳过本槽，仍处理被其他宿主请求覆盖的不可变原件。
        for path in sorted((self.root / 'requests').glob('*.json'))[:100]:
            if not UUID32.fullmatch(path.stem):
                continue
            try:
                request = read_document(path, self.pairing)
            except (OSError, ProtocolError):
                # exclusive 写入完成前可能读到半份文档，保留短暂的在写请求。
                # 过期坏文档不应永久占据队列，阻止以后有效请求被处理。
                try:
                    if time.time() - path.stat().st_mtime > 35:
                        checked_path(path, self.pairing).unlink(missing_ok=True)
                except (OSError, ProtocolError):
                    pass
                continue
            try:
                validate_request(request, self.epoch, now=self._request_now())
                if request['request_id'] != path.stem:
                    raise ProtocolError('隔离请求文件身份不一致')
            except ProtocolError:
                # 签名核验已完整成功：旧epoch/已过期请求只能丢弃，绝不再执行。
                checked_path(path, self.pairing).unlink(missing_ok=True)
                continue
            if self._process_request(path, request):
                return True
        return False

    def _duplicate_response(self, response, request_id, digest, action):
        try:
            # 已有真实回执必须保留，不能被冲突载荷/陈旧镜像改成另一结果。
            read_document(response, self.pairing)
        except FileNotFoundError:
            write_document(response, self.pairing, {'epoch': self.epoch, 'request_id': request_id,
                'request_digest': digest, 'ok': False,
                'value': {'ok': False, 'message': '隔离请求已消费，结果待核实，不重发',
                          'diagnostic': {'submission_state': 'uncertain', 'send_key': True}
                                        if action == 'send_text' else {}}})

    def _process_request(self, path, request):
        request_id = request['request_id']
        digest = request_digest(request)
        response = self.root / 'responses' / (request_id + '.json')
        marker = self.root / 'consumed' / (request_id + '.json')
        seen = self._requests_seen.get(request_id)
        if seen is not None:
            if seen['digest'] != digest:
                seen['conflict'] = True
            # 宿主正常取走回执后，旧槽不能每轮再造一个孤儿 uncertain 文件。
            # 冲突或首次回执失败只允许补一次保守回执，且始终绑定原摘要。
            if ((seen['conflict'] or not seen['response_published'])
                    and not seen['duplicate_responded']):
                seen['duplicate_responded'] = True
                try:
                    self._duplicate_response(response, request_id, seen['digest'], seen['action'])
                except (OSError, ProtocolError):
                    pass
            if not seen['conflict']:
                self._remove_matching_request(path, digest)
            return False
        if len(self._requests_seen) >= MAX_REQUESTS_SEEN:
            return False  # 不逐出仍可能被旧槽重放的身份。
        entry = {'digest': digest, 'deadline': request['deadline'], 'seen_at': time.monotonic(),
                 'action': request['action'], 'conflict': False,
                 'response_published': False, 'duplicate_responded': False}
        self._requests_seen[request_id] = entry
        try:
            canonical = read_document(path, self.pairing)
        except FileNotFoundError:
            pass  # 已验证的完整镜像本身就是同一请求，无需等新文件名可见。
        except (OSError, ProtocolError):
            return False
        else:
            if request_digest(canonical) != digest:
                entry['conflict'] = True
                return False
        try:
            if request['action'] == 'send_text' and len(list((self.root / 'consumed').glob('*.json'))) >= MAX_CONSUMED and not marker.exists():
                raise ProtocolError('隔离会话消费记录已达到上限，请重新配对')
            write_document(marker, self.pairing,
                {'epoch': self.epoch, 'request_id': request_id, 'action': request['action'],
                 'request_digest': digest, 'consumed': time.time(), 'retry_allowed': False}, exclusive=True)
        except FileExistsError:
            # 响应丢失也不重做：尤其不能重放已经消费过的发送请求。
            try:
                consumed = read_document(marker, self.pairing, 8192)
                if consumed.get('epoch') == self.epoch and consumed.get('request_id') == request_id:
                    stored_digest = consumed.get('request_digest', '')
                    entry['digest'] = stored_digest
                    entry['conflict'] = stored_digest != digest
                    entry['duplicate_responded'] = True
                    self._duplicate_response(response, request_id, stored_digest, consumed.get('action'))
                    if not entry['conflict']:
                        self._remove_matching_request(path, digest)
            except (OSError, ProtocolError):
                pass
            return False
        except (OSError, ProtocolError):
            # 首次消费落盘失败也属于本次请求结束；不留旧租约，且不恢复发送。
            # 此身份已登记，旧镜像再次可见时不会反复延长该空闲租约。
            with self._control_lock:
                self._client_touch = time.time()
                self._busy_deadline = 0.0
            return False
        with self._control_lock:
            if self._stop.is_set():
                return False
            # 只为新请求续租，旧镜像不能永久阻止宿主消失后的自动取消。
            self._client_touch = time.time()
            self._busy_deadline = request['deadline']
        try:
            # 排队与消费落盘之后仍复核截止时间，迟到的请求绝不操作微信。
            dispatch_started = False
            validate_request(request, self.epoch, now=self._request_now())
            if self._stop.is_set():
                raise ProtocolError('隔离代理已停止')
            dispatch_started = True
            value = self.dispatch(request['action'], request['args'])
            ok = True
        except Exception as error:
            value = {'ok': False, 'message': '隔离请求处理失败（' + type(error).__name__ + '）',
                     'diagnostic': {'submission_state': 'uncertain' if dispatch_started and not isinstance(error, ProtocolError) else 'not_submitted'}
                                   if request['action'] == 'send_text' else {}}
            ok = False
        finally:
            try:
                self._refresh_status()
            finally:
                with self._control_lock:
                    self._client_touch = time.time()
                    self._busy_deadline = 0.0
        if isinstance(value.get('status'), dict):
            with self._state_lock:
                value['status'] = dict(self._status)
        write_document(response, self.pairing, {'epoch': self.epoch, 'request_id': request_id,
                                                'request_digest': digest, 'ok': ok, 'value': value})
        entry['response_published'] = True
        self._remove_matching_request(path, digest)
        # 只读/幂等管理请求在响应落盘后释放短期标记；发送消费记录永久保留到重配对。
        if request['action'] != 'send_text':
            marker.unlink(missing_ok=True)
        return True

    def _watch(self):
        while not self._stop.wait(.1):
            for action in ('cancel', 'stop'):
                try:
                    value = read_document(self.root / 'control' / (action + '.json'), self.pairing, 8192)
                    with self._control_lock:
                        if (validate_control(value, self.epoch, action)
                                and value['request_id'] not in self._controls_seen):
                            self._controls_seen.add(value['request_id'])
                            self.bridge.cancel_pending_sends()
                            if action == 'stop':
                                self._stop.set()
                except (OSError, ProtocolError):
                    pass
            # 宿主消失不能留下仍会迟到发送的代理；只停止自己创建的发送进程。
            with self._control_lock:
                deadline = self._busy_deadline or self._client_touch + 15
                if time.time() > deadline:
                    self.bridge.cancel_pending_sends()
                    self.bridge.stop_receiving()
            try:
                if time.monotonic() - self._heartbeat_at >= 1:
                    self.heartbeat()
                    self._heartbeat_at = time.monotonic()
            except (OSError, ProtocolError):
                self.bridge.cancel_pending_sends()
                self._stop.set()

    def serve_forever(self):
        self.heartbeat()
        watcher = threading.Thread(target=self._watch, daemon=True, name='isolated-wechat-watch')
        watcher.start()
        try:
            while not self._stop.is_set():
                if not self.process_once():
                    self._stop.wait(.04)
        finally:
            self._stop.set()
            self.bridge.cancel_pending_sends()
            self.bridge.disconnect()
            watcher.join(1)
            self._refresh_status()
            try:
                self.heartbeat(active=False)
            except (OSError, ProtocolError):
                pass


def main(channel_dir, data_root=None):
    require_sandbox_user()
    pairing = load_pairing(channel_dir)
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, True, 'Local\\WhaleGirlIsolated-' + pairing['channel_id'])
    if not mutex or ctypes.get_last_error() == 183:
        if mutex:
            kernel.CloseHandle(mutex)
        raise ProtocolError('此隔离通道已有代理运行，未创建第二个收发实例')
    try:
        service = GuestService(channel_dir, data_root)
        service.serve_forever()
    finally:
        kernel.CloseHandle(mutex)
    return 0
