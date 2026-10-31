"""逐个点击 QQ 风格对话页控件，验证实际请求、保存失败回滚与输入保留。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication,QPushButton
from src.gui.dialogue_tab import DialogueTab
from src.gui.dialogue_rules import DialogueRulesDialog
from src.dialogue_rules import snapshot as rules_snapshot
from src.plugins.manager import PluginManager

APP=QApplication.instance() or QApplication([])


class DialogueGuiControlsTests(unittest.TestCase):
    def setUp(self):
        self.windows=[];self.posts=[]
        self.data={'success':True,'persona':'custom','notes':[{'id':'note1','content':'关心状态','enabled':True}],
            'people':[{'id':'wxid_one','name':'联系人','messages':3}],
            'subjects':[{'subject':'wxid_one','scope':'wechat|bot|private:wxid_one','name':'联系人'}],
            'facts':[{'id':'fact1','scope':'wechat|bot|private:wxid_one|persona:custom','subject':'wxid_one',
                      'content':'喜欢蓝色','status':'candidate','evidence':1}],
            'diagnostics':[{'id':'one','state':'pending','state_label':'等待处理','sender':'联系人','text':'等待测试','error':'微信离线'},
                           {'id':'two','state':'sent','state_label':'已发送','sender':'联系人','text':'成功测试','reply':'收到'}],
            'memoryEnabled':True,'autoRemember':True,'personaIsolation':True,'crossChatPreferences':False}
        self.tab=DialogueTab(self.request,lambda *a:None);self.windows.append(self.tab);self.tab.loaded(self.data)

    def tearDown(self):
        for window in self.windows:window.deleteLater()
        APP.processEvents()

    def request(self,owner,method,path,*args,**kwargs):
        if method=='GET':kwargs['on_done'](self.data)
        else:self.posts.append((path,args[0],kwargs['on_done']))

    def test_all_four_shortcuts_open_distinct_rule_groups(self):
        for label,group in [('回复触发与名单','trigger'),('连续跟聊','followup'),('人设隔离','isolation'),('跨会话偏好','preferences')]:
            with patch('src.gui.dialogue_tab.DialogueRulesDialog') as dialog:
                next(b for b in self.tab.findChildren(QPushButton) if b.text()==label).click()
                self.assertEqual(dialog.call_args.args[1],group)
                dialog.return_value.exec.assert_called_once()

    def test_add_note_failure_keeps_input_success_clears_only_submitted_value(self):
        self.tab.content.setText('新的表达规则');self.tab.add_note()
        self.assertEqual(self.posts[-1][:2],('dialogue/notes',{'content':'新的表达规则','persona':'custom'}))
        self.assertEqual(self.tab.content.text(),'新的表达规则')
        self.posts[-1][2]({'success':False,'message':'保存失败'})
        self.assertEqual(self.tab.content.text(),'新的表达规则');self.assertIn('保存失败',self.tab.notice.text())
        self.tab.add_note();self.posts[-1][2]({'success':True,'message':'已保存'})
        self.assertEqual(self.tab.content.text(),'');self.assertIn('已保存',self.tab.notice.text())

    def test_note_edit_toggle_failure_and_delete_dispatch(self):
        self.tab.notes.setCurrentRow(0);self.assertEqual(self.tab.content.text(),'关心状态')
        self.tab.content.setText('按步骤说明');self.tab.edit_note()
        self.assertEqual(self.posts[-1][1]['id'],'note1');self.assertEqual(self.posts[-1][1]['content'],'按步骤说明')
        self.posts[-1][2]({'success':True,'message':'编辑完成'})
        item=self.tab.notes.item(0);item.setCheckState(Qt.CheckState.Unchecked)
        self.assertFalse(self.posts[-1][1]['enabled'])
        # 请求中刷新必须排队，不能销毁失败回滚仍需使用的项目。
        self.tab.refresh();self.posts[-1][2]({'success':False,'message':'切换失败'})
        self.assertEqual(self.tab.notes.item(0).checkState(),Qt.CheckState.Checked)
        self.tab.notes.setCurrentRow(0);self.tab.remove_note()
        self.assertTrue(self.posts[-1][1]['delete']);self.posts[-1][2]({'success':True,'message':'已删除'})

    def test_auto_switch_failure_restores_last_saved_state(self):
        self.tab.auto.click();self.assertFalse(self.tab.auto.isChecked())
        self.assertEqual(self.posts[-1][0],'dialogue/rules')
        self.assertEqual(self.posts[-1][1]['plugins'],{'conversation_memory':{'autoRemember':False}})
        self.posts[-1][2]({'success':False,'message':'磁盘写入失败'})
        self.assertTrue(self.tab.auto.isChecked());self.assertIn('磁盘写入失败',self.tab.notice.text())

    def test_fact_add_edit_confirm_and_delete_dispatch(self):
        self.tab.fact_content.setText('喜欢吃米饭');self.tab.change_fact('add')
        self.assertEqual(self.posts[-1][0],'dialogue/facts')
        self.assertEqual(self.posts[-1][1]['subject'],'wxid_one')
        self.posts[-1][2]({'success':False,'message':'人设已变化'})
        self.assertEqual(self.tab.fact_content.text(),'喜欢吃米饭')
        self.tab.facts.selectRow(0)
        for action in ('edit','confirm','delete'):
            self.tab.fact_content.setText('喜欢深蓝色');self.tab.change_fact(action)
            self.assertEqual(self.posts[-1][1]['action'],action);self.assertEqual(self.posts[-1][1]['id'],'fact1')
            self.posts[-1][2]({'success':True,'message':'已保存'})

    def test_empty_input_or_selection_has_feedback(self):
        self.tab.add_note();self.assertIn('1–500',self.tab.notice.text())
        self.tab.remove_note();self.assertIn('选择',self.tab.notice.text())
        self.tab.change_fact('delete');self.assertIn('选择',self.tab.notice.text())
        self.assertEqual(self.posts,[])

    def test_diagnostics_filter_and_reason_are_readable(self):
        self.assertIn('等待处理',self.tab.diagnostics.toPlainText());self.assertIn('微信离线',self.tab.diagnostics.toPlainText())
        self.tab.state_filter.setCurrentIndex(self.tab.state_filter.findData('unanswered'))
        self.assertNotIn('成功测试',self.tab.diagnostics.toPlainText())
        self.tab.diagnostic_search.setText('离线');self.assertIn('等待测试',self.tab.diagnostics.toPlainText())
        self.tab.diagnostic_search.setText('不存在');self.assertIn('暂无符合',self.tab.diagnostics.toPlainText())

    def test_rule_editor_builds_actual_fields_and_retains_failed_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            data=rules_snapshot(SimpleNamespace(plugins=PluginManager(Path(temp))))
            posts=[]
            def request(owner,method,path,*args,**kwargs):
                if method=='GET':kwargs['on_done'](data)
                else:posts.append((args[0],kwargs['on_done']))
            dialog=DialogueRulesDialog(request,'followup');self.windows.append(dialog)
            spin=dialog.widgets[('short_followup','maxSentences')][0];spin.setValue(1)
            dialog.save.click()
            self.assertEqual(posts[-1][0]['plugins']['short_followup']['maxSentences'],1)
            self.assertFalse(dialog.save.isEnabled())
            posts[-1][1]({'success':False,'message':'写入失败'})
            self.assertEqual(spin.value(),1);self.assertTrue(dialog.save.isEnabled());self.assertIn('写入失败',dialog.notice.text())
