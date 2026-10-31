"""自动更新对话框（PySide6；兼容项目现有的 PyQt6 运行环境）。

只依赖 Qt，不导入主窗口或 FastAPI；所有更新动作都交给 UpdateService，
对话框本身不做网络与文件操作，避免阻塞 UI 线程。
"""
from __future__ import annotations

from typing import Any

try:  # 项目当前实际运行在 PyQt6 上，发布目标优先 PySide6。
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar,
                                   QPushButton, QTextEdit, QVBoxLayout)
except ImportError:  # pragma: no cover - 由实际安装的 Qt 绑定决定
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar,
                                 QPushButton, QTextEdit, QVBoxLayout)

_STATE_TEXT = {
    'idle': '尚未检查更新',
    'checking': '正在检查更新…',
    'available': '发现新版本',
    'up_to_date': '已是最新版本',
    'downloading': '正在下载更新…',
    'downloaded': '下载完成，可以安装',
    'installing': '正在启动安装程序…',
    'error': '更新失败',
}


class UpdateDialog(QDialog):
    """控制台内的检查、下载、校验与安装入口。"""

    def __init__(self, parent, service):
        super().__init__(parent)
        self.service = service
        self._poll = QTimer(self)
        self._poll.setInterval(350)
        self._poll.timeout.connect(self._poll_status)
        self._last_action = 'check'
        self._last_status: dict = {}

        self.setWindowTitle('检查更新')
        self.setStyleSheet(
            'QDialog { background:#F4F6F9; color:#18334A; } '
            'QLabel { color:#18334A; } '
            'QTextEdit { background:#FAFCFE; color:#18334A; }'
        )
        self.resize(680, 560)
        self.setMinimumSize(560, 440)

        layout = QVBoxLayout(self)
        self.title_label = QLabel('自动更新')
        self.title_label.setStyleSheet('font-size:18px; font-weight:600;')
        layout.addWidget(self.title_label)

        self.version_label = QLabel('当前版本：—    最新版本：—')
        self.version_label.setWordWrap(True)
        layout.addWidget(self.version_label)

        self.state_label = QLabel('尚未检查更新')
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        notes_title = QLabel('更新说明')
        notes_title.setStyleSheet('font-weight:600;')
        layout.addWidget(notes_title)
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText('这里会显示新版本的更新说明。')
        layout.addWidget(self.notes, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        self.error_label = QLabel('')
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet('color:#C0392B;')
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        self.check_button = QPushButton('检查更新')
        self.download_button = QPushButton('下载')
        self.install_button = QPushButton('安装并重启')
        self.skip_button = QPushButton('跳过此版本')
        self.retry_button = QPushButton('重试')
        self.close_button = QPushButton('关闭')
        for button in (self.check_button, self.download_button, self.install_button,
                       self.skip_button, self.retry_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.check_button.clicked.connect(self.start_check)
        self.download_button.clicked.connect(self.start_download)
        self.install_button.clicked.connect(self.start_install)
        self.skip_button.clicked.connect(self.skip_version)
        self.retry_button.clicked.connect(self.retry)
        self.close_button.clicked.connect(self.reject)
        self.retry_button.hide()

        self.refresh(self._safe_status())

    # ------------------------------------------------------------------ API
    def start_check(self) -> None:
        """供主窗口启动时静默检查，也供「检查更新」按钮使用。"""
        self._last_action = 'check'
        self._clear_error()
        self.state_label.setText('正在检查更新…')
        result = self._call('check', force=True)
        self.refresh(result)
        self._poll.start()

    def refresh(self, status: Any) -> None:
        """按服务状态刷新界面；允许直接传 status 或 {ok,status}。"""
        if not isinstance(status, dict):
            status = self._safe_status()
        if isinstance(status.get('status'), dict):
            status = status['status']
        # 纯失败结果（没有完整状态快照）不要清空界面上的版本/说明；
        # 只把错误留在红字区域，方便用户点“重试”。
        if status.get('ok') is False and 'state' not in status:
            self._show_error(str(status.get('message') or '操作失败，请重试'))
            self.retry_button.setVisible(True)
            return
        self._last_status = status
        current = str(status.get('current') or '—')
        latest = str(status.get('latest') or '—')
        state = str(status.get('state') or 'idle')
        self.version_label.setText('当前版本：%s    最新版本：%s' % (current, latest))
        text = _STATE_TEXT.get(state, state)
        if state == 'up_to_date' and status.get('skipped'):
            text = '已跳过版本 %s' % (status.get('skipped_version') or '')
        self.state_label.setText('状态：%s' % text)

        notes = status.get('notes')
        self.notes.setPlainText(str(notes) if notes else '暂无更新说明。')

        progress = status.get('progress') or {}
        received = int(progress.get('received') or 0)
        total = int(progress.get('total') or 0)
        percent = int(progress.get('percent') or 0)
        if state == 'downloading' and total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, percent)))
            self.progress.setFormat('%s / %s KB（%d%%）' % (
                received // 1024, total // 1024, max(0, min(100, percent))))
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(100 if state in ('downloaded', 'installing') else 0)
            self.progress.setFormat('%d%%' % self.progress.value())

        error = status.get('error')
        failed = status.get('ok') is False
        if isinstance(error, dict) and error.get('message'):
            self._show_error(str(error.get('message')))
        elif state != 'error' and not failed:
            self._clear_error()

        available = state == 'available'
        self.check_button.setEnabled(state not in ('checking', 'downloading', 'installing'))
        self.download_button.setEnabled(available)
        self.install_button.setEnabled(state == 'downloaded')
        self.skip_button.setEnabled(available)
        self.retry_button.setVisible(state == 'error' or failed)
        self.close_button.setEnabled(state != 'installing')

    # -------------------------------------------------------------- actions
    def start_download(self) -> None:
        self._last_action = 'download'
        self._clear_error()
        result = self._call('download')
        self.refresh(result)
        self._poll.start()

    def start_install(self) -> None:
        self._last_action = 'install'
        self._clear_error()
        result = self._call('install')
        self.refresh(result)
        status = result.get('status') if isinstance(result, dict) and isinstance(result.get('status'), dict) else result
        if isinstance(status, dict) and status.get('state') == 'installing':
            self.state_label.setText('安装程序已启动，本程序即将退出…')
            self._poll.stop()
            app = None
            try:
                from PySide6.QtWidgets import QApplication
            except ImportError:
                from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(1200, app.quit)

    def skip_version(self) -> None:
        latest = str(self._last_status.get('latest') or '')
        if not latest:
            self._show_error('当前没有可跳过的版本')
            return
        result = self._call('skip', latest)
        self.refresh(result)

    def retry(self) -> None:
        if self._last_action == 'download':
            self.start_download()
        elif self._last_action == 'install':
            self.start_install()
        else:
            self.start_check()

    # ---------------------------------------------------------------- utils
    def _call(self, name: str, *args, **kwargs) -> dict:
        method = getattr(self.service, name, None)
        if not callable(method):
            return self._failure('internal', '更新服务不可用')
        try:
            result = method(*args, **kwargs)
        except Exception:
            return self._failure('internal', '更新服务发生内部错误，请重试')
        if not isinstance(result, dict):
            return self._failure('internal', '更新服务返回了无法识别的结果')
        if result.get('ok') is False:
            self._show_error(str(result.get('message') or '操作失败，请重试'))
            return result
        if result.get('state') == 'error':
            error = result.get('error') or {}
            self._show_error(str(error.get('message') or '操作失败，请重试'))
        return result

    def _poll_status(self) -> None:
        status = self._safe_status()
        self.refresh(status)
        if str(status.get('state') or '') not in ('checking', 'downloading', 'installing'):
            self._poll.stop()

    def _safe_status(self) -> dict:
        try:
            status = self.service.status()
            return status if isinstance(status, dict) else {}
        except Exception:
            return {'state': 'error', 'current': '—', 'latest': '—',
                    'error': {'reason': 'internal', 'message': '无法读取更新状态'}}

    def _failure(self, reason: str, message: str) -> dict:
        self._show_error(message)
        return {'ok': False, 'reason': reason, 'message': message}

    def _show_error(self, message: str) -> None:
        self.error_label.setText(str(message))
        self.error_label.show()

    def _clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._poll.stop()
        super().closeEvent(event)
