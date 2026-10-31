"""外观可读性自愈 + 人设导入自动命名。

两个用户报的现象：
1. "透明度拉满，重装也解决不了" —— 配置存在数据目录、覆盖安装会保留，所以要在**加载时**钳制，
   并给一个「恢复默认外观」按钮；
2. "人设导入卡顿或不成功" —— 原来要再弹一个模态输入框，且同名会直接报错；现在自动命名 + 重名加后缀。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication

import wechat_gui as gui
from src.gui.config import MAX_PANEL_TRANSPARENCY, MAX_TRANSPARENCY, sanitize


class TransparencyClampTests(unittest.TestCase):
    def test_maxed_out_values_are_pulled_back_on_load(self):
        clean = sanitize({'transparency': 1.0, 'panel_transparency': 1.0})
        self.assertEqual(clean['transparency'], MAX_TRANSPARENCY)
        self.assertEqual(clean['panel_transparency'], MAX_PANEL_TRANSPARENCY)

    def test_normal_values_are_untouched(self):
        clean = sanitize({'transparency': 0.3, 'panel_transparency': 0.5})
        self.assertAlmostEqual(clean['transparency'], 0.3)
        self.assertAlmostEqual(clean['panel_transparency'], 0.5)

    def test_garbage_falls_back_to_defaults(self):
        clean = sanitize({'transparency': '拉满', 'panel_transparency': None})
        self.assertEqual(clean['transparency'], 0.0)
        self.assertAlmostEqual(clean['panel_transparency'], 0.35)


class AppearanceResetTests(unittest.TestCase):
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

    def test_reset_puts_everything_back_to_defaults(self):
        tab = self.window.appearance_tab
        tab.transparency_slider.setValue(95)
        tab.panel_transparency_slider.setValue(100)
        tab.blur_slider.setValue(0)
        tab._current_image = str(self.root / 'some.png')
        tab.reset_appearance()
        self.assertEqual(tab.transparency_slider.value(), 0)
        self.assertEqual(tab.panel_transparency_slider.value(), 35)
        self.assertEqual(tab.blur_slider.value(), 70)
        self.assertEqual(tab._current_image, '')
        # 立刻应用并写盘，用户不用再点保存
        self.assertAlmostEqual(self.window._cfg['transparency'], 0.0)
        self.assertAlmostEqual(self.window._cfg['panel_transparency'], 0.35)
        self.assertEqual(self.window._cfg['background_image'], '')


class PersonaImportTests(unittest.TestCase):
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
        self.persona = self.root / '我的鲸鱼娘.yaml'
        self.persona.write_text('name: 测试\nprompt: 你好\n', 'utf-8')

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def _import(self, replies):
        """replies 依次作为接口返回；记录每次请求体。"""
        calls = []
        queue = list(replies)

        def fake_call(owner, method, path, data=None, timeout=10, on_done=None):
            calls.append((path, dict(data or {})))
            if on_done is not None:
                on_done(queue.pop(0) if queue else {'success': False, 'message': ''})
            return None

        with patch.object(gui.QFileDialog, 'getOpenFileName',
                          return_value=(str(self.persona), '')), \
                patch.object(gui, 'call_api', side_effect=fake_call):
            self.window.persona_tab._import_persona()
        # 人设页自己的 refresh() 也可能走 call_api，只看导入那一条路径
        return [item for item in calls if item[0] == 'persona-files/import']

    def test_import_uses_the_file_name_without_asking_again(self):
        calls = self._import([{'success': True, 'name': '我的鲸鱼娘'}])
        self.assertEqual(len(calls), 1)
        path, body = calls[0]
        self.assertEqual(path, 'persona-files/import')
        self.assertEqual(body['name'], '我的鲸鱼娘')
        self.assertIn('prompt', body['content'])
        self.assertIn('已导入', self.window.persona_tab.notice.text())

    def test_name_conflict_retries_with_a_suffix(self):
        calls = self._import([{'success': False, 'message': '同名人设已存在，请填写新的导入标识'},
                              {'success': True, 'name': '我的鲸鱼娘_2'}])
        self.assertEqual([body['name'] for _, body in calls], ['我的鲸鱼娘', '我的鲸鱼娘_2'])
        self.assertIn('我的鲸鱼娘_2', self.window.persona_tab.notice.text())

    def test_other_errors_are_reported_verbatim(self):
        calls = self._import([{'success': False, 'message': '人设文件必须是 YAML/JSON 对象'}])
        self.assertEqual(len(calls), 1)
        self.assertIn('导入失败', self.window.persona_tab.notice.text())
        self.assertIn('YAML/JSON', self.window.persona_tab.notice.text())


if __name__ == '__main__':
    unittest.main()
