"""管理入口、管理员只读字段和实际会话选择界面。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication, QPushButton
import wechat_gui as gui
from src.gui.session_management import SessionManagementDialog
from src.plugins.manager import PluginManager

APP=QApplication.instance() or QApplication([])


class ManagementGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.manager=PluginManager(Path(self.temp.name))
        self.plugins={p['id']:p for p in self.manager.list_plugins()}
        self.windows=[]

    def tearDown(self):
        for window in self.windows:
            window.deleteLater()
        APP.processEvents()
        self.temp.cleanup()

    def test_plugin_toolbar_exposes_required_features(self):
        tab=gui.PluginTab()
        self.windows.append(tab)
        labels={b.text() for b in tab.findChildren(QPushButton)}
        self.assertTrue({'会话监控与管理员','私聊管理','唤醒关键词','关键词固定回复','群聊监控'}.issubset(labels))

    def test_admin_binding_is_readonly_and_internal_mapping_hidden(self):
        window=gui.PluginConfigDialog(self.plugins['robot_admin'])
        self.windows.append(window)
        self.assertTrue(window._widgets['ownerIds'][0].isReadOnly())
        self.assertNotIn('ownerAccounts',window._widgets)
        self.assertNotIn('ownerAccounts',window.values())

    def test_select_observed_group_adds_stable_id_and_sets_list_mode(self):
        def request(owner,method,path,*args,**kwargs):
            kwargs['on_done']({'sessions':[{'kind':'group','name':'工作群','target':'123@chatroom'}]})
        with patch.object(gui,'call_api',side_effect=request):
            window=gui.PluginConfigDialog(self.plugins['group_monitor'])
        self.windows.append(window)
        window.observed_groups.setCurrentIndex(1)
        next(b for b in window.findChildren(QPushButton) if b.text()=='加入监控名单').click()
        self.assertEqual(window.values()['groups'],'123@chatroom')
        self.assertEqual(window.values()['mode'],'selected')

    def test_session_monitor_binds_only_verified_private_target(self):
        calls=[]
        result={'success':True,'account':'测试账号','owners':[], 'sessions':[
            {'kind':'group','target':'123@chatroom','name':'群','verified':False,'received':3,'last_seen':1,'reply_allowed':True,'monitored':True},
            {'kind':'private','target':'wxid_owner','name':'同名用户','verified':True,'received':1,'last_seen':1,'reply_allowed':False,'monitored':True}]}
        def request(owner,method,path,*args,**kwargs):
            calls.append((method,path,args))
            kwargs['on_done'](result if method=='GET' else {'success':True})
        window=SessionManagementDialog(request)
        self.windows.append(window)
        window.list.setCurrentRow(0)
        self.assertFalse(window.bind.isEnabled())
        window.list.setCurrentRow(1)
        self.assertTrue(window.bind.isEnabled())
        window.bind.click()
        sent=[c for c in calls if c[0]=='POST']
        self.assertEqual(sent[0][1],'management/owner')
        self.assertEqual(sent[0][2][0],{'target':'wxid_owner','bind':True})

    def test_fixed_keyword_has_scope_and_matching_widgets(self):
        window=gui.PluginConfigDialog(self.plugins['keyword_reply'])
        self.windows.append(window)
        self.assertEqual(window._widgets['scope'][0].count(),3)
        self.assertEqual(window._widgets['match'][0].count(),2)

    def test_changed_keyword_scope_is_saved_and_reloaded(self):
        window=gui.PluginConfigDialog(self.plugins['keyword_reply'])
        self.windows.append(window)
        scope=window._widgets['scope'][0]
        scope.setCurrentIndex(scope.findData('private'))
        match=window._widgets['match'][0]
        match.setCurrentIndex(match.findData('exact'))
        def posted(path,values,timeout=10):
            self.manager.update_settings('keyword_reply',values)
            return {'success':True}
        with patch.object(gui,'api_post',side_effect=posted):
            window._save()
        saved=PluginManager(Path(self.temp.name)).get_settings('keyword_reply')
        self.assertEqual(saved['scope'],'private')
        self.assertEqual(saved['match'],'exact')
