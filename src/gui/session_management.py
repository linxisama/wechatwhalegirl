"""插件页的会话观察和管理员绑定；HTTP 请求由宿主后台线程执行。"""
from datetime import datetime
from ..admin_password import password_error
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter,QPixmap,QColor
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QComboBox,QLineEdit)


class SessionManagementDialog(QDialog):
    def __init__(self, request, parent=None, theme=None, background=None,auto_refresh=True):
        super().__init__(parent)
        self.request = request
        self._busy = False
        self._refreshing = False
        self._refresh_again = False
        self._action_feedback = False
        self._buttons = []
        self.apply_theme(theme or {},background)
        self.setWindowTitle('会话监控与管理员')
        self.resize(840,600)
        layout = QVBoxLayout(self)
        self.hint = QLabel('显示当前账号本地观察到的会话与回复结果；群名单和私聊范围请在对应插件配置中修改。')
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.status=QLabel('机器人状态待读取')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.result = QLabel('点击刷新读取本机观察到的会话。')
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.result)
        row = QHBoxLayout()
        self.scope = QComboBox()
        for label,value in [('全部会话','all'),('群聊监控','group'),('私聊联系人','private')]:
            self.scope.addItem(label,value)
        self.scope.currentIndexChanged.connect(self.render)
        row.addWidget(self.scope)
        self.refresh_button = QPushButton('刷新监控')
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.list,1)
        self.owners = QComboBox()
        self.owners.setAccessibleName('本账号已绑定管理员')
        self.owners.setPlaceholderText('本账号尚未绑定管理员')
        layout.addWidget(self.owners)
        manual=QHBoxLayout()
        self.manual_target=QLineEdit()
        self.manual_target.setPlaceholderText('也可直接输入管理员微信稳定标识（wxid_xxx，不能用昵称）')
        manual.addWidget(self.manual_target,1)
        bind_manual=QPushButton('直接绑定微信账号')
        bind_manual.clicked.connect(self.bind_manual)
        self._buttons.append(bind_manual)
        manual.addWidget(bind_manual)
        layout.addLayout(manual)
        security=QHBoxLayout()
        self.password=QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('设置管理员密码（1–128 字符，仅中文、字母和数字；留空不变）')
        security.addWidget(self.password,1)
        save_password=QPushButton('保存管理员密码')
        save_password.clicked.connect(self.save_password)
        self._buttons.append(save_password)
        security.addWidget(save_password)
        layout.addLayout(security)
        state=QHBoxLayout()
        for label,paused in [('暂停自动回复',True),('恢复自动回复',False)]:
            button=QPushButton(label)
            button.clicked.connect(lambda checked=False,p=paused:self.local_pause(p))
            self._buttons.append(button)
            state.addWidget(button)
        enable=QPushButton('启用管理员私聊指令')
        enable.clicked.connect(lambda:self._operation('启用管理员私聊指令','plugins/robot_admin',{'enabled':True}))
        self._buttons.append(enable)
        state.addWidget(enable)
        layout.addLayout(state)
        buttons = QHBoxLayout()
        self.bind = QPushButton('绑定所选私聊为管理员')
        self.bind.setEnabled(False)
        self.bind.clicked.connect(lambda:self.change_owner(True))
        buttons.addWidget(self.bind)
        self.unbind = QPushButton('解除所选管理员')
        self.unbind.setEnabled(False)
        self.unbind.clicked.connect(lambda:self.change_owner(False))
        buttons.addWidget(self.unbind)
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        close.setVisible(auto_refresh)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.guidance = QLabel('尚未读取管理员设置。')
        self.guidance.setWordWrap(True)
        layout.addWidget(self.guidance)
        self.rows = []
        if auto_refresh:
            self.refresh()

    def apply_theme(self,theme,background=None):
        self.theme=dict(theme)
        self.background=QPixmap(str(background)) if background else QPixmap()
        alpha=int(255*(1-float(self.theme.get('panel_transparency',.35))))
        self.setStyleSheet(f'QDialog {{background:transparent;}} QListWidget,QLineEdit,QComboBox {{background:rgba(255,255,255,{alpha}); color:#18334C; border:1px solid #C8D4E2; padding:6px;}} QLabel {{background:transparent; color:#18334C;}} QPushButton {{background:#2FA8A0;color:white;padding:8px;border-radius:5px;}}')
        self.update()

    def paintEvent(self,event):
        painter=QPainter(self)
        if not self.background.isNull():
            image=self.background.scaled(self.size(),Qt.AspectRatioMode.KeepAspectRatioByExpanding,Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((self.width()-image.width())//2,(self.height()-image.height())//2,image)
        else:
            painter.fillRect(self.rect(),QColor(self.theme.get('bg_color','#F4F6F9')))
        painter.end()

    def bind_manual(self):
        target=self.manual_target.text().strip()
        if not target:
            self.feedback('请先输入管理员稳定微信标识（例如 wxid_xxx），不能使用昵称。',False)
            self.manual_target.setFocus()
            return
        self._operation('绑定管理员','management/owner',{'target':target,'bind':True,'manual':True})

    def save_password(self):
        password=self.password.text()
        error=password_error(password)
        if error:
            self.feedback(error+'；留空不会更改密码。',False)
            self.password.setFocus()
            return
        def saved():
            if self.password.text()==password:
                self.password.clear()
        self._operation('保存管理员密码','management/password',{'password':password},saved)

    def local_pause(self,paused):
        self._operation('暂停自动回复' if paused else '恢复自动回复','plugins/robot_admin',{'paused':paused})

    def feedback(self,text,ok=True):
        self._action_feedback=True
        self.result.setStyleSheet('color:#137359;' if ok else 'color:#B3261E;')
        self.result.setText(text)

    def _operation(self,label,path,data,on_success=None):
        if self._busy:
            return
        self._busy=True
        self.feedback(label+'：正在处理…')
        for button in self._buttons+[self.bind,self.unbind]:
            button.setEnabled(False)
        def done(result):
            self._busy=False
            for button in self._buttons:
                button.setEnabled(True)
            self.selection_changed()
            self.unbind.setEnabled(self.owners.count()>0)
            ok=bool(result.get('success'))
            self.feedback(label+'：'+str(result.get('message') or ('操作成功' if ok else '后台未返回结果，请检查连接')),ok)
            if ok:
                if on_success:
                    on_success()
                self.refresh()
        self.request(self,'POST',path,data,timeout=10,on_done=done)

    def action_done(self,result):
        self.feedback(str(result.get('message') or '后台未返回结果'),bool(result.get('success')))
        if result.get('success'):
            self.refresh()

    def refresh(self):
        if self._refreshing:
            self._refresh_again=True
            return
        self._refreshing=True
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText('正在刷新…')
        remaining=[2]
        def deliver(callback,result):
            try:
                callback(result)
            finally:
                remaining[0]-=1
                if remaining[0]==0:
                    self._refreshing=False
                    self.refresh_button.setEnabled(True)
                    self.refresh_button.setText('刷新监控')
                    if self._refresh_again:
                        self._refresh_again=False
                        self.refresh()
        self.request(self,'GET','management/sessions',timeout=10,on_done=lambda r:deliver(self.loaded,r))
        self.request(self,'GET','status',timeout=10,on_done=lambda r:deliver(self.status_loaded,r))

    def status_loaded(self,result):
        if not result or result.get('success') is False:
            self.status.setText('读取机器人状态失败：'+str(result.get('message') or '后台未响应'))
            return
        connection=result.get('connection') or {}
        online=connection.get('online')
        text='在线' if online is True else ('离线' if online is False else '待确认')
        backlog=result.get('pending_other_accounts',0)
        detail=(f'；另有其他账号历史积压 {backlog} 条，不在本账号处理。' if backlog else '')
        if online is False:
            detail+=' 微信离线，请在控制台测试微信连接或启动机器人；本机密码、暂停等设置仍可保存。'
        self.status.setText(f"微信：{text} · 模型：{result.get('model','待确认')} · 本账号待处理：{result.get('pending_current',result.get('pending','待确认'))} · 当前阶段：{(result.get('pipeline') or {}).get('stage','待确认')}"+detail)

    def loaded(self, result):
        if not result.get('success'):
            self.guidance.setText('读取会话失败：'+str(result.get('message') or '后台未响应'))
            return
        self.rows = result.get('sessions',[])
        selected_owner=self.owners.currentData()
        self.owners.clear()
        for target in result.get('owners',[]):
            self.owners.addItem(target,target)
        index=self.owners.findData(selected_owner)
        if index>=0:
            self.owners.setCurrentIndex(index)
        elif self.owners.count():
            self.owners.setCurrentIndex(0)
        self.unbind.setEnabled(not self._busy and self.owners.count() > 0)
        self.hint.setText(f"当前账号：{result.get('account','')}；自动回复：{'暂停' if result.get('reply_paused') else '正常'}。统计为本机观察累计，最多显示最近 200 个会话；群监控范围在“群聊监控管理”配置。")
        password={'set':'已设置','invalid':'文件损坏，请重设','unset':'未设置'}.get(result.get('password_state','unset'),'待确认')
        self.guidance.setText(f"管理员密码：{password}。可从私聊联系人选择，或直接绑定稳定微信标识；启用管理指令后，管理员私聊“进入管理员模式”验证。")
        if not self._action_feedback:
            self.result.setText('监控已刷新。'+('尚未观察到本账号会话；连接微信后，联系人发来私聊才会出现在列表中。' if not self.rows else f'共 {len(self.rows)} 个会话。'))
        self.render()

    def render(self):
        if not hasattr(self,'list'):
            return
        selected=self.list.currentItem()
        old=(selected.data(Qt.ItemDataRole.UserRole) or {}).get('target') if selected else None
        self.list.clear()
        for item in self.rows:
            if self.scope.currentData() not in ('all',item['kind']):
                continue
            counts = item.get('counts',{})
            stamp = datetime.fromtimestamp(item['last_seen']).strftime('%m-%d %H:%M:%S')
            kind = '群聊' if item['kind']=='group' else '私聊'
            state = '允许回复' if item['reply_allowed'] else '仅观察 / 回复已限制'
            if not item['monitored']:
                state = '已移出监听范围'
            text = f"{kind} · {item['name']}   {item['target']}\n{state} | 收到 {item['received']} | 已发 {counts.get('sent',0)} | 待处理 {counts.get('pending',0)} | 失败/待核实 {counts.get('failed',0)+counts.get('uncertain',0)} | 最近 {stamp}"
            row = QListWidgetItem(text)
            row.setData(Qt.ItemDataRole.UserRole,item)
            self.list.addItem(row)
            if item['target']==old:
                self.list.setCurrentItem(row)
        self.selection_changed()

    def selection_changed(self):
        selected = self.list.currentItem()
        item = selected.data(Qt.ItemDataRole.UserRole) if selected else {}
        self.bind.setEnabled(not self._busy and bool(item and item['kind']=='private' and item.get('verified')))

    def change_owner(self, bind):
        selected = self.list.currentItem()
        item = selected.data(Qt.ItemDataRole.UserRole) if selected else {}
        target = item.get('target') if bind else self.owners.currentData()
        if not target or (bind and not self.bind.isEnabled()):
            return
        self._operation('绑定管理员' if bind else '解除管理员','management/owner',{'target':target,'bind':bind})
