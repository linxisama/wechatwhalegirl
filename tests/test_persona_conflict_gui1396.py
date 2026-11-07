"""人设冲突在界面可见：/api/personas 暴露 conflicts，人设页把它翻成中文短语。

冲突只在 StyleManager.load_all() 里算一次（src/style_manager.py 的 conflicts()），
界面不重新判断——否则界面和运行日志会各说一套。
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication

import wechat_gui as gui
from src.style_manager import StyleManager
from src.wechat_app import create_app
from test_wechat_app_flow import RuntimeTestCase

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent


class ConflictNotesTests(unittest.TestCase):
    """把人设冲突翻成中文短语。"""

    def test_same_name_is_rendered_as_custom_over_builtin(self):
        notes = gui.PersonaTab._conflict_notes({'conflicts': [
            {'kind': 'same_name', 'name': 'tsundere_whale_girl',
             'previous': 'builtin', 'current': 'custom'}]})
        self.assertEqual(notes, ['「tsundere_whale_girl」自定义覆盖内置'])

    def test_near_display_names_both_sides(self):
        notes = gui.PersonaTab._conflict_notes({'conflicts': [
            {'kind': 'near_display', 'name': 'a', 'other_name': 'b'}]})
        self.assertEqual(notes, ['「a」与「b」显示名近似'])

    def test_near_display_without_other_name(self):
        notes = gui.PersonaTab._conflict_notes({'conflicts': [
            {'kind': 'near_display', 'name': 'a', 'other_name': ''}]})
        self.assertEqual(notes, ['「a」显示名近似'])

    def test_unknown_kind_falls_back_to_overwrite_wording(self):
        notes = gui.PersonaTab._conflict_notes({'conflicts': [{'name': 'a'}]})
        self.assertEqual(notes, ['「a」自定义覆盖内置'])

    def test_broken_entries_are_skipped_not_fatal(self):
        notes = gui.PersonaTab._conflict_notes({'conflicts': [
            'garbage', None, 3, {'name': ''}, {'name': '   '},
            {'kind': 'same_name', 'name': 'ok'}]})
        self.assertEqual(notes, ['「ok」自定义覆盖内置'])

    def test_missing_conflicts_key_yields_no_notes(self):
        self.assertEqual(gui.PersonaTab._conflict_notes({}), [])
        self.assertEqual(gui.PersonaTab._conflict_notes({'conflicts': None}), [])


class PersonaTabConflictTests(unittest.TestCase):
    """人设页把冲突显示在当前生效人设那一行下面。"""

    def setUp(self):
        self.tabs = []

    def tearDown(self):
        for tab in self.tabs:
            tab.deleteLater()
        APP.processEvents()

    def _tab(self):
        tab = gui.PersonaTab()
        self.tabs.append(tab)
        return tab

    def test_hint_shows_conflict_count_and_tooltip_lists_all(self):
        tab = self._tab()
        with patch.object(gui, 'call_api', side_effect=lambda *a, **k: None):
            tab._apply_personas({
                'active': 'custom', 'personas': [{'name': 'custom', 'display_name': '小蓝'}],
                'conflicts': [
                    {'kind': 'same_name', 'name': 'custom'},
                    {'kind': 'near_display', 'name': 'a', 'other_name': 'b'}]})
        text = tab.active_persona_hint.text()
        self.assertIn('当前生效人设：小蓝（custom）', text)
        self.assertIn('人设冲突 2 条', text)
        self.assertIn('「custom」自定义覆盖内置', text)
        self.assertIn('「a」与「b」显示名近似', text)
        self.assertIn('自定义覆盖内置', tab.active_persona_hint.toolTip())

    def test_hint_has_no_conflict_wording_when_there_is_none(self):
        tab = self._tab()
        with patch.object(gui, 'call_api', side_effect=lambda *a, **k: None):
            tab._apply_personas({'active': 'custom',
                                 'personas': [{'name': 'custom'}], 'conflicts': []})
        self.assertNotIn('人设冲突', tab.active_persona_hint.text())
        self.assertEqual(tab.active_persona_hint.toolTip(), '')

    def test_more_than_three_conflicts_are_summarised(self):
        tab = self._tab()
        conflicts = [{'kind': 'same_name', 'name': f'p{i}'} for i in range(5)]
        with patch.object(gui, 'call_api', side_effect=lambda *a, **k: None):
            tab._apply_personas({'active': 'p0', 'personas': [{'name': 'p0'}],
                                 'conflicts': conflicts})
        text = tab.active_persona_hint.text()
        self.assertIn('人设冲突 5 条', text)
        self.assertIn('等 5 条', text)
        # 前三条进正文，后两条只在 tooltip 里
        self.assertIn('「p2」', text)
        self.assertNotIn('「p4」', text)
        self.assertIn('「p4」', tab.active_persona_hint.toolTip())

    def test_old_backend_without_conflicts_key_does_not_break_the_page(self):
        tab = self._tab()
        with patch.object(gui, 'call_api', side_effect=lambda *a, **k: None):
            tab._apply_personas({'active': 'custom', 'personas': [{'name': 'custom'}]})
        self.assertIn('当前生效人设', tab.active_persona_hint.text())
        self.assertEqual(tab.list.count(), 1)


class PersonaConflictApiTests(RuntimeTestCase):
    """GET /api/personas 必须把 conflicts 一起返回。"""

    async def asyncSetUp(self):
        self.bot = await self.start_runtime()
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app),
                                        base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_repo_styles_report_the_same_name_conflict(self):
        response = await self.client.get('/api/personas')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn('conflicts', payload)
        conflicts = payload['conflicts']
        self.assertIsInstance(conflicts, list)
        same = [c for c in conflicts if c.get('kind') == 'same_name']
        self.assertTrue(same, conflicts)
        self.assertIn('tsundere_whale_girl', [c.get('name') for c in same])
        # 自定义那份覆盖内置，字段要够界面判断"谁顶掉了谁"
        item = same[0]
        for key in ('kind', 'name', 'previous', 'current', 'previous_file', 'current_file'):
            self.assertIn(key, item)

    async def test_route_tolerates_a_style_without_conflicts(self):
        stub = SimpleNamespace(active_name='default', locked_name=None,
                               list_styles=lambda: [{'name': 'default'}])
        app = create_app(self.bot)
        app.state.bot = SimpleNamespace(style=stub)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                   base_url='http://127.0.0.1')
        try:
            response = await client.get('/api/personas')
        finally:
            await client.aclose()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['conflicts'], [])


class StyleManagerConflictBaselineTests(unittest.TestCase):
    """只有内置人设时不能凭空报冲突。"""

    def test_builtin_only_tree_has_no_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'styles', root / 'styles')
            shutil.rmtree(root / 'styles' / 'custom', ignore_errors=True)
            manager = StyleManager(str(root / 'styles'), str(root / 'styles' / 'custom'), None)
            manager.load_all()
            self.assertEqual(manager.conflicts(), [])

    def test_conflicts_returns_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'styles', root / 'styles')
            manager = StyleManager(str(root / 'styles'), str(root / 'styles' / 'custom'), None)
            manager.load_all()
            first = manager.conflicts()
            self.assertTrue(first)
            first.clear()
            self.assertTrue(manager.conflicts(), '改返回值不该清掉内部记录')


if __name__ == '__main__':
    unittest.main()
