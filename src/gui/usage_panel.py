"""只读的用量与记忆巡检（1.3.84）：看统计，不改记忆、不发消息、不花钱。"""
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QTextEdit

CODE_TEXT={'SECRET':'疑似包含密钥/口令','RELATIVE_TIME':'含相对时间词','DIARY_SMELL':'像日记口吻',
           'DUP':'与已存记忆几乎重复','TOO_VAGUE':'信息太薄'}


def _stamp(value):
    """时间戳转成本地可读；坏值一律显示“未知”，绝不抛。"""
    if value in (None,''):return '无'
    try:
        stamp=float(value)
        if stamp<=0:return '无'
        import time
        return time.strftime('%m-%d %H:%M',time.localtime(stamp))
    except Exception:
        return '未知'


class UsagePanel(QWidget):
    def __init__(self,request):
        super().__init__();self.request=request;self.busy=False;self._waiting=0;self._usage={};self._guard={};self._items=[]
        box=QVBoxLayout(self)
        hint=QLabel('这里只读：用量是按字符估算的每轮 token（只覆盖本机账本，不等于服务商账单）；巡检是记忆写入前的自检记录（只报告，不拦截）。两个文件都在本机 data/wechat-only/ 下，刷新不会修改记忆或配置，也不会调用模型。')
        hint.setWordWrap(True);box.addWidget(hint)
        row=QHBoxLayout();self.refresh_button=QPushButton('刷新');self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button);row.addStretch(1);box.addLayout(row)
        self.usage=QLabel('用量：尚未读取');self.usage.setWordWrap(True);box.addWidget(self.usage)
        self.guard=QLabel('巡检：尚未读取');self.guard.setWordWrap(True);box.addWidget(self.guard)
        self.text=QTextEdit();self.text.setReadOnly(True);box.addWidget(self.text,1)
        self.notice=QLabel('');self.notice.setWordWrap(True);box.addWidget(self.notice)
        self.timer=QTimer(self);self.timer.setInterval(15000);self.timer.timeout.connect(self.refresh)
        self.apply_theme({'panel_transparency':.35})

    def apply_theme(self,cfg):
        alpha=int(255*(1-float(cfg.get('panel_transparency',.35))))
        self.text.setStyleSheet(f'background: rgba(255,255,255,{alpha}); border: 1px solid #C8D4E2;')

    def showEvent(self,event):
        super().showEvent(event);self.timer.start();self.refresh()

    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)

    def refresh(self,*_):
        """两个接口各取一次，都回来才渲染，避免半截数字。"""
        if self.busy:return
        self.busy=True;self._waiting=2;self._usage={};self._guard={};self._items=[]
        self.refresh_button.setEnabled(False);self.refresh_button.setText('正在刷新…');self.notice.setText('')
        self._get('usage/summary',self._take_usage);self._get('memory/guard',self._take_guard)

    def _get(self,path,done):
        def finished(value):
            data=value if isinstance(value,dict) else {}
            if data.get('ok') or data.get('success'):
                try:done(data)
                except Exception as exc:self.notice.setText(f'展示失败：{exc}')
            else:self.notice.setText(str(data.get('message') or data.get('detail') or '读取失败，请检查本机后台'))
            self._waiting-=1
            if self._waiting<=0:self._finish()
        try:self.request(self,'GET',path,timeout=10,on_done=finished)
        except Exception as exc:
            self.notice.setText(f'读取失败：{exc}');self._waiting=0;self._finish()

    def _finish(self):
        self.busy=False;self.refresh_button.setEnabled(True);self.refresh_button.setText('刷新');self.render()

    def _take_usage(self,data):self._usage=dict(data.get('usage') or {})
    def _take_guard(self,data):
        self._guard=dict(data.get('summary') or {});self._items=list(data.get('items') or [])

    def render(self):
        usage=self._usage or {}
        if usage:
            source=usage.get('by_source') or {}
            self.usage.setText(f"最近 {usage.get('days',7)} 天：{usage.get('rounds',0)} 轮 · 提示约 {usage.get('prompt_tokens',0)} tok · 回复约 {usage.get('reply_tokens',0)} tok"
                f"（DSH {source.get('dsh',0)} 轮 / 本地兜底 {source.get('local',0)} 轮）；最后一条：{_stamp(usage.get('last_ts'))}")
        else:self.usage.setText('用量：账本为空，还没有回复过，或账本文件未生成。')
        summary=self._guard or {}
        if summary:
            codes=summary.get('codes') or {}
            detail='、'.join(f"{code}×{count}" for code,count in sorted(codes.items())) or '无'
            self.guard.setText(f"巡检：共 {summary.get('total',0)} 条写入前报告 · 需留意 {summary.get('warn',0)} 条 · 判定码 {detail}"
                f"；模式 {summary.get('mode','report')}（只报告，不拦截写入）；最后一条：{_stamp(summary.get('last_ts'))}")
        else:self.guard.setText('巡检：还没有记录。记忆都是从聊天里正常提炼出来的。')
        lines=[]
        for item in (self._items or [])[:20]:
            codes=item.get('codes') or []
            reason='、'.join(CODE_TEXT.get(code,code) for code in codes) or '需留意'
            extra=''
            if item.get('duplicate_of'):extra+=f"；与已存记忆重复度 {item.get('similarity','?')}"
            elif item.get('similarity'):extra+=f"；相似度 {item.get('similarity')}"
            lines.append(f"{_stamp(item.get('ts'))} · {reason}{extra}\n  来源 {item.get('source') or '未知'} · 范围 {item.get('scope') or '-'} · {item.get('text') or ''}")
        if not lines and self._guard:lines.append('最近没有需要留意的写入。')
        self.text.setPlainText('\n\n'.join(lines))
