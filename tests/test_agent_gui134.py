"""工作台失败处理通过实际Qt控件验证，不启动真实后台或微信。"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication
from src.gui.agent_workspace import WorkspaceDialog
import wechat_gui as gui

_APP = None


class WorkbenchGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP=QApplication.instance() or QApplication([])

    def test_failed_list_or_create_does_not_loop_into_new_sessions(self):
        with patch.object(gui,'call_api') as request:
            tab=gui.AgentTab()
            request.reset_mock()
            tab._apply_sessions({'success':False,'message':'后台暂不可用'})
            request.assert_not_called()
            self.assertIn('后台暂不可用',tab.result.text())
            def fail(owner,method,path,data=None,**kw):kw['on_done']({'success':False,'message':'创建失败'})
            request.side_effect=fail
            tab._new_session()
            self.assertEqual(request.call_count,1)
            self.assertIn('创建失败',tab.result.text())
            tab.close()

    def test_pending_user_text_is_plain_and_at_history_end(self):
        with patch.object(gui,'call_api'):
            tab=gui.AgentTab();tab.session_id='main'
            tab.log.setPlainText('上一轮问答')
            tab.input.setText('<b>新的问题</b>');tab._plan()
            self.assertEqual(tab.log.toPlainText(),'上一轮问答\n\n我：<b>新的问题</b>')
            self.assertFalse(tab.send.isEnabled())
            tab.close()

    def test_workspace_failed_toggle_rolls_back_and_read_can_retry(self):
        failures=set()
        def api(owner,method,path,body=None,**kw):
            if path in failures:
                result={'success':False,'message':'读取失败'}
            else:
                result={'success':True,'files':[],'items':[],'path':'',
                        'admin':{'platform':'wechat'},'owner_commands_enabled':False}
            kw['on_done'](result)
        dialog=WorkspaceDialog(api)
        failures.add('agent/workspace/owner-channel')
        dialog.owner.setChecked(True)
        dialog.request('POST','agent/workspace/owner-channel',{'enabled':True},dialog.owner_saved)
        self.assertFalse(dialog.owner.isChecked())
        failures.add('agent/workspace/note?name=retry.md')
        dialog.read_note('retry.md')
        self.assertTrue(dialog.save.isEnabled())
        self.assertIn('读取失败',dialog.result.text())
        dialog.close()
