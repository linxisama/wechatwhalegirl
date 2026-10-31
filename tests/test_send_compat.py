"""旧兼容标记的接线与禁用回归；1.3.3起永久禁止写微信进程。

保留旧案例并收紧断言：显式勾选和伪造旧回调也不能重新放行无障碍字节写入。
本文件只用假驱动，不操作真实微信。
"""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.wechat_send_worker import enable_send_compat, restrict_driver
from src.wechat_sender import IsolatedWeChatSender
from src.wechat_window import inspect_window


class RestrictDriverTests(unittest.TestCase):
    def test_every_dangerous_entrance_is_blocked(self):
        calls = []
        driver = SimpleNamespace(
            ensure_window=lambda **kw: calls.append(('ensure_window', kw)) or True,
            ensure_materialized=lambda: calls.append(('materialized',)),
            _wake_accessibility=lambda: calls.append(('wake',)),
            _hot_activate_accessibility=lambda hwnd: calls.append(('hot', hwnd)),
            _auto_login=lambda: calls.append(('login',)),
        )
        restrict_driver(driver)
        self.assertTrue(driver.ensure_window(wake=True, timeout=3))
        # ensure_window 的 wake 参数被强制为 False：不许库自己临时唤醒
        self.assertEqual(calls[-1], ('ensure_window', {'wake': False, 'timeout': 3}))
        self.assertFalse(driver._wake_accessibility())
        self.assertFalse(driver._hot_activate_accessibility(1))
        self.assertFalse(driver._auto_login())
        self.assertFalse(driver.ensure_materialized())
        self.assertEqual([name for name, *_ in calls], ['ensure_window'])


class EnableSendCompatTests(unittest.TestCase):
    def _driver(self, calls):
        return SimpleNamespace(
            ensure_window=lambda **kw: True,
            ensure_materialized=lambda: True,
            _wake_accessibility=lambda: calls.append('wake') or calls.append('hot-from-wake') or True,
            _hot_activate_accessibility=lambda hwnd=None: calls.append(('hot', hwnd)) or True,
            _auto_login=lambda: True,
        )

    def test_only_the_wake_call_is_let_through_and_then_reblocked(self):
        calls = []
        driver = self._driver(calls)
        entrance = restrict_driver(driver)
        self.assertFalse(driver._wake_accessibility())
        self.assertFalse(driver._hot_activate_accessibility(1))
        self.assertFalse(enable_send_compat(driver, entrance))
        self.assertEqual(calls, [])
        self.assertEqual(entrance, {'wake':None, 'hot':None})
        # 唤醒之后立刻恢复禁用：库不能再自己写内存
        self.assertFalse(driver._hot_activate_accessibility(2))

    def test_failure_is_reported_instead_of_raising(self):
        def boom():
            raise RuntimeError('扫描 DLL 失败')
        calls = []
        driver = SimpleNamespace(ensure_window=lambda **kw: True,
                                 _wake_accessibility=boom,
                                 _hot_activate_accessibility=lambda hwnd=None: calls.append('hot') or True,
                                 _auto_login=lambda: True)
        entrance = restrict_driver(driver)
        self.assertFalse(enable_send_compat(driver, entrance))
        # 异常也要把禁用状态恢复回去，而且没有偷偷调到写内存那一步
        self.assertFalse(driver._hot_activate_accessibility(3))
        self.assertEqual(calls, [])
        self.assertFalse(enable_send_compat(driver, {}))
        self.assertFalse(enable_send_compat(driver, None))


class RestrictDriverReturnTests(unittest.TestCase):
    def test_driver_without_the_methods_still_gets_blocked(self):
        driver = SimpleNamespace(ensure_window=lambda **kw: True)
        entrance = restrict_driver(driver)
        self.assertIsNone(entrance['wake'])
        self.assertFalse(driver.ensure_materialized())
        self.assertFalse(driver._auto_login())
        self.assertFalse(enable_send_compat(driver, entrance))


class JobPlumbingTests(unittest.TestCase):
    def _sender(self, compat):
        return IsolatedWeChatSender(Path('.'), 'account-path', compat=compat)

    def test_flag_is_sent_with_every_job(self):
        self.assertTrue(self._sender(lambda: True)._prepare_job({'action': 'text'})['send_compat'])
        self.assertFalse(self._sender(lambda: False)._prepare_job({'action': 'text'})['send_compat'])
        self.assertEqual(self._sender(lambda: True)._prepare_job({})['account_path'], 'account-path')

    def test_default_is_off_and_broken_supplier_fails_closed(self):
        self.assertFalse(IsolatedWeChatSender(Path('.'))._prepare_job({})['send_compat'])

        def boom():
            raise RuntimeError('配置读不到')

        self.assertFalse(self._sender(boom)._prepare_job({})['send_compat'])


class _FakeAccess:
    """只给一个外壳窗口，没有任何 mmui 控件 —— 就是微信 4.x 的默认状态。"""

    def __init__(self):
        self.gui_rows = [{'hwnd': 10, 'pid': 1, 'minimized': False, 'native_class': 'Qt51514QWindowIcon'}]

    def candidates(self):
        return self.gui_rows

    def matches_account(self, pid, account_path):
        return True

    def control(self, hwnd):
        return SimpleNamespace(ClassName='Qt51514QWindowIcon')

    def minimized(self, hwnd):
        return False


class InspectWindowReasonTests(unittest.TestCase):
    """检查结果里的原因必须能让用户知道"去打开那个开关"，而不是只说没找到控件。"""

    def setUp(self):
        self.lookup = {'visited': 3, 'search_status': 'exhausted', 'errors': 0,
                       'classes': ['MMUIRenderSubWindowHW', 'Qt51514QWindowIcon']}

    def _inspect(self, send_compat):
        # find_scoped 是在函数内部 import 的，所以补丁要打在 wechat_send_worker 上
        def fake_scoped(anchor, predicate, timeout=2, diagnostic=None, **kwargs):
            if diagnostic is not None:
                diagnostic.update(self.lookup)
            return None
        with patch('src.wechat_send_worker.find_scoped', side_effect=fake_scoped):
            return inspect_window('', access=_FakeAccess(),
                                  desktop_status=lambda: (True, ''), send_compat=send_compat)

    def test_switch_off_points_at_the_setting(self):
        diagnostic = self._inspect(False)
        self.assertFalse(diagnostic['ready'])
        self.assertFalse(diagnostic['send_compat'])
        self.assertIn('发送兼容模式', diagnostic['reason'])
        self.assertIn('控件树', diagnostic['reason'])

    def test_switch_on_says_it_is_still_not_materialised(self):
        diagnostic = self._inspect(True)
        self.assertFalse(diagnostic['ready'])
        self.assertTrue(diagnostic['send_compat'])
        self.assertIn('已开启发送兼容模式', diagnostic['reason'])


if __name__ == '__main__':
    unittest.main()
