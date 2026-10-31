"""源码和冻结程序共用同一套真实本机 HTTP 控件验收。"""
import os
import unittest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtWidgets import QApplication
import wechat_gui
from src.gui.acceptance import run


class LiveGuiAcceptanceTests(unittest.TestCase):
    def test_live_http_controls_and_restart(self):
        app=QApplication.instance() or QApplication([])
        result=run(wechat_gui,app)
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['checks']),13)
        for capability in ('本地对话库界面','超出近期窗口','本地聊天原文可检索','本地学习库：实际Qt按钮'):
            self.assertTrue(any(capability in check for check in result['checks']),capability)
        self.assertIn('托盘开启、停止、重新开启复用真实 HTTP 与同一后台', result['checks'])
        self.assertIn('关键词设置：示例草稿、原生下拉选项、真实HTTP保存及回读',result['checks'])
        self.assertEqual(result['wechat'],'mock')
        self.assertTrue(result['plugin_removal'])
