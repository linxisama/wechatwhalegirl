"""控制台里的「维护类」对话框：检查更新、Windows 沙盒、DSH 一键连接。

为什么单独一个文件：主窗口 wechat_gui.py 已经一万多行，新功能都按「对话框模块 +
注入 service」的方式写，既方便单测替换，也不用动主窗口的布局逻辑。

service 由 service_adapters 提供（走 HTTP 访问本机后端），对话框本身不做网络请求、
不碰文件系统，只负责显示与把按钮点击转成 service 调用。
"""
from __future__ import annotations

import threading

try:  # 本项目正式前端是 PyQt6
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget, QPushButton,
                                 QTextEdit, QVBoxLayout)
except ImportError:  # pragma: no cover - 兼容 PySide6 环境
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget, QPushButton,
                                   QTextEdit, QVBoxLayout)

_DIALOG_STYLE = ('QDialog { background:#F4F6F9; color:#18334A; } '
                 'QLabel { color:#18334A; } '
                 'QTextEdit { background:#FAFCFE; color:#18334A; }')

_DSH_STATE = {
    'ready': '已就绪：新建 DSH 会话即可使用 whale_wechat 工具',
    'error': '需要人工确认：接入脚本被本地改过，控制台不会覆盖',
    'incomplete': '还没就绪：点「一键修复连接」补齐缺失步骤',
}
def open_update_dialog(parent, get, post):
    """打开「检查更新」对话框；导入失败时把原因抛给调用方显示。"""
    from src.gui.update_dialog import UpdateDialog
    from src.gui.service_adapters import UpdateHttpService

    dialog = UpdateDialog(parent, UpdateHttpService(get, post))
    dialog.exec()
    return dialog
class SandboxProvisionDialog(QDialog):
    """包一层沙盒面板，做成独立窗口，避免把控制台主界面挤变形。"""

    def __init__(self, parent, get, post):
        super().__init__(parent)
        from src.gui.sandbox_provision_panel import SandboxProvisionPanel
        from src.gui.service_adapters import SandboxProvisionHttpService

        self.setWindowTitle('Windows 沙盒 · 启用与隔离通道')
        self.setStyleSheet(_DIALOG_STYLE)
        self.resize(660, 520)
        layout = QVBoxLayout(self)
        self.panel = SandboxProvisionPanel(SandboxProvisionHttpService(get, post), self)
        layout.addWidget(self.panel, 1)
class DshConnectDialog(QDialog):
    """DSH 零配置连接：看清状态、一键修复、开关守护线程。"""

    def __init__(self, parent, get, post):
        super().__init__(parent)
        from src.gui.service_adapters import DshConnectHttpService

        self.service = DshConnectHttpService(get, post)
        self._busy = False
        self.setWindowTitle('DSH 一键连接')
        self.setStyleSheet(_DIALOG_STYLE)
        self.resize(680, 520)

        layout = QVBoxLayout(self)
        title = QLabel('DSH 一键连接')
        title.setStyleSheet('font-size: 17px; font-weight: 600;')
        layout.addWidget(title)

        hint = QLabel('把「接入脚本 + DSH 配置 + 新会话默认可见」三步一次做完；'
                      '重复点不会重复写入，控制台也不会覆盖你手改过的接入脚本。')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.state_label = QLabel('正在读取连接状态…')
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet('font-weight: 600;')
        layout.addWidget(self.state_label)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText('这里显示连接器版本、DSH 配置状态与可执行动作。')
        layout.addWidget(self.detail, 1)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton('刷新状态')
        self.btn_refresh.clicked.connect(self.refresh)
        row.addWidget(self.btn_refresh)
        self.btn_repair = QPushButton('一键修复连接')
        self.btn_repair.clicked.connect(self.repair)
        row.addWidget(self.btn_repair)
        self.btn_watch_on = QPushButton('开启自动重连')
        self.btn_watch_on.setToolTip('端口变化、DSH 重启后自动重新发现并写回配置；不需要时随时关掉')
        self.btn_watch_on.clicked.connect(lambda: self.toggle_watch(True))
        row.addWidget(self.btn_watch_on)
        self.btn_watch_off = QPushButton('关闭自动重连')
        self.btn_watch_off.clicked.connect(lambda: self.toggle_watch(False))
        row.addWidget(self.btn_watch_off)
        row.addStretch(1)
        close_button = QPushButton('关闭')
        close_button.clicked.connect(self.accept)
        row.addWidget(close_button)
        layout.addLayout(row)

        QTimer.singleShot(0, self.refresh)

    # --- 展示 -------------------------------------------------------------
    def _render(self, status: dict) -> None:
        state = str(status.get('state') or 'unknown')
        text = _DSH_STATE.get(state, '未知状态：' + state)
        if status.get('message'):
            text += '\n' + str(status.get('message'))
        self.state_label.setText(text)
        lines = []
        if status.get('base_url'):
            lines.append('控制台地址：' + str(status.get('base_url')))
        issues = status.get('issues') or []
        if isinstance(issues, (list, tuple)) and issues:
            lines.append('待处理项：' + '、'.join(str(item) for item in issues))
        if status.get('hint'):
            lines.append('建议：' + str(status.get('hint')))
        if status.get('instructions'):
            lines.append('')
            lines.append(str(status.get('instructions')))
        self.detail.setPlainText('\n'.join(lines) or '（没有更多细节）')

    def _fail(self, result) -> None:
        message = ''
        if isinstance(result, dict):
            message = str(result.get('message') or result.get('reason') or '')
        self.state_label.setText('操作失败：' + (message or '请稍后重试'))

    def _run(self, func, on_done) -> None:
        """所有 service 调用都放到后台线程，避免请求超时把界面卡住。"""
        if self._busy:
            return
        self._busy = True
        self.btn_refresh.setEnabled(False)
        self.btn_repair.setEnabled(False)

        def worker():
            try:
                result = func()
            except Exception as error:  # 适配层理论上不抛，这里兜底
                result = {'ok': False, 'message': type(error).__name__}
            self._busy = False
            try:
                on_done(result)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # --- 动作 -------------------------------------------------------------
    def refresh(self):
        def done(result):
            self.btn_refresh.setEnabled(True)
            self.btn_repair.setEnabled(True)
            if isinstance(result, dict) and result.get('ok', True) and result.get('state') != 'error':
                self._render(result)
            else:
                self._fail(result)

        self._run(self.service.status, done)

    def repair(self):
        self.state_label.setText('正在修复连接（写入配置约需几秒）…')

        def done(result):
            self.btn_refresh.setEnabled(True)
            self.btn_repair.setEnabled(True)
            if isinstance(result, dict) and result.get('ok'):
                self.state_label.setText('修复完成：' + str(result.get('message') or '配置已写入'))
            elif isinstance(result, dict) and result.get('message'):
                self.state_label.setText('修复未完成：' + str(result.get('message')))
            else:
                self._fail(result)
            self.refresh()

        self._run(self.service.repair, done)

    def toggle_watch(self, enabled: bool):
        def done(result):
            if isinstance(result, dict) and result.get('ok') is not False:
                self.state_label.setText('自动重连已' + ('开启' if enabled else '关闭'))
            else:
                self._fail(result)
            self.refresh()

        self._run(lambda: self.service.watch(enabled), done)
class PluginPacksDialog(QDialog):
    """插件包管理：看清单、装/卸、启用/禁用。

    设计原则（跟主程序一致）：**不自动安装任何包**，只列出发现了什么、缺什么；
    用户手动选一个文件夹或 zip 才装。坏包只显示原因，绝不让界面崩。
    """

    _STATE_TEXT = {'installed': '已安装', 'not_installed': '未安装', 'invalid': '清单有问题',
                   'incompatible': '版本不兼容', 'error': '加载失败', 'builtin': '已内置',
                   'shadowed': '与已有插件同名，未生效'}

    def __init__(self, parent, get, post, delete):
        super().__init__(parent)
        from src.gui.service_adapters import PluginPacksHttpService

        self.service = PluginPacksHttpService(get, post, delete)
        self._packs = []
        self._busy = False
        self.setWindowTitle('插件包')
        self.setStyleSheet(_DIALOG_STYLE)
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        title = QLabel('插件包')
        title.setStyleSheet('font-size: 17px; font-weight: 600;')
        layout.addWidget(title)

        hint = QLabel('插件包是「一个文件夹 = 一个插件」：里面有 plugin.json 清单和 plugin.py。'
                      '控制台只加载你自己放进来的包，不会自动下载或安装。'
                      '升级控制台不会动这些包；如果升级后不见了，把文件夹放回 plugin-packs 目录再点「重新扫描」。')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.summary = QLabel('正在读取插件包…')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.listbox = QListWidget()
        self.listbox.setMinimumHeight(220)
        self.listbox.currentRowChanged.connect(self._selection_changed)
        layout.addWidget(self.listbox, 1)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(120)
        self.detail.setPlaceholderText('选中一个插件包可以看它的说明和问题原因。')
        layout.addWidget(self.detail)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton('重新扫描')
        self.btn_refresh.clicked.connect(self.refresh)
        row.addWidget(self.btn_refresh)
        self.btn_install = QPushButton('安装插件包…')
        self.btn_install.setToolTip('选一个插件包文件夹，或选打包好的 zip')
        self.btn_install.clicked.connect(self.install)
        row.addWidget(self.btn_install)
        self.btn_toggle = QPushButton('启用/停用')
        self.btn_toggle.setToolTip('只对已安装的插件包生效；停用后不参与任何消息处理')
        self.btn_toggle.clicked.connect(self.toggle)
        row.addWidget(self.btn_toggle)
        self.btn_remove = QPushButton('移除')
        self.btn_remove.setToolTip('从本机删除这个插件包（需要二次确认）')
        self.btn_remove.clicked.connect(self.remove)
        row.addWidget(self.btn_remove)
        row.addStretch(1)
        close_button = QPushButton('关闭')
        close_button.clicked.connect(self.accept)
        row.addWidget(close_button)
        layout.addLayout(row)

        self._sync_buttons()
        QTimer.singleShot(0, self.refresh)

    # --- 展示 -------------------------------------------------------------
    def _current(self):
        row = self.listbox.currentRow()
        if 0 <= row < len(self._packs):
            return self._packs[row]
        return {}

    def _sync_buttons(self):
        pack = self._current()
        installed = bool(pack.get('installed'))
        self.btn_toggle.setEnabled(bool(installed) and not self._busy)
        self.btn_remove.setEnabled(bool(installed) and not self._busy)
        self.btn_install.setEnabled(not self._busy)
        self.btn_refresh.setEnabled(not self._busy)

    def _selection_changed(self, _row):
        pack = self._current()
        self._sync_buttons()
        if not pack:
            return
        lines = ['标识：' + str(pack.get('id') or '—'),
                 '名称：' + str(pack.get('name') or '—'),
                 '版本：' + str(pack.get('version') or '—'),
                 '状态：' + self._STATE_TEXT.get(str(pack.get('status')), str(pack.get('status') or '—'))]
        if pack.get('path'):
            lines.append('位置：' + str(pack.get('path')))
        if pack.get('message'):
            lines.append('说明：' + str(pack.get('message')))
        for error in (pack.get('errors') or [])[:5]:
            lines.append('问题：' + str(error))
        self.detail.setPlainText('\n'.join(lines))

    def _apply(self, result):
        self._busy = False
        if isinstance(result, dict) and result.get('ok'):
            packs = result.get('packs')
            if isinstance(packs, list):
                self._packs = packs
                self.listbox.clear()
                for pack in packs:
                    state = self._STATE_TEXT.get(str(pack.get('status')), str(pack.get('status') or ''))
                    flag = '' if pack.get('enabled', True) else '（已停用）'
                    self.listbox.addItem(f"{pack.get('name') or pack.get('id')} · v{pack.get('version') or '?'} · {state}{flag}")
            self.summary.setText(str(result.get('message') or '已刷新'))
        else:
            message = ''
            if isinstance(result, dict):
                message = str(result.get('message') or result.get('reason') or '')
            self.summary.setText('操作失败：' + (message or '请稍后重试'))
        self._sync_buttons()

    def _run(self, func, on_done=None):
        if self._busy:
            return
        self._busy = True
        self._sync_buttons()

        def worker():
            try:
                result = func()
            except Exception as error:
                result = {'ok': False, 'message': type(error).__name__}
            self._busy = False
            try:
                (on_done or self._apply)(result)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # --- 动作 -------------------------------------------------------------
    def refresh(self):
        self._run(self.service.list_packs)

    def install(self):
        try:
            from PyQt6.QtWidgets import QFileDialog
        except ImportError:  # pragma: no cover
            from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, '选择插件包文件夹（或取消后改选 zip）')
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, '选择插件包 zip', '', '插件包 (*.zip)')
        if not path:
            return
        self.summary.setText('正在安装…')
        self._run(lambda: self.service.install(path))

    def toggle(self):
        pack = self._current()
        if not pack or not pack.get('installed'):
            return
        target = not bool(pack.get('enabled', True))
        self._run(lambda: self.service.set_enabled(str(pack.get('id')), target))

    def remove(self):
        pack = self._current()
        if not pack or not pack.get('installed'):
            return
        try:
            from PyQt6.QtWidgets import QMessageBox
        except ImportError:  # pragma: no cover
            from PySide6.QtWidgets import QMessageBox
        name = pack.get('name') or pack.get('id')
        if QMessageBox.question(self, '移除插件包',
                                f'确定要从本机删除「{name}」吗？删掉后相关功能会立刻失效。') != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: self.service.remove(str(pack.get('id'))))

__all__ = ['open_update_dialog', 'SandboxProvisionDialog', 'DshConnectDialog', 'PluginPacksDialog']
