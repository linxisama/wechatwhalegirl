"""实时发送状态与日志滚动回归；只用临时目录和离屏 Qt。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication, QAbstractSlider
from PyQt6.QtGui import QTextCursor
import wechat_gui as gui


class ConsoleLiveStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_patch = patch.object(gui, 'ROOT', Path(self.temp.name))
        self.root_patch.start()
        self.tab = gui.ConsoleTab()
        self.tab._tail_timer.stop()
        self.tab.resize(1200, 760)

    def tearDown(self):
        self.tab.close()
        self.tab.deleteLater()
        self.app.processEvents()
        self.root_patch.stop()
        self.temp.cleanup()

    def drain(self):
        for _ in range(3):
            self.app.processEvents()

    @staticmethod
    def events(count=32):
        return [{'time': i + 1, 'text': f'运行记录{i}\n' + '长内容 ' * 30}
                for i in range(count)]

    @staticmethod
    def failure():
        return {'connection': {'online': True, 'backend': 'wechatauto',
            'send_code': 'submission_uncertain', 'send_environment': {
                'ready': False, 'waiting_code': 'submission_uncertain',
                'reason': '提交结果待核实，本次不重发'}}}

    def test_wechat_probe_notice_is_invalidated_by_current_send_failure(self):
        self.tab.set_notice('严格后台控件检查通过（未输入、未发送）', source='test-wechat')
        self.tab.refresh(self.failure())
        self.assertIn('submission_uncertain', self.tab.notice.text())
        self.assertIn('结果待核实', self.tab.notice.text())
        self.assertNotIn('检查通过', self.tab.notice.text())
        self.assertIn(gui.ERROR, self.tab.notice.styleSheet())

    def test_unrelated_notice_is_preserved_and_live_probe_notice_can_recover(self):
        self.tab.set_notice('体检通过', source='test-wechat')
        self.tab.refresh(self.failure())
        self.tab.refresh({'connection': {'online': True, 'backend': 'wechatauto',
            'send_environment': {'ready': True, 'reason': '重新检查：未输入、未发送'}}})
        self.assertIn('重新检查', self.tab.notice.text())
        self.tab.set_notice('诊断包已导出：临时目录')
        self.tab.refresh(self.failure())
        self.assertEqual(self.tab.notice.text(), '诊断包已导出：临时目录')

    def test_action_finished_marks_only_wechat_probe_notices(self):
        for path in ('test-wechat', 'start'):
            worker = SimpleNamespace(path=path, response={'success': True, 'message': '完成'},
                                     deleteLater=Mock())
            tab = SimpleNamespace(set_notice=Mock())
            window = SimpleNamespace(_action_worker=worker, console_tab=tab, _busy=True,
                                     _refresh=Mock())
            with patch.object(gui, 'action_controls'):
                gui.MainWindow._action_finished(window)
            expected = {'ok': True, 'source': 'test-wechat'} if path == 'test-wechat' else {'ok': True}
            tab.set_notice.assert_called_once_with('完成', **expected)

    def test_initial_hidden_log_follows_tail_after_layout_and_resize(self):
        self.tab.refresh({'events': self.events()})
        self.tab.show()
        self.drain()
        bar = self.tab.evt_view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.assertEqual(bar.value(), bar.maximum())
        self.tab.resize(1000, 650)
        self.drain()
        self.assertEqual(bar.value(), bar.maximum())

    def test_manual_scroll_and_pause_remain_in_place_on_append(self):
        events = self.events()
        self.tab.refresh({'events': events})
        self.tab.show()
        self.drain()
        bar = self.tab.evt_view.verticalScrollBar()
        bar.setValue(bar.maximum() // 3)
        before = bar.value()
        self.tab.refresh({'events': self.events(33)})
        self.drain()
        self.assertEqual(bar.value(), before)
        bar.setValue(bar.maximum())
        self.tab.log_pause.setChecked(True)
        before = bar.value()
        self.tab.refresh({'events': self.events(34)})
        self.drain()
        self.assertEqual(bar.value(), before)

    def test_deferred_follow_does_not_override_a_selection(self):
        self.tab.refresh({'events': self.events()})
        cursor = self.tab.evt_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 6)
        self.tab.evt_view.setTextCursor(cursor)
        self.tab.show()
        self.drain()
        self.assertEqual(self.tab.evt_view.textCursor().selectedText(), cursor.selectedText())
        self.assertLess(self.tab.evt_view.verticalScrollBar().value(),
                        self.tab.evt_view.verticalScrollBar().maximum())

    def test_user_scroll_action_cancels_pending_layout_follow(self):
        self.tab.refresh({'events': self.events()})
        self.tab.show()
        bar = self.tab.evt_view.verticalScrollBar()
        # 第一帧布局已给出范围，但滚尾定时器尚未执行；真实滚动动作优先。
        bar.setValue(bar.maximum())
        bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepSub)
        before = bar.value()
        self.assertLess(before, bar.maximum())
        self.drain()
        self.assertEqual(bar.value(), before)

    def test_model_card_names_explicit_connection_test_separately_from_usage(self):
        text = gui.model_subtitle({'ai_test': '配置已更新，尚未测试',
            'usage': {'total_tokens': 3616, 'request_count': 2}})
        self.assertIn('主动连接测试', text)
        self.assertIn('3,616 token / 2 次请求', text)


if __name__ == '__main__':
    unittest.main()
