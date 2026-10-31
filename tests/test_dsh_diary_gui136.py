"""DSH查找与日记按钮的实际Qt参数和失败状态。"""
import os
import unittest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication
from src.gui.dsh_panel import DshDialog
from src.gui.agent_workspace import WorkspaceDialog

_APP=None


class DshDiaryGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP=QApplication.instance() or QApplication([])

    def test_single_verified_endpoint_fills_address_without_enabling(self):
        calls=[]
        def api(owner,method,path,data=None,**kw):
            calls.append((method,path,data))
            kw['on_done'](dict(success=True,enabled=False,base_url='http://127.0.0.1:3080'))
        dialog=DshDialog(api)
        dialog.discovered(dict(success=True,message='已查到',candidates=[dict(pid=42,base_url='http://127.0.0.1:5947')]))
        self.assertEqual(dialog.url.text(),'http://127.0.0.1:5947');self.assertFalse(dialog.enabled.isChecked())
        self.assertEqual(len(calls),1)
        dialog.close()

    def test_multiple_endpoints_require_selection_and_discovery_error_unlocks(self):
        def api(owner,method,path,data=None,**kw):kw['on_done'](dict(success=False,message='未查到'))
        dialog=DshDialog(api)
        dialog.discovered(dict(success=True,candidates=[dict(pid=p,base_url=f'http://127.0.0.1:{p}') for p in (41,42)]))
        self.assertEqual(dialog.endpoints.currentIndex(),-1)
        self.assertEqual(dialog.url.text(),'http://127.0.0.1:3080')
        dialog.endpoints.setCurrentIndex(1);self.assertEqual(dialog.url.text(),'http://127.0.0.1:42')
        dialog.discover();self.assertTrue(dialog.discover_button.isEnabled());dialog.close()

    def test_view_diary_is_readonly_write_has_long_timeout_and_failure_unlocks(self):
        calls=[]
        def api(owner,method,path,data=None,**kw):
            calls.append((method,path,kw['timeout']))
            result=dict(success=True,path='',files=[],items=[],context={},admin={})
            if method=='POST' and path.endswith('/diary'):result=dict(success=False,message='模型超时')
            kw['on_done'](result)
        dialog=WorkspaceDialog(api)
        dialog.generate_diary()
        self.assertEqual(calls[-1],('POST','agent/workspace/diary',80))
        self.assertTrue(dialog.write_diary.isEnabled());self.assertIn('模型超时',dialog.diary.toPlainText())
        dialog.close()

    def test_failed_auto_diary_save_reverts_checkbox(self):
        def api(owner,method,path,data=None,**kw):
            kw['on_done'](dict(success=False,message='保存失败') if path.endswith('/diary-settings') else
                dict(success=True,files=[],items=[],context={},admin={},diary_auto_enabled=True))
        dialog=WorkspaceDialog(api)
        dialog.auto_diary.setChecked(False);dialog.save_diary_settings(False)
        self.assertTrue(dialog.auto_diary.isChecked());self.assertTrue(dialog.auto_diary.isEnabled())
        self.assertIn('保存失败',dialog.result.text());dialog.close()
