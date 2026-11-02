"""参考 QQ 对话页：用户规则、手动记忆、可编辑档案与持久诊断。"""
from datetime import datetime
from urllib.parse import quote
from PyQt6.QtCore import Qt,QTimer
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QTabWidget,
    QListWidget,QListWidgetItem,QLineEdit,QCheckBox,QTextEdit,QComboBox,QTableWidget,QTableWidgetItem,
    QHeaderView,QAbstractItemView)
from .dialogue_rules import DialogueRulesDialog
from .conversation_archive import ConversationArchive


class DialogueTab(QWidget):
    def __init__(self,request,open_plugin):
        super().__init__()
        self.request=request;self._loading=False;self._refreshing=False;self._refresh_again=False
        self._busy=False;self._persona='';self._facts=[];self._diagnostics=[];self._auto_saved=True
        self._buttons=[]
        layout=QVBoxLayout(self)
        self.title=QLabel('对话与记忆');layout.addWidget(self.title)
        shortcuts=QHBoxLayout()
        for label,group in [('回复触发与名单','trigger'),('连续跟聊','followup'),('人设隔离','isolation'),('跨会话偏好','preferences')]:
            button=QPushButton(label);button.clicked.connect(lambda checked=False,g=group:self.open_rules(g));shortcuts.addWidget(button)
        layout.addLayout(shortcuts)
        context_button=QPushButton('上下文轮数与概率回复');context_button.clicked.connect(lambda:self.open_rules('followup'))
        layout.addWidget(context_button)
        self.summary=QLabel('规则待读取；所有设置和记忆均保存在本机。');self.summary.setWordWrap(True);layout.addWidget(self.summary)
        self.auto=QCheckBox('自动记录明确偏好（需要两条独立消息确认）');self.auto.setEnabled(False);self.auto.clicked.connect(self.set_auto);layout.addWidget(self.auto)
        self.tabs=QTabWidget()
        manual=QWidget();box=QVBoxLayout(manual)
        box.addWidget(QLabel('手动记忆按当前人设保存；启用的内容在长期记忆插件开启时进入下一轮提示。'))
        self.notes=QListWidget();self.notes.itemChanged.connect(self.toggle_note)
        self.notes.itemSelectionChanged.connect(self.select_note);box.addWidget(self.notes,1)
        row=QHBoxLayout();self.content=QLineEdit();self.content.setPlaceholderText('例如：谈到加班时，先关心对方的状态');row.addWidget(self.content,1)
        for label,action in [('添加',self.add_note),('保存选中修改',self.edit_note),('删除选中的手动记忆',self.remove_note)]:
            button=QPushButton(label);button.clicked.connect(action);self._buttons.append(button);row.addWidget(button)
        box.addLayout(row);self.tabs.addTab(manual,'手动表达偏好')

        profile=QWidget();box=QVBoxLayout(profile)
        self.fact_search=QLineEdit();self.fact_search.setPlaceholderText('搜索个人记忆内容，按回车查找');self.fact_search.returnPressed.connect(self.refresh);box.addWidget(self.fact_search)
        self.people=QTextEdit();self.people.setReadOnly(True);self.people.setMaximumHeight(85);box.addWidget(self.people)
        self.facts=QTableWidget(0,4);self.facts.setHorizontalHeaderLabels(['内容','确认状态','独立证据','用户 / 会话'])
        self.facts.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.facts.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.facts.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.facts.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.facts.horizontalHeader().setStyleSheet('QHeaderView::section {background:#E8EEF5;color:#284458;padding:6px;border:0;}')
        self.facts.verticalHeader().setVisible(False)
        self.facts.itemSelectionChanged.connect(self.select_fact);box.addWidget(self.facts,1)
        # 空表时必须说明"为什么是空的"，否则用户会以为记忆功能坏了。
        self.fact_hint=QLabel('');self.fact_hint.setWordWrap(True)
        self.fact_hint.setStyleSheet('color:#5b6b7b;');box.addWidget(self.fact_hint)
        self.subject=QComboBox();self.subject.setPlaceholderText('选择已观察到的联系人与会话');box.addWidget(self.subject)
        self.fact_content=QLineEdit();self.fact_content.setPlaceholderText('用户明确确认的个人事实或偏好，最多 300 字');box.addWidget(self.fact_content)
        row=QHBoxLayout()
        for label,action in [('添加个人记忆',lambda:self.change_fact('add')),('保存选中修改',lambda:self.change_fact('edit')),
                             ('确认选中记忆',lambda:self.change_fact('confirm')),('删除选中档案',lambda:self.change_fact('delete'))]:
            button=QPushButton(label);button.clicked.connect(action);self._buttons.append(button);row.addWidget(button)
        box.addLayout(row);self.tabs.addTab(profile,'群友与个人档案')

        diagnostic=QWidget();box=QVBoxLayout(diagnostic)
        hint=QLabel('仅展示本机已收到消息的实际状态；待处理不等于已发送。本页不会重发历史消息。');hint.setWordWrap(True);box.addWidget(hint)
        row=QHBoxLayout();self.diagnostic_search=QLineEdit();self.diagnostic_search.setPlaceholderText('搜索发送者、会话、内容或原因')
        self.diagnostic_search.textChanged.connect(self.render_diagnostics);row.addWidget(self.diagnostic_search,1)
        self.state_filter=QComboBox()
        for label,value in [('全部状态','all'),('未回复/待处理','unanswered'),('已发送','sent'),('失败/待核实','failed')]:
            self.state_filter.addItem(label,value)
        self.state_filter.currentIndexChanged.connect(self.render_diagnostics);row.addWidget(self.state_filter);box.addLayout(row)
        self.diagnostics=QTextEdit();self.diagnostics.setReadOnly(True);box.addWidget(self.diagnostics,1)
        self.tabs.addTab(diagnostic,'回复记录与未回复诊断')
        self.archive=ConversationArchive(request);self.tabs.addTab(self.archive,'本地对话库 / 导入导出')
        from .learning_panel import LearningPanel
        self.learning=LearningPanel(request);self.tabs.addTab(self.learning,'本地学习库')
        from .usage_panel import UsagePanel
        self.usage_panel=UsagePanel(request);self.tabs.addTab(self.usage_panel,'用量与记忆巡检')
        self.tabs.setCurrentIndex(3)
        layout.addWidget(self.tabs,1)
        self.notice=QLabel('');self.notice.setWordWrap(True);self.notice.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.notice)
        self.refresh_button=QPushButton('刷新');self.refresh_button.clicked.connect(self.refresh);layout.addWidget(self.refresh_button)
        self.timer=QTimer(self);self.timer.setInterval(3000)
        self.timer.timeout.connect(lambda:self.refresh() if self.tabs.currentIndex()==2 and not self._busy else None)
        self.apply_theme({'panel_transparency':.35})

    def open_rules(self,group):
        DialogueRulesDialog(self.request,group,self,on_saved=self.refresh).exec()

    def apply_theme(self,cfg):
        alpha=int(255*(1-float(cfg.get('panel_transparency',.35))))
        for widget in (self.notes,self.people,self.facts,self.diagnostics):
            widget.setStyleSheet(f'background: rgba(255,255,255,{alpha}); border: 1px solid #C8D4E2;')

    def showEvent(self,event):
        super().showEvent(event);self.timer.start()

    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)

    def refresh(self):
        if self._busy:
            self._refresh_again=True;return
        if self._refreshing:
            self._refresh_again=True;return
        self._refreshing=True;self.refresh_button.setEnabled(False);self.refresh_button.setText('正在刷新…')
        query=self.fact_search.text().strip()
        path='dialogue'+('?q='+quote(query) if query else '')
        self.request(self,'GET',path,timeout=10,on_done=self.loaded)

    def loaded(self,data):
        self._refreshing=False;self.refresh_button.setEnabled(True);self.refresh_button.setText('刷新')
        if not data.get('success'):
            self._refresh_again=False
            self.notice.setText(str(data.get('message') or '无法读取对话记录'))
            return
        self._loading=True
        self._persona=str(data.get('persona') or self._persona);self.title.setText('对话与记忆 · '+self._persona)
        self._auto_saved=bool(data.get('autoRemember',True));self.auto.setChecked(self._auto_saved)
        self.auto.setEnabled(not self._busy)
        enabled=data.get('memoryEnabled',True)
        account=('账号：'+str(data['account'])+('（离线，显示已保存记录）；' if data.get('offline') else '；')) if data.get('account') else ''
        self.summary.setText(account+f"长期记忆：{'已启用' if enabled else '已关闭，规则和档案仍保留但不会注入回复'}；"
            f"人设隔离：{'开启' if data.get('personaIsolation',True) else '关闭'}；跨会话偏好：{'开启' if data.get('crossChatPreferences',False) else '关闭'}。点击上方按钮制定规则并保存。")
        selected=self.notes.currentItem();selected_id=selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.notes.blockSignals(True);self.notes.clear()
        for note in data.get('notes',[]):
            row=QListWidgetItem(note['content']);row.setData(Qt.ItemDataRole.UserRole,note['id'])
            row.setFlags(row.flags()|Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Checked if note['enabled'] else Qt.CheckState.Unchecked);self.notes.addItem(row)
            if note['id']==selected_id:self.notes.setCurrentItem(row)
        self.notes.blockSignals(False)
        self.people.setPlainText('\n'.join(f"{v['name']} · {v['id']}：观察 {v['messages']} 条" for v in data.get('people',[])) or '当前账号暂无联系人观察记录，收到消息后可选择并添加个人记忆。')
        current=self.subject.currentData();self.subject.clear()
        for value in data.get('subjects',[]):
            self.subject.addItem(f"{value.get('name') or value['subject']} · {value['subject']} · {value['scope']}",value)
        index=self.subject.findData(current)
        if index>=0:self.subject.setCurrentIndex(index)
        elif self.subject.count():self.subject.setCurrentIndex(0)
        old=self.selected_fact();old_id=old.get('id') if old else None
        self._facts=data.get('facts',[]);self.facts.blockSignals(True);self.facts.setRowCount(len(self._facts))
        for i,value in enumerate(self._facts):
            for col,text in enumerate([value['content'],'已确认' if value['status']=='confirmed' else '待复证',str(value['evidence']),value['subject']+' / '+value['scope']]):
                self.facts.setItem(i,col,QTableWidgetItem(text))
            if value['id']==old_id:self.facts.selectRow(i)
        self.facts.blockSignals(False)
        self.fact_hint.setText('' if self._facts else
            '还没有个人记忆：只有对方明确说「我喜欢…／我不喜欢…／我叫…」这类自述才会自动记录'
            '（前缀可在插件「长期记忆」的 autoPrefixes 里改）。聊天里没有这类句子，这里就是空的 —— '
            '想让它更懂人，请看「本地学习库」：那是从群聊原文提炼语气和事实的另一套。')
        self._diagnostics=data.get('diagnostics',[]);self.render_diagnostics();self._loading=False
        if self._refresh_again:
            self._refresh_again=False;self.refresh()

    def render_diagnostics(self,*args):
        query=self.diagnostic_search.text().strip().casefold();kind=self.state_filter.currentData();lines=[]
        for value in self._diagnostics:
            state=value['state']
            if kind=='unanswered' and state=='sent':continue
            if kind=='sent' and state!='sent':continue
            if kind=='failed' and state not in ('failed','uncertain'):continue
            text=' '.join(str(value.get(k) or '') for k in ('sender','target_name','text','error','reply','state_label'))
            if query and query not in text.casefold():continue
            stamp=datetime.fromtimestamp(float(value.get('time') or 0)).strftime('%m-%d %H:%M:%S')
            lines.append(f"{stamp} · {value.get('sender','')} · {value.get('target_name','')} · {value.get('state_label',state)}\n"
                f"{value.get('text','')}\n回复：{value.get('reply') or '尚无已发送回复'}\n说明：{value.get('error') or ('微信发送接口已确认完成' if state=='sent' else '正在处理')}" )
        self.diagnostics.setPlainText('\n\n'.join(lines) or '暂无符合条件的记录；只有本机实际收到的消息才能诊断。')

    def _write(self,path,body,success=None,failure=None):
        if self._busy:return
        if self._refreshing:
            self.notice.setText('正在刷新，请等读取完成后再保存')
            if failure:failure()
            return
        if not self._persona:
            self.notice.setText('请先刷新读取当前人设与记忆');return
        self._busy=True;self.notice.setText('正在保存…')
        for button in self._buttons+[self.auto,self.notes]:button.setEnabled(False)
        def done(result):
            self._busy=False
            for button in self._buttons+[self.auto,self.notes]:button.setEnabled(True)
            self.notice.setText(str(result.get('message') or '后台未返回结果，请检查连接'))
            if result.get('success'):
                if success:success()
                self.refresh()
            else:
                if failure:failure()
                if self._refresh_again:
                    self._refresh_again=False;self.refresh()
        self.request(self,'POST',path,dict(body,persona=self._persona) if path.startswith('dialogue/') else body,timeout=15,on_done=done)

    def post(self,body,success=None,failure=None):
        self._write('dialogue/notes',body,success,failure)

    def select_note(self):
        item=self.notes.currentItem()
        if item and not self._loading:self.content.setText(item.text())

    def add_note(self):
        content=self.content.text().strip()
        if not 1<=len(content)<=500:
            self.notice.setText('请输入 1–500 个字的手动记忆');return
        self.post({'content':content},lambda:self.content.clear() if self.content.text().strip()==content else None)

    def edit_note(self):
        item=self.notes.currentItem()
        if not item:self.notice.setText('请先选择要修改的手动记忆');return
        content=self.content.text().strip()
        if not 1<=len(content)<=500:self.notice.setText('请输入 1–500 个字的手动记忆');return
        self.post({'id':item.data(Qt.ItemDataRole.UserRole),'content':content})

    def remove_note(self):
        item=self.notes.currentItem()
        if item:self.post({'id':item.data(Qt.ItemDataRole.UserRole),'delete':True})
        else:self.notice.setText('请先选择要删除的手动记忆')

    def toggle_note(self,item):
        if not self._loading:
            checked=item.checkState()==Qt.CheckState.Checked
            def restore():
                self._loading=True;item.setCheckState(Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked);self._loading=False
            self.post({'id':item.data(Qt.ItemDataRole.UserRole),'enabled':checked},failure=restore)

    def set_auto(self,checked):
        self._write('dialogue/rules',{'group':'preferences','plugins':{'conversation_memory':{'autoRemember':checked}}},
                    failure=lambda:self.auto.setChecked(self._auto_saved))

    def selected_fact(self):
        row=self.facts.currentRow()
        return self._facts[row] if 0<=row<len(self._facts) else None

    def select_fact(self):
        value=self.selected_fact()
        if value and not self._loading:self.fact_content.setText(value['content'])

    def change_fact(self,action):
        value=self.selected_fact();content=self.fact_content.text().strip();body={'action':action}
        if action!='add':
            if not value:self.notice.setText('请先选择个人记忆');return
            body['id']=value['id']
        else:
            subject=self.subject.currentData()
            if not subject:self.notice.setText('请先选择当前账号已观察到的联系人与会话');return
            body.update(subject=subject['subject'],scope=subject['scope'])
        if action in ('add','edit'):
            if not 1<=len(content)<=300:self.notice.setText('个人记忆请输入 1–300 个字');return
            body['content']=content
        self._write('dialogue/facts',body,lambda:self.fact_content.clear() if action=='add' and self.fact_content.text().strip()==content else None)
