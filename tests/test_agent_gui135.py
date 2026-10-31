"""真实Qt异步回调乱序：旧文件不能覆盖当前笔记，失败不能复用旧版本。"""
import os
import unittest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication
from src.gui.agent_workspace import WorkspaceDialog

_APP=None


class Workspace135Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP=QApplication.instance() or QApplication([])

    def setUp(self):
        self.pending=[];self.posts=[]
        def api(owner,method,path,body=None,**kw):
            if path.startswith('agent/workspace/note?'):
                self.pending.append(kw['on_done']);return
            if method=='POST':self.posts.append((path,body))
            kw['on_done'](dict(success=True,files=[],items=[],context={},admin={'platform':'wechat'}))
        self.dialog=WorkspaceDialog(api)

    def tearDown(self):self.dialog.close()

    def test_late_read_from_previous_selection_does_not_replace_current_note(self):
        d=self.dialog
        d.read_note('first.md');first=self.pending[-1]
        d.read_note('second.md');second=self.pending[-1]
        second(dict(success=True,content='当前正文',version='new'))
        first(dict(success=True,content='旧文件正文',version='old'))
        self.assertEqual(d.editor.toPlainText(),'当前正文');self.assertEqual(d.version,'new')
        self.assertEqual(d.name.text(),'second.md');self.assertFalse(d.editor.isReadOnly())

    def test_failed_read_clears_old_data_and_prevents_save(self):
        d=self.dialog
        d.editor.setPlainText('旧文件正文');d.version='old-version'
        d.read_note('missing.md');self.pending[-1](dict(success=False,message='读取失败'))
        d.save_note()
        self.assertEqual(self.posts,[]);self.assertEqual(d.editor.toPlainText(),'');self.assertEqual(d.version,'')
        self.assertIn('尚未成功读取',d.result.text())
        d.new_note();d.name.setText('created.md');d.editor.setPlainText('新笔记')
        self.assertTrue(d.note_ready)

    def test_new_draft_survives_late_read_and_loading_unlocks(self):
        d=self.dialog
        d.read_note('first.md');callback=self.pending[-1]
        self.assertTrue(d.editor.isReadOnly())
        d.new_note();d.name.setText('draft.md');d.editor.setPlainText('新草稿')
        callback(dict(success=True,content='旧正文',version='old'))
        self.assertEqual(d.editor.toPlainText(),'新草稿');self.assertEqual(d.version,'')
        self.assertFalse(d.editor.isReadOnly());self.assertTrue(d.save.isEnabled())

    def test_context_controls_submit_exact_values(self):
        d=self.dialog;d.history_turns.setValue(6);d.context_chars.setValue(3000);d.recall_limit.setValue(0)
        d.save_context()
        self.assertEqual(self.posts[-1],('agent/workspace/context',dict(history_turns=6,max_chars=3000,recall_limit=0)))
