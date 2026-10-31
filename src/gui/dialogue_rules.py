"""对话页四个独立规则入口，使用后台发布的可编辑字段。"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QScrollArea,
    QWidget,QGroupBox,QFormLayout,QCheckBox,QSpinBox,QDoubleSpinBox,QComboBox,QLineEdit,QPlainTextEdit)


class NameListEditor(QWidget):
    """一人一框：每个外号/口令一个输入框，可加可删。

    `aliases` 原来是"逗号分隔一长串"，用户不知道在哪断句；拆成一行一个框后，
    填一行就等于一个名字，不用记分隔符。
    """

    def __init__(self, value=''):
        super().__init__()
        self.rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.boxes = QVBoxLayout()
        layout.addLayout(self.boxes)
        buttons = QHBoxLayout()
        add = QPushButton('＋ 添加一个')
        remove = QPushButton('－ 删除最后一个')
        for button in (add, remove):
            button.setMaximumWidth(140)
        add.clicked.connect(lambda: self._append(''))
        remove.clicked.connect(self._remove_last)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        raw = str(value or '').replace('，', ',').replace('\n', ',')
        for name in [item.strip() for item in raw.split(',')] or ['']:
            self._append(name)

    def _append(self, text):
        box = QLineEdit(str(text or ''))
        box.setPlaceholderText('一个被叫的名字，例如：鲸鱼娘 / 小夜 / 夜夜')
        self.boxes.addWidget(box)
        self.rows.append(box)

    def _remove_last(self):
        if len(self.rows) <= 1:
            if self.rows:
                self.rows[0].clear()
            return
        box = self.rows.pop()
        self.boxes.removeWidget(box)
        box.deleteLater()

    def names(self):
        return [box.text().strip() for box in self.rows if box.text().strip()]

    def text(self):
        return ','.join(self.names())

    def toPlainText(self):
        return self.text()


class DialogueRulesDialog(QDialog):
    def __init__(self,request,group,parent=None,on_saved=None):
        super().__init__(parent)
        self.request=request;self.group=group;self.on_saved=on_saved;self.widgets={};self._saving=False
        self.setWindowTitle('对话规则');self.resize(820,720)
        self.setStyleSheet('QDialog {background:#F4F7FA;}')
        layout=QVBoxLayout(self)
        self.help=QLabel('正在读取规则…');self.help.setWordWrap(True);layout.addWidget(self.help)
        scroll=QScrollArea();scroll.setWidgetResizable(True)
        self.holder=QWidget();self.form=QVBoxLayout(self.holder);scroll.setWidget(self.holder)
        layout.addWidget(scroll,1)
        self.notice=QLabel('');self.notice.setWordWrap(True);self.notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        row=QHBoxLayout();self.save=QPushButton('保存规则');self.save.setEnabled(False);self.save.clicked.connect(self.save_rules)
        row.addWidget(self.save)
        reload=QPushButton('重新读取');reload.clicked.connect(self.load);row.addWidget(reload)
        close=QPushButton('关闭');close.clicked.connect(self.reject);row.addWidget(close);layout.addLayout(row)
        self.load()

    def load(self):
        if self._saving:
            return
        self.save.setEnabled(False)
        self.request(self,'GET','dialogue/rules',timeout=10,on_done=self.loaded)

    def loaded(self,result):
        group=(result.get('groups') or {}).get(self.group)
        if not result.get('success') or not group:
            self.notice.setText(str(result.get('message') or '规则读取失败，请点击重新读取'))
            return
        self.setWindowTitle(group['label']);self.help.setText(group['help'])
        while self.form.count():
            item=self.form.takeAt(0)
            if item.widget():item.widget().deleteLater()
        self.widgets={}
        for plugin in group['plugins']:
            box=QGroupBox(plugin['name']);form=QFormLayout(box)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
            form.setVerticalSpacing(9)
            for key,schema in plugin['schema'].items():
                value=plugin['settings'].get(key);kind=schema['type']
                if kind=='bool':
                    widget=QCheckBox(schema.get('label',key));widget.setChecked(bool(value))
                elif kind in ('int','float'):
                    widget=QSpinBox() if kind=='int' else QDoubleSpinBox()
                    widget.setRange(schema.get('min',0),schema.get('max',100000))
                    widget.setValue(value or 0)
                elif kind=='select':
                    widget=QComboBox()
                    for option in schema.get('options') or [{'value':v,'label':v} for v in schema.get('values',[])]:
                        widget.addItem(option['label'],option['value'])
                    widget.setCurrentIndex(max(0,widget.findData(value)))
                elif (plugin['id']=='chat_controls' and key=='aliases') or \
                     (plugin['id']=='sticker_steal' and key=='blockedSenders'):
                    # 一人一框：比"逗号分隔"直观得多
                    # 一人一框：比"逗号分隔"直观得多
                    widget=NameListEditor(str(value or ''))
                elif schema.get('multiline'):
                    widget=QPlainTextEdit(str(value or ''));widget.setMaximumHeight(90)
                else:
                    widget=QLineEdit(str(value or ''))
                widget.setAccessibleName(plugin['id']+'.'+key)
                widget.setToolTip(schema.get('description',''))
                if kind=='bool':
                    form.addRow(widget)
                else:
                    label=QLabel(schema.get('label',key));label.setWordWrap(True)
                    form.addRow(label,widget)
                if schema.get('description'):
                    note=QLabel(schema['description']);note.setWordWrap(True);note.setStyleSheet('color:#617286;font-size:11px;')
                    form.addRow(note)
                self.widgets[(plugin['id'],key)]=(widget,kind)
            self.form.addWidget(box)
        self.form.addStretch(1)
        self.form.activate();self.holder.adjustSize();self.holder.updateGeometry()
        self.save.setEnabled(True)
        self.notice.setText('修改后点击“保存规则”，立即生效并在重启后保留。')

    def payload(self):
        result={}
        for (pid,key),(widget,kind) in self.widgets.items():
            value=(widget.isChecked() if kind=='bool' else widget.value() if kind in ('int','float') else
                   widget.currentData() if kind=='select' else
                   (widget.text() if isinstance(widget,NameListEditor) else
                    widget.toPlainText() if isinstance(widget,QPlainTextEdit) else widget.text()))
            result.setdefault(pid,{})[key]=value
        return {'group':self.group,'plugins':result}

    def save_rules(self):
        if self._saving or not self.widgets:
            return
        self._saving=True;self.save.setEnabled(False);self.holder.setEnabled(False);self.notice.setText('正在保存规则…')
        def done(result):
            self._saving=False;self.save.setEnabled(True);self.holder.setEnabled(True)
            self.notice.setText(str(result.get('message') or '后台未返回结果'))
            if result.get('success') and self.on_saved:
                self.on_saved()
        self.request(self,'POST','dialogue/rules',self.payload(),timeout=60,on_done=done)

    def reject(self):
        if not self._saving:
            super().reject()

    def closeEvent(self,event):
        if self._saving:
            event.ignore()
        else:
            super().closeEvent(event)
