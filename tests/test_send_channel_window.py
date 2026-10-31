"""发送通道的两个真问题（2026-10-20 实测）：

1. `background_send`（严格后台）与 `send_compat`（兼容唤醒）同时开 → worker 先走
   background 分支，永远等不到输入控件，控制台一直是"发送未就绪"。两者必须互斥。
2. 微信升级到 4.1.15.56 后，`matches_account` 反查不到账号目录 → 窗口匹配失败
   （`materialized=True` 但驱动拿不到窗口）→ 发送全挂。单窗口时应当兜底沿用，
   但仍然保留"发送前校验会话名"这道保险。
"""

import unittest
from types import SimpleNamespace

from src.wechat_window import select_window


class _Access:
    """`unknown=True` 模拟 psutil 拿不到进程信息（AccessDenied）的情况。"""

    def __init__(self, rows, matched_pids=(), unknown=False):
        self._rows = rows
        self._matched = set(matched_pids)
        self._unknown = unknown

    def candidates(self):
        return list(self._rows)

    def matches_account(self, pid, path):
        if self._unknown:
            raise PermissionError('AccessDenied')
        return pid in self._matched


def row(hwnd, pid, minimized=False):
    return {'hwnd': hwnd, 'pid': pid, 'minimized': minimized}


class SelectWindowTests(unittest.TestCase):
    def test_account_match_still_wins(self):
        access = _Access([row(10, 1), row(20, 2)], matched_pids=[2])
        selected, diagnostic = select_window(access, 'bound')
        self.assertEqual(selected['hwnd'], 20)
        self.assertEqual(diagnostic['matched_count'], 1)
        self.assertNotIn('fallback', diagnostic)

    def test_single_window_is_rejected_when_account_probe_is_unknown(self):
        access = _Access([row(10, 1)], unknown=True)   # 查不到（权限/版本），只能沿用唯一窗口
        selected, diagnostic = select_window(access, 'bound')
        self.assertIsNone(selected)
        self.assertEqual(diagnostic['waiting_code'], 'account_unverified')

    def test_multiple_windows_are_refused_even_when_probe_is_unknown(self):
        # 单窗口按设计沿用（微信新版反查不到账号）；多个窗口且对不上时必须拒绝。
        for access in (_Access([row(10, 1), row(20, 2)]),
                       _Access([row(10, 1), row(20, 2)], unknown=True)):
            selected, _ = select_window(access, 'bound')
            self.assertIsNone(selected)

    def test_multiple_windows_without_account_match_is_refused(self):
        """两个窗口又不确定是哪个账号 —— 宁可不发，也不能打进别人窗口。"""
        access = _Access([row(10, 1), row(20, 2)])
        selected, diagnostic = select_window(access, 'bound')
        self.assertIsNone(selected)
        self.assertIn('无法把已选账号与微信窗口对应', diagnostic['reason'])

    def test_no_window_at_all(self):
        selected, diagnostic = select_window(_Access([]), 'bound')
        self.assertIsNone(selected)
        self.assertIn('无法把已选账号', diagnostic['reason'])


class SendModeTests(unittest.TestCase):
    """严格后台与兼容模式互斥（后端保存时强制）。"""

    def test_backend_forces_background_off_when_compat_is_on(self):
        import tempfile
        from pathlib import Path
        import yaml
        from fastapi.testclient import TestClient
        from src.wechat_app import WeChatRuntime, create_app

        root = Path(tempfile.mkdtemp(prefix='mode-'))
        config = root / 'config.wechat.yaml'
        config.write_text(yaml.safe_dump({
            'ai': {'api_url': 'https://api.deepseek.com/v1', 'api_key': 'k', 'model': 'm'},
            'wechat': {'backend': 'mock'}, 'wechat_only': {'backend': 'mock'},
        }, allow_unicode=True), 'utf-8')
        runtime = WeChatRuntime(root=root, config_path=config, db_path=root / 'm.sqlite3')
        try:
            with TestClient(create_app(runtime)) as client:
                body = {'backend': 'mock', 'db_dir': '', 'account': '', 'groups': [],
                        'listen_private': True, 'api_url': 'https://api.deepseek.com/v1',
                        'api_key': '', 'model': 'm', 'api_style': 'auto',
                        'send_compat': True, 'background_send': True}
                self.assertTrue(client.post('/api/settings', json=body).json()['success'])
                self.assertFalse(runtime.config.get('wechat.background_send'))
                self.assertTrue(runtime.config.get('wechat.send_compat'))
        finally:
            runtime.db.close()


if __name__ == '__main__':
    unittest.main()
