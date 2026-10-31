"""主人工作区编辑器；服务器的version检查避免覆盖其他Agent刚写入的内容。"""
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QCheckBox,QDialog,QHBoxLayout,QLabel,QLineEdit,
    QListWidget,QPushButton,QPlainTextEdit,QSpinBox,QFormLayout,QTabWidget,QVBoxLayout,QWidget)


class WorkspaceDialog(QDialog):
    def __init__(self,call_api,parent=None):
        super().__init__(parent)
        self.call_api=call_api
        self.version=''
        self.loaded_name=''
        self.path=''
        self.loading=False
        self.owner_confirmed=False
        self.diary_auto_confirmed=True
        self._note_epoch=0
        self.note_ready=True
        self._memory_epoch=0
        self.setWindowTitle('主人工作区 · 本地笔记与共享记忆')
        self.resize(800,650)
        self.setStyleSheet('QDialog { background: #F4F6F9; color: #182C43; } QPlainTextEdit { background: white; color: #182C43; }')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        layout=QVBoxLayout(self)
        self.location=QLabel('正在读取独立工作区…');self.location.setWordWrap(True)
        self.location.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.location)
        self.owner=QCheckBox('允许唯一微信主人通过私聊 /agent 指令使用工作区（仍遵守安全暂停）')
        self.owner.clicked.connect(lambda enabled:self.request('POST','agent/workspace/owner-channel',{'enabled':enabled},self.owner_saved))
        layout.addWidget(self.owner)
        tabs=QTabWidget();layout.addWidget(tabs,1)
        notes=QWidget();box=QVBoxLayout(notes)
        self.files=QListWidget();self.files.currentTextChanged.connect(self.read_note);box.addWidget(self.files)
        self.name=QLineEdit();self.name.setPlaceholderText('新笔记名称，例如 主人的偏好.md')
        self.name.textEdited.connect(self.name_changed);box.addWidget(self.name)
        self.editor=QPlainTextEdit();self.editor.setPlaceholderText('UTF-8本地笔记。编辑已有文件前请先选中读取。');box.addWidget(self.editor,2)
        row=QHBoxLayout()
        self.save=QPushButton('保存笔记');self.save.clicked.connect(self.save_note);row.addWidget(self.save)
        new=QPushButton('新建笔记');new.clicked.connect(self.new_note);row.addWidget(new)
        folder=QPushButton('打开工作区');folder.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(self.path)) if self.path else None);row.addWidget(folder)
        box.addLayout(row);tabs.addTab(notes,'笔记')
        memory=QWidget();box=QVBoxLayout(memory)
        box.addWidget(QLabel('显式记忆按主人保存；工作台人设对话自动保存原文，检索时仅引用相关内容。'))
        self.memory_search=QLineEdit();self.memory_search.setPlaceholderText('搜索记忆，例如 项目名、喜好；回车查询')
        self.memory_search.returnPressed.connect(self.refresh);box.addWidget(self.memory_search)
        self.memory_count=QLabel('');box.addWidget(self.memory_count)
        self.memories=QListWidget();box.addWidget(self.memories,1)
        self.memory_input=QLineEdit();self.memory_input.setPlaceholderText('要记住的偏好或事实（最多1000字）');box.addWidget(self.memory_input)
        row=QHBoxLayout()
        add=QPushButton('记住');add.clicked.connect(self.remember);row.addWidget(add)
        forget=QPushButton('忘记选中项');forget.clicked.connect(self.forget);row.addWidget(forget)
        diary=QPushButton('查看最近日记');diary.clicked.connect(lambda:self.request('GET','agent/workspace/diary',None,self.show_diary));row.addWidget(diary)
        box.addLayout(row)
        diary_row=QHBoxLayout()
        self.auto_diary=QCheckBox('大肥鱼自动写每日日记（每天至多一次模型请求）');self.auto_diary.setChecked(True)
        self.auto_diary.setToolTip('按本地原文写经历，感受属于人设表达；保留来源证据，不把日记当新事实自动学习。')
        self.auto_diary.clicked.connect(self.save_diary_settings)
        diary_row.addWidget(self.auto_diary,1)
        self.write_diary=QPushButton('让大肥鱼写今天日记');self.write_diary.clicked.connect(self.generate_diary);diary_row.addWidget(self.write_diary)
        box.addLayout(diary_row)
        self.diary=QPlainTextEdit();self.diary.setReadOnly(True);box.addWidget(self.diary)
        form=QFormLayout()
        self.history_turns=QSpinBox();self.history_turns.setRange(1,30);self.history_turns.setValue(10)
        self.context_chars=QSpinBox();self.context_chars.setRange(1000,24000);self.context_chars.setSingleStep(1000);self.context_chars.setValue(8000)
        self.recall_limit=QSpinBox();self.recall_limit.setRange(0,5);self.recall_limit.setValue(3)
        form.addRow('最近对话轮数',self.history_turns);form.addRow('历史与记忆总字符预算',self.context_chars)
        form.addRow('相关旧对话最多条数（0关闭）',self.recall_limit);box.addLayout(form)
        save_context=QPushButton('保存上下文配置');save_context.clicked.connect(self.save_context);box.addWidget(save_context)
        tabs.addTab(memory,'记忆与日记')
        bridge=QPlainTextEdit();bridge.setReadOnly(True)
        bridge.setPlainText('本地Agent / DSH / QQ接入\n\n'
            '1. 在Agent页“管理员与DSH接入”签发可撤销凭据。默认只读；编辑笔记需要“写记忆”。\n'
            '2. 外部Agent调用 GET /api/agent/bridge/handshake，携带 Authorization: Bearer <凭据>。\n'
            '3. POST /api/agent/bridge/execute 调用workspace.info获取本工作区。\n'
            '4. workspace.read/files/memories读取；workspace.write写笔记，version必须使用上次读取值。\n'
            '5. agent.chat需要改控制台权限；它继续按凭据限制记忆读写。换绑/撤销后立即失效。\n'
            '6. 新版MCP包含聊天原文检索、学习概览和学习审阅：DeepSeek Harness可用这些工具整理本地记忆。\n'
            '只读凭据可查询；批准/拒绝学习内容、重建当前会话学习结果需要“写记忆”。\n'
            '不开放SQL、原始数据库文件或整库删除，先核对account/kind/target再审阅。\n\n'
            '调用示例（body为JSON）：\n'
            '{"action":"workspace.read","query":{"name":"主人的偏好.md"}}\n'
            '{"action":"workspace.write","request_id":"note-write-001",'
            '"body":{"name":"主人的偏好.md","content":"喜欢简洁回复","version":""}}\n\n'
            '电脑执行由独立本地Agent及其工具负责。本程序实际开放本工作区txt/md编辑，'
            '不会自行运行聊天中的Shell代码、修改DSH默认会话，或把群聊授予电脑权限。')
        connection=QWidget();connection_layout=QVBoxLayout(connection)
        connection_layout.addWidget(bridge,1)
        export=QPushButton('生成本地Agent MCP连接器与配置示例')
        export.clicked.connect(lambda:self.request('POST','agent/workspace/connector',{},lambda d:self.result.setText(d['message'])))
        connection_layout.addWidget(export)
        self.dsh_write=QPushButton('一键接入 DSH（签发凭据并写入配置）')
        self.dsh_write.clicked.connect(lambda:self.dsh_provision(True));connection_layout.addWidget(self.dsh_write)
        self.dsh_copy=QPushButton('只复制配置块（不写文件）')
        self.dsh_copy.clicked.connect(lambda:self.dsh_provision(False));connection_layout.addWidget(self.dsh_copy)
        caution=QLabel('凭据等同于本机控制权，别贴到公共地方。');caution.setWordWrap(True);connection_layout.addWidget(caution)
        tabs.addTab(connection,'本地Agent连接')
        self.result=QLabel('');self.result.setWordWrap(True);self.result.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.result)
        close=QPushButton('完成');close.clicked.connect(self.accept);layout.addWidget(close)
        self.request('GET','agent/workspace',None,self.loaded)
        self.refresh()

    def dsh_provision(self,write):
        self.dsh_write.setEnabled(False);self.dsh_copy.setEnabled(False)
        self.request('POST','agent/dsh/provision',{'write':bool(write)},self.show_dsh_provision,self.show_dsh_failed)

    def show_dsh_provision(self,data):
        self.dsh_write.setEnabled(True);self.dsh_copy.setEnabled(True)
        lines=[str(data.get('message') or '接入已完成')]
        if data.get('block'): lines.append('配置块（粘贴到 cordis.patch.yml 后重启 DSH）：\n'+str(data['block']))
        if data.get('token'): lines.append('凭据（Bearer）：'+str(data['token']))
        lines.append('凭据等同于本机控制权，别贴到公共地方。')
        self.result.setText('\n\n'.join(lines))

    def show_dsh_failed(self,data):
        self.dsh_write.setEnabled(True);self.dsh_copy.setEnabled(True)
        self.result.setText(str(data.get('message') or data.get('detail') or '接入未完成')
            +'\n\n凭据等同于本机控制权，别贴到公共地方。')

    def request(self,method,path,body,done,failed=None):
        def finished(data):
            if not data.get('success'):
                self.result.setText(str(data.get('message') or data.get('detail') or '请求未完成'))
                if path.endswith('/diary'):
                    self.write_diary.setEnabled(True)
                    self.diary.setPlainText(str(data.get('message') or data.get('detail') or '日记未写成'))
                if failed: failed(data)
                elif path.split('?')[0].endswith('/note'): self.save.setEnabled(True)
                if path.endswith('/owner-channel'): self.owner.setChecked(self.owner_confirmed)
                if path.endswith('/diary-settings'):
                    self.auto_diary.setChecked(self.diary_auto_confirmed);self.auto_diary.setEnabled(True)
                return
            done(data)
        self.call_api(self,method,path,body,timeout=80 if method=='POST' and path.endswith('/diary') else 15,on_done=finished)

    def show_diary(self,data):
        self.write_diary.setEnabled(True)
        self.diary.setPlainText((data.get('day','')+'\n\n'+data['content']) if data.get('content') else data.get('message','还没有日记。'))
        if 'auto_enabled' in data:
            self.diary_auto_confirmed=bool(data['auto_enabled']);self.auto_diary.setChecked(self.diary_auto_confirmed)

    def save_diary_settings(self,enabled):
        self.auto_diary.setEnabled(False)
        def saved(data):
            self.diary_auto_confirmed=bool(data.get('enabled'))
            self.auto_diary.setChecked(self.diary_auto_confirmed);self.auto_diary.setEnabled(True)
            self.result.setText('自动日记设置已保存。')
        self.request('POST','agent/workspace/diary-settings',{'enabled':enabled},saved)

    def generate_diary(self):
        self.write_diary.setEnabled(False);self.diary.setPlainText('大肥鱼正在根据今天的记录写日记…')
        self.request('POST','agent/workspace/diary',{},self.show_diary)

    def loaded(self,data):
        self.path=data.get('path','')
        self.location.setText('主人独立工作区：'+self.path+'\n笔记单文件64KB，总量4MB；覆盖保留最近5份版本。')
        self.owner_confirmed=bool(data.get('owner_commands_enabled'))
        self.owner.setChecked(self.owner_confirmed)
        self.owner.setEnabled((data.get('admin') or {}).get('platform')=='wechat')
        context=data.get('context') or {}
        self.history_turns.setValue(context.get('history_turns',10))
        self.context_chars.setValue(context.get('max_chars',8000))
        self.recall_limit.setValue(context.get('recall_limit',3))
        self.diary_auto_confirmed=bool(data.get('diary_auto_enabled',True));self.auto_diary.setChecked(self.diary_auto_confirmed)

    def save_context(self):
        self.request('POST','agent/workspace/context',dict(history_turns=self.history_turns.value(),
            max_chars=self.context_chars.value(),recall_limit=self.recall_limit.value()),
            lambda data:self.result.setText('上下文配置已保存；下一轮生效，不删除聊天原文。'))

    def owner_saved(self,data):
        self.owner_confirmed=bool(data.get('enabled'))
        self.owner.setChecked(self.owner_confirmed)
        self.result.setText('主人通道设置已保存；不会自动连接微信。')

    def refresh(self):
        def listed(data):
            self.files.blockSignals(True);self.files.clear()
            self.files.addItems([r['name'] for r in data.get('files') or []]);self.files.blockSignals(False)
        self.request('GET','agent/workspace/files',None,listed)
        self._memory_epoch+=1
        epoch=self._memory_epoch
        def memory(data):
            if epoch!=self._memory_epoch:return
            self.memories.clear()
            for r in data.get('items') or []:
                self.memories.addItem(r['content']);self.memories.item(self.memories.count()-1).setData(Qt.ItemDataRole.UserRole,r['id'])
            total=data.get('total',self.memories.count())
            self.memory_count.setText(f'找到 {total} 条，显示 {self.memories.count()} 条'+('；请缩小搜索范围查看其他记忆' if data.get('has_more') else ''))
        from urllib.parse import urlencode
        path='agent/workspace/memories?persona=*'
        if self.memory_search.text().strip():path+='&'+urlencode({'query':self.memory_search.text().strip()})
        self.request('GET',path,None,memory)

    def read_note(self,name):
        if not name:return
        from urllib.parse import urlencode
        self._note_epoch+=1;epoch=self._note_epoch
        self.version='';self.loading=True;self.note_ready=False
        self.editor.clear();self.editor.setReadOnly(True)
        self.loaded_name=name;self.name.setText(name);self.save.setEnabled(False)
        def read(data):
            if epoch!=self._note_epoch or self.loaded_name!=name or self.name.text()!=name:return
            self.editor.setPlainText(data.get('content',''));self.version=data.get('version','');self.save.setEnabled(True)
            self.loading=False;self.note_ready=True;self.editor.setReadOnly(False)
        def failed(data):
            if epoch!=self._note_epoch:return
            self.loading=False;self.editor.setReadOnly(False);self.save.setEnabled(True)
        self.request('GET','agent/workspace/note?'+urlencode({'name':name}),None,read,failed)

    def name_changed(self):
        self._note_epoch+=1;self.loading=False;self.note_ready=True;self.editor.setReadOnly(False)
        self.loaded_name='';self.version='';self.save.setEnabled(True)

    def new_note(self):
        self.name.clear();self.editor.clear();self.name_changed();self.name.setFocus()

    def save_note(self):
        if self.loading or not self.note_ready:
            self.result.setText('笔记尚未成功读取，请重新选择该文件；新建笔记请点“新建笔记”。')
            return
        name=self.name.text().strip()
        epoch=self._note_epoch
        content=self.editor.toPlainText()
        self.save.setEnabled(False)
        def saved(data):
            if epoch!=self._note_epoch:return
            self.save.setEnabled(True)
            if self.name.text().strip()==name:
                self.loaded_name=name;self.version=data['version']
            self.result.setText('笔记已保存。' if self.editor.toPlainText()==content else '提交时的版本已保存；当前新增编辑尚未保存。');self.refresh()
        def failed(data):
            if epoch==self._note_epoch:self.save.setEnabled(True)
        self.request('POST','agent/workspace/note',{'name':name,'content':content,'version':self.version},saved,failed)

    def remember(self):
        self.request('POST','agent/workspace/memories',{'content':self.memory_input.text()},lambda d:(self.memory_input.clear(),self.refresh()))

    def forget(self):
        item=self.memories.currentItem()
        if item:self.request('POST','agent/workspace/forget',{'id':item.data(Qt.ItemDataRole.UserRole)},lambda d:self.refresh())
