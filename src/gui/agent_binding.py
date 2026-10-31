"""本机绑定与跨端凭据入口；授权值不发给模型，不写聊天日志。"""
import math
import time

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPushButton, QVBoxLayout)

MASK = '********'


def _stamp(value):
    """Unix 秒转本地时间；缺失或非法一律显示 —，不能崩。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return '—'
    try:
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(value))
    except (OSError, OverflowError, ValueError):
        return '—'


def _remaining(value):
    """剩余天数按天向上取整；已过期或缺失给出可执行结论。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return '—'
    left = value - time.time()
    return '已过期' if left <= 0 else str(max(1, math.ceil(left / 86400))) + ' 天'


def credential_line(row):
    """一行摘要：peer · 能力 · 创建 · 剩余 · 最近使用；后端没给的字段显示 —。"""
    caps = row.get('capabilities')
    capabilities = ','.join(str(item) for item in caps) if isinstance(caps, list) else ''
    return ' · '.join([str(row.get('peer') or '—'), capabilities or '—',
                       '创建 ' + _stamp(row.get('issued')), '剩余 ' + _remaining(row.get('expires')),
                       '最近使用 ' + _stamp(row.get('last_used'))])


class AgentBindingDialog(QDialog):
    def __init__(self, call_api, parent=None):
        super().__init__(parent)
        # 主窗口的透明背景样式不适合独立对话框，否则Windows可能把底色绘制成黑色。
        self.setStyleSheet('QDialog { background-color: #F4F6F9; color: #182C43; }')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.call_api = call_api
        self.revision = None
        self._token = ''
        self._revealed = False
        self._peers = []
        self.setWindowTitle('Agent管理员与跨端凭据')
        self.resize(660, 640)
        layout = QVBoxLayout(self)
        hint = QLabel('先绑定一个管理员，Agent才能更改控制台。这里的身份用于归属和审计；'
                      '微信私聊管理员的验证仍沿用机器人管理页。接入凭据只授予勾选的能力，换绑或解绑会撤销全部旧凭据。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        dsh=QPushButton('连接桌面DSH（自动查找进程与端口）')
        def open_dsh():
            from .dsh_panel import DshDialog
            DshDialog(self.call_api,self).exec()
        dsh.clicked.connect(open_dsh);layout.addWidget(dsh)
        note=QLabel('下方凭据供外部Agent调用本控制台。连接DSH生成回复，请使用上方按钮。')
        note.setWordWrap(True);layout.addWidget(note)
        form = QFormLayout()
        self.platform = QComboBox()
        for label, value in [('微信', 'wechat'), ('QQ', 'qq'), ('本机桌面', 'desktop')]:
            self.platform.addItem(label, value)
        self.account, self.subject = QLineEdit(), QLineEdit()
        self.account.setPlaceholderText('机器人稳定账号，例如wxid_xxx或QQ号')
        self.subject.setPlaceholderText('管理员稳定标识，例如wxid_xxx或QQ号，不能填昵称')
        form.addRow('管理员所在平台', self.platform)
        form.addRow('对应机器人账号', self.account)
        form.addRow('管理员用户标识', self.subject)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.bind_button, self.unbind_button = QPushButton('保存管理员绑定'), QPushButton('解除绑定')
        self.bind_button.clicked.connect(self.bind)
        self.unbind_button.clicked.connect(self.unbind)
        row.addWidget(self.bind_button); row.addWidget(self.unbind_button)
        layout.addLayout(row)
        self.status = QLabel('正在读取绑定…')
        self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.repair_button = QPushButton('重置损坏绑定（撤销旧凭据）')
        self.repair_button.clicked.connect(lambda: self.request('POST', 'agent/authority/repair', {}, self.loaded))
        self.repair_button.setVisible(False)
        layout.addWidget(self.repair_button)
        layout.addWidget(QLabel('DSH / QQ / 桌面Agent凭据（默认只读）'))
        self.peer = QLineEdit('dsh')
        self.peer.setPlaceholderText('接入端名称：dsh、qq、desktop等')
        layout.addWidget(self.peer)
        grants = QHBoxLayout()
        self.grants = {}
        for label, value, checked in [('读控制台', 'read_console', True), ('改控制台', 'manage_console', False),
                                      ('读记忆', 'read_memory', True), ('写记忆', 'write_memory', False),
                                      ('安装扩展', 'manage_extensions', False)]:
            checkbox = QCheckBox(label); checkbox.setChecked(checked)
            self.grants[value] = checkbox; grants.addWidget(checkbox)
        layout.addLayout(grants)
        self.issue_button = QPushButton('生成30天接入凭据')
        self.issue_button.clicked.connect(self.issue)
        layout.addWidget(self.issue_button)
        self.secret = QLineEdit()
        self.secret.setReadOnly(True)
        self.secret.setPlaceholderText('凭据只在生成时显示：前6位可见，其余掩码')
        secret_row = QHBoxLayout(); secret_row.addWidget(self.secret)
        self.reveal_button = QPushButton('显示'); self.reveal_button.clicked.connect(self.toggle_reveal)
        self.copy_button = QPushButton('复制'); self.copy_button.clicked.connect(self.copy_secret)
        secret_row.addWidget(self.reveal_button); secret_row.addWidget(self.copy_button)
        layout.addLayout(secret_row)
        self._render_token()
        self.credentials = QListWidget(); layout.addWidget(self.credentials, 1)
        revoke_button = QPushButton('撤销选中的接入凭据')
        revoke_button.clicked.connect(self.revoke); layout.addWidget(revoke_button)
        close = QPushButton('完成'); close.clicked.connect(self.accept); layout.addWidget(close)
        self.refresh()

    def _set_token(self, token):
        """凭据原文只留在这个属性里，任何 QLabel 都不显示它。"""
        self._token = token if isinstance(token, str) else ''
        self._revealed = False
        self.reveal_button.setText('显示')
        self._render_token()

    def _render_token(self):
        if not self._token:
            self.secret.clear()
        else:
            self.secret.setText(self._token if self._revealed else self._token[:6] + MASK)
        self.reveal_button.setEnabled(bool(self._token))
        self.copy_button.setEnabled(bool(self._token))

    def toggle_reveal(self):
        if not self._token:
            return
        self._revealed = not self._revealed
        self.reveal_button.setText('隐藏' if self._revealed else '显示')
        self._render_token()

    def copy_secret(self):
        if not self._token:
            return
        try:
            QGuiApplication.clipboard().setText(self._token)
        except Exception:
            self.status.setText('复制失败：无法访问系统剪贴板，请点「显示」后手动复制。')
            return
        self.status.setText('凭据已复制到剪贴板；不要粘贴到聊天记录或公开文档里。')

    def request(self, method, path, body, callback):
        def completed(data):
            if data.get('success') is False:
                self.status.setText(str(data.get('message') or data.get('detail') or '操作失败'))
                return
            callback(data)
        self.call_api(self, method, path, body, timeout=15, on_done=completed)

    def refresh(self):
        self.request('GET', 'agent/authority', None, self.loaded)

    def loaded(self, data):
        self.revision = data.get('revision')
        self.repair_button.setVisible(bool(data.get('invalid')))
        admin = data.get('admin') or next(iter(data.get('known_admins') or []), {})
        if isinstance(admin, dict) and admin:
            index = self.platform.findData(admin.get('platform'))
            self.platform.setCurrentIndex(max(0, index))
            self.account.setText(str(admin.get('account') or '')); self.subject.setText(str(admin.get('subject') or ''))
        self.status.setText('已绑定；Agent可在权限范围内操作。' if data.get('bound') else
                            data.get('message', '尚未绑定。已有微信私聊管理员可预填；点击保存后才生效。'))
        self.issue_button.setEnabled(bool(data.get('bound')))
        self.unbind_button.setEnabled(bool(data.get('bound')))
        self.credentials.clear()
        self._peers = []
        for row in data.get('credentials') or []:
            if not isinstance(row, dict):
                continue
            self.credentials.addItem(credential_line(row))
            self.credentials.item(self.credentials.count()-1).setData(Qt.ItemDataRole.UserRole, row.get('id'))
            peer = str(row.get('peer') or '')
            if peer and peer not in self._peers:
                self._peers.append(peer)

    def bind(self):
        self._set_token('')
        self.request('POST', 'agent/authority', {'platform': self.platform.currentData(),
            'account': self.account.text().strip(), 'subject': self.subject.text().strip(),
            'revision': self.revision}, self.loaded)

    def unbind(self):
        if self.revision is None:
            return
        self._set_token('')
        self.request('DELETE', 'agent/authority?revision='+str(self.revision), None, self.loaded)

    def issue(self):
        self.request('POST', 'agent/credentials', {'peer': self.peer.text().strip(),
            'capabilities': [key for key, box in self.grants.items() if box.isChecked()]}, self.issued)

    def issued(self, data):
        credential = data.get('credential') if isinstance(data, dict) else None
        self._set_token((credential or {}).get('token') if isinstance(credential, dict) else '')
        self.refresh()

    def revoke(self):
        item = self.credentials.currentItem()
        credential_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not item or credential_id is None:
            self.status.setText('请先在列表里选中要撤销的接入凭据。')
            return
        # 撤销前必须让人看到影响范围，误点一次就是接入端断线。
        answer = QMessageBox.question(self, '撤销接入凭据',
            '撤销后这条凭据立即失效，对应接入端要重新生成凭据才能继续调用本控制台。\n将影响接入端：'
            + ('，'.join(self._peers) or '—'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText('已取消撤销，凭据保持不变。')
            return
        self._set_token('')
        self.request('DELETE', 'agent/credentials/'+str(credential_id), None, lambda data: self.refresh())
