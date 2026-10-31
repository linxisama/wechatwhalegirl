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
                 'unsupported': '不支持', 'unknown': '状态未知', 'failed': '启用失败',
                 'pending_reboot': '等待重启', 'elevation_started': '等待管理员授权'}
_INITIAL_NOTICE = '正在读取 Windows 沙盒状态…'


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
        return value

    def _sync_buttons(self, step):
        ready = not self._busy
        self.enable_button.setEnabled(ready and step == 'enable')
        self.recheck_button.setEnabled(ready and step == 'reboot')
        self.channel_button.setEnabled(ready and step == 'create_channel')
        # “查看禁用方法”只是读一段文本，任何时候都能点。
        self.rollback_button.setEnabled(ready)
        return ready

    def append_log(self, text):
        stamp = time.strftime('%H:%M:%S')
        for line in str(text or '').splitlines() or ['']:
            self.log_view.appendPlainText('[' + stamp + '] ' + line)

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
