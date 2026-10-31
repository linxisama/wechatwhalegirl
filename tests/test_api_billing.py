"""「模型与 API」按 QQ 版补齐：回复超时 / 自动重试 / 计费方式 / 单价与花费估算。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication

import wechat_gui as gui
from src.wechat_app import WeChatRuntime, create_app


def make_runtime(root):
    (root / 'styles').mkdir(exist_ok=True)
    config = root / 'config.wechat.yaml'
    config.write_text(yaml.safe_dump({
        'ai': {'api_url': 'https://api.deepseek.com/v1', 'api_key': 'k',
               'model': 'deepseek-v4-pro', 'api_style': 'auto'},
        'wechat': {'backend': 'mock'}, 'wechat_only': {'backend': 'mock'},
    }, allow_unicode=True), 'utf-8')
    return WeChatRuntime(root=root, config_path=config, db_path=root / 'm.sqlite3')


class SettingsRoundTripTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = make_runtime(self.root)
        self.ctx = TestClient(create_app(self.runtime))
        self.client = self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)
        self.runtime.db.close()
        self._tmp.cleanup()

    def _save(self, **overrides):
        body = {'backend': 'mock', 'db_dir': '', 'account': '', 'groups': [],
                'listen_private': True, 'api_url': 'https://api.deepseek.com/v1',
                'api_key': '', 'model': 'deepseek-v4-pro', 'api_style': 'auto'}
        body.update(overrides)
        return self.client.post('/api/settings', json=body)

    def test_defaults_are_the_qq_like_values(self):
        settings = self.client.get('/api/settings').json()
        self.assertEqual(settings['request_timeout'], 20)
        self.assertTrue(settings['auto_retry'])
        self.assertEqual(settings['billing_mode'], 'token')
        self.assertEqual(settings['unit_price'], 0)

    def test_values_are_saved_and_reach_the_engine(self):
        answer = self._save(request_timeout=30, auto_retry=False,
                            billing_mode='request', unit_price=0.02)
        self.assertTrue(answer.json()['success'], answer.text)
        again = self.client.get('/api/settings').json()
        self.assertEqual(again['request_timeout'], 30)
        self.assertFalse(again['auto_retry'])
        self.assertEqual(again['billing_mode'], 'request')
        self.assertAlmostEqual(again['unit_price'], 0.02)
        # 关掉自动重试 = 引擎只试一次；超时也要跟着改
        self.assertEqual(self.runtime.ai.max_retries, 1)
        self.assertEqual(self.runtime.ai.timeout, 30)

    def test_auto_retry_on_means_two_attempts(self):
        self._save(auto_retry=True)
        self.assertEqual(self.runtime.ai.max_retries, 2)

    def test_timeout_is_clamped_to_a_sane_range(self):
        self._save(request_timeout=1)
        self.assertEqual(self.client.get('/api/settings').json()['request_timeout'], 3)
        self._save(request_timeout=9999)
        self.assertEqual(self.client.get('/api/settings').json()['request_timeout'], 300)


class BillingEstimateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = make_runtime(Path(self._tmp.name))

    def tearDown(self):
        self.runtime.db.close()
        self._tmp.cleanup()

    def _usage(self, prompt=1000, completion=500):
        self.runtime.ai.usage.add(prompt, completion)

    def test_token_mode_uses_price_per_million(self):
        self._usage()
        self.runtime.config.set('ai.billing_mode', 'token')
        self.runtime.config.set('ai.unit_price', 2.0)
        billing = self.runtime.billing_estimate()
        self.assertEqual(billing['tokens'], 1500)
        self.assertAlmostEqual(billing['cost'], 0.003)

    def test_request_mode_uses_price_per_call(self):
        self._usage()
        self.runtime.config.set('ai.billing_mode', 'request')
        self.runtime.config.set('ai.unit_price', 0.01)
        self.assertEqual(self.runtime.billing_estimate()['cost'], 0.01)

    def test_zero_price_reports_usage_without_money(self):
        self._usage()
        self.runtime.config.set('ai.unit_price', 0)
        billing = self.runtime.billing_estimate()
        self.assertIsNone(billing['cost'])
        self.assertEqual(billing['tokens'], 1500)

    def test_status_exposes_billing(self):
        self._usage()
        self.runtime.config.set('ai.unit_price', 2.0)
        self.assertIn('billing', self.runtime.status())


class SettingsTabTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._patches = [patch.object(gui, 'GUI_CFG', self.root / 'gui.json'),
                         patch.object(gui, 'ROOT', self.root),
                         patch.object(gui.sys, 'argv', ['gui', '--no-backend'])]
        for item in self._patches:
            item.start()
        self.window = gui.MainWindow()
        self.window._timer.stop()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def test_fields_round_trip_through_the_tab(self):
        tab = self.window.settings_tab
        tab.populate({'api_url': 'https://api.deepseek.com/v1', 'model': 'm', 'api_style': 'auto',
                      'request_timeout': 45, 'auto_retry': False,
                      'billing_mode': 'request', 'unit_price': 0.5})
        self.assertEqual(tab.request_timeout.value(), 45)
        self.assertFalse(tab.auto_retry.isChecked())
        collected = tab.collect()
        self.assertEqual(collected['request_timeout'], 45)
        self.assertFalse(collected['auto_retry'])
        self.assertEqual(collected['billing_mode'], 'request')
        self.assertAlmostEqual(collected['unit_price'], 0.5)

    def test_model_card_shows_usage_and_cost(self):
        line = gui.model_subtitle({'ai_test': '配置已更新',
                                            'billing': {'tokens': 2100, 'requests': 3,
                                                        'cost': 0.0042}})
        self.assertIn('2,100 token', line)
        self.assertIn('3 次请求', line)
        self.assertIn('¥0.0042', line)

    def test_model_card_omits_money_when_price_is_zero(self):
        line = gui.model_subtitle({'ai_test': '', 'billing': {'tokens': 10,
                                                                      'requests': 1, 'cost': None}})
        self.assertIn('10 token', line)
        self.assertNotIn('¥', line)


if __name__ == '__main__':
    unittest.main()
