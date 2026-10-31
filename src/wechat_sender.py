"""单次发送子进程桥接；调用方仍通过单线程闸门串行发送。"""
import json
import logging
import subprocess
import sys
import uuid
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from .logging_setup import (RUNTIME_LOGGER_NAME, backend_log_dir, log_send_failure,
                            mask_chat_name)


PHASE_NAMES = {'starting': '启动', 'request': '读取任务', 'desktop': '检查锁屏/交互桌面', 'dependencies': '加载原生依赖',
               'window': '定位窗口', 'conversation': '核对会话', 'input_control': '定位输入框',
               'focus': '确认焦点', 'input': '写入正文/图片', 'readback': '回读正文',
               'send_key': '执行发送键', 'confirmation': '确认发送结果', 'clipboard': '检查剪贴板'}


def phase_detail(path, started=None):
    elapsed = f'；总耗时：{round((time.monotonic() - started) * 1000)}ms' if started is not None else ''
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        name = PHASE_NAMES.get(state.get('stage'), '未知阶段')
        return f'阶段：{name}；发送键：{"已执行或正在执行" if state.get("send_key") else "未执行"}' + elapsed
    except (OSError, ValueError, AttributeError):
        return '阶段：子进程启动/未返回状态' + elapsed


def interrupted_diagnostic(path, *, inspection=False):
    """读子进程已持久化的提交证据；无状态时保留未知，不能猜成未发送。"""
    if inspection:
        return {'submission_state': 'not_submitted', 'send_key': False,
                'waiting_code': 'inspection_failed'}
    try:
        state = json.loads(Path(path).read_text(encoding='utf-8'))
        if state.get('send_key') is False:
            return {'submission_state': 'not_submitted', 'send_key': False}
        if state.get('send_key') is True:
            return {'submission_state': 'uncertain', 'send_key': True}
    except (OSError, ValueError, AttributeError):
        pass
    return {'submission_state': 'uncertain'}


def log_send_timing(spawn_ms, rpc_ms, guest_total_ms=None):
    """一行 INFO 记子进程启动/往返/客人端总耗时；日志坏了绝不连累发送。"""
    try:
        guest = '-' if guest_total_ms is None else guest_total_ms
        logging.getLogger(RUNTIME_LOGGER_NAME).info(
            f'send timing worker spawn_ms={spawn_ms} rpc_ms={rpc_ms} guest_total_ms={guest}')
    except BaseException:
        pass


@dataclass
class SendResult:
    ok: bool
    message: str
    requires_reconnect: bool = True
    diagnostic: dict = field(default_factory=dict)

    def __post_init__(self):
        self.diagnostic = dict(self.diagnostic or {})
        self.diagnostic.setdefault('delivery_verified', False)
        if not self.ok:
            # 窗口发现先写入的ready不能覆盖后续输入/提交失败，防止界面假绿。
            self.diagnostic = dict(self.diagnostic or {}, ready=False, reason=self.message)
            self.diagnostic.setdefault('waiting_code', 'send_failed')

    def __bool__(self):
        return self.ok

    def __str__(self):
        return self.message


class IsolatedWeChatSender:
    def __init__(self, data_dir: Path, account_path: str = '', compat=None, open_chat=None, background=None,
                 submit=None, hot=None):
        self.data_dir = data_dir
        self.account_path = str(account_path)
        # compat 是"发送兼容模式"的取值函数（微信 4.x 需要它才能拿到输入控件）。
        # 用函数而不是布尔值：设置页随时改，下一次发送就要用新值，不必重连微信。
        self._compat = compat or (lambda: False)
        # open_chat：会话不是目标会话时，是否允许"搜索并打开"（用户明确要求的原始行为）
        self._open_chat = open_chat or (lambda: False)
        self._background = background or (lambda: False)
        self._submit = submit or (lambda: 'invoke')
        # 旧hot形参仅兼容调用；生产任务不再接入微信进程写入。
        self._hot = hot or (lambda: False)
        self._lock = threading.Lock()
        self._stopped = False
        self._send_epoch = 0
        self._process = None
        # 数据目录在 <根>/data/<子目录>：上溯两级就是安装根，日志按 <根>/logs 走
        self.log_dir = backend_log_dir(Path(data_dir).resolve().parent.parent / 'logs')

    def stop(self):
        """只终止自己创建的发送进程，绝不结束微信。"""
        with self._lock:
            self._stopped = True
            self._send_epoch += 1
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def resume(self):
        with self._lock:
            self._stopped = False

    def _prepare_job(self, job: dict) -> dict:
        """补上这次发送需要的上下文：绑定账号 + 发送兼容模式开关。

        为什么开关要每次现取：用户在设置页改完就该在**下一次发送**生效，
        不能要求断开重连微信；用函数而不是布尔快照也便于测试。
        """
        job = dict(job)
        try:
            job['background_send'] = bool(self._background())
        except Exception:
            # 读取失败不得悄悄切回会抢焦点的发送路径。
            job['background_send'] = True
        # 明确选择前台时从头走前台，不先尝试尚未证实有效的后台Invoke而留下半成品草稿。
        # 旧子进程任务没有这个字段时仍按旧协议处理，提交失败后仍禁止前台补发。
        job['foreground_only'] = not job['background_send']
        try:
            job['background_submit'] = self._submit()
        except Exception:
            job['background_submit'] = 'invalid'
        if self.account_path:
            job['account_path'] = self.account_path
        try:
            job['send_compat'] = bool(self._compat())
        except Exception:
            job['send_compat'] = False
        try:
            job['auto_open_chat'] = bool(self._open_chat())
        except Exception:
            job['auto_open_chat'] = False
        job['hot_activate'] = False
        # 微信 4.1.x 没有 mmui:: 控件，UIA 通道必失败；允许子进程在**未按发送键**时
        # 降级到用户批准的「坐标+OCR」通道。默认不开，直接调 execute() 的老任务不受影响。
        job['guia_send'] = True
        # 子进程按这个目录写 backend.log，父子进程才落到同一份日志里
        job['log_dir'] = str(self.log_dir)
        return job

    def _run(self, job):
        with self._lock:
            epoch = self._send_epoch
        started = time.monotonic()
        tick = spawned = finished = time.perf_counter()   # 归因专用高精度钟，不改既有 started
        job = self._prepare_job(job)
        folder = self.data_dir / "send_jobs"
        folder.mkdir(parents=True, exist_ok=True)
        request = folder / (uuid.uuid4().hex + ".json")
        output = request.with_suffix(".result.json")
        status = request.with_suffix('.status.json')
        request.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--wechat-send-worker", str(request)]
        else:
            command = [sys.executable, str(Path(__file__).with_name("wechat_send_worker.py")), str(request)]
        process = None
        resources = ExitStack()
        inspection = job.get('action') == 'window-check'
        def before_process(message):
            return SendResult(False, message, False,
                              {'submission_state': 'not_submitted', 'send_key': False})
        def interrupted(message):
            diagnostic = interrupted_diagnostic(status, inspection=inspection)
            suffix = ('；本次未按发送键，不自动重发' if diagnostic['submission_state'] == 'not_submitted'
                      else '；结果不确定，本次不重发')
            if inspection:
                suffix = '；只读检查失败，未发送消息'
            return SendResult(False, message + suffix, not inspection, diagnostic)
        try:
            with self._lock:
                if epoch != self._send_epoch or (self._stopped and job.get('action') != 'window-check'):
                    return before_process('机器人已停止，未启动发送进程')
            if job.get('as_image'):
                if job.get('background_send'):
                    return before_process('严格后台模式只发送文字，本次未操作剪贴板或发送图片')
                from .picture_clipboard import PictureClipboard
                if len(job.get('paths', [])) != 1:
                    return before_process('每轮最多发送一张图片')
                try:
                    # 其他应用的延迟剪贴板可能阻塞；绝不能持有停止进程所需的锁。
                    clipboard = resources.enter_context(PictureClipboard(job['paths'][0]))
                    job = dict(job, clipboard_sequence=clipboard.sequence)
                    request.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
                except Exception:
                    return before_process('剪贴板暂不可用或无法安全恢复，本轮只保留文字回复')
            with self._lock:
                if epoch != self._send_epoch or (self._stopped and job.get('action') != 'window-check'):
                    return before_process('机器人已停止，未启动发送进程')
                try:
                    process = subprocess.Popen(command, cwd=str(self.data_dir.resolve()),
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except OSError as exc:
                    return before_process(f'无法启动微信发送子进程（{type(exc).__name__}），本次未输入、未发送')
                spawned = time.perf_counter()
                self._process = process
            # 比外层 25 秒闸门略早结束，确保超时后没有继续输入/迟到发送的进程。
            try:
                code = process.wait(timeout=24)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
                # 超时被杀的子进程来不及自己写日志，失败记录只能由父进程补
                if not inspection:
                    log_send_failure('subprocess_timeout', job.get('who'),
                                     len(str(job.get('text') or '')), channel='uia',
                                     hint=f'发送进程超过 24 秒未结束，已强制终止；{phase_detail(status, started)}')
                return interrupted('微信检查进程超时，已终止；' + phase_detail(status, started) if inspection
                                   else '发送进程超时，已终止；' + phase_detail(status, started))
            finally:
                finished = time.perf_counter()
            if code != 0 or not output.exists():
                if not inspection:
                    log_send_failure('subprocess_exit', job.get('who'),
                                     len(str(job.get('text') or '')), channel='uia',
                                     hint=f'发送进程异常退出（0x{code & 0xffffffff:08X}）；{phase_detail(status, started)}')
                return interrupted(f"微信{'检查' if inspection else '发送'}进程异常退出（0x{code & 0xffffffff:08X}）；{phase_detail(status, started)}")
            try:
                result = json.loads(output.read_text(encoding="utf-8"))
                if not isinstance(result, dict):
                    raise ValueError('invalid worker result')
            except (OSError, ValueError):
                return interrupted('微信子进程结果无法读取；' + phase_detail(status, started))
            detail = str(result.get("message") or "发送未完成")
            if not result.get('ok'):
                detail += '；' + phase_detail(status, started)
            diagnostic = dict(result.get('diagnostic') or {})
            if inspection:
                diagnostic.update(submission_state='not_submitted', send_key=False)
            else:
                if 'send_key' in result:
                    diagnostic.setdefault('send_key', result['send_key'])
                diagnostic.setdefault('submission_state', 'confirmed' if result.get('ok') else
                                      'uncertain' if result.get('send_key') is not False else 'not_submitted')
            timings = dict(diagnostic.get('timings') or {})
            total = timings.get('total')
            log_send_timing(round((spawned - tick) * 1000),
                            round((finished - spawned) * 1000),
                            round(total, 3) if isinstance(total, (int, float)) else None)
            return SendResult(bool(result.get("ok")), detail,
                              bool(result.get('requires_reconnect', True)), diagnostic)
        finally:
            try:
                if process is not None and process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    process.wait(timeout=2)
            finally:
                with self._lock:
                    if self._process is process:
                        self._process = None
                try:
                    request.unlink(missing_ok=True)
                    output.unlink(missing_ok=True)
                    status.unlink(missing_ok=True)
                    status.with_suffix('.tmp').unlink(missing_ok=True)
                finally:
                    resources.close()

    def SendMsg(self, text, who, at=None, exact=True):
        return self._run({"action": "text", "who": who, "text": text})

    def inspect_environment(self, who=''):
        return self._run({'action': 'window-check', 'who': who})

    def SendFiles(self, paths, who, exact=True):
        return self._run({"action": "files", "who": who,
                          "paths": [paths] if isinstance(paths, str) else list(paths)})

    def send_image(self, path, who):
        return self._run({"action": "files", "who": who, "paths": [path], "as_image": True})

    def send_images(self, paths, who):
        return self._run({"action": "files", "who": who, "paths": list(paths), "as_image": True})
