"""本机DSH会话接入，和管理员凭据握手区别展示。"""
import json
import time
from urllib.parse import urlsplit

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QGuiApplication
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout)

from ..runtime_identity import APP_VERSION

MISSING_PREFIX = '当前值（候选里没有）: '
MISSING_HINT = '当前值不在 DSH 的模型目录里，保存会保留原值；建议从下拉里重新选'
STATE_LABELS = {'ok': '可用', 'drift': '端口已变更', 'process_missing': '未找到桌面DSH进程',
                'port_unreachable': '端口没有响应', 'protocol_failed': '协议核验失败',
                'dsh_missing': '这台电脑没装桌面DSH', 'dsh_offline': '桌面DSH没在运行'}
BANNER_DRIFT = 'QFrame { background: #FFF3E0; border: 1px solid #FB8C00; border-radius: 6px; }'
BANNER_HINT = 'QFrame { background: #FFF8E1; border: 1px solid #E0C200; border-radius: 6px; }'
BANNER_OK = 'QFrame { background: #E8F5E9; border: 1px solid #66BB6A; border-radius: 6px; }'
DEFAULT_URL = 'http://127.0.0.1:3080'
ANNOUNCE_NONE = '尚无（在 DeepSeek Harness 新建会话并选择预设「鲸鱼娘工作台」后，控制台会自动重新发现地址）'
ANNOUNCE_RECONNECT = '在 DeepSeek Harness 新建会话并选择预设「鲸鱼娘工作台」，控制台会自动重新发现 DSH 地址'
# 只有这些 stage 说明端点已核验或正在走回合；其余（未检测/disabled/失败）都要给可操作提示。
LIVE_STAGES = frozenset({'ready', 'completed', 'connecting', 'submitted', 'waiting_turn',
                         'generating', 'finalizing'})


def _plain(value):
    return '—' if value is None or value == '' else str(value)


def _code_lines(data):
    """把后端给的连接码渲染成两行中文；缺字段就少一行，绝不让界面因为码崩掉。"""
    if not isinstance(data, dict):
        return []
    code = str(data.get('code') or '').strip()
    if not code:
        return []
    text = str(data.get('code_text') or '').strip()
    hint = str(data.get('code_hint') or '').strip()
    lines = ['连接码：' + code + ('（' + text + '）' if text else '')]
    if hint:
        lines.append('下一步：' + hint)
    return lines


def normalize_url(text):
    """返回（可保存地址, 错误文案）。本机只用 http，其它协议一律不提交。"""
    value = (text or '').strip()
    if not value:
        return '', '请填写 DSH 地址，例如 http://127.0.0.1:13421'
    if '://' not in value:
        value = 'http://' + value
    scheme, _, rest = value.partition('://')
    if scheme.lower() != 'http':
        return '', '地址需以 http:// 开头（本机只支持 http）'
    rest = rest.strip()
    if not rest or rest.startswith('/') or any(char in rest for char in ' \t\r\n'):
        return '', '地址缺少主机和端口，例如 http://127.0.0.1:13421'
    return value.rstrip('/'), ''


def _port_of(url):
    """只用于横幅展示：取不到端口就返回空串。"""
    try:
        return str(urlsplit(str(url)).port or '')
    except (ValueError, AttributeError):
        return ''


def _clock(stamp):
    """unix 秒 → 本地时间 HH:MM:SS；缺失或非法一律 —，不抛异常。"""
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return '—'
    try:
        return time.strftime('%H:%M:%S', time.localtime(stamp))
    except (OSError, OverflowError, ValueError):
        return '—'


def announce_line(record):
    """「DSH 主动上报」的只读三态文案；只认 connected/failed，字段缺失退化成 —，不抛异常。"""
    state = str(record.get('state') or '') if isinstance(record, dict) else ''
    if state == 'failed':
        return '最近 ' + _clock(record.get('at')) + ' · 失败：' + _plain(record.get('message'))
    if state == 'connected':
        return '最近 ' + _clock(record.get('at')) + ' · 已连 ' + _plain(record.get('base_url'))
    return ANNOUNCE_NONE


class _ChoiceCombo(QComboBox):
    """不可编辑的下拉，但保留 text()/setText() 语义，导入配置与既有调用点继续可用。"""

    def text(self):
        data = self.currentData()
        return '' if data is None else str(data)

    def setText(self, value):
        value = '' if value is None else str(value)
        if not value:
            return
        index = self.findData(value)
        if index < 0:
            # 候选外取值只能以“当前值”占位项存在，不新增自由文本输入。
            self.insertItem(0, MISSING_PREFIX + value, value)
            index = 0
        self.setCurrentIndex(index)


class DshDetailDialog(QDialog):
    """失败详情：只展示后端已解析字段，不回显任何凭据。"""

    def __init__(self, detail, message='', parent=None):
        super().__init__(parent)
        self.setWindowTitle('DSH失败详情')
        self.resize(680, 520)
        outer = QVBoxLayout(self)
        head = QLabel(message or '后端返回了失败详情，请按下方建议处理后重新检测。')
        head.setWordWrap(True); outer.addWidget(head)
        facts = QLabel('HTTP 状态码：' + _plain(detail.get('status')) + '\n'
                       'request id：' + _plain(detail.get('rpc_id')) + '\n'
                       '错误码：' + _plain(detail.get('code')))
        facts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        facts.setWordWrap(True); outer.addWidget(facts)
        outer.addWidget(QLabel('后端原始响应体（可选中复制）'))
        body = detail.get('body')
        if not isinstance(body, str):
            try:
                body = json.dumps(body, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                body = str(body)
        if len(body) > 20000:
            body = body[:20000] + '\n……（已截断）'
        self.body = QPlainTextEdit(body or '（后端没有返回响应体）')
        self.body.setReadOnly(True)
        font = QFont('Consolas'); font.setStyleHint(QFont.StyleHint.Monospace)
        self.body.setFont(font)
        outer.addWidget(self.body, 1)
        hints = [str(item).strip() for item in (detail.get('hints') or []) if str(item).strip()][:20]
        outer.addWidget(QLabel('建议：\n' + ('\n'.join('· ' + hint for hint in hints) if hints
            else '· 没有可用的建议，请核对 DSH 是否已启动并登录。')))
        close = QPushButton('关闭'); close.clicked.connect(self.accept); outer.addWidget(close)


class DshDialog(QDialog):
    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = request
        self._operation_epoch = 0
        self._saved = {'provider': '', 'model': ''}
        self._providers = []
        self._catalog_error = ''
        self._drift = None
        self._just_updated = False
        self._last_result = {}
        self.setWindowTitle('DSH对话引擎 · 独立微信会话')
        self.resize(760, 780)
        self.setStyleSheet('QDialog { background: #F4F6F9; color: #182C43; } '
            'QLineEdit, QComboBox, QSpinBox { background: #FFFFFF; color: #182C43; '
            'padding: 6px; min-height: 20px; border: 1px solid #D2DCEA; border-radius: 6px; }')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        self.banner = QFrame(); self.banner.setVisible(False)
        banner_row = QHBoxLayout(self.banner); banner_row.setContentsMargins(8, 6, 8, 6)
        self.banner_text = QLabel(''); self.banner_text.setWordWrap(True)
        self.banner_button = QPushButton('更新端口'); self.banner_button.setVisible(False)
        self.banner_button.clicked.connect(self.update_to_drift)
        banner_row.addWidget(self.banner_text, 1); banner_row.addWidget(self.banner_button)
        outer.addWidget(self.banner)
        hint = QLabel('默认沿用原API。启用后，已触发回复的消息交给本机DSH专用无工具人设生成，'
            '外发仍由微信原通道完成。各账号、群/私聊和人设独立会话；本地记忆继续检索。'
            '这是对话引擎接入，不能解决微信缺少输入控件或抢焦点。'
            '凭据与管理员绑定在『设置 → Agent 管理员与跨端凭据』面板。')
        hint.setWordWrap(True); outer.addWidget(hint)
        form = QFormLayout()
        self.enabled = QCheckBox('使用DSH生成微信回复（默认关闭）')
        self.url = QLineEdit(DEFAULT_URL)
        self.url.setPlaceholderText('例如 http://127.0.0.1:13421（每次启动端口可能不同）')
        self.url.editingFinished.connect(self.normalize_url)
        self.url.textEdited.connect(self._url_edited)
        self.url_error = QLabel(''); self.url_error.setWordWrap(True)
        self.url_error.setStyleSheet('color: #C0392B;')
        self.provider = _ChoiceCombo(); self.provider.setEditable(False)
        self.provider.currentIndexChanged.connect(self._on_provider_changed)
        self.model = _ChoiceCombo(); self.model.setEditable(False)
        self.catalog_hint = QLabel(''); self.catalog_hint.setWordWrap(True)
        self.catalog_hint.setStyleSheet('color: #C0392B;')
        self.effort = QComboBox()
        for title, value in [('模型默认', ''), ('low（较快）', 'low'), ('high', 'high'), ('max', 'max')]:
            self.effort.addItem(title, value)
        self.profile = QComboBox(); self.profile.addItems(['desktop', 'web', 'web-desktop'])
        self.timeout = QSpinBox(); self.timeout.setRange(5, 120); self.timeout.setSuffix(' 秒')
        self.timeout.setValue(60)
        form.addRow('', self.enabled)
        form.addRow('DSH 地址（含 http://）', self.url)
        form.addRow('', self.url_error)
        form.addRow('DSH 提供商', self.provider)
        form.addRow('DSH 模型', self.model)
        form.addRow('', self.catalog_hint)
        form.addRow('思考强度', self.effort)
        form.addRow('实际profile', self.profile)
        form.addRow('回合等待上限', self.timeout); outer.addLayout(form)
        outer.addWidget(QLabel('发现的端点'))
        discover_row = QHBoxLayout()
        self.endpoints = QComboBox(); self.endpoints.setMinimumWidth(280)
        self.endpoints.addItem('自动查找后选择DSH进程 / 端口', '')
        self.endpoints.currentIndexChanged.connect(self.select_endpoint)
        self.discover_button = QPushButton('自动查找桌面DSH')
        self.discover_button.clicked.connect(self.discover)
        discover_row.addWidget(self.endpoints, 1); discover_row.addWidget(self.discover_button)
        outer.addLayout(discover_row)
        import_button = QPushButton('从qq-bridge的config.json读取接入参数…')
        import_button.clicked.connect(self.import_bridge)
        outer.addWidget(import_button)
        self.status = QLabel('先保存地址 → 安装专用人设 → 手动重启DSH → 检测。需要先绑定管理员。')
        self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.status)
        self.announce = QLabel('DSH 主动上报：' + ANNOUNCE_NONE)
        self.announce.setWordWrap(True)
        outer.addWidget(self.announce)
        tools_row = QHBoxLayout()
        self.detail_button = QPushButton('查看详情'); self.detail_button.setEnabled(False)
        self.detail_button.clicked.connect(self.show_detail)
        self.copy_button = QPushButton('复制诊断信息')
        self.copy_button.clicked.connect(self.copy_diagnostics)
        tools_row.addWidget(self.detail_button); tools_row.addWidget(self.copy_button)
        self.binding_button = QPushButton('打开接入凭据…')
        self.binding_button.clicked.connect(self.open_binding)
        tools_row.addWidget(self.binding_button)
        tools_row.addStretch(1); outer.addLayout(tools_row)
        row = QHBoxLayout()
        self.save_button = QPushButton('保存设置'); self.save_button.clicked.connect(self.save)
        self.preset_button = QPushButton('安装专用人设（先备份）'); self.preset_button.clicked.connect(self.install)
        self.probe_button = QPushButton('只读检测DSH'); self.probe_button.clicked.connect(self.probe)
        for button in (self.save_button, self.preset_button, self.probe_button):
            row.addWidget(button)
        outer.addLayout(row)
        self.connect_button = QPushButton('连接选中的DSH（核验后保存并启用）')
        self.connect_button.clicked.connect(self.connect_selected); outer.addWidget(self.connect_button)
        note = QLabel('DSH内的内部推理不转发到微信；只收完整最终答复。事件中断/超时不重新提交、'
            '不转旧API补问。学习保存在本地学习库，不修改模型权重，不自动执行电脑工具。')
        note.setWordWrap(True); outer.addWidget(note)
        close = QPushButton('关闭'); close.clicked.connect(self.accept); outer.addWidget(close)
        self._fill_providers()
        self._apply_validity()
        self.call('GET', 'dsh', None, self.load)
        # 候选目录延后到事件循环里取，构造期只发一次读取设置的请求。
        QTimer.singleShot(0, self._fetch_catalog)

    def normalize_url(self):
        """把输入框写成规范化地址；协议不对就红字提示并禁用保存/探测/连接。"""
        value, error = normalize_url(self.url.text())
        self.url_error.setText(error)
        if not error and self.url.text() != value:
            self.url.setText(value)
        self._apply_validity()
        return not error

    def _url_edited(self, *_):
        self.url_error.setText('')
        self._apply_validity()

    def _apply_validity(self):
        valid = not self.url_error.text()
        for button in (self.save_button, self.probe_button, self.connect_button):
            button.setEnabled(valid)

    def _fetch_catalog(self):
        """打开面板后拉一次真实候选；失败只提示，绝不清空或覆盖已保存值。"""
        def done(data):
            if not isinstance(data, dict):
                data = {'success': False, 'message': '模型目录返回内容无法识别，请关掉面板重开再试。'}
            failed = data.get('success') is False
            providers = [] if failed else (data.get('providers') or [])
            self._providers = [row for row in providers if isinstance(row, dict)]
            message = data.get('message')
            self._catalog_error = (str(message) if isinstance(message, str) and message.strip()
                else '读取DSH模型目录失败，请确认桌面DSH已启动并登录后重开面板。') if failed else ''
            self._fill_providers()
        self.request(self, 'GET', 'dsh/catalog', None, timeout=15, on_done=done)

    def _fill_providers(self):
        current = self.provider.text() or self._saved.get('provider') or ''
        ids = [str(row.get('id')) for row in self._providers if row.get('id')]
        self.provider.blockSignals(True)
        self.provider.clear()
        if current and current not in ids:
            self.provider.addItem(MISSING_PREFIX + current, current)
        for row in self._providers:
            provider_id = str(row.get('id') or '')
            if provider_id:
                self.provider.addItem(str(row.get('displayName') or provider_id), provider_id)
        if not self.provider.count():
            self.provider.addItem(MISSING_PREFIX + (current or '—'), current)
        self.provider.setCurrentIndex(max(0, self.provider.findData(current)))
        self.provider.blockSignals(False)
        # 重填后保持模型下拉的现值，否则延后到达的目录会改写表单值。
        self._fill_models(self.model.text() or self._saved.get('model') or '')

    def _on_provider_changed(self, *args):
        self._fill_models(None)

    def _fill_models(self, prefer):
        """prefer 是要优先选中的模型标识；None 表示用户刚切了提供商，改挑该提供商的首项。"""
        provider_id = self.provider.text()
        models = []
        for row in self._providers:
            if str(row.get('id') or '') == provider_id:
                models = [item for item in (row.get('models') or []) if isinstance(item, dict)]
                break
        saved = self._saved.get('model') or ''
        ids = [str(item.get('id')) for item in models if item.get('id')]
        selected = (saved if saved in ids else (ids[0] if ids else saved)) if prefer is None else (prefer or saved)
        self.model.blockSignals(True)
        self.model.clear()
        if selected and selected not in ids:
            self.model.addItem(MISSING_PREFIX + selected, selected)
        for item in models:
            model_id = str(item.get('id') or '')
            if model_id:
                self.model.addItem(str(item.get('name') or model_id), model_id)
        if not self.model.count():
            self.model.addItem(MISSING_PREFIX + (saved or '—'), saved)
        self.model.setCurrentIndex(max(0, self.model.findData(selected)) if selected else 0)
        self.model.blockSignals(False)
        self._update_catalog_hint()

    def _update_catalog_hint(self):
        if self._catalog_error:
            self.catalog_hint.setText(self._catalog_error)
            return
        missing = [combo for combo in (self.provider, self.model)
                   if combo.currentText().startswith(MISSING_PREFIX)]
        self.catalog_hint.setText(MISSING_HINT if missing else '')

    def call(self, method, path, data=None, done=None):
        if method != 'GET':
            self.normalize_url()  # 保存/探测/连接一律使用规范化后的地址
        self._operation_epoch += 1
        epoch = self._operation_epoch
        snapshot = self.values()
        self.status.setText('正在处理…')

        def completed(result):
            if path.endswith('/discover'):
                self.discover_button.setEnabled(True)
            if epoch != self._operation_epoch:
                return
            if not isinstance(result, dict):
                result = {'success': False, 'message': '后台返回内容无法识别，请重试。'}
            self._remember(result)
            if snapshot != self.values():
                self.status.setText('操作已结束；表单已有新编辑，保留当前内容。请核对后重新保存或检测。')
                return
            (done or self.result)(result)
        self.request(self, method, path, data, timeout=25, on_done=completed)

    def _remember(self, data):
        """记下最近一次结果，供「查看详情」和「复制诊断信息」使用。"""
        detail = data.get('detail') if isinstance(data.get('detail'), dict) else {}
        self._last_result = {'state': str(data.get('state') or ''), 'status': detail.get('status'),
                             'rpc_id': detail.get('rpc_id') or '', 'detail': detail or None}
        self.detail_button.setEnabled(bool(detail))
        self._apply_state(data)

    def _apply_state(self, data):
        state = str(data.get('state') or '')
        drift = data.get('drift') if isinstance(data.get('drift'), dict) else None
        if not state and not drift:
            return
        if state == 'drift' and drift:
            self._drift = drift
            to_port = str(drift.get('to_port') or _port_of(drift.get('to')) or '')
            from_port = str(drift.get('from_port') or _port_of(drift.get('from')) or '')
            self.banner_text.setText('端口已变更：' + (from_port or '—') + ' → ' + (to_port or '—'))
            self.banner_button.setText('更新为 ' + (to_port or '新端口'))
            self.banner_button.setVisible(True); self.banner_button.setEnabled(True)
            self.banner.setStyleSheet(BANNER_DRIFT); self.banner.setVisible(True)
        elif state == 'ok':
            self._drift = None
            if self._just_updated:
                self._just_updated = False
                self.banner_text.setText('端点可用'); self.banner_button.setVisible(False)
                self.banner.setStyleSheet(BANNER_OK); self.banner.setVisible(True)
            else:
                self.banner.setVisible(False)
        else:
            self._drift = None
            self.banner_button.setVisible(False)
            self.banner_text.setText(str(data.get('hint') or '没有找到可用的DSH端点，请按提示处理后重新检测。'))
            self.banner.setStyleSheet(BANNER_HINT); self.banner.setVisible(True)

    def update_to_drift(self):
        """用户点了「更新为 X」才改地址：保存成功后立刻回检测一次。"""
        drift = self._drift or {}
        value, error = normalize_url(drift.get('to') or '')
        if error:
            self.status.setText('端口更新失败：' + error + '；请手动填写地址后保存。')
            return
        self.url.setText(value); self.url_error.setText(''); self._apply_validity()
        self.banner_button.setEnabled(False)

        def saved(result):
            if result.get('success') is False:
                self.banner_button.setEnabled(True)
                self.result(result)
                return
            self._just_updated = True
            self.call('POST', 'dsh/probe', {}, None)
        self.call('POST', 'dsh', self.values(), saved)

    def load(self, data):
        self.enabled.setChecked(bool(data.get('enabled')))
        self._saved = {'provider': str(data.get('provider') or ''), 'model': str(data.get('model') or '')}
        self.url.setText(str(data.get('base_url') or DEFAULT_URL))
        self.normalize_url()
        self.effort.setCurrentIndex(max(0, self.effort.findData(data.get('reasoning_effort', ''))))
        self.profile.setCurrentText(str(data.get('profile') or 'desktop'))
        timeout = data.get('timeout')
        self.timeout.setValue(int(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else 60)
        self._fill_providers()
        stage = str(data.get('stage') or '未检测')
        text = ('状态：' + stage + '；'
            + str(data.get('last_error') or '请保存后检测协议和专用人设')
            + ('' if stage in LIVE_STAGES else '\n' + ANNOUNCE_RECONNECT))
        # 有码就把「是什么/怎么办」贴在状态下面：用户不用点「查看详情」才知道下一步做什么。
        code = _code_lines(data)
        if code:
            text += '\n' + '\n'.join(code)
        self.status.setText(text)
        self._apply_announce(data)

    def _apply_announce(self, data):
        """只读渲染上报行：不写回配置、不自动启用、不重试连接、不进 values()。"""
        self.announce.setText('DSH 主动上报：'
            + announce_line(data.get('last_announce') if isinstance(data, dict) else None))

    def result(self, data):
        message = data.get('message')
        if not isinstance(message, str) or not message.strip():
            message = ('操作未完成，请点「查看详情」核对后端响应。' if isinstance(data.get('detail'), dict)
                       else '操作未完成')
        lines = [message]
        state = str(data.get('state') or '')
        if state:
            lines.append('状态：' + state + '（' + STATE_LABELS.get(state, '未识别') + '）')
        lines.extend(_code_lines(data))
        ports = data.get('checked_ports')
        if isinstance(ports, int) and not isinstance(ports, bool):
            extra = ''
            bad = data.get('unverified_ports')
            if isinstance(bad, int) and not isinstance(bad, bool) and bad:
                extra = '，其中 ' + str(bad) + ' 个未通过只读核验'
            lines.append('已只读核验端口：' + str(ports) + ' 个' + extra)
        if data.get('port_owner_known') is False:
            lines.append('当前权限读不到端口归属，已改为按 DSH 自己留下的线索端口逐个核验（同样要过协议校验）。')
        self.status.setText('\n'.join(lines))

    def values(self):
        return {'enabled': self.enabled.isChecked(), 'base_url': self.url.text().strip(),
            'model': self.model.text(), 'provider': self.provider.text(),
            'reasoning_effort': self.effort.currentData(), 'profile': self.profile.currentText(),
            'timeout': self.timeout.value()}

    def save(self):
        values = self.values()
        if not values['provider'] or not values['model']:
            self.status.setText('请先从下拉里选择DSH提供商与模型，再保存。')
            return
        self.call('POST', 'dsh', values)

    def connect_selected(self):
        def done(data):
            if data.get('success') and data.get('enabled'):
                self.enabled.setChecked(True)
            self.result(data)
        self.call('POST', 'dsh/connect', self.values(), done)

    def install(self):
        self.call('POST', 'dsh/install-preset', {})

    def probe(self):
        self.call('POST', 'dsh/probe', {})

    def discover(self):
        self.normalize_url()
        self.discover_button.setEnabled(False)
        self.call('POST', 'dsh/discover', {}, self.discovered)

    def discovered(self, data):
        self.discover_button.setEnabled(True)
        self.endpoints.blockSignals(True); self.endpoints.clear()
        for row in data.get('candidates') or []:
            base_url = str(row.get('base_url') or '') if isinstance(row, dict) else ''
            if base_url:
                pid = row.get('pid')
                self.endpoints.addItem(('PID ' + str(pid) + ' · ' if pid else '本机探测 · ') + base_url, base_url)
        if not self.endpoints.count():
            self.endpoints.addItem('没有通过只读核验的端点', '')
        self.endpoints.blockSignals(False)
        if self.endpoints.count() == 1 and self.endpoints.currentData():
            self.select_endpoint()
        else:
            self.endpoints.setCurrentIndex(-1)
        self.result(data)

    def select_endpoint(self, *args):
        url = self.endpoints.currentData()
        if url:
            self.url.setText(str(url))
            self.normalize_url()
            self.status.setText('地址已填入，请保存后检测专用人设；提供商与模型沿用已有配置，未自动启用。')

    def show_detail(self):
        detail = (self._last_result or {}).get('detail')
        if not isinstance(detail, dict):
            self.status.setText('这次结果没有可诊断的后端详情。')
            return
        DshDetailDialog(detail, self.status.text(), self).exec()

    def copy_diagnostics(self):
        last = self._last_result or {}
        lines = ['版本：' + APP_VERSION,
                 'DSH 地址：' + (self.url.text().strip() or '—'),
                 '提供商：' + (self.provider.text() or '—'),
                 '模型：' + (self.model.text() or '—'),
                 '思考强度：' + str(self.effort.currentData() or '模型默认'),
                 '回合等待上限：' + str(self.timeout.value()) + ' 秒',
                 '最近状态：' + (last.get('state') or '—'),
                 'HTTP 状态码：' + _plain(last.get('status')),
                 'request id：' + (last.get('rpc_id') or '—')]
        try:
            QGuiApplication.clipboard().setText('\n'.join(lines))
        except Exception:
            self.status.setText('复制失败：无法访问系统剪贴板，请手动抄写上方地址与状态码。')
            return
        self.status.setText('诊断信息已复制到剪贴板；内容不含任何凭据。')

    def open_binding(self):
        """跳回接入凭据面板；函数内import，避免与agent_binding在模块顶层互相导入。"""
        from .agent_binding import AgentBindingDialog
        AgentBindingDialog(self.request, self).exec()

    def import_bridge(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择本机qq-bridge配置（只读接入参数）', '', 'JSON (*.json)')
        if not path:
            return
        from pathlib import Path
        from src.dsh_bridge import local_url
        try:
            file = Path(path)
            if file.stat().st_size > 1024 * 1024:
                raise ValueError()
            cfg = json.loads(file.read_text('utf-8-sig'))['dsh']
            url = local_url(cfg['baseUrl'])
            model, provider = cfg['model'], cfg.get('provider', 'deepseek-official')
            if any(not isinstance(s, str) or not 1 <= len(s) <= 100 for s in (model, provider)):
                raise ValueError()
        except (ValueError, OSError, KeyError, TypeError):
            self.status.setText('配置格式或本机地址无效，未导入；没有修改文件或保存密钥。')
            return
        self.url.setText(url); self.normalize_url()
        self.provider.setText(provider); self.model.setText(model)
        self.effort.setCurrentIndex(max(0, self.effort.findData(cfg.get('reasoningEffort', ''))))
        self.status.setText('只读取地址、提供商、模型与思考强度，未复制任何密钥。请核对profile后保存；未自动启用。')
