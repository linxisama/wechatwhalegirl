"""1.3.51：跨机器兼容性降级建议（compat_advice）与 /api/compat/advice 接线。

全部离线：不启服务、不监听端口、不跑真实系统探测（site_profile 与 install_guard 的探测
一律注入假数据或替换成 Mock）、不碰真实微信，也不往真实数据目录写东西。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import compat_advice as advice
from src.agent_authority import authority

KEYS = ('data_root', 'wechat', 'ocr', 'windows_sandbox', 'dsh', 'port',
        'isolated_channel', 'upgrade_data')
FIELDS = {'key', 'severity', 'title', 'detail', 'fix', 'evidence'}
CHANNEL = 'a' * 32


def healthy_profile():
    return {
        'ok': True,
        'generated_at': '2026-10-14T12:56:00+08:00',
        'platform': {'app_version': '1.3.50', 'port': 8091, 'frozen': True,
                     'executable': '~/Applications/WhaleGirlWeChat.exe',
                     'python': '3.12.10', 'os': 'Windows-11-10.0.22631-SP0', 'machine': 'AMD64'},
        'items': [
            {'key': 'scale', 'status': 'ok', 'detail': '缩放 100%', 'data': {'scale_percent': 100}},
            {'key': 'screen', 'status': 'ok', 'detail': '主屏 1920x1080', 'data': {}},
            {'key': 'wechat', 'status': 'ok', 'detail': '微信 4.1.2 正在运行',
             'data': {'running': True, 'version': '4.1.2', 'version_ok': True,
                      'exe': '~\\Weixin\\Weixin.exe',
                      'window': {'width': 1200, 'height': 800}}},
            {'key': 'ocr', 'status': 'ok', 'detail': '系统 OCR 有中文语言包（zh-Hans），共 1 种可用',
             'data': {'languages': ['zh-Hans'], 'total': 1, 'chinese': ['zh-Hans']}},
            {'key': 'windows_sandbox', 'status': 'ok', 'detail': 'Windows 11 专业版，可以开 Windows 沙盒',
             'data': {'windows_version': '10.0.22631', 'edition_id': 'Professional',
                      'product_name': 'Windows 11 专业版', 'display_version': '23H2',
                      'build': '22631', 'sandbox_exe': 'C:\\Windows\\System32\\WindowsSandbox.exe',
                      'sandbox_exe_exists': True, 'sandbox_available': True}},
            {'key': 'port', 'status': 'ok', 'detail': '8091 是本控制台后台在用（v1.3.50）',
             'data': {'port': 8091, 'self_running': True, 'bindable': False, 'version': '1.3.50'}},
            {'key': 'data_root', 'status': 'ok', 'detail': '数据目录可写（~/AppData/Local/WhaleGirlWeChat），剩余 42.5 GB，安装版',
             'data': {'root': '~/AppData/Local/WhaleGirlWeChat', 'writable': True,
                      'free_gb': 42.5, 'frozen': True,
                      'executable': '~/Applications/WhaleGirlWeChat.exe'}},
        ],
        'summary': '7 项探测全部正常，这台机器可以直接跑',
    }


def healthy_data_root():
    return {'primary': 'D:/WhaleGirlWeChat', 'active': 'D:/WhaleGirlWeChat',
            'fallback_used': False, 'writable': True, 'atomic_replace': True,
            'reason': '', 'candidates': ['D:/WhaleGirlWeChat'], 'checked_at': '2026-10-14T12:56:00+08:00'}


def healthy_self_check():
    return {'root': 'D:/WhaleGirlWeChat', 'generated_at': '2026-10-14T12:56:00+08:00', 'ok': True,
            'items': {}, 'problems': [], 'notes': [],
            'message': '升级数据自检：配置 1 项，历史 128 条，插件设置 3 条，人设 2 个，隔离通道 1 对（核验不过 0 个）'}


def healthy_channels():
    return {'root': 'D:/WhaleGirlWeChat/data/isolated_wechat', 'exists': True,
            'verified': [{'channel_id': CHANNEL, 'short_id': CHANNEL[:12],
                          'path': 'D:/WhaleGirlWeChat/data/isolated_wechat/' + CHANNEL}],
            'unverified': [], 'ignored': [], 'message': ''}


def healthy_dsh():
    return {'settings': {'enabled': True, 'base_url': 'https://api.deepseek.com',
                         'model': 'deepseek-chat', 'provider': 'deepseek',
                         'reasoning_effort': '', 'timeout': 60},
            'stage': 'ready', 'last_error': ''}


def changed_profile(key, **data):
    profile = healthy_profile()
    for item in profile['items']:
        if item['key'] == key:
            item['data'].update(data)
    return profile


def run_advice(**overrides):
    sources = {'profile': healthy_profile(), 'data_root': healthy_data_root(),
               'self_check': healthy_self_check(), 'channels': healthy_channels(),
               'dsh': healthy_dsh()}
    sources.update(overrides)
    return advice.collect_advice(**sources)


def pick(result, key):
    return next(item for item in result['items'] if item['key'] == key)


class AdviceShapeTests(unittest.TestCase):
    def test_items_are_fixed_shape_and_ordered(self):
        result = run_advice()
        self.assertEqual(tuple(item['key'] for item in result['items']), KEYS)
        for item in result['items']:
            self.assertEqual(set(item), FIELDS)
            self.assertIn(item['severity'], advice.SEVERITIES)
            self.assertTrue(item['title'] and item['detail'] and item['fix'])
            json.dumps(item)  # evidence 只能是 JSON 原语，界面才渲染得出来

    def test_healthy_machine_has_no_blocking_or_degraded(self):
        result = run_advice()
        self.assertTrue(result['ok'])
        self.assertEqual(result['blocking_count'], 0)
        self.assertEqual(result['degraded_count'], 0)
        self.assertEqual(result['info_count'], len(KEYS))
        self.assertIn('没有发现问题', result['summary'])

    def test_blocking_summary_wins_over_degraded(self):
        result = run_advice(data_root={**healthy_data_root(), 'writable': False,
                                       'atomic_replace': None, 'reason': '写入被系统拒绝'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['blocking_count'], 1)
        self.assertIn('必须先解决', result['summary'])

    def test_evidence_machine_only(self):
        profile = changed_profile('wechat', exe=str(Path.home()) + r'\Weixin\Weixin.exe')
        result = run_advice(profile=profile)
        wechat = pick(result, 'wechat')
        self.assertEqual(wechat['evidence']['scale_percent'], 100)
        self.assertEqual(wechat['evidence']['window'], '1200x800')
        self.assertNotIn(str(Path.home()), json.dumps(wechat))
        for item in result['items']:
            for banned in ('wxid', 'nickname', 'nick', 'content', 'title_hidden'):
                self.assertNotIn(banned, item['evidence'])


class DataRootAdviceTests(unittest.TestCase):
    def test_unwritable_data_root_is_blocking(self):
        result = run_advice(data_root={**healthy_data_root(), 'writable': False,
                                       'atomic_replace': None, 'reason': '写入被系统拒绝'})
        item = pick(result, 'data_root')
        self.assertEqual(item['severity'], 'blocking')
        self.assertIn('不可写', item['title'])
        self.assertIn('写入被系统拒绝', item['detail'])

    def test_atomic_replace_failure_is_degraded(self):
        result = run_advice(data_root={**healthy_data_root(), 'atomic_replace': False,
                                       'reason': '改名替换被拒绝'})
        item = pick(result, 'data_root')
        self.assertEqual(item['severity'], 'degraded')
        self.assertIn('原子替换', item['title'])

    def test_unknown_writable_is_info(self):
        result = run_advice(data_root={**healthy_data_root(), 'writable': None,
                                       'atomic_replace': None, 'reason': '探测超时'})
        item = pick(result, 'data_root')
        self.assertEqual(item['severity'], 'info')
        self.assertIn('超时', item['detail'])


class WechatAdviceTests(unittest.TestCase):
    def test_wechat_not_running_is_degraded(self):
        profile = healthy_profile()
        for item in profile['items']:
            if item['key'] == 'wechat':
                item['status'] = 'warn'
                item['detail'] = '微信进程没在跑；版本和窗口尺寸无法核对，先打开微信再自检'
                item['data'] = {'running': False}
        result = run_advice(profile=profile)
        item = pick(result, 'wechat')
        self.assertEqual(item['severity'], 'degraded')
        self.assertIn('打开微信', item['fix'])

    def test_wechat_version_outside_gate_is_degraded_with_evidence(self):
        profile = healthy_profile()
        for entry in profile['items']:
            if entry['key'] == 'wechat':
                entry['status'] = 'warn'
                entry['detail'] = '微信 3.9.5 正在运行；版本未验证'
                entry['data'].update({'version': '3.9.5', 'version_ok': False})
        result = run_advice(profile=profile)
        item = pick(result, 'wechat')
        self.assertEqual(item['severity'], 'degraded')
        self.assertIn('3.9.5', item['title'])
        self.assertEqual(item['evidence']['wechat_version'], '3.9.5')
        self.assertIn('4.1.x', item['detail'])


class OcrAdviceTests(unittest.TestCase):
    def test_missing_chinese_pack_is_degraded_and_explains_fail_closed(self):
        profile = healthy_profile()
        for item in profile['items']:
            if item['key'] == 'ocr':
                item['status'] = 'warn'
                item['detail'] = '系统 OCR 可用但只有 3 种语言、没有中文识别包'
                item['data'] = {'languages': ['en-US'], 'total': 3, 'chinese': []}
        result = run_advice(profile=profile)
        item = pick(result, 'ocr')
        self.assertEqual(item['severity'], 'degraded')
        self.assertIn('fail-closed', item['detail'])
        self.assertIn('发送会被拒绝', item['detail'])
        self.assertIn('光学字符识别', item['fix'])

    def test_chinese_pack_present_is_info(self):
        item = pick(run_advice(), 'ocr')
        self.assertEqual(item['severity'], 'info')


class SandboxAdviceTests(unittest.TestCase):
    def test_unavailable_sandbox_is_info_and_says_local_direct_still_works(self):
        profile = healthy_profile()
        for item in profile['items']:
            if item['key'] == 'windows_sandbox':
                item['status'] = 'warn'
                item['detail'] = 'Windows 10 家庭中文版 不是专业版/企业版，Windows 沙盒不可用；隔离后台跑不了'
                item['data'].update({'edition_id': 'Core', 'product_name': 'Windows 10 家庭中文版',
                                     'sandbox_exe_exists': False, 'sandbox_available': False})
        result = run_advice(profile=profile)
        item = pick(result, 'windows_sandbox')
        self.assertEqual(item['severity'], 'info')
        self.assertIn('可选增强', item['detail'])
        self.assertIn('本机直连', item['detail'])
        self.assertEqual(item['evidence']['edition_id'], 'Core')


class DshAdviceTests(unittest.TestCase):
    def test_disabled_dsh_is_info(self):
        snapshot = {'settings': {'enabled': False, 'model': '', 'provider': ''},
                    'stage': 'disabled', 'last_error': ''}
        item = pick(run_advice(dsh=snapshot), 'dsh')
        self.assertEqual(item['severity'], 'info')
        self.assertIn('本地插件', item['title'])

    def test_failed_dsh_is_degraded_not_fault(self):
        snapshot = {'settings': {'enabled': True, 'model': 'deepseek-chat', 'provider': 'deepseek'},
                    'stage': 'failed', 'last_error': '连接被拒绝'}
        item = pick(run_advice(dsh=snapshot), 'dsh')
        self.assertEqual(item['severity'], 'degraded')
        self.assertIn('本地插件回复', item['title'])
        self.assertIn('降级不是故障', item['detail'])
        self.assertIn('连接被拒绝', item['detail'])

    def test_enabled_without_model_is_degraded(self):
        snapshot = {'settings': {'enabled': True, 'model': '', 'provider': ''},
                    'stage': 'configured', 'last_error': ''}
        item = pick(run_advice(dsh=snapshot), 'dsh')
        self.assertEqual(item['severity'], 'degraded')
        self.assertIn('模型', item['title'])

    def test_missing_bot_keeps_dsh_unknown(self):
        item = pick(advice.collect_advice(profile=healthy_profile(), data_root=healthy_data_root(),
                                          self_check=healthy_self_check(),
                                          channels=healthy_channels()), 'dsh')
        self.assertEqual(item['severity'], 'info')
        self.assertIn('未知', item['title'])


class PortAdviceTests(unittest.TestCase):
    def test_port_taken_by_other_process_is_blocking(self):
        profile = healthy_profile()
        for item in profile['items']:
            if item['key'] == 'port':
                item['status'] = 'warn'
                item['detail'] = '8091 被别的进程占着、又不像本控制台后台；先关掉占用者，否则后台起不来'
                item['data'] = {'port': 8091, 'self_running': False, 'bindable': False, 'version': ''}
        result = run_advice(profile=profile)
        item = pick(result, 'port')
        self.assertEqual(item['severity'], 'blocking')
        self.assertIn('8091', item['title'])
        self.assertIn('netstat', item['fix'])

    def test_version_mismatch_is_degraded(self):
        profile = healthy_profile()
        for item in profile['items']:
            if item['key'] == 'port':
                item['data'].update({'version': '1.3.50'})
        self.assertEqual(pick(run_advice(profile=profile), 'port')['severity'], 'info')
        profile = healthy_profile()
        for item in profile['items']:
            if item['key'] == 'port':
                item['data'].update({'version': '1.3.49'})
        item = pick(run_advice(profile=profile), 'port')
        self.assertEqual(item['severity'], 'degraded')
        self.assertIn('1.3.49', item['detail'])
        self.assertEqual(item['evidence']['app_version'], '1.3.50')


class ChannelAdviceTests(unittest.TestCase):
    def test_unverified_channel_is_degraded_and_points_at_cleanup(self):
        channels = {'root': 'D:/WhaleGirlWeChat/data/isolated_wechat', 'exists': True,
                    'verified': [], 'ignored': [],
                    'unverified': [{'channel_id': CHANNEL, 'short_id': CHANNEL[:12],
                                    'path': 'D:/WhaleGirlWeChat/data/isolated_wechat/' + CHANNEL,
                                    'reason': 'pairing.json 缺失',
                                    'suggestion': '可以在界面「清理残留通道」里删掉它'}],
                    'message': ''}
        result = run_advice(channels=channels)
        item = pick(result, 'isolated_channel')
        self.assertEqual(item['severity'], 'degraded')
        self.assertIn('清理', item['fix'])
        self.assertIn('二次确认', item['fix'])
        self.assertEqual(item['evidence']['unverified_count'], 1)

    def test_no_channel_dir_is_info(self):
        channels = {'root': '', 'exists': False, 'verified': [], 'unverified': [], 'ignored': [],
                    'message': '还没有隔离通道目录（没配对过隔离微信，正常）'}
        item = pick(run_advice(channels=channels), 'isolated_channel')
        self.assertEqual(item['severity'], 'info')
        self.assertIn('正常', item['detail'])


class UpgradeAdviceTests(unittest.TestCase):
    def test_upgrade_problems_are_degraded(self):
        self_check = {**healthy_self_check(), 'ok': False,
                      'problems': ['配置缺失：config.wechat.yaml 不存在'],
                      'notes': ['没有找到人设目录']}
        item = pick(run_advice(self_check=self_check), 'upgrade_data')
        self.assertEqual(item['severity'], 'degraded')
        self.assertIn('config.wechat.yaml', item['detail'])


class FallbackTests(unittest.TestCase):
    def test_probe_errors_degrade_every_item_to_info(self):
        result = advice.collect_advice(profile=object(),
                                       data_root={'probe_error': 'OSError: 磁盘掉线'},
                                       self_check={'probe_error': 'PermissionError: 拒绝访问'},
                                       channels={'probe_error': 'FileNotFoundError: 目录没了'},
                                       dsh={'probe_error': 'RuntimeError: 设置读不出来'})
        self.assertEqual(len(result['items']), len(KEYS))
        for item in result['items']:
            self.assertEqual(item['severity'], 'info')
            self.assertTrue(item['detail'])
        self.assertIn('磁盘掉线', pick(result, 'data_root')['detail'])
        self.assertIn('目录没了', pick(result, 'isolated_channel')['detail'])
        self.assertIn('设置读不出来', pick(result, 'dsh')['detail'])
        self.assertEqual(result['blocking_count'], 0)
        self.assertTrue(result['ok'])

    def test_builder_exception_becomes_info_instead_of_raising(self):
        with patch.object(advice, '_advice_wechat', side_effect=ValueError('判定炸了')):
            result = run_advice()
        item = pick(result, 'wechat')
        self.assertEqual(item['severity'], 'info')
        self.assertIn('没能判定', item['title'])
        self.assertIn('ValueError', item['detail'])


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'profile'
        self.root.mkdir()
        self.profile_probe = Mock(return_value=healthy_profile())
        self.data_root_probe = Mock(return_value=healthy_data_root())
        self.self_check_probe = Mock(return_value=healthy_self_check())
        self.channels_probe = Mock(return_value=healthy_channels())
        for target, name, replacement in (
                (advice.site_profile, 'collect', self.profile_probe),
                (advice.install_guard, 'resolve_data_root', self.data_root_probe),
                (advice.install_guard, 'upgrade_self_check', self.self_check_probe),
                (advice.install_guard, 'channel_inventory', self.channels_probe)):
            patcher = patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_probes_reuse_existing_functions_with_root(self):
        result = advice.collect_advice(self.root, bot=SimpleNamespace(root=self.root, dsh=None))
        self.assertEqual(len(result['items']), len(KEYS))
        self.profile_probe.assert_called_once_with()
        self.data_root_probe.assert_called_once_with(self.root)
        self.self_check_probe.assert_called_once_with(self.root)
        self.channels_probe.assert_called_once_with(self.root)

    def test_dsh_snapshot_only_reads_existing_state(self):
        dsh = SimpleNamespace(settings=lambda: {'enabled': True, 'model': 'm', 'provider': 'p'},
                              stage='ready', last_error='')
        snapshot = advice.dsh_snapshot(SimpleNamespace(dsh=dsh))
        self.assertEqual(snapshot['stage'], 'ready')
        self.assertEqual(snapshot['settings']['model'], 'm')
        self.assertIsNone(advice.dsh_snapshot(SimpleNamespace(dsh=None)))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'profile'
        self.root.mkdir()
        self.bot = SimpleNamespace(root=self.root, dsh=None)
        self.app = FastAPI()
        self.app.state.bot = self.bot
        advice.add_routes(self.app)
        for target, name, value in ((advice.site_profile, 'collect', healthy_profile()),
                                    (advice.install_guard, 'resolve_data_root', healthy_data_root()),
                                    (advice.install_guard, 'upgrade_self_check', healthy_self_check()),
                                    (advice.install_guard, 'channel_inventory', healthy_channels())):
            patcher = patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(self.app)

    def test_route_is_registered(self):
        paths = {getattr(route, 'path', '') for route in self.app.routes}
        self.assertIn('/api/compat/advice', paths)

    def test_route_works_without_agent_binding(self):
        # 1.3.67：兼容建议是只读诊断，全新安装还没绑 Agent 时也必须能看，
        # 否则用户第一次打开面板只会看到一句「未绑定」。
        self.assertEqual(self.client.get('/api/compat/advice').status_code, 200)

    def test_route_returns_advice_after_binding(self):
        authority(self.bot).bind({'platform': 'desktop', 'account': 'local', 'subject': 'owner'})
        response = self.client.get('/api/compat/advice')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn('summary', body)
        self.assertEqual(body['blocking_count'], 0)
        self.assertEqual([item['key'] for item in body['items']], list(KEYS))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    import wechat_gui
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover - 环境缺 PyQt6 时整类跳过
    HAVE_QT = False

_APP = None


def _qt_app():
    # 必须持有模块级引用：QApplication 被回收后再造 QWidget 会直接终止进程
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@unittest.skipUnless(HAVE_QT, '缺少 PyQt6，跳过界面接线测试')
class ConsoleWiringTests(unittest.TestCase):
    """控制台「环境与自检」区域：只读展示与二次确认，全部离线。"""

    def setUp(self):
        _qt_app()
        self.tab = wechat_gui.ConsoleTab()
        self.addCleanup(self.tab.deleteLater)
        self.calls = []

    def _record(self, owner, method, path, data=None, timeout=10, on_done=None):
        self.calls.append({'method': method, 'path': path, 'data': data})
        return None

    def _channel(self, **extra):
        payload = {'channel_id': CHANNEL, 'short_id': 'aaaaaa', 'reason': '目录名对不上'}
        payload.update(extra)
        return payload

    def test_panel_widgets_exist_and_start_disabled(self):
        for name in ('btn_compat_advice', 'btn_selfcheck', 'btn_channel_remove',
                     'channel_pick', 'channel_confirm', 'compat_view', 'guard_view'):
            self.assertTrue(hasattr(self.tab, name), name)
        self.assertFalse(self.tab.btn_channel_remove.isEnabled())
        self.assertFalse(self.tab.channel_confirm.isEnabled())
        self.assertEqual(self.tab.compat_view.toPlainText(), '')

    def test_advice_view_lists_fix_grouped_by_severity(self):
        payload = {'ok': False,
                   'summary': '这台机器现在跑不起来：1 项必须先解决，另有 1 项降级',
                   'blocking_count': 1, 'degraded_count': 1, 'info_count': 0,
                   'items': [
                       {'key': 'data_root', 'severity': 'blocking',
                        'title': '数据目录不可写，配置和聊天记录都存不下来',
                        'detail': '探测说不可写', 'fix': '换到可写的本地路径', 'evidence': {}},
                       {'key': 'ocr', 'severity': 'degraded',
                        'title': 'OCR 缺中文语言包',
                        'detail': '标题核验 fail-closed，发送会被拒绝',
                        'fix': '装中文（简体）光学字符识别', 'evidence': {}}]}

        def deliver(owner, method, path, data=None, timeout=10, on_done=None):
            self.calls.append({'method': method, 'path': path})
            if on_done is not None:
                on_done(payload)
            return payload

        with patch.object(wechat_gui, 'call_api', deliver):
            self.tab._load_compat_advice()
        self.assertEqual(self.calls, [{'method': 'GET', 'path': 'compat/advice'}])
        text = self.tab.compat_view.toPlainText()
        self.assertIn('必须先解决', text)
        self.assertIn('降级（能跑，但会退化）', text)
        self.assertIn('怎么修：换到可写的本地路径', text)
        self.assertIn('怎么修：装中文（简体）光学字符识别', text)
        self.assertTrue(self.tab.btn_compat_advice.isEnabled())
        self.assertIn('1 项必须先解决', self.tab.notice.text())

    def test_install_guard_reads_three_endpoints_in_order(self):
        payloads = {
            'install-guard/data-root': {'success': True, 'primary': '~\\profile', 'active': '~\\profile',
                                        'writable': True, 'atomic_replace': False,
                                        'fallback_used': False, 'reason': '替换被占用'},
            'install-guard/self-check': {'success': True, 'ok': True, 'message': '5 项都好',
                                         'problems': [], 'notes': [], 'items': {}},
            'install-guard/channels': {'success': True, 'verified': [{'channel_id': CHANNEL}],
                                       'unverified': [], 'message': '还没有残留通道'},
        }

        def deliver(owner, method, path, data=None, timeout=10, on_done=None):
            self.calls.append(path)
            if on_done is not None:
                on_done(payloads[path])

        with patch.object(wechat_gui, 'call_api', deliver):
            self.tab._load_install_guard()
        self.assertEqual(self.calls, ['install-guard/data-root', 'install-guard/self-check',
                                      'install-guard/channels'])
        text = self.tab.guard_view.toPlainText()
        self.assertIn('数据目录：', text)
        self.assertIn('被占用，退化为就地重写', text)
        self.assertIn('升级自检：5 项都好', text)
        self.assertIn('残留 0 个', text)
        self.assertFalse(self.tab.btn_channel_remove.isEnabled())

    def test_confirm_mismatch_sends_no_request(self):
        self.tab._fill_channels({'unverified': [self._channel()]})
        self.assertTrue(self.tab.btn_channel_remove.isEnabled())
        self.tab.channel_confirm.setText('a' * 31)
        with patch.object(wechat_gui, 'call_api', self._record):
            self.tab._remove_channel()
        self.assertEqual(self.calls, [])
        self.assertIn('二次确认没对上', self.tab.notice.text())
        self.assertTrue(self.tab.btn_channel_remove.isEnabled())

    def test_confirm_match_posts_exactly_once(self):
        self.tab._fill_channels({'unverified': [self._channel()]})
        self.tab.channel_pick.setCurrentIndex(0)
        self.tab.channel_confirm.setText(CHANNEL)
        with patch.object(wechat_gui, 'call_api', self._record):
            self.tab._remove_channel()
        removes = [call for call in self.calls if call['path'] == 'install-guard/channels/remove']
        self.assertEqual(len(removes), 1)
        self.assertEqual(removes[0]['method'], 'POST')
        self.assertEqual(removes[0]['data'], {'channel_id': CHANNEL, 'confirm': CHANNEL})

    def test_no_unverified_channels_blocks_remove(self):
        self.tab._fill_channels({'unverified': []})
        self.assertFalse(self.tab.btn_channel_remove.isEnabled())
        self.assertFalse(self.tab.channel_confirm.isEnabled())
        with patch.object(wechat_gui, 'call_api', self._record):
            self.tab._remove_channel()
        self.assertEqual(self.calls, [])
        self.assertIn('先点「安装自检」', self.tab.notice.text())


if __name__ == '__main__':
    unittest.main()
