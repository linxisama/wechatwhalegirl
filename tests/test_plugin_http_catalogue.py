"""从真实后台 JSON 经 GUI 解析器加载插件，避免只 mock api_get 漏掉数组契约。"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication,QPushButton
from fastapi.testclient import TestClient
import wechat_gui as gui
from src.wechat_app import WeChatRuntime,create_app
from src.plugins.manager import PLUGIN_MODULES
from test_wechat_app_flow import write_config,ROOT

APP=QApplication.instance() or QApplication([])


class PluginHttpCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        shutil.copytree(ROOT/'styles',self.root/'styles')
        self.bot=WeChatRuntime(root=self.root,config_path=write_config(self.root),db_path=self.root/'m.sqlite')
        self.backend=TestClient(create_app(self.bot))
        self.backend.__enter__()
        self.patches=[patch.object(gui,'ROOT',self.root),
                      patch.object(gui._PROXY_FREE,'get',side_effect=lambda url,**kw:self.backend.get(url,**kw)),
                      patch.object(gui._PROXY_FREE,'post',side_effect=lambda url,**kw:self.backend.post(url,**kw))]
        for item in self.patches:
            item.start()
        self.tab=gui.PluginTab()

    def tearDown(self):
        self.tab.deleteLater()
        APP.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.backend.__exit__(None,None,None)
        self.temp.cleanup()

    def test_real_backend_array_loads_and_toggle_persists(self):
        self.assertIsInstance(self.backend.get('/api/plugins').json(),list)
        self.tab.refresh()
        # 数量以插件注册表为准：写死数字会在新增内置插件时无谓地变红
        self.assertEqual(len(self.tab._rows),len(PLUGIN_MODULES))
        self.assertTrue({'context_chime','repeat_echo','question_relay','at_mention'}.isdisjoint(self.tab._rows))
        self.assertTrue(self.tab._loaded)
        self.assertNotIn('非预期',self.tab.notice.text())
        self.assertTrue(self.tab.btn_reload.isEnabled())
        plugin_id='reply_preferences'
        self.tab._rows[plugin_id]['toggle'].setChecked(False)
        self.assertFalse(self.tab._rows[plugin_id]['plugin']['enabled'])
        self.bot.plugins.reload()
        self.assertFalse(self.bot.plugins.get_settings(plugin_id)['enabled'])

    def test_each_shortcut_loads_then_opens_its_actual_plugin(self):
        for label,pid in [('私聊管理','private_replies'),('唤醒关键词','keyword_trigger'),
                          ('关键词固定回复','keyword_reply'),('群聊监控','group_monitor'),('称呼与速度','reply_preferences')]:
            with self.subTest(label=label):
                self.tab._plugins=[]
                self.tab._loaded=False
                with patch.object(gui,'PluginConfigDialog') as dialog:
                    dialog.return_value.exec.return_value=0
                    next(b for b in self.tab.findChildren(QPushButton) if b.text()==label).click()
                    self.assertEqual(dialog.call_count,1)
                    self.assertEqual(dialog.call_args.args[0]['id'],pid)
                    dialog.return_value.exec.assert_called_once()

    def test_session_shortcut_and_add_plugin_open_dialogs(self):
        with patch.object(gui,'SessionManagementDialog') as dialog:
            self.tab.btn_sessions.click()
            dialog.return_value.exec.assert_called_once()
        with patch.object(gui,'PluginInstallDialog') as dialog:
            dialog.return_value.installed=False
            self.tab.btn_add.click()
            dialog.return_value.exec.assert_called_once()


class PluginHttpShapeTests(unittest.TestCase):
    def test_only_plugin_list_accepts_arrays_and_error_status_is_not_accepted(self):
        for path,code,body,success in [('plugins',200,[],True),('plugins',200,[{'id':'a'}],True),
                                      ('status',200,[],False),('plugins/custom',200,[],False),
                                      ('plugins',500,[],False),('plugins',200,'<html>',False),('plugins',200,None,False)]:
            with self.subTest(path=path,code=code,body=body):
                response=SimpleNamespace(status_code=code,json=lambda:body)
                with patch.object(gui,'backend_error',return_value=''),patch.object(gui._PROXY_FREE,'get',return_value=response):
                    result=gui.api_get(path)
                if success:
                    self.assertEqual(result,{'plugins':body})
                else:
                    self.assertFalse(result['success'])

    def test_error_detail_and_invalid_json_remain_visible(self):
        response=SimpleNamespace(status_code=403,json=lambda:{'detail':'权限不足'})
        self.assertEqual(gui._response_result(response,list_key='plugins')['message'],'权限不足')
        response=SimpleNamespace(status_code=200,json=Mock(side_effect=ValueError('bad json')))
        self.assertFalse(gui._response_result(response,list_key='plugins')['success'])


class PluginLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tab=gui.PluginTab()
        self.calls=[]
        self.patcher=patch.object(gui,'call_api',side_effect=self.request)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tab.deleteLater()
        APP.processEvents()

    def request(self,owner,method,path,*args,**kwargs):
        self.calls.append((method,path,kwargs['on_done']))

    def plugin(self,pid='private_replies'):
        return {'id':pid,'name':pid,'enabled':True,'schema':{'enabled':{'type':'bool'}},'settings':{'enabled':True}}

    def test_first_click_waits_for_load_and_opens_once_before_custom_metadata(self):
        with patch.object(gui,'PluginConfigDialog') as dialog:
            dialog.return_value.exec.return_value=0
            self.tab._open_management('private_replies')
            self.assertFalse(self.tab.btn_reload.isEnabled())
            self.tab.refresh()
            self.assertEqual(len(self.calls),1)
            dialog.assert_not_called()
            self.calls[0][2]({'plugins':[self.plugin()]})
            self.assertEqual(dialog.call_count,1)
            self.assertIn('private_replies',self.tab._rows)
            self.assertTrue(self.tab.btn_reload.isEnabled())
            self.assertEqual(self.calls[1][1],'plugins/custom')

    def test_failed_load_is_visible_and_retry_opens_pending_config(self):
        self.tab._open_management('private_replies')
        self.calls[0][2]({'success':False,'message':'后台未就绪'})
        self.assertIn('后台未就绪',self.tab.notice.text())
        self.assertTrue(self.tab.btn_reload.isEnabled())
        self.assertFalse(self.tab._loading)
        with patch.object(gui,'PluginConfigDialog') as dialog:
            dialog.return_value.exec.return_value=0
            self.tab._open_management('private_replies')
            self.calls[-1][2]([self.plugin()])
            self.assertEqual(dialog.call_count,1)

    def test_missing_plugin_and_bad_data_do_not_silently_ignore_click(self):
        self.tab._open_management('not_present')
        self.calls[0][2]([])
        self.assertIn('没有插件 not_present',self.tab.notice.text())
        self.tab.refresh()
        self.calls[-1][2]([self.plugin(),self.plugin()])
        self.assertIn('数据不完整',self.tab.notice.text())
        self.assertTrue(self.tab.btn_reload.isEnabled())

    def test_late_custom_reply_cannot_replace_new_list_or_reenable_pending_toggle(self):
        self.tab.refresh()
        self.calls[0][2]([self.plugin('custom_one')])
        first_custom=self.calls[1][2]
        self.tab.refresh()
        self.calls[2][2]([self.plugin('custom_two')])
        second_custom=self.calls[3][2]
        toggle=self.tab._rows['custom_two']['toggle']
        toggle.setChecked(False)
        self.assertFalse(toggle.isEnabled())
        first_custom({'success':True,'plugins':[{'id':'custom_one'}]})
        self.assertEqual(set(self.tab._rows),{'custom_two'})
        second_custom({'success':True,'plugins':[{'id':'custom_two'}]})
        self.assertIs(self.tab._rows['custom_two']['toggle'],toggle)
        self.assertFalse(toggle.isEnabled())
        self.assertIsNotNone(self.tab._rows['custom_two']['remove'])
        self.calls[-1][2]({'success':False,'message':'保存失败'})
        self.assertTrue(toggle.isEnabled())
        self.assertTrue(toggle.isChecked())
