"""人设文件入口、当前人设选择和管理页背景/透明度。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtGui import QImage,QColor
from PyQt6.QtWidgets import QApplication,QPushButton
import wechat_gui as gui
from src.gui.session_management import SessionManagementDialog
from src.gui.dialogue_tab import DialogueTab

APP=QApplication.instance() or QApplication([])


class PersonaGuiFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.windows=[]

    def tearDown(self):
        for window in self.windows:
            window.deleteLater()
        APP.processEvents()
        self.temp.cleanup()

    def test_active_persona_is_selected_instead_of_first_row(self):
        tab=gui.PersonaTab();self.windows.append(tab)
        def request(owner,method,path,*args,**kwargs):
            kwargs['on_done']({'name':'custom','system_prompt':'正文','keywords':['星星']})
        with patch.object(gui,'call_api',side_effect=request):
            tab._apply_personas({'active':'custom','personas':[{'name':'default'},{'name':'custom'}]})
        self.assertEqual(tab._current,'custom')
        self.assertEqual(tab.list.currentRow(),1)
        self.assertEqual(tab.ed_keywords.text(),'星星')

    def test_persona_payload_and_import_export_buttons(self):
        tab=gui.PersonaTab();self.windows.append(tab)
        tab.ed_keywords.setText('星星, 月亮')
        tab.ed_image_note.setText('蓝色头发')
        payload=tab._payload(False)
        self.assertEqual(payload['keywords'],'星星, 月亮')
        self.assertEqual(payload['image_note'],'蓝色头发')
        labels={b.text() for b in tab.findChildren(QPushButton)}
        self.assertTrue({'导入人设文件','导出人设文件','上传形象图','识别形象'}.issubset(labels))

    def test_export_writes_selected_file_in_json_format(self):
        tab=gui.PersonaTab();self.windows.append(tab)
        tab._current='custom'
        target=self.root/'human.json'
        def request(owner,method,path,*args,**kwargs):
            kwargs['on_done']({'success':True,'content':'persona:\n  name: custom\n  keywords: [星星]\n'})
        with patch.object(gui.QFileDialog,'getSaveFileName',return_value=(str(target),'')),patch.object(gui,'call_api',side_effect=request):
            tab._export_persona()
        self.assertEqual(json.loads(target.read_text('utf-8'))['persona']['keywords'],['星星'])

    def test_management_background_is_visible_and_transparency_tracks_setting(self):
        path=self.root/'background.png'
        image=QImage(64,64,QImage.Format.Format_RGB32)
        image.fill(QColor('#DE1234'));image.save(str(path))
        window=SessionManagementDialog(lambda *a,**k:None,theme={'panel_transparency':.8},background=path,auto_refresh=False)
        self.windows.append(window)
        window.show();APP.processEvents()
        color=window.grab().toImage().pixelColor(2,2)
        self.assertGreater(color.red(),180)
        self.assertLess(color.green(),80)
        self.assertIn('255,255,255,50',window.styleSheet())
        window.apply_theme({'panel_transparency':.1},path)
        self.assertIn('255,255,255,229',window.styleSheet())

    def test_memory_panel_opacity_tracks_appearance(self):
        tab=DialogueTab(lambda *a,**k:None,lambda *a:None);self.windows.append(tab)
        tab.apply_theme({'panel_transparency':.5})
        self.assertIn('127',tab.notes.styleSheet())
        tab.loaded({'success':True,'persona':'custom','notes':[{'id':'one','content':'记得关心','enabled':True}],
                    'people':[],'diagnostics':[]})
        self.assertEqual(tab.notes.count(),1)
        self.assertIn('custom',tab.title.text())
