"""后台探测与 GET 失败必须可见；不连接真实后台、不操作微信。"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import requests
import wechat_gui as gui


class BackendReconnectTests(unittest.TestCase):
    def window(self):
        return SimpleNamespace(console_tab=SimpleNamespace(set_notice=Mock(), refresh=Mock()),
            _start_backend_inprocess=Mock(), _load_settings=Mock(),
            _backend_proc=None, _settings_loaded=False, _busy=False)

    def test_task_probe_exception_is_reported_and_does_not_start_a_backend(self):
        window = self.window()
        failure = RuntimeError('probe unavailable')
        job = gui.TaskJob(Mock(side_effect=failure))
        job.set_callback(lambda result: gui.apply_backend_probe(window, result))
        job.done.connect(job.deliver)
        with patch.object(gui, 'record_exception') as record:
            job.run()
        window.console_tab.set_notice.assert_called_once()
        message = window.console_tab.set_notice.call_args.args[0]
        self.assertIn('后台连接检查失败', message)
        self.assertIn('RuntimeError', message)
        self.assertFalse(window.console_tab.set_notice.call_args.kwargs['ok'])
        window._start_backend_inprocess.assert_not_called()
        record.assert_called_once_with(failure, message='后台连接检查失败')

    def test_malformed_probe_results_are_failures_instead_of_silent_success(self):
        for result in (None, {}, [], ('', True), ('', True, False, False),
                       ('', 'yes', False), ('', True, 1), (None, True, False)):
            with self.subTest(result=result):
                window = self.window()
                gui.apply_backend_probe(window, result)
                window.console_tab.set_notice.assert_called_once()
                self.assertIn('无效结果', window.console_tab.set_notice.call_args.args[0])
                self.assertFalse(window.console_tab.set_notice.call_args.kwargs['ok'])
                window._start_backend_inprocess.assert_not_called()

    def test_failed_probe_without_detail_still_replaces_connecting_notice(self):
        window = self.window()
        gui.apply_backend_probe(window, ('', False, False))
        window.console_tab.set_notice.assert_called_once()
        self.assertTrue(window.console_tab.set_notice.call_args.args[0])
        self.assertFalse(window.console_tab.set_notice.call_args.kwargs['ok'])
        window._start_backend_inprocess.assert_not_called()

    def test_valid_probe_keeps_existing_start_and_failure_semantics(self):
        for result, starts in ((('', True, False), 0), (('已关闭旧后台', True, True), 1),
                               (('端口被占用', False, False), 0)):
            with self.subTest(result=result):
                window = self.window()
                gui.apply_backend_probe(window, result)
                self.assertEqual(window._start_backend_inprocess.call_count, starts)
                if result[0]:
                    window.console_tab.set_notice.assert_called_once_with(result[0], ok=result[1])


class BackendGetFailureTests(unittest.TestCase):
    def request_failure(self, failure, path='status'):
        with patch.object(gui, 'backend_error', return_value=''), \
             patch.object(gui._PROXY_FREE, 'get', side_effect=failure):
            return gui.api_get(path)

    def test_get_timeout_has_explicit_failure_instead_of_empty_dictionary(self):
        result = self.request_failure(requests.Timeout('raw request details'))
        self.assertIs(result.get('success'), False)
        self.assertIn('读取后台响应超时', result['message'])
        self.assertNotIn('raw request details', result['message'])

    def test_get_connection_failure_has_reconnect_action(self):
        result = self.request_failure(requests.ConnectionError('raw connection details'))
        self.assertIs(result.get('success'), False)
        self.assertIn('无法连接后台', result['message'])
        self.assertIn('重连后台', result['message'])
        self.assertNotIn('raw connection details', result['message'])

    def test_other_get_failure_still_has_structured_error(self):
        result = self.request_failure(ValueError('raw response details'))
        self.assertIs(result.get('success'), False)
        self.assertIn('读取后台响应失败', result['message'])
        self.assertNotIn('raw response details', result['message'])

    def test_failed_status_does_not_refresh_from_stale_data_and_next_success_recovers(self):
        window = BackendReconnectTests().window()
        result = self.request_failure(requests.Timeout())
        gui.MainWindow._on_status(window, result)
        window.console_tab.set_notice.assert_called_once_with(result['message'], ok=False)
        window.console_tab.refresh.assert_not_called()
        window._load_settings.assert_not_called()
        self.assertFalse(window._settings_loaded)
        status = {'running': False, 'wechat_online': False}
        gui.MainWindow._on_status(window, status)
        window.console_tab.refresh.assert_called_once_with(status)
        window._load_settings.assert_called_once()

    def test_failed_settings_get_does_not_populate_or_enable_saving(self):
        result = self.request_failure(requests.ConnectionError(), path='settings')
        window = BackendReconnectTests().window()
        window.settings_tab = SimpleNamespace(populate=Mock(), btn_save=Mock(), save_lbl=Mock())
        def request(owner, method, path, timeout, on_done):
            on_done(result)
        with patch.object(gui, 'call_api', side_effect=request):
            gui.MainWindow._load_settings(window)
        window.settings_tab.populate.assert_not_called()
        self.assertFalse(window._settings_loaded)
        self.assertFalse(window._settings_loading)
        window.settings_tab.btn_save.setEnabled.assert_called_with(False)
        window.settings_tab.save_lbl.setText.assert_called_once_with(result['message'])


if __name__ == '__main__':
    unittest.main()
