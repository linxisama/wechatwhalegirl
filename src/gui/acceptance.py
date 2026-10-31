"""使用真实 HTTP 和 Qt 请求线程验收安装版控件；外部微信和模型始终为模拟。"""
import asyncio
import json
import shutil
import socket
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import patch


SHUTDOWN_GRACE=10.0   # 等后台线程自己退出的上限（秒）
DRAIN_LIMIT=2.0       # 收尾时代码自己结束遗留任务的上限（秒），不能等成无限


def _thread_stack(thread,limit=14):
    """把后台线程当前的调用栈压成几行文本，让失败断言本身携带现场证据。"""
    frame=sys._current_frames().get(thread.ident) if thread.is_alive() else None
    if frame is None:
        return '    <线程已无活动帧>'
    rows=[]
    for item in traceback.format_stack(frame)[-limit:]:
        rows.append('    '+' <- '.join(line.strip() for line in item.strip().splitlines() if line.strip()))
    return '\n'.join(rows)


def shutdown_loop(loop,limit=DRAIN_LIMIT):
    """关事件循环，但**不等**默认线程池里的外部网络请求。

    实测根因（栈证据见 tests/test_acceptance_shutdown1341.py 的说明）：插件
    web_context.on_pre_ai 会用 asyncio.to_thread 抓真实要闻/热搜（urlopen，
    timeoutSec 默认 8 秒）。`asyncio.run()` 收尾时要跑
    `loop.shutdown_default_executor(THREAD_JOIN_TIMEOUT)`，而本机 Python 3.12 的
    THREAD_JOIN_TIMEOUT 是 300 秒，于是后台线程退出的时刻被那几个 HTTPS 请求的真实
    耗时决定，join(10) 就偶发失败。这里照抄 asyncio.run 取消遗留任务那一步，但把
    "等线程池跑完"换成有界等待：`loop.close()` 自身按 wait=False 收尾（CPython 文档
    明说"shuts down the executor, but does not wait for the executor to finish"），
    残留的 urlopen 由它自己的 socket 超时结束，不再决定后台线程何时退出。
    """
    pending=[task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.wait(pending,timeout=limit))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()


def stop_backend(server,thread,sock,grace=SHUTDOWN_GRACE):
    """等后台线程退出；超时就失败，并把那一刻的线程栈写进断言。

    断言保持 fail-closed：线程真的不退出时依然失败，只是失败信息带着现场，
    不能靠加长等待掩盖。
    """
    if server is not None:
        server.should_exit=True
    thread.join(grace)
    try:
        sock.close()
    except OSError:
        pass
    assert not thread.is_alive(),(
        '验收后台没有正常退出：%0.0f 秒内后台线程没有退出\n后台线程栈：\n%s'%(grace,_thread_stack(thread)))


def run(gui, app):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QPushButton
    from .dialogue_rules import DialogueRulesDialog
    from .smoke import _pump, _write_config
    from ..wechat_app import WeChatRuntime, create_app
    import uvicorn

    checks=[]
    attachment_checks=[]
    def wait(condition,label):
        assert _pump(app,condition,15), '安装版贯通验收失败：'+label
    def click(owner,label):
        matches=[b for b in owner.findChildren(QPushButton) if b.text()==label and b.isEnabled()]
        assert len(matches)==1, (label,len(matches))
        matches[0].click()
    def post(path,body=None):
        result=gui.api_post(path,body)
        assert result.get('success'), (path,result)
        return result

    # 随机端口和独立目录避免接触真实后台；线程内部创建/关闭运行时以保持事件循环归属。
    class Backend:
        def __init__(self,root):
            self.error=None;self.server=None;self.ai_calls=[]
            self.socket=socket.socket();self.socket.bind(('127.0.0.1',0))
            self.url='http://127.0.0.1:'+str(self.socket.getsockname()[1])
            self.thread=threading.Thread(target=self.serve,args=(root,),daemon=True)
            self.thread.start()
            wait(lambda:self.error or (self.server and self.server.started),'本机 HTTP 启动')
            assert not self.error,self.error
        def serve(self,root):
            async def main():
                bot=WeChatRuntime(root=root,config_path=root/'config.wechat.yaml',db_path=root/'messages.sqlite')
                async def fake_chat(*args,**kwargs):
                    self.ai_calls.append((args,kwargs))
                    return '模拟模型回复。'
                bot.ai.chat=fake_chat
                self.server=uvicorn.Server(uvicorn.Config(create_app(bot),host='127.0.0.1',port=0,
                    log_config=None,log_level='error',access_log=False,ws='none',http='h11'))
                try:await self.server.serve(sockets=[self.socket])
                finally:await bot.close()
            # 自己建 loop 而不用 asyncio.run：见 shutdown_loop() 的实测根因。
            loop=asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:loop.run_until_complete(main())
            except BaseException as exc:self.error=repr(exc)
            finally:
                shutdown_loop(loop);asyncio.set_event_loop(None)
        def close(self):
            stop_backend(self.server,self.thread,self.socket)
            assert not self.error,self.error

    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);shutil.copytree(gui.BUNDLE/'styles',root/'styles');_write_config(root)
        for restarted in (False,True):
            backend=Backend(root);window=None;dialogs=[]
            try:
                with patch.object(gui,'ROOT',root),patch.object(gui,'GUI_CFG',root/'gui.json'), \
                     patch.object(gui,'BASE_URL',backend.url),patch.object(sys,'argv',['wechat_gui','--no-backend']):
                    assert not gui.backend_error(), '真实 HTTP 身份校验失败'
                    window=gui.MainWindow();window._timer.stop();window.show()
                    tab=window.dialogue_tab;window.tabs.setCurrentWidget(tab)
                    wait(lambda:bool(tab._persona) and not tab._refreshing,'读取对话页')
                    if restarted:
                        settings={p['id']:p['settings'] for p in gui.api_get('plugins')['plugins']}
                        assert settings['keyword_trigger']['keywords']=='验收唤醒'
                        assert settings['short_followup']['maxSentences']==1
                        assert settings['conversation_memory']['personaIsolation'] is False
                        assert settings['conversation_memory']['autoPrefixes']=='我偏爱'
                        assert tab.notes.count()==1 and tab.notes.item(0).text()=='验收保留的自我记忆'
                        assert tab.facts.rowCount()==1 and tab._facts[0]['content']=='喜欢深紫色'
                        assert tab._diagnostics, '重启后诊断丢失'
                        management=gui.api_get('management/sessions')
                        assert management['owners']==['wxid_acceptance_admin']
                        assert management['password_state']=='set'
                        assert not management['reply_paused']
                        removed=next(p for p in gui.api_get('plugins')['plugins'] if p['id']=='quick_reply')
                        assert removed['removed'] and not removed['enabled'],'插件移除选择重启后丢失'
                        checks.append('真实后台重启后保留规则、记忆、档案、诊断和管理员设置')
                        archived=gui.api_get('dialogue/archive?kind=group&target=acceptance@chatroom&q=云杉会议预算')
                        assert archived['total']>=1,'重启后本地聊天原文丢失'
                        checks.append('真实 HTTP 后台重启后本地聊天原文可检索')
                        assert gui.api_get('agent/workspace')['context']==dict(history_turns=6,max_chars=3000,recall_limit=2),gui.api_get('agent/workspace')['context']
                        assert gui.api_get('agent/workspace/note?name=acceptance.md')['content']=='实际Qt保存的主人笔记'
                        assert gui.api_get('agent/workspace/attachments')['items'][0]['name']=='acceptance.txt'
                        attachment_checks.append('工作台附件原件与提取文字在实际后台重启后保留')
                    else:
                        window._tray.update(gui.api_get('status'))
                        window._tray.start_action.trigger()
                        wait(lambda:not window._busy and gui.api_get('status').get('running'), '托盘开启机器人')
                        window._tray.update(gui.api_get('status'))
                        window._tray.stop_action.trigger()
                        wait(lambda:not window._busy and not gui.api_get('status').get('running'), '托盘停止机器人')
                        window._tray.update(gui.api_get('status'))
                        window._tray.start_action.trigger()
                        wait(lambda:not window._busy and gui.api_get('status').get('running'), '托盘重新开启')
                        checks.append('托盘开启、停止、重新开启复用真实 HTTP 与同一后台')
                        keyword_meta = next(p for p in gui.api_get('plugins')['plugins'] if p['id'] == 'keyword_trigger')
                        keyword_dialog = gui.PluginConfigDialog(keyword_meta, window)
                        dialogs.append(keyword_dialog); keyword_dialog.show(); app.processEvents()
                        assert keyword_dialog.values() == keyword_meta['settings'], '打开唤醒表单改变了原值'
                        click(keyword_dialog, '添加示例')
                        keyword_dialog._widgets['mode'][0].setCurrentIndex(1)
                        keyword_dialog._widgets['includePersona'][0].setChecked(False)
                        keyword_dialog._widgets['keywords'][0].setPlainText('界面验收')
                        click(keyword_dialog, '保存设置')
                        wait(lambda:keyword_dialog.saved, '关键词原生表单真实保存')
                        stored = next(p['settings'] for p in gui.api_get('plugins')['plugins'] if p['id'] == 'keyword_trigger')
                        assert stored['keywords'] == '界面验收' and stored['mode'] == 'replace'
                        assert not stored['includePersona']
                        checks.append('关键词设置：示例草稿、原生下拉选项、真实HTTP保存及回读')
                        edits={
                            'trigger':{('keyword_trigger','keywords'):'验收唤醒',('keyword_trigger','mode'):'replace',
                                ('keyword_trigger','includePersona'):False,('private_replies','mode'):'off',
                                ('group_monitor','mode'):'selected',('group_monitor','groups'):'acceptance@chatroom'},
                            'followup':{('short_followup','maxSentences'):1,('short_followup','strictLimit'):True},
                            'isolation':{('conversation_memory','personaIsolation'):False},
                            'preferences':{('conversation_memory','autoRemember'):True,('conversation_memory','autoPrefixes'):'我偏爱'},
                        }
                        for group,fields in edits.items():
                            dialog=DialogueRulesDialog(gui.call_api,group,tab);dialogs.append(dialog);dialog.show()
                            wait(lambda:bool(dialog.widgets),'打开规则 '+group)
                            for key,value in fields.items():
                                widget,kind=dialog.widgets[key]
                                if kind=='bool':widget.setChecked(value)
                                elif kind=='select':widget.setCurrentIndex(widget.findData(value))
                                elif kind in ('int','float'):widget.setValue(value)
                                elif hasattr(widget,'setPlainText'):widget.setPlainText(value)
                                else:widget.setText(value)
                            dialog.save.click();wait(lambda:not dialog._saving,'保存规则 '+group)
                            assert '已保存' in dialog.notice.text(),dialog.notice.text()
                            dialog.close()
                        checks.append('四组规则通过实际控件、后台线程及 HTTP 保存')
                        wait(lambda:not tab._refreshing,'规则保存后刷新')
                        tab.content.setText('验收临时记忆');click(tab.tabs.widget(0),'添加')
                        wait(lambda:not tab._busy and not tab._refreshing,'添加自我记忆')
                        assert tab.notes.count()==1
                        tab.notes.setCurrentRow(0);tab.content.setText('验收保留的自我记忆')
                        click(tab.tabs.widget(0),'保存选中修改')
                        wait(lambda:not tab._busy and not tab._refreshing,'修改自我记忆')
                        tab.notes.item(0).setCheckState(Qt.CheckState.Unchecked)
                        wait(lambda:not tab._busy and not tab._refreshing,'停用自我记忆')
                        assert tab.notes.item(0).checkState()==Qt.CheckState.Unchecked
                        tab.notes.item(0).setCheckState(Qt.CheckState.Checked)
                        wait(lambda:not tab._busy and not tab._refreshing,'启用自我记忆')
                        tab.content.setText('待删除的自我记忆');click(tab.tabs.widget(0),'添加')
                        wait(lambda:not tab._busy and not tab._refreshing,'添加待删自我记忆')
                        row=next(i for i in range(tab.notes.count()) if tab.notes.item(i).text()=='待删除的自我记忆')
                        tab.notes.setCurrentRow(row);click(tab.tabs.widget(0),'删除选中的手动记忆')
                        wait(lambda:not tab._busy and not tab._refreshing,'删除自我记忆')
                        assert tab.notes.count()==1
                        for enabled in (False,True):
                            tab.auto.click();wait(lambda:not tab._busy and not tab._refreshing,'保存自动记忆开关')
                            assert tab.auto.isChecked()==enabled
                        checks.append('手动记忆添加、编辑、启停、删除和自动记忆开关通过真实 HTTP')
                        post('mock/recv',{'chat':'acceptance@chatroom','sender':'wxid_acceptance_user','text':'我偏爱紫色','kind':'group'})
                        tab.refresh();wait(lambda:not tab._refreshing and tab.facts.rowCount()==1,'自动观察和个人档案')
                        assert tab._facts[0]['status']=='candidate'
                        tab.tabs.setCurrentIndex(1);tab.facts.selectRow(0)
                        click(tab.tabs.widget(1),'确认选中记忆')
                        wait(lambda:not tab._busy and not tab._refreshing,'确认个人档案')
                        assert tab._facts[0]['status']=='confirmed'
                        tab.facts.selectRow(0);tab.fact_content.setText('喜欢深紫色')
                        click(tab.tabs.widget(1),'保存选中修改')
                        wait(lambda:not tab._busy and not tab._refreshing,'修改个人档案')
                        tab.fact_content.setText('临时的个人事实');click(tab.tabs.widget(1),'添加个人记忆')
                        wait(lambda:not tab._busy and not tab._refreshing,'添加个人档案')
                        assert tab.facts.rowCount()==2
                        row=next(i for i,v in enumerate(tab._facts) if v['content']=='临时的个人事实')
                        tab.facts.selectRow(row);click(tab.tabs.widget(1),'删除选中档案')
                        wait(lambda:not tab._busy and not tab._refreshing,'删除个人档案')
                        assert tab.facts.rowCount()==1
                        tab.fact_search.setText('深紫');tab.refresh()
                        wait(lambda:not tab._refreshing,'搜索个人档案')
                        assert tab.facts.rowCount()==1
                        tab.fact_search.setText('不存在的档案');tab.refresh()
                        wait(lambda:not tab._refreshing,'搜索无匹配个人档案')
                        assert tab.facts.rowCount()==0
                        tab.fact_search.clear();tab.refresh();wait(lambda:not tab._refreshing,'清除搜索')
                        checks.append('档案自动观察、人工确认及添加/修改/删除通过真实 HTTP')
                        # keyword_reply 属市场插件：冻结版只带基础插件，没装就跳过这一段，
                        # 否则安装包自检会因为一个可选插件永远失败（实测踩过三次）。
                        # 用现场查询而不是复用 `settings`：后者只在重启分支里赋值，
                        # 写在别处会 UnboundLocalError（实测）。
                        installed_ids = {p['id'] for p in gui.api_get('plugins').get('plugins', [])}
                        if 'keyword_reply' in installed_ids:
                            post('plugins/keyword_reply',{'enabled':True,'rules':'验收暗号=验收固定回复'})
                            post('mock/recv',{'chat':'acceptance@chatroom','sender':'wxid_acceptance_user',
                                              'text':'验收暗号','kind':'group'})
                            wait(lambda:any(v['text']=='验收固定回复' for v in gui.api_get('mock/sent').get('sent',[])),
                                 '关键词实际模拟发送')
                            checks.append('关键词命中并进入模拟微信发送记录')
                        else:
                            checks.append('关键词插件属市场包：冻结版跳过该段（安装后可用）')
                        post('mock/recv',{'chat':'acceptance@chatroom','sender':'wxid_acceptance_user',
                            'text':'验收唤醒 请解释第二项技术方案','kind':'group','at':True})
                        wait(lambda:any(v['text']=='模拟模型回复。' for v in gui.api_get('mock/sent').get('sent',[])),'记忆参与模型回复')
                        assert backend.ai_calls
                        args,kwargs=backend.ai_calls[-1]
                        assert '验收保留的自我记忆' in kwargs['system_prompt']
                        assert '喜欢深紫色' in str(args)
                        checks.append('已保存记忆进入模型调用参数并完成模拟发送')
                        # 旧话题超出近期窗口，仍须进入模型；全部消息只注入 mock。
                        post('mock/recv',{'chat':'acceptance@chatroom','sender':'wxid_acceptance_user',
                            'text':'云杉会议预算定为五千元','kind':'group'})
                        for index in range(20):
                            post('mock/recv',{'chat':'acceptance@chatroom','sender':'wxid_background_user',
                                'text':f'下午游戏讨论 {index}','kind':'group'})
                        wait(lambda:gui.api_get('dialogue/archive?kind=group&target=acceptance@chatroom').get('total',0)>=23,
                             '本地聊天归档')
                        archive=tab.archive;tab.tabs.setCurrentIndex(3);archive.load(True)
                        wait(lambda:not archive.busy and archive.rooms.count()>0,'本地对话库列表')
                        selected=next(i for i in range(archive.rooms.count()) if archive.rooms.itemData(i)['target']=='acceptance@chatroom')
                        archive.rooms.setCurrentIndex(selected);archive.search.setText('云杉会议预算')
                        click(archive,'读取 / 搜索');wait(lambda:not archive.busy,'本地聊天原文检索')
                        assert '五千元' in archive.text.toPlainText()
                        backup=root/'archive-backup.json'
                        with patch.object(gui.QFileDialog,'getSaveFileName',return_value=(str(backup),'JSON')):
                            click(archive,'导出当前会话 JSON')
                        wait(lambda:not archive.busy and backup.exists(),'界面对话库导出')
                        assert '五千元' in backup.read_text('utf-8')
                        with patch('src.gui.conversation_archive.QFileDialog.getOpenFileName',return_value=(str(backup),'JSON')), \
                             patch('src.gui.conversation_archive.QMessageBox.question',return_value=gui.QMessageBox.StandardButton.Yes):
                            click(archive,'导入 JSON')
                        wait(lambda:not archive.busy,'界面对话库合并导入')
                        checks.append('本地对话库界面：真实 HTTP 读取/搜索/JSON 导出/合并导入')
                        learning=tab.learning;tab.tabs.setCurrentIndex(4);learning.load()
                        wait(lambda:not learning.busy and learning.rooms.count()>0,'本地学习库列表')
                        index=next(i for i in range(learning.rooms.count()) if learning.rooms.itemData(i)['target']=='acceptance@chatroom')
                        learning.rooms.setCurrentIndex(index)
                        click(learning,'从最近1000条记录学习');wait(lambda:not learning.busy,'本机提取学习结果')
                        click(learning,'读取学习库');wait(lambda:not learning.busy,'本机学习库刷新')
                        assert '/1000' in learning.notice.text()
                        assert learning.items.count()>0,'本地学习结果为空'
                        identity=learning.items.currentData()
                        click(learning,'批准表达/事实');wait(lambda:not learning.busy,'批准学习条目')
                        assert next(item for item in gui.api_get('dialogue/learning?kind=group&target=acceptance@chatroom')['items'] if item['id']==identity)['status']=='approved'
                        click(learning,'拒绝');wait(lambda:not learning.busy,'拒绝学习条目')
                        assert next(item for item in gui.api_get('dialogue/learning?kind=group&target=acceptance@chatroom')['items'] if item['id']==identity)['status']=='rejected'
                        click(learning,'删除');wait(lambda:not learning.busy,'删除学习条目')
                        assert identity not in [item['id'] for item in gui.api_get('dialogue/learning?kind=group&target=acceptance@chatroom')['items']]
                        checks.append('本地学习库：实际Qt按钮经HTTP提取与读取1000条活动窗口')
                        previous_calls=len(backend.ai_calls)
                        post('plugins/reply_dedupe',{'enabled':False})
                        post('mock/recv',{'chat':'acceptance@chatroom','sender':'wxid_acceptance_user',
                            'text':'验收唤醒 之前云杉会议预算是多少','kind':'group','at':True})
                        wait(lambda:len(backend.ai_calls)>previous_calls,'旧话题参与模型请求')
                        assert '云杉会议预算定为五千元' in str(backend.ai_calls[-1][0])
                        checks.append('超出近期窗口的相关原文进入模型请求，未跨群检索')
                        tab.tabs.setCurrentIndex(2)
                        tab.refresh();wait(lambda:not tab._refreshing,'刷新实际诊断')
                        tab.tabs.setCurrentIndex(2);tab.state_filter.setCurrentIndex(tab.state_filter.findData('sent'))
                        if 'keyword_reply' in installed_ids:
                            tab.diagnostic_search.setText('验收暗号')
                            assert '验收固定回复' in tab.diagnostics.toPlainText()
                        else:
                            tab.diagnostic_search.clear()
                        window.tabs.setCurrentWidget(window.management_tab);management=window.management_tab
                        wait(lambda:not management._refreshing,'读取管理员页')
                        management.manual_target.setText('wxid_acceptance_admin');click(management,'直接绑定微信账号')
                        wait(lambda:not management._busy and not management._refreshing,'绑定管理员')
                        assert management.owners.count()==1,management.result.text()
                        management.password.setText('A');click(management,'保存管理员密码')
                        wait(lambda:not management._busy and not management._refreshing,'保存单字符密码')
                        assert management.password.text()=='' and '已设置' in management.guidance.text()
                        for label in ('暂停自动回复','恢复自动回复','启用管理员私聊指令'):
                            click(management,label);wait(lambda:not management._busy and not management._refreshing,label)
                            assert '正在处理' not in management.result.text()
                        checks.append('管理员绑定、密码保存、暂停/恢复和启用按钮贯通')
                        # 新工作台也要经实际Qt请求线程和HTTP保存，不能只在假回调里通过。
                        post('agent/authority',dict(platform='desktop',account='acceptance',subject='owner'))
                        from .agent_workspace import WorkspaceDialog
                        from PyQt6.QtWidgets import QTabWidget
                        workspace=WorkspaceDialog(gui.call_api,window);dialogs.append(workspace);workspace.show()
                        wait(lambda:bool(workspace.path),'读取主人工作区')
                        workspace.findChild(QTabWidget).setCurrentIndex(1)
                        workspace.history_turns.setValue(6);workspace.context_chars.setValue(3000);workspace.recall_limit.setValue(2)
                        click(workspace,'保存上下文配置')
                        wait(lambda:'上下文配置已保存' in workspace.result.text(),'Qt保存上下文预算')
                        assert gui.api_get('agent/workspace')['context']==dict(history_turns=6,max_chars=3000,recall_limit=2)
                        workspace.findChild(QTabWidget).setCurrentIndex(0);workspace.new_note()
                        workspace.name.setText('acceptance.md');workspace.editor.setPlainText('实际Qt保存的主人笔记')
                        click(workspace,'保存笔记')
                        wait(lambda:workspace.result.text()=='笔记已保存。','Qt保存主人笔记')
                        assert gui.api_get('agent/workspace/note?name=acceptance.md')['content']=='实际Qt保存的主人笔记'
                        workspace.close()
                        # 文件上传、选择和问答也走真实Qt线程→HTTP，冻结包必须带解析器。
                        from .agent_attachments import AttachmentsDialog
                        import base64
                        uploaded=post('agent/workspace/attachments',dict(name='acceptance.txt',
                            data_base64=base64.b64encode('附件验收预算为3200元'.encode()).decode()))['item']
                        attachments=AttachmentsDialog(gui.call_api,parent=window);dialogs.append(attachments)
                        attachments.show();wait(lambda:not attachments._busy and attachments.items.count()==1,'Qt读取附件列表')
                        attachments.items.item(0).setCheckState(Qt.CheckState.Checked)
                        attachments.choose()
                        agent=window.agent_tab
                        agent.session_id='acceptance-files'
                        agent._set_attachments(attachments.chosen)
                        agent.input.setText('这份资料的预算是多少？');agent._plan()
                        wait(lambda:not agent._chat_busy,'Qt携带附件完成模型问答')
                        assert '3200元' in str(backend.ai_calls[-1][0]),'附件提取文字没有进入模型'
                        import pypdf
                        assert pypdf.__version__,'冻结包缺少PDF解析器'
                        attachment_checks.append('工作台实际Qt选择附件→HTTP→模型参数；原件本地保存，内容未自动学为偏好')
                        window.tabs.setCurrentWidget(window.plugin_tab);catalogue=window.plugin_tab
                        wait(lambda:catalogue._loaded and not catalogue._loading,'读取可移除插件')
                        assert {'context_chime','repeat_echo','question_relay','at_mention'}.isdisjoint(catalogue._rows)
                        assert not catalogue._rows['conversation_memory']['remove'],'核心组件不能删除'
                        def remove_plugin(pid):
                            with patch.object(gui.QMessageBox,'warning',return_value=gui.QMessageBox.StandardButton.Yes):
                                catalogue._rows[pid]['remove'].click()
                            wait(lambda:catalogue._rows[pid]['plugin'].get('removed',False),'界面移除 '+pid)
                            assert not catalogue._rows[pid]['toggle'].isEnabled()
                        remove_plugin('quick_reply')
                        catalogue.state_filter.setCurrentIndex(catalogue.state_filter.findData('removed'))
                        catalogue._rows['quick_reply']['remove'].click()
                        wait(lambda:not catalogue._rows['quick_reply']['plugin'].get('removed',False),'恢复原插件')
                        catalogue.state_filter.setCurrentIndex(catalogue.state_filter.findData('all'))
                        remove_plugin('quick_reply')
                    tab.timer.stop();window._timer.stop()
                    wait(lambda:not window._api_jobs,'等待实际 Qt HTTP 线程结束')
                    window.close();window=None
            finally:
                if window:
                    window.dialogue_tab.timer.stop();window._timer.stop()
                    for dialog in dialogs:dialog.close()
                    _pump(app,lambda:not window._api_jobs,20);window.close()
                backend.close()
    return {'ok':True,'transport':'real localhost HTTP + Qt ApiJob','wechat':'mock','checks':checks,'plugin_removal':True,
            'agent_workspace_context':True,'attachment_checks':attachment_checks}
