"""工作台迟到响应与附件选择经实际Qt控件验证。"""
import os
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
import wechat_gui as gui
from src.gui.agent_attachments import AttachmentsDialog

_APP=None


class AgentGui138(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP=QApplication.instance() or QApplication([])

    def test_late_history_cannot_erase_pending_text_or_newer_history(self):
        pending=[]
        def api(*args,**kw):pending.append((args[2],kw['on_done']))
        with patch.object(gui,'call_api',side_effect=api):
            tab=gui.AgentTab();tab.session_id='main'
            tab._reload_chat();old=pending[-1][1]
            tab.input.setText('本轮问题');tab._plan()
            old(dict(success=True,messages=[dict(role='user',content='旧历史')]))
            self.assertIn('本轮问题',tab.log.toPlainText());self.assertNotIn('旧历史',tab.log.toPlainText())
            pending[-1][1](dict(success=True))
            self.assertFalse(tab._chat_busy);tab.close()

    def test_stale_cards_and_plan_do_not_fill_different_session(self):
        pending=[]
        def api(*args,**kw):pending.append((args[2],kw['on_done']))
        with patch.object(gui,'call_api',side_effect=api):
            tab=gui.AgentTab();tab.session_id='first';tab._reload_cards();old=pending[-1][1]
            tab.session_id='second'
            old(dict(success=True,session=dict(cards=[dict(id='old',reply='旧卡片')])))
            self.assertEqual(tab.cards,[])
            tab.mode.setCurrentIndex(1);tab.input.setText('关闭插件');tab._plan();plan=pending[-1][1]
            tab.session_id='third';plan(dict(success=True,card=dict(reply='旧方案')))
            self.assertNotIn('旧方案',tab.log.toPlainText());tab.close()

    def test_attachment_only_send_contains_ids_and_session_change_clears(self):
        calls=[]
        def api(*args,**kw):calls.append(args)
        with patch.object(gui,'call_api',side_effect=api):
            tab=gui.AgentTab();tab.session_id='main'
            tab._set_attachments([dict(id='a'*32,name='预算.md')]);tab._plan()
            self.assertEqual(calls[-1][3]['attachments'],['a'*32]);self.assertEqual(tab.attachments,[])
            tab._set_attachments([dict(id='b'*32,name='语音.wav')]);tab.sessions.addItem('新会话','new')
            tab._session_changed();self.assertEqual(tab.attachments,[]);tab.close()

    def test_attachment_limit_and_preview_is_plain_text(self):
        def api(*args,**kw):
            kw['on_done'](dict(success=True,items=[dict(id=str(i)*32,name=f'{i}.txt',kind='document',text='<b>资料</b>',chars=9) for i in range(4)]))
        dialog=AttachmentsDialog(api)
        dialog.items.setCurrentRow(0);self.assertEqual(dialog.text.toPlainText(),'<b>资料</b>')
        for i in range(4):dialog.items.item(i).setCheckState(Qt.CheckState.Checked)
        dialog.choose();self.assertIn('最多',dialog.status.text())
        dialog.items.item(3).setCheckState(Qt.CheckState.Unchecked);dialog.choose()
        self.assertEqual(len(dialog.chosen),3);dialog.close()

    def test_unbind_clears_owner_view_and_rejects_late_workspace_response(self):
        pending=[]
        def api(*args,**kw):pending.append(kw['on_done'])
        with patch.object(gui,'call_api',side_effect=api):
            tab=gui.AgentTab()
            tab._authority(dict(bound=True,revision=1,admin=dict(subject='old')))
            old=pending[-1]
            tab._workspace_key='old';tab.log.setPlainText('上一位主人的记录')
            tab._set_attachments([dict(id='a'*32,name='私人资料.md')])
            tab._authority(dict(bound=False,revision=2))
            self.assertEqual(tab.log.toPlainText(),'');self.assertEqual(tab.attachments,[])
            old(dict(success=True,workspace='old',personas=[]))
            self.assertEqual(tab._workspace_key,'');tab.close()


if __name__=='__main__':unittest.main()
