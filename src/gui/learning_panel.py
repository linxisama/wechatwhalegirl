"""可见、可审阅的本地学习库，不把空白自我记忆页当作聊天历史。"""
from urllib.parse import urlencode
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,QPushButton,QTextEdit


class LearningPanel(QWidget):
    def __init__(self,request):
        super().__init__();self.request=request;self.busy=False;self.account=''
        box=QVBoxLayout(self)
        hint=QLabel('聊天原文保存在“本地对话库”；这里显示从记录中提炼的学习内容。每个群/私聊的活动窗口最多1000条，旧上下文自动移除，原始归档保留。事实需同一人两次独立自述；表达候选批准后才引用，不改人设、不花额外模型费用。')
        hint.setWordWrap(True);box.addWidget(hint)
        self.rooms=QComboBox();box.addWidget(self.rooms)
        self.rooms.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.rooms.setMinimumContentsLength(15)
        row=QHBoxLayout()
        for label,action in [('读取学习库',self.load),('从最近1000条记录学习',lambda:self.change('learn')),
                             ('全部会话一键学习',lambda:self.bulk('learn-all')),
                             ('全部批准表达',lambda:self.bulk('approve-expressions')),
                             ('整理记忆',self.maintain)]:
            button=QPushButton(label);button.clicked.connect(action);row.addWidget(button)
        box.addLayout(row)
        self.text=QTextEdit();self.text.setReadOnly(True);self.text.setStyleSheet('background:#FAFCFE;color:#18334A;');box.addWidget(self.text,1)
        self.items=QComboBox();box.addWidget(self.items)
        self.items.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.items.setMinimumContentsLength(15)
        row=QHBoxLayout()
        for label,action in [('批准表达/事实','approve'),('拒绝','reject'),('删除','delete')]:
            button=QPushButton(label);button.clicked.connect(lambda checked=False,a=action:self.change(a));row.addWidget(button)
        box.addLayout(row)
        self.notice=QLabel('尚未读取。没有学习条目时，可先到本地对话库检查是否已有消息。');self.notice.setWordWrap(True);box.addWidget(self.notice)

    def query(self):return self.rooms.currentData() or dict(kind='group',target='')

    def call(self,method,path,body,done):
        if self.busy:return
        self.busy=True;self.setEnabled(False)
        def finished(value):
            self.busy=False;self.setEnabled(True)
            if value.get('success'):done(value)
            else:self.notice.setText(value.get('message') or '读取失败，请检查本机后台')
        self.request(self,method,path,body,timeout=30,on_done=finished)

    def load(self,*_):self.call('GET','dialogue/learning?'+urlencode(self.query()),None,self.loaded)

    def loaded(self,value):
        chosen=self.query();self.account=value['account'];self.rooms.clear()
        for room in value.get('rooms',[]):
            self.rooms.addItem(f"{room['target']} · {room['count']} 条归档",dict(kind=room['kind'],target=room['target']))
        index=self.rooms.findData(chosen)
        if index>=0:self.rooms.setCurrentIndex(index)
        elif not chosen['target'] and self.rooms.count():QTimer.singleShot(0,self.load)
        self.items.clear();lines=[]
        for item in value['items']:
            status={'approved':'已确认','candidate':'待确认','rejected':'已拒绝'}.get(item['status'],item['status'])
            category={'fact':'明确事实','expression':'表达例句'}.get(item['category'],item['category'])
            label=f"{status} · {item.get('subject_name') or item['subject']} · {item['content']}"
            self.items.addItem(label,item['id'])
            lines.append(label+f"\n类型：{category}；独立证据 {item['hits']} 条；消息ID："+'、'.join(item['evidence']))
        self.text.setPlainText('\n\n'.join(lines) or '尚无学习结果。先选择会话，点击“从最近1000条记录学习”。单次自述和表达候选不会直接成为已确认事实。')
        self.notice.setText(f"活动窗口 {value['window_count']}/{value['window_limit']} 条；学习条目 {len(value['items'])}。只会在同一账号、会话和发送者的后续回复中按需引用。")

    def maintain(self):
        """整理记忆：合并近重复、清理过期条目（只动本地库，会先弹确认）。"""
        if self.busy:
            return
        from PyQt6.QtWidgets import QMessageBox
        ok = QMessageBox.question(
            self, '整理记忆',
            '会做三件事：\n· 合并重复的记忆（保留较新的一条）\n· 清理 7 天前被替换的旧记忆\n'
            '· 清理 30 天前只有一次证据的候选\n\n已确认且唯一的记忆不会被删除。继续吗？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ok != QMessageBox.StandardButton.Yes:
            return
        self.call('POST', 'dialogue/memory/maintain', {}, lambda result: self.maintained(result))

    def maintained(self, result):
        self.notice.setText(str(result.get('message') or '整理完成'))
        self.load()

    def bulk(self,action):
        """一键给所有会话补学/批准，不用逐个会话点。"""
        if self.busy:return
        body=dict(account=self.account,action=action,kind='',target='')
        self.call('POST','dialogue/learning/bulk',body,lambda result:self.after_bulk(result))

    def after_bulk(self,result):
        self.notice.setText(str(result.get('message') or '已完成'))
        self.load()

    def overview(self):
        """进来先给个总览：多少会话、多少活动窗口、多少已学，避免页面像空白。"""
        if self.busy:return
        # 旧包或后台没跟上时 /overview 可能不存在：不能卡住页面，先退回原来的读取路径。
        def first(value):
            rooms = value.get('rooms')
            if rooms is None:self.load();return
            self.overviewed(value)
        self.call('GET','dialogue/learning/overview',None,first)

    def overviewed(self,value):
        rooms=value.get('rooms') or []
        if not rooms:
            self.notice.setText('还没有归档的会话。先在“本地对话库”确认已监听并保存了消息，再来学习。')
            return
        window=sum(room['window'] for room in rooms)
        learned=sum(room['facts']+room['expressions'] for room in rooms)
        candidates=sum(room['candidate'] for room in rooms)
        archive=sum(int(room.get('archive') or 0) for room in rooms)
        if not learned and archive:
            # 升级上来的老机器：归档有原文、学习库却是空的，光说“0 条”用户不知道还能怎么办。
            self.notice.setText(f"共 {value.get('rooms_total',len(rooms))} 个会话、{archive} 条聊天记录，"
                                f"但学习库还是空的：升级后老记录要重新提炼一次，"
                                f"点“全部会话一键学习”即可（同一条不会重复计数）。")
        else:
            self.notice.setText(f"共 {value.get('rooms_total',len(rooms))} 个会话：活动窗口 {window} 条，"
                                f"已学 {learned} 条（待确认 {candidates} 条）。"
                                f"没有内容时可点“全部会话一键学习”。")
        self.load()

    def change(self,action):
        query=self.query()
        if not query['target'] or action!='learn' and not self.items.currentData():return
        self.call('POST','dialogue/learning',dict(query,account=self.account,action=action,identity=self.items.currentData() or ''),lambda result:self.load())

    def showEvent(self,event):
        super().showEvent(event)
        if not self.account:QTimer.singleShot(0,self.overview)
