"""工作台附件预览；解析结果先展示，用户选择后才随对话提交模型。"""
import base64
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QListWidget,QListWidgetItem,QPlainTextEdit,QFileDialog)


class AttachmentsDialog(QDialog):
    def __init__(self,request,selected=None,parent=None):
        super().__init__(parent)
        self.request=request;self.chosen=list(selected or []);self._epoch=0;self._busy=False
        self.setWindowTitle('Agent 文件与语音 · 先预览再发送');self.resize(760,620)
        self.setStyleSheet('QDialog {background:#F4F6F9;color:#182C43;} QPlainTextEdit,QListWidget {background:white;color:#182C43;}')
        layout=QVBoxLayout(self)
        tip=QLabel('上传txt/md/csv/json、文字PDF、docx或音频，单文件8MB。语音使用已安装的本地模型转写。'
            '勾选最多3份；点击工作台发送后，提取文字才会交给所选API/DSH。扫描PDF暂不支持。')
        tip.setWordWrap(True);layout.addWidget(tip)
        self.items=QListWidget();self.items.currentItemChanged.connect(self.preview);layout.addWidget(self.items,1)
        self.text=QPlainTextEdit();self.text.setReadOnly(True);layout.addWidget(self.text,1)
        self.status=QLabel('正在读取附件库…');self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        row=QHBoxLayout()
        self.upload=QPushButton('上传文件或语音…');self.upload.clicked.connect(self.pick);row.addWidget(self.upload)
        self.remove=QPushButton('删除选中附件');self.remove.clicked.connect(self.delete);row.addWidget(self.remove)
        self.use=QPushButton('附加到本轮');self.use.clicked.connect(self.choose);row.addWidget(self.use)
        close=QPushButton('取消');close.clicked.connect(self.reject);row.addWidget(close);layout.addLayout(row)
        self.reload()

    def call(self,method,path,body,done,timeout=15):
        self._epoch+=1;epoch=self._epoch;self._busy=True
        for button in (self.upload,self.remove,self.use):button.setEnabled(False)
        def completed(data):
            if epoch!=self._epoch:return
            self._busy=False
            for button in (self.upload,self.remove,self.use):button.setEnabled(True)
            if not data.get('success'):
                self.status.setText(str(data.get('detail') or data.get('message') or '附件处理失败'))
                return
            done(data)
        self.request(self,method,path,body,timeout=timeout,on_done=completed)

    def reload(self):
        def loaded(data):
            self.items.clear();self.text.clear()
            for r in data.get('items') or []:
                label=r['name']+f" · {r.get('chars',0)}字"+('（节选）' if r.get('truncated') else '')
                item=QListWidgetItem(label)
                # 列表宽度不够时文件名会被省略号截断，悬停要能看到完整文件名。
                item.setToolTip(r['name'])
                item.setData(Qt.ItemDataRole.UserRole,r)
                item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if r['id'] in [x['id'] for x in self.chosen] else Qt.CheckState.Unchecked)
                self.items.addItem(item)
            if self.items.count():
                self.status.setText(f'本地主人附件 {self.items.count()}/50份；勾选需要引用的资料。')
            else:
                self.status.setText('附件库还是空的：点“上传文件或语音…”添加，单文件 8MB，最多保留 50 份。')
        self.call('GET','agent/workspace/attachments',None,loaded)

    def preview(self,item,*_):
        self.text.setPlainText((item.data(Qt.ItemDataRole.UserRole) or {}).get('text','') if item else '')

    def pick(self):
        if self._busy:return
        path,_=QFileDialog.getOpenFileName(self,'上传给Agent的资料','','资料与语音 (*.txt *.md *.csv *.json *.pdf *.docx *.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.webm)')
        if not path:return
        try:
            file=Path(path)
            if not 0<file.stat().st_size<=8*1024*1024:raise ValueError('文件为空或超过8MB')
            body=dict(name=file.name,data_base64=base64.b64encode(file.read_bytes()).decode('ascii'))
        except (OSError,ValueError) as exc:
            self.status.setText(str(exc));return
        self.status.setText('正在本地解析或转写；尚未发送给模型…')
        def uploaded(data):
            row=data['item']
            if row['id'] not in [r['id'] for r in self.chosen] and len(self.chosen)<3:self.chosen.append(row)
            self.reload()
        self.call('POST','agent/workspace/attachments',body,uploaded,timeout=75)

    def choose(self):
        if self._busy:return
        selected=[self.items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.items.count())
                  if self.items.item(i).checkState()==Qt.CheckState.Checked]
        if len(selected)>3:self.status.setText('每轮最多附加3份资料，请取消多选项。');return
        self.chosen=selected;self.accept()

    def delete(self):
        if self._busy or not self.items.currentItem():return
        row=self.items.currentItem().data(Qt.ItemDataRole.UserRole)
        from PyQt6.QtWidgets import QMessageBox
        if QMessageBox.question(self,'删除附件','删除本地副本和提取文字？已保存的聊天原文仍保留。')!=QMessageBox.StandardButton.Yes:return
        def deleted(data):
            self.chosen=[r for r in self.chosen if r['id']!=row['id']];self.reload()
        self.call('DELETE','agent/workspace/attachments/'+row['id'],None,deleted)

    def done(self,result):
        self._epoch+=1
        super().done(result)
