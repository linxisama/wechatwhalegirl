"""1.3.76：安装标记（install-marker.txt）——不看退出码也能判断装没装上。"""
import tempfile
import unittest
from pathlib import Path

from src import install_guard as guard


class MarkerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_marker(self, text):
        (self.folder / guard.MARKER_NAME).write_text(text, encoding='utf-8')


class ReadMarkerTests(MarkerCase):
    def test_missing_marker_explains_itself(self):
        state = guard.read_install_marker(self.folder)
        self.assertFalse(state['exists'])
        self.assertEqual(state['version'], '')
        self.assertIn('install-marker.txt', state['note'])

    def test_marker_is_parsed(self):
        self.write_marker('version=1.3.76\r\ninstalled=2026-11-02 10:00:00\r\nexe=C:\\x\\WhaleGirlWeChat.exe\r\n')
        state = guard.read_install_marker(self.folder)
        self.assertTrue(state['exists'])
        self.assertEqual(state['version'], '1.3.76')
        self.assertEqual(state['installed_at'], '2026-11-02 10:00:00')
        self.assertIn('1.3.76', state['note'])

    def test_marker_without_version_is_flagged(self):
        self.write_marker('installed=2026-11-02 10:00:00\r\n')
        state = guard.read_install_marker(self.folder)
        self.assertTrue(state['exists'])
        self.assertEqual(state['version'], '')
        self.assertIn('没有版本号', state['note'])


class MismatchTests(MarkerCase):
    def test_missing_when_no_marker(self):
        report = guard.marker_mismatch(self.folder, '1.3.76')
        self.assertEqual(report['state'], 'missing')
        self.assertEqual(report['running_version'], '1.3.76')

    def test_match_when_versions_equal(self):
        self.write_marker('version=1.3.76\r\ninstalled=2026-11-02 10:00:00\r\n')
        report = guard.marker_mismatch(self.folder, '1.3.76')
        self.assertEqual(report['state'], 'match')

    def test_mismatch_when_versions_differ(self):
        self.write_marker('version=1.3.70\r\ninstalled=2026-11-02 10:00:00\r\n')
        report = guard.marker_mismatch(self.folder, '1.3.76')
        self.assertEqual(report['state'], 'mismatch')
        self.assertIn('1.3.70', report['note'])
        self.assertIn('1.3.76', report['note'])

    def test_unknown_running_version_does_not_fake_mismatch(self):
        self.write_marker('version=1.3.76\r\n')
        report = guard.marker_mismatch(self.folder, '')
        self.assertEqual(report['state'], 'match')


class RouteTests(unittest.TestCase):
    def test_route_is_registered(self):
        from fastapi import FastAPI
        app = FastAPI()
        guard.add_routes(app)
        paths = {route.path for route in app.routes}
        self.assertIn('/api/install-guard/install-marker', paths)


if __name__ == '__main__':
    unittest.main()
