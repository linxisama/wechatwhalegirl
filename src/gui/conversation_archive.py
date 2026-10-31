"""本地对话库：HTTP 查询、搜索、导入导出和限定会话删除。"""
import json
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,
    QComboBox,QTextEdit,QFileDialog,QMessageBox)


class ConversationArchive(QWidget):
    def __init__(self,request):
        super().__init__();self.request=request;self.offset=0;self.account='';self.records=[];self.busy=False
        layout=QVBoxLayout(self)
        hint=QLabel('这里保存本机监听到的聊天原文和已发送回复，按账号、群聊/私聊隔离，重启后保留。启用“长期记忆与群友档案”后，回答时自动检索同一会话的相关旧记录；无需逐条手动添加。节省 Token 只压缩本轮输入，不删除这里的记录。')
        hint.setWordWrap(True);layout.addWidget(hint)
        row=QHBoxLayout();self.rooms=QComboBox();row.addWidget(self.rooms,2)
        self.search=QLineEdit();self.search.setPlaceholderText('搜索内容或发送者，回车查询')
        self.search.returnPressed.connect(lambda:self.load(True));row.addWidget(self.search,2)
        self.reload=QPushButton('读取 / 搜索');self.reload.clicked.connect(lambda:self.load(True));row.addWidget(self.reload)
        layout.addLayout(row)
        self.text=QTextEdit();self.text.setReadOnly(True);self.text.setStyleSheet('background:rgba(255,255,255,235);color:#18334A;')
        layout.addWidget(self.text,1)
        self.selected=QComboBox();self.selected.setToolTip('选择要删除的单条记录');layout.addWidget(self.selected)
        row=QHBoxLayout()
        for label, action in [('上一页',lambda:self.page(-100)),('下一页',lambda:self.page(100)),
                              ('导出当前会话 JSON',self.export),('导入 JSON',self.restore),
                              ('删除选中记录',lambda:self.delete(False)),('清空当前会话记忆',lambda:self.delete(True))]:
            button=QPushButton(label);button.clicked.connect(action);row.addWidget(button)
        layout.addLayout(row)
        self.notice=QLabel('点“读取”后选择会话，再次点击读取；群聊总结指令：/总结群聊 24。')
        self.notice.setWordWrap(True);layout.addWidget(self.notice)

    def query(self):
        selected=self.rooms.currentData() or {}
        return dict(kind=selected.get('kind','group'),target=selected.get('target',''))

    def call(self,method,path,body=None,done=None):
        if self.busy:return
        self.busy=True;self.setEnabled(False)
        def finished(result):
            self.busy=False;self.setEnabled(True)
            if not result.get('success'):
                self.notice.setText(str(result.get('message') or '读取失败，请检查后台连接'));return
            if done:done(result)
        self.request(self,method,path,body,timeout=30,on_done=finished)

    def load(self,reset=False):
        if reset:self.offset=0
        query=self.query();query.update(q=self.search.text().strip(),offset=self.offset)
        self.call('GET','dialogue/archive?'+urlencode(query),done=self.loaded)

    def loaded(self,result):
        chosen=self.query();self.account=result.get('account','');self.rooms.clear()
        for row in result.get('rooms',[]):
            self.rooms.addItem(f"{row.get('name') or row['target']} · {row['count']} 条",dict(kind=row['kind'],target=row['target']))
        index=self.rooms.findData(chosen)
        if index>=0:self.rooms.setCurrentIndex(index)
        elif not chosen['target'] and self.rooms.count():QTimer.singleShot(0,lambda:self.load(True))
        self.records=result.get('records',[]);self.selected.clear();lines=[]
        for row in self.records:
            stamp=datetime.fromtimestamp(row['created']).strftime('%m-%d %H:%M:%S')
            lines.append(f"{stamp} · {row['sender_name']} · {row['sender_id']}\n{row['content']}\n"
                         +(f"已发送回复（{row['persona'] or '旧版人设'}）：{row['reply']}" if row['reply'] else ''))
            self.selected.addItem(f"{stamp} · {row['sender_name']} · {row['content'][:40]}",row['key'])
        self.text.setPlainText('\n\n'.join(lines) or '请选择会话并点击“读取 / 搜索”。')
        self.notice.setText(f"共 {result.get('total',0)} 条匹配记录，本页 {len(self.records)} 条；偏移 {self.offset}。导出最多最近 2000 条，原记录继续保留。")

    def page(self,amount):
        self.offset=max(0,self.offset+amount);self.load()

    def showEvent(self,event):
        super().showEvent(event)
        if not self.account:QTimer.singleShot(0,lambda:self.load(True))

    def export(self):
        query=self.query()
        if not query['target']:return
        path,_=QFileDialog.getSaveFileName(self,'导出本地对话','对话记忆.json','JSON (*.json)')
        if not path:return
        def done(result):
            try:
                Path(path).write_text(json.dumps(result['bundle'],ensure_ascii=False,indent=2),'utf-8')
                self.notice.setText('已导出。含原始文字及训练样本，请自行审阅；没有自动训练或上传。')
            except OSError:
                self.notice.setText('无法写入文件，请选择有写入权限的位置')
        self.call('GET','dialogue/archive/export?'+urlencode(query),done=done)

    def restore(self):
        query=self.query()
        if not query['target']:return
        path,_=QFileDialog.getOpenFileName(self,'导入本地对话','','JSON (*.json)')
        if not path:return
        try:
            if Path(path).stat().st_size>64*1024*1024:raise ValueError('文件超过 64MB')
            bundle=json.loads(Path(path).read_text('utf-8-sig'))
        except (OSError,ValueError) as exc:
            self.notice.setText('导入失败：'+str(exc));return
        answer=QMessageBox.question(self,'选择导入方式','是：合并，保留已有记录。\n否：覆盖当前选定会话的本地对话记忆。\n取消：不导入。',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No|QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer==QMessageBox.StandardButton.Cancel:return
        self.call('POST','dialogue/archive/import',dict(query,account=self.account,bundle=bundle,
            mode='merge' if answer==QMessageBox.StandardButton.Yes else 'replace'),done=lambda result:self.load(True))

    def delete(self,clear):
        query=self.query()
        if not query['target'] or not clear and not self.selected.currentData():return
        if QMessageBox.question(self,'删除本地记忆','确认清空当前会话的全部对话记忆？' if clear else '确认删除选定记录？',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        self.call('POST','dialogue/archive/delete',dict(query,account=self.account,clear=clear,
            key=self.selected.currentData()),done=lambda result:self.load(True))
