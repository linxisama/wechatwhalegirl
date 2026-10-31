"""安装版真实启动、配置保存、后台异常隔离和重连；不启动微信机器人。"""
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def run(gui,output):
    os.environ['QT_QPA_PLATFORM']='offscreen'
    from PyQt6.QtWidgets import QApplication
    from .smoke import _pump,_write_config
    from ..runtime_identity import APP_VERSION
    from ..desktop_startup import stop_backend_process
    app=QApplication.instance() or QApplication([])
    def wait(check,label):assert _pump(app,check,30),'桌面启动验收失败：'+label
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);shutil.copytree(gui.BUNDLE/'styles',root/'styles');_write_config(root)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        # 只改变测试数据目录和随机端口；保留生产环境的启动、请求、保存和进程处理。
        with patch.object(gui,'ROOT',root),patch.object(gui,'GUI_CFG',root/'gui.json'), \
             patch.object(gui,'BASE_URL',f'http://127.0.0.1:{port}'),patch.object(gui,'PORT',port), \
             patch.object(gui,'FROZEN',True),patch.object(sys,'argv',['desktop-check']):
            window=gui.MainWindow();window.show()
            try:
                wait(lambda:window._settings_loaded,'启动后台并读取配置')
                assert window._backend_proc and window._backend_proc.poll() is None
                window.tabs.setCurrentWidget(window.settings_tab)
                window.settings_tab.model_name.setText('desktop-check-model')
                window.settings_tab.btn_save.click()
                wait(lambda:not window._busy and not window._settings_loading,'配置保存完成')
                assert gui.api_get('settings')['model']=='desktop-check-model'
                window.resize(900,620);app.processEvents()
                assert len(window._resize_handles.items)==8
                assert all(v.width()>0 and v.height()>0 for v in window._resize_handles.items)
                process=window._backend_proc
                stop_backend_process(process)
                window._poll_status()
                wait(lambda:'后台进程异常退出' in window.console_tab.notice.text(),'后台退出反馈')
                assert window.isVisible(),'后台异常不应关闭控制台'
                window.console_tab.btn_retry_backend.click()
                wait(lambda:window._backend_proc is not process and window._settings_loaded,'手动重连后台')
                assert gui.api_get('settings')['model']=='desktop-check-model','后台重启丢失配置'
                wait(lambda:not window._api_jobs and not window._polling,'请求线程结束')
            finally:
                window._timer.stop();window.dialogue_tab.timer.stop()
                _pump(app,lambda:not window._api_jobs and not window._polling and not window._busy,15)
                window.close()
                if window._backend_proc:
                    stop_backend_process(window._backend_proc)
    Path(output).write_text(json.dumps({'ok':True,'version':APP_VERSION,'frozen':bool(getattr(sys,'frozen',False)),
        'backend_process':True,'settings_saved':True,'backend_crash_survived':True,
        'reconnect_preserved_settings':True,'resize_handles':8,'real_wechat_started':False},indent=2),'utf-8')
