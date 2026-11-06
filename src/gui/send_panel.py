"""只读的发送诊断（1.3.85）：看最近发送阶段、失败原因与耗时。

数据来源（1.3.88）：阶段/失败读按天保存的事件文件（保留 7 天），耗时仍读后台日志尾部——
backend.log 满 5MB 就轮转，只读日志尾部会把十几分钟前的失败冲掉。
边界：只发一个 GET，不重发、不清草稿、不改任何设置；日志里的会话名本来就是打码的。
"""
from PyQt6.QtCore import Qt,QTimer
from PyQt6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QTextEdit


def _stamp(ts):
    """日志时间戳只取到分钟；坏值显示「未知」。"""
    text=str(ts or '')
    return text[5:16] if len(text)>=16 else (text or '未知')


def _short(value,limit=12):
    text=str(value or '')
    return text if len(text)<=limit else text[:limit]+'…'


class SendPanel(QWidget):
    def __init__(self,request):
        super().__init__()
        self.request=request
        self.busy=False
        self.data={}
        box=QVBoxLayout(self)
        hint=QLabel('这里只读：最近 50 条发送记录（阶段 / 失败 / 耗时），取自本机按天保存的发送记录（保留 7 天）；'
                    '不会重发、不清草稿、不改设置。日志里的会话名与标题本来就是打码的。')
        hint.setWordWrap(True)
        box.addWidget(hint)
        row=QHBoxLayout()
        self.refresh_button=QPushButton('刷新')
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        row.addStretch(1)
        box.addLayout(row)
        self.summary=QLabel('概览：尚未读取')
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.summary)
        self.failure=QLabel('最近一次失败：尚未读取')
        self.failure.setWordWrap(True)
        # 失败说明要能选中复制：用户报障时抄的就是这段失败码与建议。
        self.failure.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.failure)
        self.text=QTextEdit()
        self.text.setReadOnly(True)
        box.addWidget(self.text,1)
        self.notice=QLabel('')
        self.notice.setWordWrap(True)
        box.addWidget(self.notice)
        self.timer=QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(self.refresh)
        self.apply_theme({'panel_transparency':.35})

    def apply_theme(self,cfg):
        cfg=cfg if isinstance(cfg,dict) else {}
        alpha=int(255*(1-float(cfg.get('panel_transparency',.35))))
        self.text.setStyleSheet(f'background: rgba(255,255,255,{alpha}); border: 1px solid #C8D4E2;')

    def showEvent(self,event):
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self,event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self,*_):
        if self.busy:
            return
        self.busy=True
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText('正在刷新…')
        self.notice.setText('')
        try:
            self.request(self,'GET','send/history?limit=50',timeout=10,on_done=self._done)
        except Exception as exc:
            self.notice.setText(f'读取失败：{exc}')
            self.busy=False
            self.refresh_button.setEnabled(True)
            self.refresh_button.setText('刷新')

    def _done(self,value):
        data=value if isinstance(value,dict) else {}
        if not (data.get('ok') or data.get('success')):
            self.notice.setText(str(data.get('message') or data.get('detail') or '读取失败，请检查本机后台'))
        else:
            self.data=data
            try:
                self.render()
            except Exception as exc:
                self.notice.setText(f'展示失败：{exc}')
        self.busy=False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText('刷新')

    def lines(self):
        data=self.data if isinstance(self.data,dict) else {}
        items=data.get('items') or []
        out=[]
        timing=data.get('last_timing')
        if isinstance(timing,dict) and timing:
            metrics=timing.get('metrics') or {}
            parts=' '.join(f'{k}={v}' for k,v in metrics.items() if k!='total_ms')
            out.append('最近一次耗时：%s · %s 总计 %sms%s' % (_stamp(timing.get('ts')),timing.get('scope') or '',
                timing.get('total_ms') or 0,('（'+parts+'）') if parts else ''))
        for row in items:
            if not isinstance(row,dict):
                continue
            kind=row.get('kind')
            if kind=='phase':
                out.append('%s 阶段 %s · %s · %s 字 · %s%s' % (_stamp(row.get('ts')),row.get('phase') or '',
                    _short(row.get('who')),row.get('len') or 0,row.get('channel') or '',
                    (' · '+str(row.get('reason'))) if row.get('reason') else ''))
            elif kind=='failed':
                out.append('%s 失败 %s · %s · %s 字 · pid %s' % (_stamp(row.get('ts')),row.get('reason') or '',
                    _short(row.get('who')),row.get('len') or 0,row.get('pid') or ''))
            else:
                out.append('%s 耗时 %s · %s' % (_stamp(row.get('ts')),row.get('scope') or '',
                    ' '.join(f'{k}={v}' for k,v in (row.get('metrics') or {}).items())))
        if not items:
            out.append('还没有记录：发一条消息后再回来看。')
        return out

    def render(self):
        data=self.data if isinstance(self.data,dict) else {}
        counts=data.get('counts') or {}
        codes=data.get('failure_codes') or {}
        extra=''
        if codes:
            extra='；失败码 '+'、'.join(f'{k}×{v}' for k,v in sorted(codes.items()))
        self.summary.setText('概览：阶段 %d 条 · 失败 %d 条 · 耗时 %d 条%s' % (
            int(counts.get('phases') or 0),int(counts.get('failures') or 0),
            int(counts.get('timings') or 0),extra))
        last=data.get('last_failure')
        if isinstance(last,dict) and last:
            text='最近一次失败：%s · %s（%s）· 会话 %s' % (_stamp(last.get('ts')),
                last.get('text') or last.get('reason') or '未知原因',last.get('reason') or '未知码',
                _short(last.get('who')))
            if last.get('advice'):
                text+='\n建议：'+str(last.get('advice'))
        else:
            text='最近一次失败：无（最近 7 天的发送记录里没有失败）'
        self.failure.setText(text)
        # 悬停给出整段失败说明，方便用户一次抄全。注意 who 在写记录时就被
        # mask_chat_name 截成 8 字 + …（src/logging_setup.py:548-553），界面拿不到完整会话名，
        # 所以只能如实写"记录里的会话名"。
        raw=str(last.get('who') or '') if isinstance(last,dict) else ''
        self.failure.setToolTip(text+(('\n记录里的会话名：'+raw) if raw else ''))
        self.text.setPlainText('\n'.join(self.lines()))
        self.notice.setText('已更新（只读）')
