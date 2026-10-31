"""1.3.64 自动更新模块测试。"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import updater
from src.update_service import UpdateService
from src import update_service
from src.update_api import register


SHA_A = 'a' * 64
SHA_B = 'b' * 64


class FakeResponse:
    def __init__(self, data: bytes, headers=None):
        self.data = data
        self.pos = 0
        self.headers = headers or {}
        self.closed = False

    def read(self, size=-1):
        if self.pos >= len(self.data):
            return b''
        if size is None or size < 0:
            size = len(self.data) - self.pos
        block = self.data[self.pos:self.pos + size]
        self.pos += len(block)
        return block

    def close(self):
        self.closed = True


class InterruptingResponse(FakeResponse):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.calls = 0

    def read(self, size=-1):
        self.calls += 1
        if self.calls == 1:
            return self.data[:4]
        raise urllib.error.URLError('simulated disconnect')


def _opener(response):
    def open_it(request, timeout=0):
        return response
    return open_it


def _wait(predicate, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _release(version='1.3.64', sha=SHA_B, size=5):
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


def _make_downloaded(tmp, sha=None, payload=b'abcde'):
    """构造一个已经进入 downloaded 状态的服务，返回 (service, path)。"""
    if sha is None:
        sha = hashlib.sha256(payload).hexdigest()
    def fake_fetch(url, timeout=0):
        return _release(sha=sha, size=len(payload))

    def fake_download(url, dest, **kwargs):
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {'ok': True, 'path': str(path), 'size': len(payload),
                'sha256': hashlib.sha256(payload).hexdigest()}

    service = UpdateService(Path(tmp), '1.3.62', feed_url='https://feed.test/latest',
                            fetch=fake_fetch, download=fake_download)
    service.check()
    assert _wait(lambda: service.status()['state'] == 'available'), service.status()
    service.download()
    assert _wait(lambda: service.status()['state'] == 'downloaded'), service.status()
    return service, Path(service.status()['downloaded_path'])


class VersionTests(unittest.TestCase):
    def test_parse_plain(self):
        self.assertEqual(updater.parse_version('1.3.62'), (1, 3, 62))

    def test_parse_v_prefix(self):
        self.assertEqual(updater.parse_version('v1.3.62'), (1, 3, 62))

    def test_parse_prerelease_numbers(self):
        self.assertEqual(updater.parse_version('1.3.62-beta.1'), (1, 3, 62, 1))

    def test_parse_missing_parts(self):
        self.assertEqual(updater.parse_version('1.3'), (1, 3, 0))

    def test_parse_invalid(self):
        self.assertEqual(updater.parse_version('not-a-version'), ())

    def test_parse_empty(self):
        self.assertEqual(updater.parse_version(''), ())

    def test_is_newer(self):
        self.assertTrue(updater.is_newer('1.3.64', '1.3.62'))

    def test_is_newer_equal_false(self):
        self.assertFalse(updater.is_newer('v1.3.62', '1.3.62'))


class ParseReleaseTests(unittest.TestCase):
    def test_github_shape(self):
        payload = {
            'tag_name': 'v1.3.64', 'body': '说明', 'published_at': '2026-01-02',
            'assets': [
                {'name': 'WhaleGirlWeChat-Setup-1.3.64.exe', 'size': 12,
                 'browser_download_url': 'https://x/a.exe', 'digest': 'sha256:' + SHA_A},
                {'name': 'a.exe.sha256', 'size': 65,
                 'browser_download_url': 'https://x/a.exe.sha256'},
            ],
        }
        result = updater.parse_release(payload)
        self.assertEqual(result['version'], '1.3.64')
        self.assertEqual(result['notes'], '说明')
        self.assertEqual([a['kind'] for a in result['assets']], ['installer', 'checksum'])
        self.assertEqual(result['assets'][0]['sha256'], SHA_A)

    def test_custom_flat_shape(self):
        result = updater.parse_release({
            'version': '1.3.64', 'notes': '说明', 'name': 'WhaleGirlWeChat-Setup-1.3.64.exe',
            'url': 'https://x/a.exe', 'size': 100, 'sha256': SHA_A,
        })
        self.assertEqual(result['version'], '1.3.64')
        self.assertEqual(result['assets'][0]['kind'], 'installer')
        self.assertEqual(result['assets'][0]['size'], 100)

    def test_update_json_shape(self):
        result = updater.parse_release({
            'version': '1.3.64', 'notes': '说明',
            'installer': {'name': 'WhaleGirlWeChat-Setup-1.3.64.exe',
                          'url': 'https://x/a.exe', 'size': 9,
                          'sha256': SHA_A, 'checksum_url': 'https://x/a.exe.sha256'},
        })
        self.assertEqual(len(result['assets']), 2)
        self.assertEqual(result['assets'][1]['kind'], 'checksum')

    def test_no_assets(self):
        result = updater.parse_release({'tag_name': 'v1.3.64', 'body': '说明'})
        self.assertEqual(result['version'], '1.3.64')
        self.assertEqual(result['assets'], [])

    def test_prerelease_and_digest(self):
        result = updater.parse_release({
            'tag_name': 'v1.3.64-beta.2',
            'assets': [{'name': 'WhaleGirlWeChat-Setup-1.3.64.exe',
                        'browser_download_url': 'https://x/a.exe',
                        'digest': 'sha256:' + SHA_B}],
        })
        self.assertEqual(result['version'], '1.3.64-beta.2')
        self.assertEqual(result['assets'][0]['sha256'], SHA_B)


class FetchReleaseTests(unittest.TestCase):
    def test_relative_asset_urls_are_resolved(self):
        payload = {
            'version': '1.3.64', 'notes': '说明',
            'installer': {
                'name': 'WhaleGirlWeChat-Setup-1.3.64.exe',
                'url': 'WhaleGirlWeChat-Setup-1.3.64.exe',
                'size': 3, 'sha256': SHA_A,
                'checksum_url': 'WhaleGirlWeChat-Setup-1.3.64.exe.sha256',
            },
        }
        opener = _opener(FakeResponse(json.dumps(payload).encode('utf-8')))
        result = updater.fetch_release('https://host.test/releases/update.json', opener=opener)
        self.assertEqual(result['assets'][0]['url'],
                         'https://host.test/releases/WhaleGirlWeChat-Setup-1.3.64.exe')
        self.assertEqual(result['assets'][1]['url'],
                         'https://host.test/releases/WhaleGirlWeChat-Setup-1.3.64.exe.sha256')


class CheckTests(unittest.TestCase):
    def test_checksum_asset_is_linked_to_installer(self):
        release = _release()
        release['assets'].append({
            'name': 'WhaleGirlWeChat-Setup-1.3.64.exe.sha256',
            'url': 'https://feed.test/WhaleGirlWeChat-Setup-1.3.64.exe.sha256',
            'size': 65, 'sha256': '', 'kind': 'checksum',
        })
        with mock.patch.object(updater, 'fetch_release', return_value=release):
            result = updater.check('https://feed.test/latest', '1.3.62')
        self.assertEqual(result['state'], 'update_available')
        self.assertEqual(result['installer']['checksum_url'],
                         'https://feed.test/WhaleGirlWeChat-Setup-1.3.64.exe.sha256')

    def test_up_to_date(self):
        with mock.patch.object(updater, 'fetch_release', return_value=_release('1.3.62')):
            result = updater.check('https://feed.test/latest', '1.3.62')
        self.assertEqual(result['state'], 'up_to_date')
        self.assertIsNone(result['error'])

    def test_update_available(self):
        with mock.patch.object(updater, 'fetch_release', return_value=_release()):
            result = updater.check('https://feed.test/latest', '1.3.62')
        self.assertEqual(result['state'], 'update_available')
        self.assertEqual(result['installer']['name'], 'WhaleGirlWeChat-Setup-1.3.64.exe')

    def test_offline(self):
        with mock.patch.object(updater, 'fetch_release',
                               side_effect=updater.UpdateError('offline', '网络不可用')):
            result = updater.check('https://feed.test/latest', '1.3.62')
        self.assertEqual(result['state'], 'error')
        self.assertEqual(result['error']['reason'], 'offline')

    def test_not_configured(self):
        result = updater.check('', '1.3.62')
        self.assertEqual(result['state'], 'error')
        self.assertEqual(result['error']['reason'], 'not_configured')


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / 'pkg.exe'

    def test_success_with_sha(self):
        data = b'hello update package'
        seen = []
        result = updater.download(
            'https://x/a.exe', self.dest, expected_size=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
            progress=lambda got, total: seen.append((got, total)),
            opener=_opener(FakeResponse(data)), chunk=3)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.dest.read_bytes(), data)
        self.assertFalse(self.dest.with_name(self.dest.name + '.part').exists())
        self.assertTrue(seen)

    def test_checksum_mismatch_removes_files(self):
        self.dest.write_bytes(b'old')
        result = updater.download('https://x/a.exe', self.dest,
                                  expected_sha256='0' * 64,
                                  opener=_opener(FakeResponse(b'new')))
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'checksum_mismatch')
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name(self.dest.name + '.part').exists())

    def test_size_mismatch(self):
        self.dest.write_bytes(b'old')
        result = updater.download('https://x/a.exe', self.dest, expected_size=99,
                                  opener=_opener(FakeResponse(b'short')))
        self.assertEqual(result['reason'], 'size_mismatch')
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name(self.dest.name + '.part').exists())

    def test_interrupted(self):
        self.dest.write_bytes(b'old')
        result = updater.download('https://x/a.exe', self.dest,
                                  opener=_opener(InterruptingResponse(b'partial')))
        self.assertEqual(result['reason'], 'interrupted')
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name(self.dest.name + '.part').exists())

    def test_too_large(self):
        result = updater.download('https://x/a.exe', self.dest,
                                  expected_size=updater.MAX_UPDATE_BYTES + 1)
        self.assertEqual(result['reason'], 'too_large')

    def test_progress_callback_error_is_ignored(self):
        data = b'payload'
        def bad_progress(got, total):
            raise RuntimeError('ui callback failed')
        result = updater.download('https://x/a.exe', self.dest, expected_size=len(data),
                                  expected_sha256=hashlib.sha256(data).hexdigest(),
                                  progress=bad_progress, opener=_opener(FakeResponse(data)))
        self.assertTrue(result['ok'], result)


class InstallTests(unittest.TestCase):
    def test_not_downloaded_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = UpdateService(Path(tmp), '1.3.62')
            result = service.install()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'not_downloaded')

    def test_changed_sha_rejected_without_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, path = _make_downloaded(tmp)
            path.write_bytes(b'changed')
            with mock.patch.object(update_service, 'launch_installer') as launch:
                result = service.install()
            self.assertFalse(result['ok'])
            self.assertEqual(result['error']['reason'], 'checksum_mismatch')
            launch.assert_not_called()

    def test_success_launches_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _make_downloaded(tmp)
            with mock.patch.object(update_service, 'launch_installer',
                                   return_value={'ok': True, 'pid': 4321}) as launch:
                result = service.install()
            self.assertTrue(result['ok'])
            self.assertEqual(result['state'], 'installing')
            self.assertEqual(result['installer_pid'], 4321)
            launch.assert_called_once()


class ServiceTests(unittest.TestCase):
    def test_state_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _make_downloaded(tmp)
            self.assertEqual(service.status()['state'], 'downloaded')
            with mock.patch.object(update_service, 'launch_installer',
                                   return_value={'ok': True, 'pid': 7}):
                result = service.install()
            self.assertTrue(result['ok'])
            self.assertEqual(result['state'], 'installing')

    def test_skip_effective(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = UpdateService(Path(tmp), '1.3.62', feed_url='https://feed.test/latest',
                                    fetch=lambda url, timeout=0: _release())
            service.check()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'available'))
            service.skip('1.3.64')
            service.check(force=True)
            self.assertTrue(_wait(lambda: service.status()['state'] == 'up_to_date'))
            self.assertTrue(service.status()['skipped'])

    def test_concurrent_check_only_one(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow_fetch(url, timeout=0):
            calls.append(1)
            started.set()
            release.wait(3)
            return _release()

        with tempfile.TemporaryDirectory() as tmp:
            service = UpdateService(Path(tmp), '1.3.62', feed_url='https://feed.test/latest',
                                    fetch=slow_fetch)
            service.check()
            self.assertTrue(started.wait(2))
            second = service.check()
            self.assertEqual(second['state'], 'checking')
            self.assertEqual(len(calls), 1)
            release.set()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'available'))

    def test_download_fetches_checksum_when_digest_missing(self):
        payload = b'payload'
        digest = hashlib.sha256(payload).hexdigest()
        def fake_fetch(url, timeout=0):
            release = _release(sha='', size=len(payload))
            release['assets'][0]['sha256'] = ''
            release['assets'].append({
                'name': 'WhaleGirlWeChat-Setup-1.3.64.exe.sha256',
                'url': 'https://feed.test/WhaleGirlWeChat-Setup-1.3.64.exe.sha256',
                'size': 65, 'sha256': '', 'kind': 'checksum',
            })
            return release
        seen = []
        def fake_download(url, dest, **kwargs):
            seen.append(kwargs.get('expected_sha256'))
            path = Path(dest)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            return {'ok': True, 'path': str(path), 'size': len(payload),
                    'sha256': digest}
        with tempfile.TemporaryDirectory() as tmp:
            service = UpdateService(Path(tmp), '1.3.62', feed_url='https://feed.test/latest',
                                    fetch=fake_fetch, download=fake_download)
            service.check()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'available'))
            with mock.patch.object(update_service, 'fetch_checksum',
                                   return_value={'ok': True, 'sha256': digest}):
                service.download()
                self.assertTrue(_wait(lambda: service.status()['state'] == 'downloaded'))
            self.assertEqual(seen, [digest])
            self.assertEqual(service.status()['installer']['sha256'], digest)

    def test_download_can_retry_after_failed_attempt(self):
        payload = b'abcde'
        digest = hashlib.sha256(payload).hexdigest()
        calls = []
        def fake_download(url, dest, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return {'ok': False, 'reason': 'offline', 'message': '模拟断网'}
            path = Path(dest)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            return {'ok': True, 'path': str(path), 'size': len(payload),
                    'sha256': digest}
        with tempfile.TemporaryDirectory() as tmp:
            service = UpdateService(Path(tmp), '1.3.62', feed_url='https://feed.test/latest',
                                    fetch=lambda url, timeout=0: _release(sha=digest),
                                    download=fake_download)
            service.check()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'available'))
            service.download()
            self.assertTrue(_wait(lambda: service.status()['state'] == 'error'))
            second = service.download()
            self.assertNotEqual(second.get('state'), 'error')
            self.assertTrue(_wait(lambda: service.status()['state'] == 'downloaded'))
            self.assertEqual(len(calls), 2)

    def test_corrupt_state_file_does_not_break_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'update_state.json').write_text('{not json', encoding='utf-8')
            service = UpdateService(Path(tmp), '1.3.62')
            self.assertEqual(service.status()['state'], 'idle')


class ApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        self.app = FastAPI()

        class StubService:
            def __init__(self):
                self.force = None
                self.configured = None
                self.state = {'state': 'idle', 'current': '1.3.62', 'latest': '',
                              'notes': '', 'progress': {'received': 0, 'total': 0, 'percent': 0},
                              'error': None, 'last_check_at': None, 'next_check_at': None,
                              'auto': True, 'feed_url': '', 'downloaded_path': '',
                              'installing': False}
            def status(self):
                return self.state
            def check(self, force=False):
                self.force = force
                return self.state
            def configure(self, feed_url=None, auto_check=None, interval_hours=None):
                self.configured = (feed_url, auto_check, interval_hours)
                return self.state

        self.stub = StubService()
        self.runtime = SimpleNamespace(update_service=self.stub, config={})
        register(self.app, self.runtime)
        self.client = TestClient(self.app)

    def test_status(self):
        response = self.client.get('/api/update/status')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['status']['state'], 'idle')

    def test_check(self):
        response = self.client.post('/api/update/check', json={'force': True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertTrue(self.stub.force)

    def test_settings(self):
        response = self.client.post('/api/update/settings',
                                    json={'auto_check': False, 'interval_hours': 12})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(self.stub.configured, (None, False, 12.0))


if __name__ == '__main__':
    unittest.main()
