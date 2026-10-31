"""1.3.60：DSH 探测失败时顺手找一次本机端点，把「端口已变更」直接交给面板。"""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.agent_authority import authority
from src.dsh_bridge import DshError
from src.wechat_app import WeChatRuntime, create_app

ROOT = Path(__file__).resolve().parent.parent
DRIFT = {'state': 'drift', 'drift': {'from': 'http://127.0.0.1:3080', 'to': 'http://127.0.0.1:9527',
                                     'from_port': 3080, 'to_port': 9527},
         'candidates': [{'pid': 0, 'base_url': 'http://127.0.0.1:9527'}],
         'hint': '端口已变更：3080 → 9527',
         'message': '连接失败，但本机 DSH 在另一个端口（9527）；点「更新为 9527」后重新检测。'}


class ProbeFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'styles', root / 'styles')
        config = root / 'config.wechat.yaml'
        config.write_text((ROOT / 'config.wechat.yaml').read_text('utf-8'), 'utf-8')
        self.bot = WeChatRuntime(root=root, config_path=config, db_path=root / 'm.sqlite3')
        self._cm = TestClient(create_app(self.bot))
        self.client = self._cm.__enter__()
        self.addCleanup(self._close)
        authority(self.bot).bind({'platform': 'desktop', 'account': 'mock_machine', 'subject': 'owner'})

    def _close(self):
        try:
            self._cm.__exit__(None, None, None)
        except Exception:
            pass
        try:
            asyncio.run(self.bot.close())
        except Exception:
            pass

    def probe(self, discover_result):
        with patch.object(self.bot.dsh, 'probe', new=AsyncMock(side_effect=DshError('无法连接 DSH 地址（连接被拒绝或超时）'))), \
             patch('src.dsh_discovery.discover', new=AsyncMock(return_value=discover_result)):
            return self.client.post('/api/dsh/probe').json()

    def test_dead_saved_address_reports_the_port_that_is_actually_open(self):
        body = self.probe(DRIFT)
        self.assertFalse(body['success'])
        self.assertEqual(body['state'], 'drift')
        self.assertEqual(body['drift']['to_port'], 9527)
        self.assertIn('更新为 9527', body['hint'])
        # 原来的失败原因必须原样留着，不能被「端口变了」盖掉
        self.assertIn('无法连接 DSH 地址', body['message'])

    def test_discovery_without_drift_keeps_the_original_error(self):
        body = self.probe({'state': 'dsh_offline', 'drift': None, 'hint': '没找到'})
        self.assertFalse(body['success'])
        self.assertNotIn('state', body)
        self.assertIn('无法连接 DSH 地址', body['message'])

    def test_discovery_crash_never_masks_the_real_failure(self):
        with patch.object(self.bot.dsh, 'probe', new=AsyncMock(side_effect=DshError('无法连接 DSH 地址（连接被拒绝或超时）'))), \
             patch('src.dsh_discovery.discover', new=AsyncMock(side_effect=RuntimeError('boom'))):
            body = self.client.post('/api/dsh/probe').json()
        self.assertFalse(body['success'])
        self.assertIn('无法连接 DSH 地址', body['message'])


if __name__ == '__main__':
    unittest.main()
