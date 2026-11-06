"""1.3.92 自动下载更新包测试：check → available → 后台下载 → downloaded。

自动下载只下载、只校验，绝不自动安装；安装必须由用户确认。
"""
from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.update_service import UpdateService
from src.update_api import get_service, register


SHA_B = 'b' * 64
PAYLOAD = b'abcde'


def _wait(predicate, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _wait_idle(service, timeout=4.0):
    """等后台线程归零，避免下一步撞上“已有任务在进行”。"""
    return _wait(lambda: getattr(service, '_thread', None) is None, timeout)


def _release(version='1.3.64', sha=SHA_B, size=len(PAYLOAD)):
    return {
        'version': version,
        'notes': '修复若干问题',
        'published_at': '2026-01-01T00:00:00Z',
        'assets': [{
            'name': 'WhaleGirlWeChat-Setup-%s.exe' % version,
            'url': 'https://example.test/WhaleGirlWeChat-Setup-%s.exe' % version,
            'size': size,
            'sha256': sha,
            'kind': 'installer',
        }],
    }


def _ok_download(payload=PAYLOAD):
    """成功的下载替身：真的落盘，返回真实摘要。"""
    digest = hashlib.sha256(payload).hexdigest()

    def fake_download(url, dest, **kwargs):
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {'ok': True, 'path': str(path), 'size': len(payload), 'sha256': digest}

    return fake_download


def _fail_download():
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        return {'ok': False, 'reason': 'offline', 'message': '模拟断网'}

    return fake_download, calls


def _service(tmp, download, auto_download=True):
    return UpdateService(Path(tmp), '1.3.62', feed_url='https://feed.test/latest',
                         auto_download=auto_download,
                         fetch=lambda url, timeout=0: _release(),
                         download=download)


class AutoDownloadTests(unittest.TestCase):
    def test_default_auto_download_is_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = UpdateService(Path(tmp), '1.3.62')
            self.assertTrue(service.auto_download)
            self.assertIs(service.status()['auto_download'], True)

    def test_status_exposes_auto_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = UpdateService(Path(tmp), '1.3.62', auto_download=False)
            self.assertIs(service.status()['auto_download'], False)
            # 落盘的最后结果也要带上，重启后界面才读得到。
            self.assertIs(service._status_result()['auto_download'], False)

    def test_check_reaches_downloaded_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            ok = _ok_download()

            def counting(url, dest, **kwargs):
                calls.append(url)
                return ok(url, dest, **kwargs)

            service = _service(tmp, counting)
            service.check()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'downloaded'),
                            service.status())
            status = service.status()
            self.assertEqual(len(calls), 1)
            self.assertEqual(status['installer']['sha256'], SHA_B)
            downloaded = Path(status['downloaded_path'])
            self.assertTrue(downloaded.is_file())
            # 落盘的字节要和摘要对得上，才算真的校验通过。
            self.assertEqual(hashlib.sha256(downloaded.read_bytes()).hexdigest(),
                             hashlib.sha256(PAYLOAD).hexdigest())
            # 只下载不安装：必须停在 downloaded 等用户点安装。
            self.assertFalse(status['installing'])
            self.assertEqual(status['state'], 'downloaded')

    def test_disabled_auto_download_stays_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, calls = _fail_download()
            service = _service(tmp, fake, auto_download=False)
            service.check()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'available'))
            time.sleep(service.AUTO_DOWNLOAD_DELAY + 0.5)
            self.assertEqual(service.status()['state'], 'available')
            self.assertEqual(calls, [])

    def test_auto_download_failure_lands_in_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, calls = _fail_download()
            service = _service(tmp, fake)
            service.check()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'error'),
                            service.status())
            status = service.status()
            self.assertEqual(status['error']['reason'], 'offline')
            # 失败不许卡在 downloading，也不许重试。
            self.assertEqual(len(calls), 1)
            self.assertTrue(_wait_idle(service))

    def test_same_version_is_not_retried_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, calls = _fail_download()
            service = _service(tmp, fake)
            service.check()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'error'))
            self.assertTrue(_wait_idle(service))
            # 同一版本再检查一次：回到 available，但不再自动下载。
            service.check(force=True)
            self.assertTrue(_wait(lambda: service.status()['state'] == 'available'),
                            service.status())
            time.sleep(service.AUTO_DOWNLOAD_DELAY + 0.5)
            self.assertEqual(service.status()['state'], 'available')
            self.assertEqual(len(calls), 1)

    def test_manual_download_still_retries_after_auto_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = _ok_download()
            calls = []

            def flaky(url, dest, **kwargs):
                calls.append(url)
                if len(calls) == 1:
                    return {'ok': False, 'reason': 'offline', 'message': '模拟断网'}
                return ok(url, dest, **kwargs)

            service = _service(tmp, flaky)
            service.check()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'error'))
            self.assertTrue(_wait_idle(service))
            # 用户手动点下载属于显式重试，必须还能成功。
            result = service.download()
            self.assertNotEqual(result.get('state'), 'error')
            self.assertTrue(_wait(lambda: service.status()['state'] == 'downloaded'),
                            service.status())
            self.assertEqual(len(calls), 2)

    def test_configure_toggles_auto_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = UpdateService(Path(tmp), '1.3.62')
            self.assertIs(service.configure(auto_download=False)['auto_download'], False)
            self.assertIs(service.configure(auto_download=True)['auto_download'], True)
            # 不传就保持原值。
            self.assertIs(service.configure()['auto_download'], True)

    def test_configure_reenabling_allows_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, calls = _fail_download()
            service = _service(tmp, fake)
            service.check()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'error'))
            self.assertTrue(_wait_idle(service))
            # 关掉再打开，等于用户显式重新授权自动下载，失败记忆要清掉。
            service.configure(auto_download=False)
            service.configure(auto_download=True)
            service.check(force=True)
            self.assertTrue(_wait(lambda: len(calls) == 2), calls)
            self.assertTrue(_wait(lambda: service.status()['state'] == 'error'),
                            service.status())


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        self.app = FastAPI()

        class StubService:
            def __init__(self):
                self.configured = None
                self.state = {'state': 'idle', 'current': '1.3.62', 'latest': '',
                              'notes': '', 'progress': {'received': 0, 'total': 0, 'percent': 0},
                              'error': None, 'last_check_at': None, 'next_check_at': None,
                              'auto': True, 'auto_download': True, 'feed_url': '',
                              'downloaded_path': '', 'installing': False}

            def status(self):
                return self.state

            def check(self, force=False):
                return self.state

            def configure(self, feed_url=None, auto_check=None, interval_hours=None,
                          auto_download=None):
                self.configured = (feed_url, auto_check, interval_hours, auto_download)
                if auto_download is not None:
                    self.state = dict(self.state, auto_download=auto_download)
                return self.state

        self.stub = StubService()
        self.runtime = SimpleNamespace(update_service=self.stub, config={})
        register(self.app, self.runtime)
        self.client = TestClient(self.app)

    def test_settings_accepts_auto_download(self):
        response = self.client.post('/api/update/settings', json={'auto_download': False})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'], body)
        self.assertEqual(self.stub.configured, (None, None, None, False))
        self.assertIs(self.runtime.config['update']['auto_download'], False)
        self.assertIs(body['status']['auto_download'], False)

    def test_settings_rejects_non_bool_auto_download(self):
        response = self.client.post('/api/update/settings', json={'auto_download': 'yes'})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body['ok'])
        self.assertEqual(body['reason'], 'bad_request')
        self.assertEqual(body['message'], '自动下载开关必须是布尔值')
        self.assertIsNone(self.stub.configured)

    def test_settings_without_auto_download_keeps_old_call_shape(self):
        response = self.client.post('/api/update/settings', json={'auto_check': False})
        self.assertTrue(response.json()['ok'])
        self.assertEqual(self.stub.configured, (None, False, None, None))


class ServiceFactoryTests(unittest.TestCase):
    def test_get_service_reads_auto_download_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = SimpleNamespace(update_data_dir=tmp, app_version='1.3.62',
                                      config={'update': {'feed_url': '',
                                                         'auto_download': False}})
            self.assertFalse(get_service(runtime).status()['auto_download'])

    def test_get_service_defaults_auto_download_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = SimpleNamespace(update_data_dir=tmp, app_version='1.3.62',
                                      config={'update': {'feed_url': ''}})
            self.assertTrue(get_service(runtime).status()['auto_download'])


if __name__ == '__main__':
    unittest.main()
