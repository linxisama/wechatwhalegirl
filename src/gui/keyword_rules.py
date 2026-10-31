"""Row editor for keyword rules; preserves order and validates before saving."""
import json
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,
                             QTextEdit,QTableWidget,QHeaderView,QAbstractItemView,QFileDialog,
                             QMessageBox)
from ..keyword_rules import read_rule_rows,write_rule_rows,validate_rule_rows


def parse_imported(text):
    """把导入文件内容读成规则行；兼容三种写法（都是"关键词 + 回复"）。

    支持：①本程序自己导出的文本行 `关键词=回复`；②`{"format":..,"rules":[{keyword,reply}]}`；
    ③直接一个数组 `[{"keyword":..,"reply":..}]` 或 `[["关键词","回复"]]`。
    为什么要兼容这么多：用户从别处（QQ 版、表格、旧文档）拿来的多半不是我们的格式。
    """
    raw = str(text or '').strip().lstrip('\ufeff')
    if not raw:
        raise ValueError('文件是空的')
    if raw.startswith('{') or raw.startswith('['):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f'不是合法的 JSON：{exc}') from None
        rows = data.get('rules') if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError('JSON 里没有 rules 数组')
        result = []
        for item in rows:
            if isinstance(item, dict):
                result.append((str(item.get('keyword') or ''), str(item.get('reply') or '')))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                result.append((str(item[0]), str(item[1])))
            else:
                raise ValueError('规则项需要是 {keyword,reply} 或 [关键词, 回复]')
        return validate_rule_rows(result)
    # 文本行：`关键词=回复`（没有等号的行按"关键词 | 回复"再试一次）
    result = []
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if '=' in line:
            key, _, reply = line.partition('=')
        elif '|' in line:
            key, _, reply = line.partition('|')
        else:
            raise ValueError(f'第 {len(result) + 1} 行没有 "=" 或 "|"，无法分辨关键词和回复')
        result.append((key, reply))
    return validate_rule_rows(result)


class KeywordRulesEditor(QWidget):
    def __init__(self,value='',parent=None):
        super().__init__(parent)
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        toolbar=QHBoxLayout()
        self.add_button=QPushButton('＋ 新增规则')
        self.example_button=QPushButton('填入示例')
        self.import_button=QPushButton('导入…')
        self.import_button.setToolTip('从 JSON 或文本文件批量导入（关键词=回复；也认 {rules:[...]} 与 [[关键词,回复]]）')
        self.export_button=QPushButton('导出…')
        self.export_button.setToolTip('把当前规则导出成 UTF-8 文本/JSON，方便备份或换机器')
        for button in (self.add_button,self.example_button):
            button.setStyleSheet('background:#2FA8A0;color:white;border:none;border-radius:7px;padding:8px 14px;font-weight:bold;')
            toolbar.addWidget(button)
        for button in (self.import_button,self.export_button):
            button.setStyleSheet('background:#E8EDF3;color:#1F2733;border:1px solid #D3DAE3;border-radius:7px;padding:8px 12px;')
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.hint=QLabel('消息包含关键词就会触发；多条命中时优先回复上面的规则。回复可换行。')
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet('color:#5F6B7A;font-size:12px;')
        layout.addWidget(self.hint)
        self.table=QTableWidget(0,3)
        self.table.setHorizontalHeaderLabels(['关键词','回复内容','操作'])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(280)
        self.table.setStyleSheet('QTableWidget {background:white;border:1px solid #D3DAE3;border-radius:6px;gridline-color:#E8EDF3;} QHeaderView::section {background:#E8EDF3;color:#1F2733;border:none;padding:8px;font-weight:bold;}')
        header=self.table.horizontalHeader()
        header.setSectionResizeMode(0,QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2,QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0,150)
        self.table.setColumnWidth(2,118)
        layout.addWidget(self.table,1)
        self.add_button.clicked.connect(lambda:self.add_row())
        self.example_button.clicked.connect(self.add_examples)
        self.import_button.clicked.connect(self.import_rules)
        self.export_button.clicked.connect(self.export_rules)
        self.set_rules(value)

    # ── 导入 / 导出 ────────────────────────────────────────────────────────
    def import_rules(self):
        """导入时**先解析校验、再一次性替换**：坏文件不能把现有规则弄坏。"""
        path,_=QFileDialog.getOpenFileName(self,'导入关键词规则','',
                                           '规则文件 (*.txt *.json);;所有文件 (*)')
        if not path:
            return
        try:
            text=Path(path).read_text('utf-8-sig')
            rows=parse_imported(text)
        except (OSError,ValueError) as exc:
            QMessageBox.warning(self,'导入失败',str(exc))
            return
        if not rows:
            QMessageBox.information(self,'没有规则','文件里没有可导入的规则（空行和 # 注释会被忽略）。')
            return
        answer=QMessageBox.question(
            self,'确认导入',
            f'将导入 {len(rows)} 条规则，并**替换**当前表格里的内容（保存后才写入配置）。继续吗？',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer!=QMessageBox.StandardButton.Yes:
            return
        self.set_rows(rows)

    def export_rules(self):
        rows=self.rows()
        if not rows:
            QMessageBox.information(self,'没有规则','当前没有可导出的规则。')
            return
        path,_=QFileDialog.getSaveFileName(self,'导出关键词规则','关键词规则.txt',
                                           '文本 (*.txt);;JSON (*.json)')
        if not path:
            return
        try:
            if path.lower().endswith('.json'):
                body=json.dumps({'format':'keyword-rules-1',
                                 'rules':[{'keyword':key,'reply':reply} for key,reply in rows]},
                                ensure_ascii=False,indent=1)
            else:
                body='\n'.join(f'{key}={reply}'.replace('\n','\\n') for key,reply in rows)
            Path(path).write_text(body,'utf-8')
        except OSError as exc:
            QMessageBox.warning(self,'导出失败',str(exc))
            return
        QMessageBox.information(self,'已导出',f'已导出 {len(rows)} 条规则到：\n{path}')

    def rows(self):
        return [(self.table.cellWidget(row,0).text(),self.table.cellWidget(row,1).toPlainText())
                for row in range(self.table.rowCount())]

    def set_rules(self,value):
        try:
            rows=read_rule_rows(value)
        except ValueError as exc:
            rows=[(str(value),'')]
            self.hint.setText(str(exc)+'；原始内容已保留在关键词栏，修正或删除后再保存。')
        self.set_rows(rows)

    def set_rows(self,rows):
        self.table.setRowCount(0)
        for keyword,reply in rows or [('', '')]:
            self.add_row(keyword,reply,focus=False)

    def add_row(self,keyword='',reply='',focus=True):
        row=self.table.rowCount()
        self.table.insertRow(row)
        key=QLineEdit(keyword)
        key.setMaximumHeight(42)
        key.setPlaceholderText('例如：你好')
        key.setStyleSheet('background:white;color:#1F2733;border:1px solid #D3DAE3;border-radius:5px;padding:7px;')
        response=QTextEdit()
        response.setAcceptRichText(False)
        response.setPlainText(reply)
        response.setPlaceholderText('例如：你好呀～今天过得怎么样？')
        response.setStyleSheet('background:white;color:#1F2733;border:1px solid #D3DAE3;border-radius:5px;padding:7px;')
        self.table.setCellWidget(row,0,key)
        self.table.setCellWidget(row,1,response)
        actions=QWidget()
        buttons=QHBoxLayout(actions)
        buttons.setContentsMargins(3,3,3,3)
        buttons.setSpacing(3)
        for label,tip,handler in [('↑','提高优先级',lambda checked=False,r=row:self.move_row(r,-1)),
                                  ('↓','降低优先级',lambda checked=False,r=row:self.move_row(r,1)),
                                  ('删除','删除这条规则',lambda checked=False,r=row:self.remove_row(r))]:
            button=QPushButton(label)
            button.setToolTip(tip)
            button.setStyleSheet('background:#E8EDF3;color:#1F2733;border:none;border-radius:5px;padding:6px 4px;')
            button.clicked.connect(handler)
            buttons.addWidget(button)
        self.table.setCellWidget(row,2,actions)
        self.table.setRowHeight(row,100)
        if focus:
            self.table.scrollToBottom()
            key.setFocus()

    def remove_row(self,row):
        rows=self.rows()
        rows.pop(row)
        self.set_rows(rows)

    def move_row(self,row,direction):
        target=row+direction
        rows=self.rows()
        if not 0<=target<len(rows):
            return
        rows[row],rows[target]=rows[target],rows[row]
        self.set_rows(rows)
        self.table.cellWidget(target,0).setFocus()

    def add_examples(self):
        rows=[row for row in self.rows() if row[0].strip() or row[1].strip()]
        seen={key.strip().lower() for key,_ in rows}
        rows.extend((key,reply) for key,reply in [('你好','你好呀～今天过得怎么样？'),('晚安','晚安，早点休息～')]
                    if key.lower() not in seen)
        self.set_rows(rows)

    def value(self):
        return write_rule_rows(self.rows())
