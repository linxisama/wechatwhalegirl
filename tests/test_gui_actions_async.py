import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import requests
import wechat_gui

class ActionTests(unittest.TestCase):
    def test_action_returns_while_request_is_pending_and_blocks_duplicate(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {'success': True, 'message': 'done'}
        window = SimpleNamespace(_busy=False, console_tab=SimpleNamespace(set_notice=Mock()),
                                 _action_finished=Mock(), _action_worker=None)
        with patch.object(wechat_gui, 'api_post', side_effect=slow) as post:
            try:
                wechat_gui.MainWindow._action(window, 'start')
                self.assertTrue(entered.wait(1))
                self.assertTrue(window._busy)
                self.assertTrue(window._action_worker.isRunning())
                wechat_gui.MainWindow._action(window, 'start')
                post.assert_called_once_with('start', None, timeout=120)
            finally:
                release.set()
                window._action_worker.wait(4000)

    def test_timeout_is_uncertain_and_never_retried(self):
        with patch.object(wechat_gui, 'backend_error', return_value=''), patch.object(
                wechat_gui._PROXY_FREE, 'post', side_effect=requests.ReadTimeout('raw')) as post:
            result = wechat_gui.api_post('start')
        self.assertFalse(result['success'])
        self.assertIn('仍在执行', result['message'])
        post.assert_called_once()

    def test_close_keeps_live_worker(self):
        event = Mock()
        window = SimpleNamespace(_action_worker=SimpleNamespace(isRunning=lambda: True),
                                 console_tab=SimpleNamespace(set_notice=Mock()))
        wechat_gui.MainWindow.closeEvent(window, event)
        event.ignore.assert_called_once()
