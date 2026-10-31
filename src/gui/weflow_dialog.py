"""独立的只读数据工具，打开窗口不会启动微信或替换机器人监听。"""
import json
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout)


class WeFlowDialog(QDialog):
    def __init__(self, parent, request):
        super().__init__(parent)
        self.request = request
        self.busy = False
        self.bundle = None
        self.account=''
        self.setWindowTitle('WeFlow · 本机只读数据检查')
        # 主窗口允许透明背景；独立工具窗必须自己铺底，否则提示文字会落在透明黑底上。
        self.setStyleSheet('QDialog { background:#F4F6F9; color:#18334A; } '
                          'QLabel { color:#18334A; }')
        self.resize(850, 650)
        self.setMinimumSize(700, 480)
        layout = QVBoxLayout(self)
        hint = QLabel('先在 WeFlow 的设置中开启 API 服务并配置 Token，再检查连接、读取会话。'
                      '\n预览100条；可将选定会话最近1000条文字历史导入本地学习库，不发送历史消息。'
                      '\nwcdb_api.dll 应配置在 WeFlow 的数据库第三方组件路径，并具备配套 WCDB.dll；DLL本身不是HTTP服务。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.url = QLineEdit('http://127.0.0.1:5031')
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText('WeFlow API Token；留空沿用已保存值')
        form.addRow('本机 API 地址', self.url)
        form.addRow('API Token', self.token)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.actions = []
        for label, action in [('保存连接配置', self.save), ('清除已存 Token', self.clear_token),
                              ('只读检查', self.probe), ('读取会话', self.sessions)]:
            button = QPushButton(label)
            button.clicked.connect(action)
            row.addWidget(button)
            self.actions.append(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.rooms = QComboBox()
        row.addWidget(self.rooms, 1)
        self.preview_button = QPushButton('预览选中会话')
        self.preview_button.clicked.connect(self.preview)
        row.addWidget(self.preview_button)
        self.actions.append(self.preview_button)
        self.import_button=QPushButton('导入最近1000条到记忆库')
        self.import_button.clicked.connect(self.import_history)
        row.addWidget(self.import_button);self.actions.append(self.import_button)
        layout.addLayout(row)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setStyleSheet('background:#FAFCFE;color:#18334A;')
        layout.addWidget(self.text, 1)
        self.notice = QLabel('未检查；数据库可读不等于微信已登录，也不等于能发送消息。')
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        row = QHBoxLayout()
        self.export_button = QPushButton('导出本次预览 JSON')
        self.export_button.clicked.connect(self.export)
        self.export_button.setEnabled(False)
        row.addWidget(self.export_button)
        close = QPushButton('关闭')
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self.rooms.currentIndexChanged.connect(self.invalidate)
        self.url.textChanged.connect(self.connection_changed)
        self.token.textChanged.connect(self.connection_changed)
        self.call('GET', 'weflow/config', done=self.loaded_config)

    def invalidate(self, *_):
        self.bundle = None
        self.export_button.setEnabled(False)
        self.text.clear()

    def connection_changed(self, *_):
        self.rooms.clear()
        self.invalidate()

    def draft(self):
        return {'url': self.url.text().strip(), 'token': self.token.text().strip(),
                'talker': self.rooms.currentData() or ''}

    def call(self, method, path, body=None, done=None):
        if self.busy:
            return
        self.busy = True
        for widget in (*self.actions, self.url, self.token, self.rooms):
            widget.setEnabled(False)
        self.export_button.setEnabled(False)
        self.notice.setText('正在读取本机服务…（不会操作微信窗口）')

        def finished(result):
            self.busy = False
            for widget in (*self.actions, self.url, self.token, self.rooms):
                widget.setEnabled(True)
            self.export_button.setEnabled(self.bundle is not None)
            if not result.get('success'):
                self.notice.setText(str(result.get('message') or '请求失败，请检查本机后台'))
                return
            if done:
                done(result)
        self.request(self, method, path, body, timeout=25, on_done=finished)

    def loaded_config(self, result):
        self.account=result.get('account','')
        self.url.setText(result.get('url') or 'http://127.0.0.1:5031')
        self.token.clear()
        self.notice.setText('Token 已保存；输入框留空可沿用。' if result.get('token_set') else '请填写 WeFlow API Token。')

    def save(self):
        self.call('POST', 'weflow/config', self.draft(), self.loaded_config)

    def import_history(self):
        if not self.rooms.currentData():
            self.notice.setText('请先读取并选择会话。');return
        self.call('POST','weflow/import-history',dict(self.draft(),account=self.account),
                  lambda result:self.notice.setText(result['message']))

    def clear_token(self):
        self.call('POST', 'weflow/config', {**self.draft(), 'token': '', 'clear_token': True}, self.loaded_config)

    def probe(self):
        self.call('POST', 'weflow/probe', self.draft(), lambda result:self.notice.setText(result['message']))

    def sessions(self):
        self.invalidate()

        def done(result):
            self.rooms.clear()
            for room in result.get('sessions', []):
                self.rooms.addItem(str(room.get('displayName') or room['username']) + ' · ' + room['username'], room['username'])
            self.notice.setText(f'读取 {self.rooms.count()} 个会话（最多 100 个）；请选择后预览。')
        self.call('POST', 'weflow/sessions', self.draft(), done)

    def preview(self):
        if not self.rooms.currentData():
            self.notice.setText('请先读取并选择会话。')
            return
        self.invalidate()

        def done(result):
            self.bundle = result
            lines = []
            for message in result['messages']:
                sender = message.get('senderUsername') or '发送者未知'
                content = message.get('parsedContent') or message.get('content') or '（非文字内容）'
                stamp = message.get('createTime')
                try:
                    stamp = datetime.fromtimestamp(float(stamp)).strftime('%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError, OverflowError, OSError):
                    stamp = '时间未知'
                lines.append(f"{stamp} · {sender}\n{content}")
            self.text.setPlainText('\n\n'.join(lines) or '该会话没有可预览记录。')
            self.export_button.setEnabled(True)
            self.notice.setText(f"本次 {len(lines)} 条；" + ('还有更早记录，导出仅包含本次预览。' if result.get('hasMore') else '已读取。')
                                + ' 未触发机器人回复。')
        self.call('POST', 'weflow/messages', self.draft(), done)

    def export(self):
        if self.busy or self.bundle is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, '导出本次只读预览', 'WeFlow预览.json', 'JSON (*.json)')
        if not filename:
            return
        try:
            Path(filename).write_text(json.dumps(self.bundle, ensure_ascii=False, indent=2), encoding='utf-8')
            self.notice.setText('本次预览已导出到指定文件；不包含 Token，也没有自动上传或导入机器人记忆。')
        except OSError:
            self.notice.setText('导出失败，请选择有写入权限的位置。')
