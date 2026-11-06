"""账号安全暂停只能由本机用户确认解除，不能由模型或重连自动解除。"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog,QVBoxLayout,QLabel,QLineEdit,QPushButton
from src.wechat_safety import RESET_PHRASE


class SafetyDialog(QDialog):
    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request=request
        self.setWindowTitle('账号安全暂停')
        self.resize(590,300)
        self.setStyleSheet('QDialog { background:#F4F6F9; color:#182C43; } QLineEdit { background:white; padding:8px; }')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        layout=QVBoxLayout(self)
        hint=QLabel('发现微信安全提醒后，机器人停止收发并保持暂停，重启不会自动恢复。'
            '请先在微信安全提醒的“详情”内完成官方安全验证。机器人不能解除微信账号限制，'
            '也不会自动登录、发送测试消息或继续回复微信团队。')
        hint.setWordWrap(True);layout.addWidget(hint)
        self.confirmation=QLineEdit()
        self.confirmation.setPlaceholderText(RESET_PHRASE)
        layout.addWidget(self.confirmation)
        self.notice=QLabel('只在已完成官方处理后输入确认文字。解除的只是机器人暂停，不证明账号已恢复。')
        self.notice.setWordWrap(True);self.notice.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.notice)
        self._busy=False
        self.button=QPushButton('解除机器人暂停（不自动启动）')
        self.button.clicked.connect(self.clear);layout.addWidget(self.button)
        close=QPushButton('保持暂停并关闭');close.clicked.connect(self.accept);layout.addWidget(close)

    def _set_busy(self,busy):
        """提交期间锁住按钮：连点两次会发出两个解除请求，第二个的回复容易让人误判当前状态。"""
        self._busy=bool(busy)
        self.button.setEnabled(not self._busy)
        self.button.setText('正在解除…' if self._busy else '解除机器人暂停（不自动启动）')

    def clear(self):
        if self._busy:
            return
        if self.confirmation.text().strip()!=RESET_PHRASE:
            self.notice.setText('请先完成微信官方安全验证，再输入：'+RESET_PHRASE)
            return
        self._set_busy(True)
        self.notice.setText('已提交解除请求，等待本机后台确认…')
        def finished(value):
            self._set_busy(False)
            data=value if isinstance(value,dict) else {}
            ok=bool(data.get('success'))
            detail=str(data.get('message') or data.get('detail')
                or ('操作完成' if ok else '后台未返回结果，请检查本机后台连接'))
            self.notice.setText(('已解除机器人暂停：' if ok else '未解除暂停：')+detail)
        try:
            self.request(self,'POST','wechat/safety/reset',{'confirmation':self.confirmation.text().strip()},
                timeout=10,on_done=finished)
        except Exception as exc:
            self._set_busy(False)
            self.notice.setText(f'解除失败：{exc}')
