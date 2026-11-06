"""自动更新对话框（PySide6；兼容项目现有的 PyQt6 运行环境）。

只依赖 Qt，不导入主窗口或 FastAPI；所有更新动作都交给 UpdateService，
对话框本身不做网络与文件操作，避免阻塞 UI 线程。
"""
from __future__ import annotations

from typing import Any

try:  # 项目当前实际运行在 PyQt6 上，发布目标优先 PySide6。
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout,
                                   QLabel, QLineEdit, QProgressBar, QPushButton,
                                   QSpinBox, QTextEdit, QToolButton, QVBoxLayout,
                                   QWidget)
except ImportError:  # pragma: no cover - 由实际安装的 Qt 绑定决定
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout,
                                 QLabel, QLineEdit, QProgressBar, QPushButton,
                                 QSpinBox, QTextEdit, QToolButton, QVBoxLayout,
                                 QWidget)

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

        # 高级设置：默认折叠。更新源/间隔是少数人才改的东西，摊开在首屏只会挤掉更新说明。
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText('高级设置')
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setAutoRaise(True)
        self.advanced_toggle.setStyleSheet('QToolButton { color:#18334A; font-weight:600; }')
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_toggle)

        self.advanced_panel = QWidget()
        form = QFormLayout(self.advanced_panel)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.feed_edit = QLineEdit()
        self.feed_edit.setPlaceholderText('更新源地址，留空表示不检查更新')
        self.auto_check_box = QCheckBox('启动后自动检查更新')
        self.auto_download_box = QCheckBox('发现新版本后自动下载')
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 8760)  # 1 小时 ~ 一年
        self.interval_spin.setSuffix(' 小时')
        self.save_settings_button = QPushButton('保存设置')
        self.save_settings_button.clicked.connect(self.save_settings)
        form.addRow('更新源', self.feed_edit)
        form.addRow('自动检查', self.auto_check_box)
        form.addRow('自动下载', self.auto_download_box)
        form.addRow('检查间隔', self.interval_spin)
        form.addRow('', self.save_settings_button)
        # 用户动过就进入草稿态，350ms 的状态轮询不会把他的输入改回去
        self._settings_dirty = False
        self._syncing = False
        self.feed_edit.textEdited.connect(self._mark_settings_dirty)
        self.auto_check_box.toggled.connect(self._mark_settings_dirty)
        self.auto_download_box.toggled.connect(self._mark_settings_dirty)
        self.interval_spin.valueChanged.connect(self._mark_settings_dirty)
        self.advanced_panel.hide()
        layout.addWidget(self.advanced_panel)

        # 保存成功的提示：压在红字错误区上方，成功与失败占同一块视觉位置
        self.settings_hint = QLabel('')
        self.settings_hint.setStyleSheet('color:#1E8449;')
        self.settings_hint.hide()
        layout.addWidget(self.settings_hint)

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
        self._sync_settings(status)

    # ------------------------------------------------------- 高级设置
    def _toggle_advanced(self, expanded: bool) -> None:
        self.advanced_panel.setVisible(bool(expanded))
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def _mark_settings_dirty(self, *_args) -> None:
        """控件被用户动过：此后状态轮询不再覆盖输入。"""
        if not self._syncing:
            self._settings_dirty = True

    @staticmethod
    def _interval_from_status(status: dict) -> int:
        """status() 没有 interval_hours 键，只能用上次/下次检查时间反推。"""
        try:
            last = status.get('last_check_at')
            nxt = status.get('next_check_at')
            if last is None or nxt is None:
                return 24
            hours = int(round((float(nxt) - float(last)) / 3600.0))
        except (TypeError, ValueError):
            return 24
        return max(1, min(8760, hours)) if hours > 0 else 24

    def _sync_settings(self, status: dict) -> None:
        """把服务端状态写进四个控件；用户正在编辑时（草稿态）一律不动。"""
        if self._settings_dirty and self.advanced_toggle.isChecked():
            return
        if 'auto' not in status and 'feed_url' not in status:
            return  # 不是完整状态快照（例如只有 {'ok': True}），别拿空值覆盖用户输入
        self._syncing = True
        try:
            self.feed_edit.setText(str(status.get('feed_url') or ''))
            self.auto_check_box.setChecked(bool(status.get('auto', True)))
            self.auto_download_box.setChecked(bool(status.get('auto_download', True)))
            self.interval_spin.setValue(self._interval_from_status(status))
        finally:
            self._syncing = False

    def save_settings(self) -> None:
        """保存高级设置；成功给绿字提示，失败沿用红字错误区。"""
        self._clear_error()
        self.settings_hint.hide()
        method = getattr(self.service, 'configure', None)
        if not callable(method):
            self._show_error('当前更新服务不支持修改设置')
            return
        try:
            result = method(feed_url=self.feed_edit.text().strip(),
                            auto_check=self.auto_check_box.isChecked(),
                            auto_download=self.auto_download_box.isChecked(),
                            interval_hours=self.interval_spin.value())
        except Exception:
            self._show_error('保存设置失败，请重试')
            return
        if isinstance(result, dict) and result.get('ok') is False:
            self._show_error(str(result.get('message') or '保存设置失败，请重试'))
            return
        self._settings_dirty = False
        self.settings_hint.setText('设置已保存')
        self.settings_hint.show()
        # 只有真的拿到状态快照才回刷界面，否则空结果会把版本/说明清成占位符
        if isinstance(result, dict) and ('state' in result or isinstance(result.get('status'), dict)):
            self.refresh(result)

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
