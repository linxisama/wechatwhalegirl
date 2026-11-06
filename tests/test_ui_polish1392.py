"""1.3.92 界面小修：附件悬停提示与空态、会话存档空态、解除暂停忙碌态。

全部在 offscreen 下构造真实控件，只把 request 换成同步假实现：
控件拿到的仍是同一个回调协议（on_done(result)），所以测的是界面逻辑本身，
不连后台、不发任何请求。
"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication

from src.gui.agent_attachments import AttachmentsDialog
from src.gui.conversation_archive import ConversationArchive
from src.gui.safety_panel import SafetyDialog
from src.gui.send_panel import SendPanel
from src.wechat_safety import RESET_PHRASE

APP = None


def answer_request(answer):
    """同步假 request：立刻用 answer 调 on_done，等价于无顶层窗口时的同步退化路径。

    真实链路（wechat_gui.py:489 call_api）签名是 (owner, method, path, body,
    timeout=, on_done=)，这里保持同形，少一个参数就会漏测真正的调用点。
    """
    calls = []

    def request(owner, method, path, body=None, timeout=10, on_done=None):
        calls.append((method, path, body))
        result = answer(method, path, body) if callable(answer) else answer
        if on_done is not None:
            on_done(result)
        return result

    request.calls = calls
    return request


def capture_request():
    """只记录回调、不立即回复：用来观察"提交中"这一中间态。"""
    calls = []
    pending = []

    def request(owner, method, path, body=None, timeout=10, on_done=None):
        calls.append((method, path, body))
        pending.append(on_done)

    request.calls = calls
    request.pending = pending
    return request


class AttachmentsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def dialog(self, items):
        return AttachmentsDialog(answer_request({'success': True, 'items': items}))

    def test_item_tooltip_keeps_full_file_name(self):
        """列表里的文件名会被省略号截断，悬停必须能看全。"""
        long_name = '这个文件名长到列表宽度绝对放不下' * 3 + '.md'
        dialog = self.dialog([{'id': 'a' * 32, 'name': long_name, 'chars': 12}])
        item = dialog.items.item(0)
        self.assertEqual(item.toolTip(), long_name)
        self.assertIn(long_name, item.text())
        self.assertIn('12字', item.text())
        dialog.close()

    def test_tooltip_marks_excerpt_items(self):
        """节选标记属于列表文案，不该混进 tooltip（tooltip 只放文件名）。"""
        dialog = self.dialog([{'id': 'b' * 32, 'name': '长资料.pdf', 'chars': 9000, 'truncated': True}])
        item = dialog.items.item(0)
        self.assertEqual(item.toolTip(), '长资料.pdf')
        self.assertIn('（节选）', item.text())
        dialog.close()

    def test_empty_library_uses_empty_state_text(self):
        """附件库为空时给空态指引，而不是让人看着"0/50份"发愣。"""
        dialog = self.dialog([])
        text = dialog.status.text()
        self.assertIn('附件库还是空的', text)
        self.assertNotIn('0/50', text)
        self.assertIn('上传文件或语音', text)
        dialog.close()

    def test_non_empty_library_keeps_count_text(self):
        dialog = self.dialog([{'id': 'c' * 32, 'name': '预算.md', 'chars': 3}])
        self.assertIn('1/50', dialog.status.text())
        self.assertIn('勾选需要引用的资料', dialog.status.text())
        dialog.close()


class ConversationArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    ROOM = {'kind': 'group', 'target': 'demo@chatroom', 'name': '测试群', 'count': 3}

    def archive(self, answer):
        return ConversationArchive(answer_request(answer))

    def select_room(self, widget):
        widget.rooms.addItem('测试群 · 3 条', dict(kind='group', target='demo@chatroom'))
        widget.rooms.setCurrentIndex(0)

    def test_empty_state_without_session(self):
        """没选会话时的空态要告诉用户下一步点什么，而不是空白框。"""
        widget = self.archive({'success': True, 'rooms': [], 'records': [], 'total': 0})
        widget.loaded({'success': True, 'rooms': [], 'records': [], 'total': 0})
        text = widget.text.toPlainText()
        self.assertIn('请先在上方选择会话', text)
        self.assertIn('读取 / 搜索', text)
        widget.close()

    def test_empty_state_for_search_without_hits(self):
        """搜索无命中时要把这次查的词回显出来，否则用户不知道是"没记录"还是"搜错了"。"""
        widget = self.archive({'success': True, 'rooms': [self.ROOM], 'records': [], 'total': 0})
        self.select_room(widget)
        widget.search.setText('云杉会议预算')
        widget.load(True)
        text = widget.text.toPlainText()
        self.assertIn('云杉会议预算', text)
        self.assertIn('没有包含', text)
        self.assertNotIn('还没有本地记录', text)
        widget.close()

    def test_empty_state_for_session_without_records(self):
        widget = self.archive({'success': True, 'rooms': [self.ROOM], 'records': [], 'total': 0})
        self.select_room(widget)
        widget.load(True)
        text = widget.text.toPlainText()
        self.assertIn('还没有本地记录', text)
        self.assertNotIn('请先在上方选择会话', text)
        widget.close()

    def test_search_term_comes_from_the_query_not_the_live_box(self):
        """空态引用的是"这一次查询用的词"：请求在路上的这段时间用户可能已经改了搜索框。"""
        request = capture_request()
        widget = ConversationArchive(request)
        self.select_room(widget)
        widget.search.setText('云杉预算')
        widget.load(True)
        self.assertEqual(len(request.pending), 1)   # 请求已发出，结果还没回来
        widget.search.setText('改成别的词了')        # 用户又改了输入框
        request.pending[0]({'success': True, 'rooms': [self.ROOM], 'records': [], 'total': 0})
        text = widget.text.toPlainText()
        self.assertIn('云杉预算', text)
        self.assertNotIn('改成别的词了', text)
        widget.close()

    def test_records_render_instead_of_empty_state(self):
        widget = self.archive({'success': True, 'rooms': [self.ROOM], 'total': 1, 'records': [
            {'created': 1762000000, 'sender_name': '测试用户', 'sender_id': 'wxid_demo',
             'content': '这条有记录', 'persona': '鲸鱼娘', 'reply': '收到', 'key': 'k1'}]})
        self.select_room(widget)
        widget.load(True)
        text = widget.text.toPlainText()
        self.assertIn('这条有记录', text)
        self.assertIn('已发送回复', text)
        self.assertNotIn('还没有本地记录', text)
        self.assertEqual(widget.selected.count(), 1)
        widget.close()

    def test_delete_without_session_shows_notice(self):
        """原来是静默 return，点了"删除选中记录"什么反馈都没有。"""
        widget = self.archive({'success': True})
        widget.rooms.clear()
        widget.notice.setText('')
        widget.delete(False)
        self.assertIn('请先选择会话', widget.notice.text())
        widget.close()

    def test_delete_without_selected_record_shows_notice(self):
        widget = self.archive({'success': True})
        self.select_room(widget)
        widget.notice.setText('')
        widget.delete(False)                       # selected 为空，且 clear=False
        self.assertIn('选择要删除的单条记录', widget.notice.text())
        widget.close()


class SafetyDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def ready(self, request):
        dialog = SafetyDialog(request)
        dialog.confirmation.setText(RESET_PHRASE)
        return dialog

    def test_set_busy_disables_button_and_changes_text(self):
        dialog = self.ready(capture_request())
        dialog._set_busy(True)
        self.assertFalse(dialog.button.isEnabled())
        self.assertEqual(dialog.button.text(), '正在解除…')
        self.assertTrue(dialog._busy)
        dialog.close()

    def test_set_busy_false_restores_button(self):
        dialog = self.ready(capture_request())
        dialog._set_busy(True)
        dialog._set_busy(False)
        self.assertTrue(dialog.button.isEnabled())
        self.assertEqual(dialog.button.text(), '解除机器人暂停（不自动启动）')
        self.assertFalse(dialog._busy)
        dialog.close()

    def test_callback_restores_button_and_reports_success(self):
        request = capture_request()
        dialog = self.ready(request)
        dialog.clear()
        self.assertEqual(len(request.pending), 1)
        self.assertFalse(dialog.button.isEnabled())
        self.assertIn('等待本机后台确认', dialog.notice.text())
        request.pending[0]({'success': True, 'message': '仅解除机器人暂停；未连接微信、未开始发送。'})
        self.assertTrue(dialog.button.isEnabled())
        self.assertEqual(dialog.button.text(), '解除机器人暂停（不自动启动）')
        self.assertIn('已解除机器人暂停', dialog.notice.text())
        dialog.close()

    def test_failure_callback_restores_button_and_shows_reason(self):
        request = capture_request()
        dialog = self.ready(request)
        dialog.clear()
        request.pending[0]({'success': False, 'message': '请先完成微信官方安全验证，再输入完整确认文字'})
        self.assertTrue(dialog.button.isEnabled())
        self.assertIn('未解除暂停', dialog.notice.text())
        self.assertIn('请先完成微信官方安全验证', dialog.notice.text())
        dialog.close()

    def test_request_exception_unlocks_button(self):
        """最容易漏的一条：request 抛异常时按钮不能永久卡在禁用态。"""
        def boom(owner, method, path, body=None, timeout=10, on_done=None):
            raise RuntimeError('断线')
        dialog = self.ready(boom)
        dialog.clear()
        self.assertFalse(dialog._busy)
        self.assertTrue(dialog.button.isEnabled())
        self.assertEqual(dialog.button.text(), '解除机器人暂停（不自动启动）')
        self.assertIn('解除失败', dialog.notice.text())
        self.assertIn('断线', dialog.notice.text())
        dialog.close()

    def test_double_click_sends_only_one_request(self):
        """连点两次会发出两个解除请求，第二个的回复容易让人误判当前状态。"""
        request = capture_request()
        dialog = self.ready(request)
        dialog.clear()
        dialog.clear()
        dialog.clear()
        self.assertEqual(len(request.calls), 1)
        self.assertEqual(request.calls[0][:2], ('POST', 'wechat/safety/reset'))
        self.assertEqual(request.calls[0][2], {'confirmation': RESET_PHRASE})
        dialog.close()

    def test_wrong_phrase_never_calls_request(self):
        request = capture_request()
        dialog = SafetyDialog(request)
        dialog.confirmation.setText('随便写的')
        dialog.clear()
        self.assertEqual(request.calls, [])
        self.assertTrue(dialog.button.isEnabled())
        self.assertIn(RESET_PHRASE, dialog.notice.text())
        dialog.close()


class SendPanelRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def test_list_rows_keep_short_chat_name(self):
        """列表行仍按 12 字截断：长会话名会把整列挤变形。"""
        def request(owner, method, path, body=None, timeout=10, on_done=None):
            on_done({'ok': True, 'counts': {'phases': 1}, 'items': [
                {'ts': '2026-11-02 18:43:01', 'kind': 'phase', 'phase': 'guia_input',
                 'who': 'x' * 20, 'len': 3, 'channel': 'guia'}]})
        panel = SendPanel(request)
        panel.refresh()
        self.assertIn('x' * 12 + '…', panel.text.toPlainText())
        self.assertNotIn('x' * 20, panel.text.toPlainText())
        panel.close()

    def test_tooltip_does_not_promise_a_full_name(self):
        """who 写记录时就被打码，界面拿不到完整会话名，tooltip 不能写"完整会话名"。"""
        def request(owner, method, path, body=None, timeout=10, on_done=None):
            on_done({'ok': True, 'counts': {'failures': 1}, 'last_failure': {
                'ts': '2026-11-02 18:43:05', 'kind': 'failed', 'reason': 'send_failed',
                'who': '西莲寺紫…', 'len': 19, 'text': '「坐标+OCR」发送失败',
                'advice': '请核对聊天记录'}})
        panel = SendPanel(request)
        panel.refresh()
        tip = panel.failure.toolTip()
        self.assertIn('西莲寺紫…', tip)
        self.assertIn('记录里的会话名', tip)
        self.assertNotIn('完整会话名', tip)
        self.assertIn('请核对聊天记录', tip)
        panel.close()


class OffscreenConstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def test_all_four_widgets_construct_offscreen(self):
        widgets = [
            AttachmentsDialog(answer_request({'success': True, 'items': []})),
            ConversationArchive(answer_request({'success': True})),
            SafetyDialog(capture_request()),
            SendPanel(answer_request({'ok': True, 'items': []})),
        ]
        for widget in widgets:
            self.assertIsNotNone(widget)
            widget.close()


if __name__ == '__main__':
    unittest.main()
