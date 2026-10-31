"""1.3.51 批3：回滚脚本（只生成、绝不执行）与 /api/install-guard/rollback* 接线。

全部离线：脚本只写进 tempfile；不碰真实安装目录、不碰真实数据根、不启端口、
不执行 taskkill、不改名/覆盖任何 exe、不重启控制台、不碰真实微信。
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import install_guard as guard
from src.agent_authority import authority

CUR = guard.EXE_NAME
PREV = guard.PREV_EXE_NAME
BINDING = {'platform': 'desktop', 'account': 'local', 'subject': 'owner'}


def make_install_dir(folder, prev=1234, current=999):
    """造一个像安装目录的 tempfile 目录：两个 exe，可选是否有 .prev.exe。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if current is not None:
        (folder / CUR).write_bytes(b'c' * current)
    if prev is not None:
        (folder / PREV).write_bytes(b'p' * prev)
    return folder


class RollbackStateTests(unittest.TestCase):
    """rollback_state：只读判断有没有上一版备份，任何异常都降级成中文说明。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / '安装 目录'

    def test_missing_backup_is_reported_without_raising(self):
        make_install_dir(self.folder, prev=None)
        state = guard.rollback_state(self.folder)
        self.assertFalse(state['available'])
        self.assertIsNone(state['prev_size'])
        self.assertIn('没有', state['note'])
        self.assertIn(PREV, state['note'])
        self.assertEqual(state['prev_path'], str(self.folder / PREV))
        self.assertEqual(state['current_path'], str(self.folder / CUR))

    def test_backup_sizes_and_mtime_are_reported(self):
        make_install_dir(self.folder, prev=1234, current=999)
        state = guard.rollback_state(self.folder)
        self.assertTrue(state['available'])
        self.assertEqual(state['prev_size'], 1234)
        self.assertEqual(state['current_size'], 999)
        self.assertRegex(state['prev_mtime'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$')
        self.assertIn(PREV, state['note'])

    def test_missing_install_dir_returns_chinese_reason(self):
        state = guard.rollback_state(self.folder / 'no-such-dir')
        self.assertFalse(state['available'])
        self.assertIn('安装目录不存在', state['note'])
        self.assertIn('no-such-dir', state['note'])

    def test_unreadable_install_dir_is_reported_not_raised(self):
        with patch.object(Path, 'is_dir', side_effect=OSError('denied')):
            state = guard.rollback_state(self.folder)
        self.assertFalse(state['available'])
        self.assertIn('读不出来', state['note'])

    def test_default_install_dir_is_project_root_in_source_mode(self):
        if getattr(guard.sys, 'frozen', False):
            self.skipTest('冻结态下安装目录是 exe 旁边')
        self.assertEqual(guard._default_install_dir(), Path(guard.__file__).resolve().parent.parent)


class RollbackScriptTests(unittest.TestCase):
    """write_rollback_script：只落 .cmd，全 ASCII + CRLF，同名覆盖，失败给中文原因。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # 故意用 ASCII 但带空格的名字：既能走同目录脚本，也能验证「退到数据根」的绝对路径分支
        self.install = Path(self.temp.name) / 'install dir'
        self.dataroot = Path(self.temp.name) / 'data root'
        self.dataroot.mkdir()

    def test_script_is_ascii_crlf_and_has_the_required_steps(self):
        make_install_dir(self.install)
        result = guard.write_rollback_script(self.install)
        self.assertTrue(result['success'], result['message'])
        self.assertEqual(result['mode'], 'install_dir')
        path = Path(result['script_path'])
        self.assertEqual(path.parent, self.install)
        self.assertEqual(path.name, guard.ROLLBACK_SCRIPT_NAME)
        raw = path.read_bytes()
        self.assertTrue(all(byte < 128 for byte in raw), '脚本必须全 ASCII')
        self.assertEqual(raw.count(b'\n') - raw.count(b'\r\n'), 0, '只允许 CRLF，不许裸 LF')
        self.assertGreater(raw.count(b'\r\n'), 10)
        text = raw.decode('ascii')
        self.assertIn('set "PREV=%~dp0' + PREV + '"', text)
        self.assertIn('set "CUR=%~dp0' + CUR + '"', text)
        self.assertIn('if not exist "%PREV%" goto missing', text)
        self.assertIn('taskkill /F /IM WhaleGirlWeChat.exe', text)
        self.assertIn('copy /Y "%PREV%" "%CUR%"', text)
        self.assertIn('if errorlevel 1 goto failed', text)
        self.assertIn('start "" "%CUR%"', text)
        self.assertEqual(text.count('pause'), 2)
        self.assertEqual(text.count('exit /b 1'), 2)
        for chinese in ('回滚', '上一版', '控制台'):
            self.assertNotIn(chinese, text, '中文只放在返回值与界面文案里')
        self.assertIn('回滚脚本已生成', result['message'])

    def test_failure_branches_exit_nonzero_before_starting(self):
        text = guard._rollback_script_text('%~dp0' + PREV, '%~dp0' + CUR)
        self.assertLess(text.index(':missing'), text.index(':failed'))
        self.assertLess(text.index('if errorlevel 1 goto failed'), text.index('start ""'))
        missing_block = text[text.index(':missing'):text.index(':failed')]
        failed_block = text[text.index(':failed'):]
        for block in (missing_block, failed_block):
            self.assertIn('pause', block)
            self.assertIn('exit /b 1', block)
            self.assertNotIn('start ""', block)  # 失败路径绝不再启动任何东西

    def test_second_run_overwrites_the_same_file(self):
        make_install_dir(self.install)
        first = guard.write_rollback_script(self.install)
        second = guard.write_rollback_script(self.install)
        self.assertTrue(first['success'] and second['success'])
        self.assertEqual(first['script_path'], second['script_path'])
        scripts = sorted(path.name for path in self.install.iterdir() if path.suffix == '.cmd')
        self.assertEqual(scripts, [guard.ROLLBACK_SCRIPT_NAME])

    def test_no_backup_refuses_and_writes_nothing(self):
        make_install_dir(self.install, prev=None)
        result = guard.write_rollback_script(self.install)
        self.assertFalse(result['success'])
        self.assertIn('没有可回滚的上一版备份', result['message'])
        self.assertIn(PREV, result['message'])
        self.assertEqual(list(self.install.glob('*.cmd')), [])

    def test_unwritable_install_dir_falls_back_to_data_root(self):
        make_install_dir(self.install)
        if not str(self.install).isascii():
            self.skipTest('临时目录含非 ASCII 字符时只能生成同目录脚本')
        def writer(path, text):
            if Path(path).parent == self.install:
                raise PermissionError('denied')
            Path(path).write_text(text, encoding='ascii', newline='\r\n')
        with patch.object(guard, '_default_root', return_value=self.dataroot):
            result = guard.write_rollback_script(self.install, remove=writer)
        self.assertTrue(result['success'], result['message'])
        self.assertEqual(result['mode'], 'data_root')
        script = Path(result['script_path'])
        self.assertEqual(script.parent, self.dataroot)
        self.assertIn('数据根', result['message'])
        text = script.read_text(encoding='ascii')
        self.assertIn('set "PREV=' + str(self.install / PREV) + '"', text)
        self.assertEqual(list(self.install.glob('*.cmd')), [])

    def test_unwritable_everywhere_reports_chinese_failure(self):
        make_install_dir(self.install)
        def writer(path, text):
            raise PermissionError('denied')
        with patch.object(guard, '_default_root', return_value=self.dataroot):
            result = guard.write_rollback_script(self.install, remove=writer)
        self.assertFalse(result['success'])
        self.assertIn('回滚脚本写不出来', result['message'])
        self.assertIn('手工', result['message'])
        self.assertEqual(list(self.dataroot.glob('*.cmd')), [])

    def test_non_ascii_path_with_unwritable_dir_refuses_instead_of_mojibake(self):
        chinese = Path(self.temp.name) / '中文 安装#&目录'
        make_install_dir(chinese)
        def writer(path, text):
            raise PermissionError('denied')
        with patch.object(guard, '_default_root', return_value=self.dataroot):
            result = guard.write_rollback_script(chinese, remove=writer)
        self.assertFalse(result['success'])
        self.assertIn('非 ASCII', result['message'])
        self.assertEqual(result['script_path'], '')
        self.assertEqual(list(self.dataroot.glob('*.cmd')), [], '含中文路径时不许生成绝对路径脚本')

    def test_module_never_calls_taskkill_or_touches_the_exe(self):
        source = Path(guard.__file__).read_text(encoding='utf-8')
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('os.system', source)
        # taskkill /F /IM 只能作为脚本文本出现一次（另一处是讲硬约束的中文注释），绝不进执行路径
        self.assertEqual(source.count('taskkill /F /IM WhaleGirlWeChat.exe'), 1)
        self.assertIn('后端禁止调用 taskkill', source)

    def test_hard_constraint_is_documented_in_code(self):
        source = Path(guard.__file__).read_text(encoding='utf-8')
        self.assertIn('控制台不能替换自己正在运行的 exe', source)
        self.assertIn('后端禁止调用 taskkill', source)


class RollbackRouteTests(unittest.TestCase):
    """两条路由：GET 只读状态，POST 必须带严格口令，未绑定管理员一律 403。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / '安装 目录'
        make_install_dir(self.folder)
        self.bot = SimpleNamespace(root=self.folder)
        self.app = FastAPI()
        self.app.state.bot = self.bot
        guard.add_routes(self.app)
        self.client = TestClient(self.app)
        patcher = patch.object(guard, '_default_install_dir', return_value=self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_routes_are_registered(self):
        paths = {getattr(route, 'path', '') for route in self.app.routes}
        self.assertIn('/api/install-guard/rollback', paths)
        self.assertIn('/api/install-guard/rollback-script', paths)

    def test_both_routes_require_bound_admin(self):
        self.assertEqual(self.client.get('/api/install-guard/rollback').status_code, 403)
        response = self.client.post('/api/install-guard/rollback-script',
                                    json={'confirm': guard.ROLLBACK_CONFIRM})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(list(self.folder.glob('*.cmd')), [])

    def test_state_route_returns_readonly_sizes(self):
        authority(self.bot).bind(BINDING)
        body = self.client.get('/api/install-guard/rollback').json()
        self.assertTrue(body['available'])
        self.assertEqual(body['prev_size'], 1234)
        self.assertEqual(body['current_size'], 999)

    def test_missing_or_wrong_confirm_is_400_in_chinese(self):
        authority(self.bot).bind(BINDING)
        for payload in ({}, {'confirm': ''}, {'confirm': '已停止微信发送'},
                        {'confirm': 'Yes'}, {'confirm': guard.ROLLBACK_CONFIRM + ' '}):
            response = self.client.post('/api/install-guard/rollback-script', json=payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertIn(guard.ROLLBACK_CONFIRM, response.json()['detail'])
        self.assertEqual(list(self.folder.glob('*.cmd')), [], '口令不对时不许落任何脚本')

    def test_confirm_ok_generates_script_next_to_the_exe(self):
        authority(self.bot).bind(BINDING)
        response = self.client.post('/api/install-guard/rollback-script',
                                    json={'confirm': guard.ROLLBACK_CONFIRM})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['success'], body['message'])
        self.assertEqual(body['mode'], 'install_dir')
        self.assertTrue(Path(body['script_path']).is_file())

    def test_no_backup_branch_reports_chinese_reason(self):
        empty = Path(self.temp.name) / 'empty'
        make_install_dir(empty, prev=None)
        authority(self.bot).bind(BINDING)
        with patch.object(guard, '_default_install_dir', return_value=empty):
            body = self.client.post('/api/install-guard/rollback-script',
                                    json={'confirm': guard.ROLLBACK_CONFIRM}).json()
        self.assertFalse(body['success'])
        self.assertIn('没有可回滚的上一版备份', body['message'])


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    import wechat_gui
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover - 缺 PyQt6 时整类跳过
    HAVE_QT = False

_APP = None


def _qt_app():
    # 必须持有模块级引用：QApplication 被回收后再造 QWidget 会直接终止进程
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@unittest.skipUnless(HAVE_QT, '缺少 PyQt6，跳过界面接线测试')
class RollbackConsoleTests(unittest.TestCase):
    """界面：先读备份状态，再弹确认框，确认后才发 POST；按钮没有备份时禁用。"""

    def setUp(self):
        _qt_app()
        self.tab = wechat_gui.ConsoleTab()
        self.addCleanup(self.tab.deleteLater)
        self.calls = []
        patcher = patch.object(wechat_gui, 'call_api', side_effect=self._record)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self, owner, method, path, data=None, timeout=10, on_done=None):
        self.calls.append({'method': method, 'path': path, 'data': data, 'on_done': on_done})
        return None

    def _state(self, **extra):
        payload = {'install_dir': 'D:/安装 目录', 'available': True,
                   'prev_path': 'D:/安装 目录/' + PREV, 'prev_size': 1234,
                   'current_size': 999, 'prev_mtime': '2026-10-31T10:00:00',
                   'current_mtime': '2026-10-31T11:00:00', 'note': '有上一版备份'}
        payload.update(extra)
        return payload

    def test_confirm_constant_matches_backend(self):
        self.assertEqual(wechat_gui.ROLLBACK_CONFIRM, guard.ROLLBACK_CONFIRM)

    def test_widgets_exist_and_script_button_starts_disabled(self):
        for name in ('btn_rollback_state', 'btn_rollback_script', 'rollback_state_label'):
            self.assertTrue(hasattr(self.tab, name), name)
        self.assertFalse(self.tab.btn_rollback_script.isEnabled())
        self.assertIn('还没', self.tab.rollback_state_label.text())

    def test_read_state_uses_get_and_changes_nothing(self):
        self.tab._load_rollback_state()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['method'], 'GET')
        self.assertEqual(self.calls[0]['path'], 'install-guard/rollback')
        self.assertIsNone(self.calls[0]['data'])

    def test_state_without_backup_keeps_generate_disabled(self):
        self.tab._load_rollback_state()
        self.calls[0]['on_done'](self._state(available=False, prev_size=None,
                                             note='没有 WhaleGirlWeChat.prev.exe'))
        self.assertFalse(self.tab.btn_rollback_script.isEnabled())
        self.assertIn('没有', self.tab.rollback_state_label.text())

    def test_state_with_backup_enables_generate_and_shows_size(self):
        self.tab._load_rollback_state()
        self.calls[0]['on_done'](self._state())
        self.assertTrue(self.tab.btn_rollback_script.isEnabled())
        self.assertIn('1234', self.tab.rollback_state_label.text())
        self.assertIn(PREV, self.tab.rollback_state_label.text())

    def test_generate_asks_for_confirmation_before_posting(self):
        self.tab._load_rollback_state()
        self.calls[0]['on_done'](self._state())
        with patch.object(wechat_gui.QMessageBox, 'question',
                          return_value=wechat_gui.QMessageBox.StandardButton.No) as asked:
            self.tab._generate_rollback_script()
        self.assertEqual(asked.call_count, 1)
        self.assertEqual([call['path'] for call in self.calls], ['install-guard/rollback'])

    def test_generate_posts_strict_confirm_after_yes(self):
        self.tab._load_rollback_state()
        self.calls[0]['on_done'](self._state())
        with patch.object(wechat_gui.QMessageBox, 'question',
                          return_value=wechat_gui.QMessageBox.StandardButton.Yes):
            self.tab._generate_rollback_script()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]['method'], 'POST')
        self.assertEqual(self.calls[1]['path'], 'install-guard/rollback-script')
        self.assertEqual(self.calls[1]['data'], {'confirm': guard.ROLLBACK_CONFIRM})

    def test_generate_without_state_sends_nothing(self):
        self.tab._generate_rollback_script()
        self.assertEqual(self.calls, [])
