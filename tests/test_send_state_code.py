"""发送未就绪必须给出机器可读的原因码（交接指南第 5 节）。"""
import unittest

from src.wechat_bridge import WeChatBridge


class _Bridge(WeChatBridge):
    def __init__(self, env):          # 绕过真实连接
        self.backend = 'wechatauto'
        self.requested_backend = 'auto'
        self.online = True
        self.account_name = 'acc'
        self.self_id = 'wxid_self'
        self.account_dir = 'dir'
        self._db_dir_hint = 'db'
        self.degraded = ''
        self.last_error = ''
        self._listener = object()
        self._poll_thread = None
        self.send_environment = env
        self.connected_at = 0.0
        self.last_message_at = 0.0
        self.message_count = 0
        self.login_state = 'unknown'
        self._names_db = None
        self._names_refresh_at = 0.0
        self._names_lock = __import__('threading').Lock()
        self._send_lock = __import__('threading').Lock()


import unittest

from src.wechat_bridge import WeChatBridge


def make_bridge(env):
    """用真实桥对象、只覆盖状态字段；手搓假对象会不断缺属性（踩过）。"""
    import tempfile
    bridge = WeChatBridge(tempfile.mkdtemp())
    bridge.online = True
    bridge.send_environment = env
    return bridge


class SendCodeTests(unittest.TestCase):
    def test_waiting_code_is_exposed(self):
        status = make_bridge({'ready': False, 'waiting_code': 'minimized',
                              'reason': '严格后台等待：微信已最小化'}).status()
        self.assertEqual(status['send_state'], 'waiting')
        self.assertEqual(status['send_code'], 'minimized')
        self.assertIn('最小化', status['send_reason'])

    def test_ready_has_empty_code(self):
        status = make_bridge({'ready': True}).status()
        self.assertEqual(status['send_state'], 'ready')
        self.assertEqual(status['send_code'], '')

    def test_unchecked_has_empty_code(self):
        status = make_bridge({}).status()
        self.assertEqual(status['send_state'], 'unchecked')
        self.assertEqual(status['send_code'], '')


if __name__ == '__main__':
    unittest.main()
