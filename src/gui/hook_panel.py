"""候选 Hook 通道设置；所有操作走本机 API，不执行安装或注入脚本。"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout


class HookDialog(QDialog):
    FIELDS = ('api_url', 'account', 'account_path', 'target')

    def __init__(self, call, parent=None):
        super().__init__(parent)
        self.request = call
        self._busy = False
        self._closed = False
        self._loading = False
        self._generation = 0
        self._saved = None
        self._proof = None
        self.setWindowTitle('协议后台 · 候选文字通道')
        self.resize(760, 560)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet('QDialog {background:#F4F6F9;color:#182C43;} QLineEdit {padding:6px;background:white;}')
        layout = QVBoxLayout(self)
        for text in (
            '候选通道仅支持已绑定账号与指定单个会话的被动文字回复；模型、记忆和日志继续保存在鲸鱼娘。',
            '要求精确匹配微信 4.1.10.27，并核验真实账号、进程和发送回执。'
            '原包 DLL 的无鉴权全网监听不符合启用条件，会被拒绝。',
            '当前仅完成候选接入，尚未通过正式通道的真机收发验收；'
            '现有原版 DLL 不能直接使用，不能据此认为微信最小化后台回复已经可用。',
            '本窗口不会下载或安装 DLL、更换微信、修改更新设置、启动机器人或发送测试消息。'
            '保存只登记停用的候选配置；通过只读核验后才可启用。'):
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
        self.environment = QLabel('当前运行环境：正在读取…')
        self.environment.setWordWrap(True)
        self.environment.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.environment)
        form = QFormLayout()
        labels = {'api_url': '本机接口地址', 'account': '机器人稳定 wxid',
                  'account_path': '该账号数据目录', 'target': '唯一回复目标 wxid / 群 ID'}
        self.fields = {}
        for key in self.FIELDS:
            field = QLineEdit()
            field.setObjectName('hook_' + key)
            field.textChanged.connect(self._edited)
            self.fields[key] = field
            setattr(self, key, field)
            form.addRow(labels[key], field)
        self.api_url.setPlaceholderText('http://127.0.0.1:30001')
        self.target.setPlaceholderText('filehelper、wxid_… 或 …@chatroom')
        layout.addLayout(form)
        row = QHBoxLayout()
        self.save_button = QPushButton('保存候选配置')
        self.probe_button = QPushButton('只读核验')
        self.activate_button = QPushButton('启用已核验通道')
        self.deactivate_button = QPushButton('切回本机')
        self.save_button.clicked.connect(self.save)
        self.probe_button.clicked.connect(self.probe)
        self.activate_button.clicked.connect(self.activate)
        self.deactivate_button.clicked.connect(self.deactivate)
        for button in (self.save_button, self.probe_button, self.activate_button, self.deactivate_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel('先保存候选配置，再执行只读核验；启用通道后机器人保持停止。')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        layout.addStretch()
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        layout.addWidget(close)
        self._buttons()
        self._load()

    def collect(self):
        return {key: self.fields[key].text().strip() for key in self.FIELDS}

    def _edited(self, *_):
        if self._loading:
            return
        self._generation += 1
        self._proof = None
        self._buttons()

    def _buttons(self):
        for field in self.fields.values():
            field.setEnabled(not self._busy)
        for button in (self.save_button, self.probe_button, self.deactivate_button):
            button.setEnabled(not self._busy)
        self.activate_button.setEnabled(not self._busy and self._proof is not None
            and self._proof == self.collect() == self._saved)

    @staticmethod
    def _diagnostic(result):
        status = result.get('status')
        status = status if isinstance(status, dict) else {}
        reason = str(status.get('reason') or result.get('message') or result.get('detail') or
                     ('操作完成' if result.get('success') else '操作未完成'))
        code = status.get('waiting_code')
        return reason + ('（' + str(code) + '）' if code else '')

    def _load(self):
        generation = self._generation
        def done(result):
            if self._closed or generation != self._generation:
                return
            if result.get('success') is False:
                self.status.setText(self._diagnostic(result))
                return
            settings = result.get('settings')
            if not isinstance(settings, dict):
                self.status.setText('候选通道配置未返回，请稍后重新打开。')
                return
            self._loading = True
            try:
                for key in self.FIELDS:
                    self.fields[key].setText(str(settings.get(key) or ''))
            finally:
                self._loading = False
            self._saved = self.collect()
            self.environment.setText('当前运行环境：' +
                {'hook': '协议后台', 'sandbox': '隔离后台', 'local': '本机'}.get(result.get('environment'), '本机'))
            self.status.setText(self._diagnostic(result))
            self._buttons()
        self.request(self, 'GET', 'wechat/hook', None, timeout=15, on_done=done)

    def _operate(self, action, notice):
        if self._closed or self._busy:
            return
        if action == 'activate' and not (self._proof == self.collect() == self._saved and self._proof is not None):
            self.status.setText('请先保存当前配置并通过只读核验；尚未启用通道。')
            return
        values = self.collect()
        self._generation += 1
        generation = self._generation
        self._busy = True
        if action != 'activate':
            self._proof = None
        self.status.setText(notice)
        self._buttons()
        def done(result):
            if self._closed:
                return
            self._busy = False
            if generation != self._generation:
                self._buttons()
                return
            self.status.setText(self._diagnostic(result))
            successful = result.get('success') is True
            if action == 'config' and successful:
                self._saved = values
            elif action == 'probe' and successful and isinstance(result.get('status'), dict) and result['status'].get('ready') is True:
                self._proof = values
            elif action == 'activate':
                self._proof = None
                if successful:
                    self.environment.setText('当前运行环境：协议后台；机器人保持停止')
            elif action == 'deactivate' and successful:
                self.environment.setText('当前运行环境：本机；机器人保持停止')
            self._buttons()
        payload = values if action in ('config', 'probe') else {}
        self.request(self, 'POST', 'wechat/hook/' + action, payload,
                     timeout=30 if action == 'probe' else 40, on_done=done)

    def save(self):
        self._operate('config', '正在保存停用的候选配置…')

    def probe(self):
        self._operate('probe', '正在只读核验版本、账号与本机接口…')

    def activate(self):
        self._operate('activate', '正在启用已核验通道，机器人保持停止…')

    def deactivate(self):
        self._operate('deactivate', '正在切回本机通道，机器人保持停止…')

    def done(self, result):
        self._closed = True
        self._generation += 1
        super().done(result)

    def closeEvent(self, event):
        self._closed = True
        self._generation += 1
        super().closeEvent(event)
