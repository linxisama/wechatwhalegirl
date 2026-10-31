import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from src import desktop_startup as startup
from src.gui.config import load_gui_config,ensure_migrated
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtCore import QRect,QPoint,QPointF,Qt
from PyQt6.QtWidgets import QApplication
from src.gui.window_sizing import fit_geometry,keep_ratio,WindowResizeHandles


class ProportionalResizeTests(unittest.TestCase):
    """等比例缩放：拖四角时保持宽高比，对角固定，最小尺寸兜底。

    用户要求"加一个可以等比例拖拉窗口大小的功能"。比例约束必须自己算：
    原来的实现把拖动交给系统（startSystemResize），交出去之后拖动过程就不归我们，
    没法逐帧约束比例 —— 所以这里把算法抽成纯函数，四角/两个方向/最小尺寸都能断言。
    """

    def setUp(self):
        self.base=QRect(100,100,1200,800)      # 3:2
        self.ratio=1200/800

    def test_ratio_is_kept_from_every_corner_and_anchor_stays(self):
        """四角都要保比例，而且**没被拖的那两条边必须钉在起始位置**。

        锚点写错过一次：拿自由拖动后的矩形当基准，拖"上+右"角时下边会被一起拽走
        （实测 699 != 899），窗口整体偏移。
        """
        E=Qt.Edge
        for edges in (E.RightEdge|E.BottomEdge,E.LeftEdge|E.TopEdge,
                      E.RightEdge|E.TopEdge,E.LeftEdge|E.BottomEdge):
            with self.subTest(edges=edges):
                dragged=QRect(self.base)
                if edges & E.LeftEdge:dragged.setLeft(self.base.left()-300)
                else:dragged.setRight(self.base.right()+300)
                if edges & E.TopEdge:dragged.setTop(self.base.top()-200)
                else:dragged.setBottom(self.base.bottom()+200)
                result=keep_ratio(dragged,self.base,self.ratio,edges,720,480)
                self.assertAlmostEqual(result.width()/result.height(),self.ratio,places=2)
                if edges & E.LeftEdge:
                    self.assertEqual(result.right(),self.base.right(),'右边应当不动')
                else:
                    self.assertEqual(result.left(),self.base.left(),'左边应当不动')
                if edges & E.TopEdge:
                    self.assertEqual(result.bottom(),self.base.bottom(),'下边应当不动')
                else:
                    self.assertEqual(result.top(),self.base.top(),'上边应当不动')

    def test_vertical_dominant_drag_drives_height(self):
        """斜着拖时，变化量大的那个方向说了算，否则窗口跟不上鼠标。"""
        E=Qt.Edge
        result=keep_ratio(QRect(100,100,1250,1200),self.base,self.ratio,E.RightEdge|E.BottomEdge,720,480)
        self.assertAlmostEqual(result.height(),1200,delta=1)
        self.assertAlmostEqual(result.width()/result.height(),self.ratio,places=2)

    def test_minimum_size_wins_over_ratio(self):
        E=Qt.Edge
        result=keep_ratio(QRect(100,100,200,150),self.base,self.ratio,E.RightEdge|E.BottomEdge,720,480)
        self.assertGreaterEqual(result.width(),720)
        self.assertGreaterEqual(result.height(),480)
        self.assertAlmostEqual(result.width()/result.height(),self.ratio,places=2)

    def test_without_ratio_geometry_is_untouched(self):
        for ratio in (0,None,-1):
            with self.subTest(ratio=ratio):
                self.assertEqual(keep_ratio(self.base,self.base,ratio,Qt.Edge.RightEdge,720,480),self.base)

    def test_window_hands_the_toggle_to_every_handle(self):
        app=QApplication.instance() or QApplication([])
        import wechat_gui as gui
        with tempfile.TemporaryDirectory() as temp,patch.object(gui,'GUI_CFG',Path(temp)/'gui.json'),patch.object(gui.sys,'argv',['gui','--no-backend']):
            window=gui.MainWindow();window._timer.stop()
            self.assertFalse(window._resize_handles.proportional)
            window.appearance_tab.proportional_box.setChecked(True)
            window.appearance_tab._apply()
            self.assertTrue(window._cfg['proportional_resize'])
            self.assertTrue(window._resize_handles.proportional)
            self.assertTrue(all(handle.proportional for handle in window._resize_handles.items))
            window.close();window.deleteLater();app.processEvents()

    def test_toggle_survives_a_round_trip_through_the_config_file(self):
        from src.gui.config import sanitize
        self.assertTrue(sanitize({'proportional_resize':True})['proportional_resize'])
        for bad in ('true',1,None,[]):
            with self.subTest(bad=bad):
                self.assertFalse(sanitize({'proportional_resize':bad})['proportional_resize'])


class StartupTests(unittest.TestCase):
    def test_supervisor_records_native_exit_without_loading_qt_or_config(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(startup.subprocess,'Popen',return_value=Mock(wait=lambda:-1073741819)) as spawn,patch.object(startup,'show_failure') as notify:
                self.assertEqual(startup.supervise(Path(temp),[]),-1073741819)
                self.assertIn('--gui-worker',spawn.call_args.args[0])
                notify.assert_called_once()
            data=json.loads((Path(temp)/'startup-diagnostics.json').read_text('utf-8'))
            self.assertEqual(data['exit_hex'],'0xC0000005');self.assertNotIn('api_key',data)

    def test_successful_exit_has_no_failure_popup(self):
        with patch.object(startup.subprocess,'Popen',return_value=Mock(wait=lambda:0)),patch.object(startup,'show_failure') as notify:
            self.assertEqual(startup.supervise(Path('.'),[]),0);notify.assert_not_called()

    def test_bad_gui_config_never_crashes_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'gui.json'
            for value in ('oops',{},[],float('nan')):
                path.write_text(json.dumps({'config_version':value,'transparency':float('nan'),
                    'geometry':{'w':10**100,'h':-400,'x':10**99,'y':1}}),'utf-8')
                cfg=ensure_migrated(path)
                self.assertEqual(cfg['transparency'],0.0);self.assertNotIn('w',cfg['geometry'])
            path.write_bytes(b'\xff\xfe\x00')
            self.assertIsInstance(load_gui_config(path),dict)

    def test_window_fit_handles_small_screens_and_disconnected_monitor(self):
        area=QRect(0,0,1280,680)
        rect=fit_geometry({'w':4000,'h':2000,'x':6000,'y':3000},[area])
        self.assertTrue(area.contains(rect));self.assertEqual(rect.size(),area.size())
        area=QRect(-1600,0,1600,900)
        self.assertTrue(area.contains(fit_geometry({'x':-1400,'y':10,'w':900,'h':700},[area])))

    def test_window_controls_fit_and_failed_backend_stays_visible(self):
        import wechat_gui as gui
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temp,patch.object(gui,'GUI_CFG',Path(temp)/'gui.json'),patch.object(gui.sys,'argv',['gui','--no-backend']):
            window=gui.MainWindow();window._timer.stop();window.show();app.processEvents()
            self.assertEqual(len(window._resize_handles.items),8)
            self.assertEqual(len(window._page_scrolls),5)
            window._backend_proc=Mock(poll=lambda:-1073741819)
            window._on_status({'success':False})
            self.assertIn('0xC0000005',window.console_tab.notice.text());self.assertTrue(window.isVisible())
            window.settings_tab.btn_save.click();self.assertFalse(window._busy)
            window._backend_proc=None;window.close();window.deleteLater();app.processEvents()

    def test_all_eight_resize_handles_adjust_geometry(self):
        from types import SimpleNamespace
        import wechat_gui as gui
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temp,patch.object(gui,'GUI_CFG',Path(temp)/'gui.json'),patch.object(gui.sys,'argv',['gui','--no-backend']):
            window=gui.MainWindow();window._timer.stop()
            for handle in window._resize_handles.items:
                before=QRect(50,50,900,650);window.setGeometry(before)
                handle._press=(QPoint(0,0),before)
                handle.mouseMoveEvent(SimpleNamespace(globalPosition=lambda:QPointF(40,30),accept=lambda:None))
                after=window.geometry()
                self.assertNotEqual(after,before)
                if handle.edges & Qt.Edge.RightEdge:self.assertEqual(after.right(),before.right()+40)
                if handle.edges & Qt.Edge.LeftEdge:self.assertEqual(after.left(),before.left()+40)
                if handle.edges & Qt.Edge.BottomEdge:self.assertEqual(after.bottom(),before.bottom()+30)
                if handle.edges & Qt.Edge.TopEdge:self.assertEqual(after.top(),before.top()+30)
            window.close();window.deleteLater();app.processEvents()

    def test_settings_refresh_requested_during_load_is_not_lost(self):
        from types import SimpleNamespace
        import wechat_gui as gui
        callbacks=[]
        window=SimpleNamespace(_busy=False,_settings_loaded=False,settings_tab=SimpleNamespace(populate=Mock()))
        window._load_settings=lambda:gui.MainWindow._load_settings(window)
        def request(*args,**kw):callbacks.append(kw['on_done'])
        with patch.object(gui,'call_api',side_effect=request):
            window._load_settings();window._load_settings()
            self.assertEqual(len(callbacks),1)
            callbacks[0]({'success':False,'message':'旧请求失败'})
            self.assertEqual(len(callbacks),2)
            callbacks[1]({'api_url':'https://example.invalid/v1','model':'new-model'})
            self.assertTrue(window._settings_loaded)
            window.settings_tab.populate.assert_called_once()

    def test_huge_saved_window_is_clamped_before_first_show(self):
        import wechat_gui as gui
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'gui.json'
            path.write_text(json.dumps({'config_version':2,'geometry':{'w':2**28,'h':2**28,'x':9000,'y':9000}}),'utf-8')
            with patch.object(gui,'GUI_CFG',path),patch.object(gui.sys,'argv',['gui','--no-backend']):
                window=gui.MainWindow();window._timer.stop()
                self.assertTrue(any(screen.availableGeometry().contains(window.geometry()) for screen in app.screens()))
                window.close();window.deleteLater();app.processEvents()
