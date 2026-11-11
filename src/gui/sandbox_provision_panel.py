"""Windows 沙盒启用面板：状态行 + 原因/建议 + 四个按钮 + 日志区。

面板只认构造时注入的 service（status/enable/check/create_channel/rollback_text），
不 import 主窗口、不认识 FastAPI；慢动作（dism、提权、创建通道）都走后台线程，
界面只负责显示，按钮按 next_step 自动启用/禁用，避免用户在错误阶段点错。
"""
from __future__ import annotations

import threading
import time

try:  # 本项目正式前端是 PyQt6
    from PyQt6 import QtCore, QtWidgets
except ImportError:  # 兼容 PySide6 环境
    from PySide6 import QtCore, QtWidgets

_SIGNAL = getattr(QtCore, 'pyqtSignal', None) or QtCore.Signal
_ALIGN_LEFT = QtCore.Qt.AlignmentFlag.AlignLeft if hasattr(QtCore.Qt, 'AlignmentFlag') else QtCore.Qt.AlignLeft

_STATE_TITLES = {'enabled': '已启用', 'disabled': '未启用', 'absent': '组件缺失',
                 'no_cli': '已启用，但缺少 wsb 命令行（Windows 10）',
                 'broken': '容器管理器服务损坏（Windows 沙盒起不来）',
                 'unsupported': '不支持', 'unknown': '状态未知', 'failed': '启用失败',
                 'pending_reboot': '等待重启', 'elevation_started': '等待管理员授权'}
_INITIAL_NOTICE = '正在读取 Windows 沙盒状态…'
# 等授权/核对结果时自动复查：dism 跑在提权进程里，面板只能靠读日志知道它结束了。
_POLL_INTERVAL_MS = 2500
_POLL_WAIT_TICKS = 48  # 还没看到提权日志：48 × 2.5 秒 ≈ 2 分钟（多半是 UAC 没点是）
_POLL_RUN_TICKS = 120  # 日志已经在长：dism 真在跑，给到 5 分钟
_POLL_STATES = ('elevation_started', 'pending')
# check() 的结论不带 next_step，这里补上：成功→去建通道，失败→允许立刻重试。
_VERDICT_STEPS = {'enabled': 'create_channel', 'failed': 'enable'}


class _Bridge(QtCore.QObject):
    """后台线程把结果送回界面线程；跨线程 emit 会走队列连接，不直接碰控件。"""

    done = _SIGNAL(str, object)


class SandboxProvisionPanel(QtWidgets.QWidget):
    """一个自包含面板：宿主只需要 new 出来塞进布局，传一个 service。"""

    def __init__(self, service, parent=None, autostart=True):
        super().__init__(parent)
        self.service = service
        self._busy = False
        self._next_step = 'none'
        self._last_status = {}
        self._poll_ticks = 0
        self._poll_inflight = False
        self._poll_state = ''
        self._poll_seen_log = False
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._finished)
        self.setObjectName('SandboxProvisionPanel')

        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel('Windows 沙盒')
        title.setStyleSheet('font-weight:600;font-size:14px;')
        layout.addWidget(title)

        self.status_label = QtWidgets.QLabel(_INITIAL_NOTICE)
        self.status_label.setObjectName('SandboxProvisionStatus')
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(_ALIGN_LEFT and QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)

        self.detail_label = QtWidgets.QLabel('')
        self.detail_label.setObjectName('SandboxProvisionDetail')
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.detail_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail_label)

        row = QtWidgets.QHBoxLayout()
        self.enable_button = QtWidgets.QPushButton('启用 Windows 沙盒')
        self.recheck_button = QtWidgets.QPushButton('我已重启，重新检测')
        self.channel_button = QtWidgets.QPushButton('创建隔离通道')
        self.rollback_button = QtWidgets.QPushButton('查看禁用方法')
        for button in (self.enable_button, self.recheck_button, self.channel_button,
                       self.rollback_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.enable_button.clicked.connect(self.enable)
        self.recheck_button.clicked.connect(self.recheck)
        self.channel_button.clicked.connect(self.create_channel)
        self.rollback_button.clicked.connect(self.show_rollback)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setObjectName('SandboxProvisionLog')
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(140)
        self.log_view.setMaximumBlockCount(600)
        layout.addWidget(self.log_view, 1)

        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_tick)

        self._sync_buttons('none')
        if autostart:
            QtCore.QTimer.singleShot(0, self.refresh)

    # ── 对外动作 ──────────────────────────────────────────────────────────
    def refresh(self):
        self.append_log('读取 Windows 沙盒状态…')
        return self._run('status', self.service.status)

    def enable(self):
        self.append_log('请求启用 Windows 沙盒（可能弹出管理员授权窗口）…')
        return self._run('enable', self.service.enable)

    def recheck(self):
        self.append_log('重新核对启用结果并刷新状态…')
        def call():
            checked = self.service.check()
            return checked if isinstance(checked, dict) else {'ok': False, 'message': '核对结果异常'}
        return self._run('check', call)

    def create_channel(self):
        self.append_log('正在创建隔离通道…')
        return self._run('channel', self.service.create_channel)

    def show_rollback(self):
        """禁用方法是一段固定文本，直接同步取；取失败也只往日志写一行中文。"""
        try:
            text = self.service.rollback_text()
        except Exception as error:
            text = '读取禁用方法失败：' + type(error).__name__
        self.append_log(text if isinstance(text, str) else str(text))

    # ── 状态渲染 ──────────────────────────────────────────────────────────
    def apply_status(self, status):
        """外部（测试或宿主）也可以直接把一份 status 字典喂进来渲染。"""
        value = status if isinstance(status, dict) else {}
        self._last_status = dict(value)
        state = str(value.get('state') or 'unknown')
        step = str(value.get('next_step') or 'none')
        self._next_step = step
        title = _STATE_TITLES.get(state, state)
        edition = str(value.get('edition') or '')
        line = '当前状态：' + title + ('（' + edition + '）' if edition else '')
        if value.get('needs_reboot'):
            line += ' · 需要重启'
        self.status_label.setText(line)
        detail = [str(value.get('message') or '')]
        hint = str(value.get('hint') or '')
        if hint:
            detail.append('下一步：' + hint)
        self.detail_label.setText(chr(10).join(part for part in detail if part))
        self._sync_buttons(step)
        self._sync_poll(state)
        return value

    def _sync_buttons(self, step):
        ready = not self._busy
        self.enable_button.setEnabled(ready and step == 'enable')
        # “我已重启，重新检测”只是只读探测：状态再怎么怪，用户都要有这一个出口，永远可点。
        self.recheck_button.setEnabled(ready)
        self.channel_button.setEnabled(ready and step in ('create_channel', 'launch'))
        # “查看禁用方法”只是读一段文本，任何时候都能点。
        self.rollback_button.setEnabled(ready)
        return ready

    def append_log(self, text):
        stamp = time.strftime('%H:%M:%S')
        for line in str(text or '').splitlines() or ['']:
            self.log_view.appendPlainText('[' + stamp + '] ' + line)

    # ── 自动核对 ──────────────────────────────────────────────────────────
    def _sync_poll(self, state):
        """等授权/核对结果时开表；出现结论或不用等就停表。"""
        if state in _POLL_STATES:
            self._start_poll()
        else:
            self._stop_poll()

    def _start_poll(self):
        if self._poll_timer.isActive():
            return
        self._poll_ticks = 0
        self._poll_inflight = False
        self._poll_state = ''
        self._poll_seen_log = False
        self._poll_timer.start()
        self.append_log('等待管理员授权结果，每 2.5 秒自动核对一次…')

    def _stop_poll(self):
        if self._poll_timer.isActive():
            self._poll_timer.stop()
        self._poll_inflight = False

    def _poll_limit(self):
        """还没看到提权日志（多半是 UAC 没点是）只等 2 分钟；dism 真在跑就给 5 分钟。"""
        return _POLL_RUN_TICKS if self._poll_seen_log else _POLL_WAIT_TICKS

    def _poll_tick(self):
        """一次 tick 只读提权日志；不走 _run，免得用户自己的操作被轮询挡住。"""
        if self._busy or self._poll_inflight:
            return
        self._poll_ticks += 1
        if self._poll_ticks > self._poll_limit():
            self._stop_poll()
            self.append_log('自动核对等得够久了，先暂停；还没结果就点「我已重启，重新检测」再试一次。')
            return
        self._poll_inflight = True
        worker = threading.Thread(target=self._work, args=('poll', self.service.check), daemon=True)
        worker.start()

    def _poll_done(self, result):
        """pending 静默等下一轮；拿到结论才写日志、渲染并停表。"""
        self._poll_inflight = False
        value = self._unwrap(result)
        state = str(value.get('state') or '')
        if state == 'pending':
            if str(value.get('reason') or '') in ('no_result', 'log_unreadable'):
                self._poll_seen_log = True
            return
        if not state:
            message = str(value.get('message') or '')
            if message and message != self._poll_state:
                self._poll_state = message
                self.append_log(message)
            return
        self._stop_poll()
        if state != self._poll_state:
            self._poll_state = state
            self.append_log(str(value.get('message') or '自动核对结束'))
        merged = dict(value)
        if not merged.get('next_step'):
            merged['next_step'] = _VERDICT_STEPS.get(state, 'none')
        self.apply_status(merged)

    # ── 后台线程 ──────────────────────────────────────────────────────────
    def _run(self, name, call):
        if self._busy:
            self.append_log('上一个操作还没结束，请稍候')
            return False
        self._busy = True
        self._sync_buttons(self._next_step)
        worker = threading.Thread(target=self._work, args=(name, call), daemon=True)
        worker.start()
        return True

    def _work(self, name, call):
        try:
            result = call()
        except Exception as error:
            result = {'ok': False, 'reason': 'internal',
                      'message': type(error).__name__ + ': ' + str(error)}
        try:
            self._bridge.done.emit(name, result)
        except RuntimeError:
            pass  # 面板已经销毁，没人收结果了

    @staticmethod
    def _unwrap(result):
        if isinstance(result, dict):
            inner = result.get('status')
            if isinstance(inner, dict):
                merged = dict(inner)
                for key in ('ok', 'reason', 'message'):
                    if key in result and key not in merged:
                        merged[key] = result[key]
                return merged
            return dict(result)
        return {}

    def _finished(self, name, result):
        if name == 'poll':
            self._poll_done(result)
            return
        self._busy = False
        value = self._unwrap(result)
        if name == 'rollback':
            self.append_log(str(value.get('text') or ''))
            self._sync_buttons(self._next_step)
            return
        if name == 'channel':
            self.append_log(str(value.get('message') or '隔离通道操作结束'))
            self._sync_buttons(self._next_step)
            QtCore.QTimer.singleShot(0, self.refresh)
            return
        if name == 'check':
            self.append_log(str(value.get('message') or '核对结束'))
            QtCore.QTimer.singleShot(0, self.refresh)
            return
        if value.get('ok') is False and value.get('state') not in _STATE_TITLES:
            self.append_log(str(value.get('message') or '操作失败'))
            self._sync_buttons(self._next_step)
            return
        self.append_log(str(value.get('message') or '状态已刷新'))
        self.apply_status(value)


__all__ = ['SandboxProvisionPanel']
