"""当前会话隔离后台的本机管理窗口；慢操作经宿主请求线程执行。"""
import re

from PyQt6.QtCore import QStandardPaths, Qt, QTimer
from PyQt6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout)

from ..runtime_identity import APP_VERSION


_INITIAL_NOTICE = '先打开沙盒并登录微信，再准备和启用隔离通道。'

_OPERATIONS = {'share_program': '共享程序', 'share_channel': '共享通道',
    'process_probe': '只读进程核验', 'launch': '启动代理', 'verify_heartbeat': '核验代理心跳',
    'commit_session': '登记配对', 'preflight': '续接前核验', 'complete': '完成',
    'session_probe': '检查沙盒交互会话', 'repair_preflight': '目录修复前核验',
    'stage_channel': '复制原配对目录', 'archive_channel': '保留原目录',
    'install_channel': '交接修复目录'}
_STAGES = {'reserved': '已保留恢复记录', 'mapping_program': '正在共享程序',
    'mapping_channel': '正在共享通道', 'channel_mapped': '通道映射已确认',
    'launching': '正在启动代理', 'ready': '恢复已完成', 'failed': '恢复失败',
    'preflight': '续接前核验', 'program_mapped': '程序映射已确认',
    'verifying': '正在核验心跳', 'committing': '正在登记配对',
    'repair_staging': '正在复制原配对目录', 'repair_archiving': '正在保留原目录',
    'repair_installing': '正在交接修复目录'}


def _diagnostic_text(pair):
    """只显示已知阶段和数值码；服务端异常原文不是这里的诊断数据。"""
    if not pair:
        return ''
    lines = []
    codes = []
    for prefix, title in (('initial', '首次准备失败'), ('recovery', '恢复记录')):
        details = []
        operation = pair.get(prefix + '_failed_operation')
        if isinstance(operation, str) and operation in _OPERATIONS:
            details.append(_OPERATIONS[operation])
        code = pair.get(prefix + '_hresult')
        if isinstance(code, str) and re.fullmatch(r'0x[0-9A-F]{8}', code):
            details.append('HRESULT ' + code)
            codes.append(code)
        number = pair.get(prefix + '_returncode')
        if type(number) is int:
            details.append('返回码 ' + str(number))
        if details:
            lines.append(title + '：' + ' · '.join(details))
    stage = pair.get('recovery_stage')
    if isinstance(stage, str) and stage in _STAGES:
        lines.append('恢复阶段：' + _STAGES[stage])
    state = pair.get('recovery_state')
    if state == 'resumable':
        lines.append('原映射已确认；重新核验后可继续恢复，不重复映射通道。')
    elif state == 'repairable':
        lines.append('共享在通道映射前被拒绝；可修复目录，保留原配对与微信登录。')
    elif state == 'blocked' or (state is not None and state not in ('none', 'ready')):
        lines.append('恢复证据不足或映射结果未知；已保留记录，当前不能继续恢复。')
    elif state == 'ready':
        lines.append('配对恢复已完成；尚未自动启动机器人。')
    if '0x80070005' in codes:
        lines.append('共享目录访问被拒绝；这不表示微信没有登录。')
    if '0x80070520' in codes:
        lines.append('沙盒交互会话尚未就绪；请先连接并打开沙盒桌面，再刷新检查。')
    return '\n'.join(lines)


def _recovery_allowed(pair):
    if not pair or pair.get('stage') != 'failed':
        return False
    # 兼容旧状态的首次恢复；新版未知值与明确 blocked 都不能放行续接。
    return pair.get('recovery_state') in (None, 'none', 'resumable', 'repairable')


class SandboxDialog(QDialog):
    def __init__(self, call, parent=None):
        super().__init__(parent)
        self.request = call
        self._pairs = []
        self._busy = False
        self._refreshing = False
        self._status_valid = False
        self._closed = False
        self._operation_epoch = 0
        self._running = False
        self._preparing = False
        self.setWindowTitle('隔离后台 · 当前会话回复')
        self.resize(760, 500)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet('QDialog { background:#F4F6F9; color:#182C43; } '
            'QComboBox { background:white; padding:7px; min-height:22px; }')
        layout = QVBoxLayout(self)
        intro = QLabel('微信在 Windows Sandbox 内运行，宿主沙盒窗口可以最小化。'
            '只回复沙盒微信当前已打开的群或联系人；请手动登录、打开目标会话并保持草稿为空。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        warning = QLabel('新沙盒不自带微信，可用“在沙盒内安装微信”选择本机安装包静默安装，再手动登录。关闭沙盒会丢失其中的安装和登录状态。'
            '本机模型、记忆与日志继续保存在原数据目录。配对不会启动机器人或解除安全暂停。')
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.environment = QLabel('当前运行环境：正在读取…')
        self.environment.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.environment)
        row = QHBoxLayout()
        self.sandboxes = QComboBox()
        self.sandboxes.setMinimumWidth(260)
        self.sandboxes.currentIndexChanged.connect(self._selection_changed)
        row.addWidget(self.sandboxes, 1)
        self.refresh_button = QPushButton('刷新沙盒')
        self.refresh_button.clicked.connect(lambda: self.refresh(explicit=True))
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.pair_info = QLabel('尚未配对')
        self.pair_info.setWordWrap(True)
        self.pair_info.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.pair_info)
        self.diagnostics = QLabel('')
        self.diagnostics.setWordWrap(True)
        self.diagnostics.setTextFormat(Qt.TextFormat.PlainText)
        self.diagnostics.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.diagnostics)
        self.upgrade_hint = QLabel('')
        self.upgrade_hint.setWordWrap(True)
        self.upgrade_hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.upgrade_hint)
        row = QHBoxLayout()
        self.launch_button = QPushButton('打开 Windows 沙盒')
        self.launch_button.clicked.connect(self.launch)
        row.addWidget(self.launch_button)
        self.install_button = QPushButton('在沙盒内安装微信')
        self.install_button.clicked.connect(self.install)
        row.addWidget(self.install_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.upgrade_button = QPushButton('原地升级代理到 ' + APP_VERSION)
        self.upgrade_button.clicked.connect(self.upgrade)
        row.addWidget(self.upgrade_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.prepare_button = QPushButton('准备所选沙盒')
        self.prepare_button.clicked.connect(self.prepare)
        self.recover_button = QPushButton('修复未完成配对')
        self.recover_button.clicked.connect(self.recover)
        self.activate_button = QPushButton('启用此隔离通道')
        self.activate_button.clicked.connect(self.activate)
        self.deactivate_button = QPushButton('切回本机通道')
        self.deactivate_button.clicked.connect(self.deactivate)
        for button in (self.prepare_button, self.recover_button, self.activate_button, self.deactivate_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel(_INITIAL_NOTICE)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        layout.addStretch()
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        layout.addWidget(close)
        self._poll = QTimer(self)
        self._poll.setInterval(4000)
        self._poll.timeout.connect(self.refresh)
        self._poll.start()
        self.refresh()

    def _selected_pair(self):
        selected = self.sandboxes.currentData()
        return next((pair for pair in self._pairs if pair.get('sandbox_id') == selected), None)

    def _selection_changed(self, *_):
        pair = self._selected_pair()
        self.pair_info.setText(('通道 ' + str(pair.get('short_id') or pair.get('channel_id', '')[:12])
            + ' · ' + str(pair.get('reason') or pair.get('stage') or '等待检查'))
            if pair else '所选沙盒尚未配对' if self._status_valid else '配对状态暂时无法核验，请刷新检查')
        self.diagnostics.setText(_diagnostic_text(pair))
        self.upgrade_hint.setText(self._upgrade_hint(pair))
        self._buttons()

    def _buttons(self):
        self.sandboxes.setEnabled(not self._busy)
        self.refresh_button.setEnabled(not self._busy and not self._refreshing)
        pair = self._selected_pair()
        blocked = self._upgrade_blocked(pair)
        self.upgrade_button.setText('原地升级代理到 ' + APP_VERSION)
        self.upgrade_button.setEnabled(not self._busy and not blocked)
        self.upgrade_button.setToolTip(blocked or
            '只更新隔离代理程序并保留微信登录；不会启动机器人，也不会自动连接微信。')
        self.prepare_button.setEnabled(not self._busy and self._status_valid and bool(self.sandboxes.currentData())
            and not (pair and pair.get('stage') in ('failed', 'preparing')))
        self.recover_button.setText('修复目录并恢复配对' if pair and pair.get('recovery_state') == 'repairable'
            else '继续恢复配对' if pair and pair.get('recovery_state') == 'resumable'
            else '恢复受阻（保留记录）' if pair and not _recovery_allowed(pair)
                 and pair.get('stage') == 'failed' else '修复未完成配对')
        self.recover_button.setEnabled(not self._busy and self._status_valid and _recovery_allowed(pair))
        self.activate_button.setEnabled(not self._busy and self._status_valid and bool(pair)
            and pair.get('agent_online') is True
            and pair.get('stage') in ('agent_ready', 'ready'))
        self.deactivate_button.setEnabled(not self._busy)
        launch_blocked = self._launch_blocked()
        self.launch_button.setEnabled(not self._busy and not launch_blocked)
        self.launch_button.setToolTip(launch_blocked or
            '由控制台启动一台 Windows 沙盒，约占用 1.6GB 内存；关闭沙盒窗口后其中的安装与登录会丢失。')
        install_blocked = self._install_blocked()
        self.install_button.setEnabled(not self._busy and not install_blocked)
        self.install_button.setToolTip(install_blocked or
            '选择本机的微信安装包，映射进所选沙盒后静默安装；沙盒内已装好则直接复用，不重复运行安装包。')

    def refresh(self, explicit=False):
        if self._closed or self._busy or self._refreshing:
            return
        self._refreshing = True
        epoch = self._operation_epoch
        self._buttons()
        if explicit:
            self.status.setText('正在读取沙盒状态…')
        def done(result):
            if self._closed:
                return
            self._refreshing = False
            if epoch != self._operation_epoch:
                self._buttons()
                return
            if result.get('success') is False:
                # 不保留失去核验的旧候选；用户刷新成功后再启用通道。
                self._pairs = []
                self._status_valid = False
                self._selection_changed()
                if explicit:
                    self.status.setText(str(result.get('message') or result.get('detail') or '读取沙盒状态失败'))
                self._buttons()
                return
            self._status_valid = result.get('available') is not False
            selected = self.sandboxes.currentData()
            self.sandboxes.blockSignals(True)
            self.sandboxes.clear()
            for row in result.get('sandboxes') or []:
                ident = str(row.get('id') or '')
                if ident:
                    self.sandboxes.addItem('Windows Sandbox · ' + ident[:12], ident)
            if not self.sandboxes.count():
                self.sandboxes.addItem('未发现运行中的沙盒', '')
            index = self.sandboxes.findData(selected)
            self.sandboxes.setCurrentIndex(max(0, index))
            self.sandboxes.blockSignals(False)
            self._pairs = list(result.get('pairs') or [])
            self._running = result.get('running') is True
            self._preparing = result.get('preparing') is True
            self.environment.setText('当前运行环境：' +
                ('隔离后台' if result.get('environment') == 'sandbox' else '本机通道'))
            self._selection_changed()
            pair = self._selected_pair()
            if self.status.text() == _INITIAL_NOTICE and pair and pair.get('recovery_state') == 'repairable':
                self.status.setText('微信登录可保持；点击“修复目录并恢复配对”，完成后再启用通道。')
            if explicit or not self._status_valid:
                self.status.setText(str(result.get('message') or '状态已刷新'))
        self.request(self, 'GET', 'sandbox/status', None, timeout=25, on_done=done)

    def _operate(self, path, payload, notice, timeout=55):
        if self._closed or self._busy:
            return
        self._operation_epoch += 1
        epoch = self._operation_epoch
        self._busy = True
        self._buttons()
        self.status.setText(notice)
        def done(result):
            if self._closed or epoch != self._operation_epoch:
                return
            self._busy = False
            self.status.setText(str(result.get('message') or result.get('detail') or
                ('操作完成' if result.get('success') else '操作未完成')))
            self._buttons()
            self.refresh()  # 状态刷新不覆盖上面的成功或失败原文。
        self.request(self, 'POST', path, payload, timeout=timeout, on_done=done)

    def _upgrade_blocked(self, pair):
        """原地升级复用 prepare 的服务端路径；这里只决定可点击条件与原因。"""
        if not self._status_valid:
            return '配对状态尚未核验，请先刷新成功后再升级'
        if not pair or not self.sandboxes.currentData():
            return '请先准备所选沙盒的隔离通道'
        if pair.get('stage') != 'version_mismatch':
            return '所选沙盒没有版本不一致的在线代理，无需原地升级'
        if pair.get('agent_online') is not True:
            return '隔离代理心跳已过期；请重新打开沙盒并登录后可原地升级'
        if self._running:
            return '机器人运行中，请先停止机器人再原地升级代理'
        if self._preparing:
            return '隔离后台正在配对，请等待本次结果后再升级'
        return ''

    def _upgrade_hint(self, pair):
        if not pair or pair.get('stage') != 'version_mismatch':
            return ''
        blocked = self._upgrade_blocked(pair)
        return blocked or ('隔离代理在线但版本与控制台不一致；点击“原地升级代理到 %s”即可更新，'
            '升级不会启动机器人、不会自动连接微信。' % APP_VERSION)

    def upgrade(self):
        blocked = self._upgrade_blocked(self._selected_pair())
        if blocked:
            self.status.setText(blocked)
            return
        # 与“准备所选沙盒”同一服务端路径：版本不符时 prepare_sandbox 走原地升级。
        self._operate('sandbox/prepare', {'id': self.sandboxes.currentData()},
            '正在原地升级隔离代理到 %s，保留微信登录；不会启动机器人，也不会自动连接微信…' % APP_VERSION,
            timeout=180)

    def _launch_blocked(self):
        """已有实例就不再启动第二台；沙盒非常吃内存。"""
        if not self._status_valid:
            return '沙盒状态尚未核验，请先刷新成功后再打开沙盒'
        if self.sandboxes.currentData():
            return '已有运行中的 Windows 沙盒，未重复启动；请直接使用该实例'
        return ''

    def launch(self):
        blocked = self._launch_blocked()
        if blocked:
            self.status.setText(blocked)
            return
        self._operate('sandbox/launch', {},
            '正在由控制台打开 Windows 沙盒，首次启动可能需要几十秒…', timeout=150)

    def _install_blocked(self):
        """没有实例可装：沙盒未打开就不进入服务端，避免白跑一次客体探测。"""
        if not self._status_valid:
            return '沙盒状态尚未核验，请先刷新成功后再安装微信'
        if not self.sandboxes.currentData():
            return '未发现运行中的沙盒；请先打开 Windows 沙盒，等它进入桌面后再安装微信'
        return ''

    def install(self):
        blocked = self._install_blocked()
        if blocked:
            self.status.setText(blocked)
            return
        path, _ = QFileDialog.getOpenFileName(self, '选择微信安装包（WeChatSetup*.exe）',
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation),
            '微信安装包 (*.exe)')
        if not path:
            return
        # 安装与轮询都在服务端有界执行；这里只等结果，不自己碰沙盒。
        self._operate('sandbox/install-client',
            {'id': self.sandboxes.currentData(), 'installer': path},
            '正在把安装包映射进沙盒并静默安装微信，通常几十秒；请勿关闭沙盒窗口…', timeout=360)

    def prepare(self):
        if not self._status_valid:
            self.status.setText('配对状态尚未核验，请先刷新成功后再准备；未修改映射。')
            return
        ident = self.sandboxes.currentData()
        if not ident:
            self.status.setText('请先打开 Windows Sandbox 并手动登录微信，再刷新清单。')
            return
        pair = self._selected_pair()
        if pair and pair.get('stage') in ('failed', 'preparing'):
            self.status.setText('所选沙盒已有未完成配对；请查看失败诊断并使用可用的恢复入口，未重复准备映射。')
            return
        # 首次暂存与核验程序后，还需两次共享和一次启动；异步请求覆盖整段有界操作。
        self._operate('sandbox/prepare', {'id': ident}, '正在准备专属隔离通道，首次复制可能需要稍等…', timeout=180)

    def activate(self):
        if not self._status_valid:
            self.status.setText('配对状态尚未核验，请先刷新成功后再启用。')
            return
        pair = self._selected_pair()
        if not pair:
            self.status.setText('请先准备所选沙盒的隔离通道。')
            return
        if (pair.get('agent_online') is not True
                or pair.get('stage') not in ('agent_ready', 'ready')):
            self.status.setText('隔离代理尚未就绪，请等待状态刷新后再启用。')
            return
        self._operate('sandbox/activate', {'channel_id': pair['channel_id']}, '正在启用隔离通道…')

    def recover(self):
        if not self._status_valid:
            self.status.setText('配对状态尚未核验，请先刷新成功后再恢复。')
            return
        pair = self._selected_pair()
        if not pair or pair.get('stage') != 'failed':
            self.status.setText('只有所选沙盒的未完成配对可以修复，请先刷新状态。')
            return
        if not _recovery_allowed(pair):
            self.status.setText('恢复证据不足或映射结果未知；已保留记录，未重复映射。请查看上方失败阶段和错误码。')
            return
        self._operate('sandbox/recover', {'id': self.sandboxes.currentData()},
            ('正在重新核验已确认的映射并续接恢复；保留微信和当前登录，不启动机器人…'
             if pair.get('recovery_state') == 'resumable' else
             '正在核验旧通道并修复未完成配对；保留微信和当前登录，不启动机器人…'), timeout=180)

    def deactivate(self):
        self._operate('sandbox/deactivate', {}, '正在切回本机通道…')

    def _closing(self):
        self._closed = True
        self._poll.stop()
        self._operation_epoch += 1

    def closeEvent(self, event):
        self._closing()
        super().closeEvent(event)

    def done(self, result):
        self._closing()
        super().done(result)
