"""无边框窗口的八方向缩放及跨显示器恢复，不依赖截图或桌面自动化。"""
from PyQt6.QtCore import Qt,QRect
from PyQt6.QtWidgets import QWidget,QScrollArea,QVBoxLayout,QFrame


def fit_geometry(saved,areas):
    areas=[QRect(v) for v in areas if v.width()>0 and v.height()>0]
    if not areas:return QRect(0,0,1000,700)
    def number(key,default):
        value=saved.get(key,default)
        return value if isinstance(value,int) and not isinstance(value,bool) and abs(value)<=2**30 else default
    wanted=QRect(number('x',areas[0].x()),number('y',areas[0].y()),max(1,number('w',1200)),max(1,number('h',760)))
    area=max(areas,key=lambda v:v.intersected(wanted).width()*v.intersected(wanted).height())
    width=min(area.width(),max(min(720,area.width()),wanted.width()))
    height=min(area.height(),max(min(480,area.height()),wanted.height()))
    x=min(max(wanted.x(),area.x()),area.right()-width+1)
    y=min(max(wanted.y(),area.y()),area.bottom()-height+1)
    return QRect(x,y,width,height)


def make_scrollable(page):
    old=page.layout()
    holder=QWidget();holder.setLayout(old)
    scroll=QScrollArea();scroll.setFrameShape(QFrame.Shape.NoFrame);scroll.setWidgetResizable(True)
    scroll.setWidget(holder)
    layout=QVBoxLayout(page);layout.setContentsMargins(0,0,0,0);layout.addWidget(scroll)
    return scroll


def keep_ratio(rect, base, ratio, edges, min_width, min_height):
    """把自由拖动出来的 ``rect`` 收成 ``ratio``（宽/高）的矩形，返回新的 QRect。

    为什么单独抽成纯函数：拖动是"对角固定、被拖的那角跟着鼠标走"，四角要分开算，
    直接写在鼠标事件里几乎没法测；这里的每一步都能用数字断言。

    取哪条边当基准：比较宽高各自相对起点的变化量，**用户拖得多的那个方向说了算**，
    否则斜着拖时窗口会跟不上鼠标。高度按 ``ratio`` 反算，再用最小尺寸兜底。
    """
    if not ratio or ratio <= 0:
        return rect
    width_change = abs(rect.width() - base.width())
    height_change = abs(rect.height() - base.height())
    if height_change > width_change:
        height = max(int(rect.height()), int(min_height))
        width = max(int(round(height * ratio)), int(min_width))
        height = max(int(round(width / ratio)), int(min_height))
    else:
        width = max(int(rect.width()), int(min_width))
        height = max(int(round(width / ratio)), int(min_height))
        width = max(int(round(height * ratio)), int(min_width))
    result = QRect(rect)
    # 锚点必须取**起始几何**：被拖的两条边跟着鼠标走，另外两条边要钉在按下时的位置。
    # （踩过：拿自由拖动后的 rect 当锚点，左上/右下之外的组合会把窗口整体拽偏 ——
    #  例如拖"上+右"角时 rect.bottom() 已经被改过，不再是原来的下边。）
    if edges & Qt.Edge.LeftEdge:
        right = base.right()
        result.setRight(right)
        result.setLeft(right - width + 1)
    else:
        left = base.left()
        result.setLeft(left)
        result.setRight(left + width - 1)
    if edges & Qt.Edge.TopEdge:
        bottom = base.bottom()
        result.setBottom(bottom)
        result.setTop(bottom - height + 1)
    else:
        top = base.top()
        result.setTop(top)
        result.setBottom(top + height - 1)
    return result


class ResizeHandle(QWidget):
    def __init__(self,window,edges,cursor):
        super().__init__(window);self.edges=edges;self.setCursor(cursor)
        self._press=None;self._ratio=None
        self.proportional=False          # 由外观页的「等比例缩放」开关写入
        self.setToolTip('拖动调整窗口大小；按住 Shift 保持宽高比')
        self.setStyleSheet('background:transparent;')

    def mousePressEvent(self,event):
        if event.button()!=Qt.MouseButton.LeftButton:return
        window=self.window()
        shift=bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        corner=bool(self.edges & (Qt.Edge.LeftEdge|Qt.Edge.RightEdge)) and \
               bool(self.edges & (Qt.Edge.TopEdge|Qt.Edge.BottomEdge))
        # 只有"四角"能保持比例；左右/上下边只能改一个方向，谈比例没有意义。
        # 等比例时**不能**交给系统（startSystemResize 之后拖动过程就不归我们了，
        # 没法逐帧约束），改为自己算几何。
        self._ratio=None
        if corner and (self.proportional or shift) and window.height()>0:
            self._ratio=window.width()/window.height()
        if self._ratio is None:
            handle=window.windowHandle()
            if handle and handle.startSystemResize(self.edges):event.accept();return
        self._press=(event.globalPosition().toPoint(),QRect(window.geometry()));event.accept()

    def mouseMoveEvent(self,event):
        if not self._press:return
        point,rect=self._press;delta=event.globalPosition().toPoint()-point;result=QRect(rect);window=self.window()
        if self.edges & Qt.Edge.LeftEdge:result.setLeft(min(rect.left()+delta.x(),rect.right()-window.minimumWidth()+1))
        if self.edges & Qt.Edge.RightEdge:result.setRight(max(rect.right()+delta.x(),rect.left()+window.minimumWidth()-1))
        if self.edges & Qt.Edge.TopEdge:result.setTop(min(rect.top()+delta.y(),rect.bottom()-window.minimumHeight()+1))
        if self.edges & Qt.Edge.BottomEdge:result.setBottom(max(rect.bottom()+delta.y(),rect.top()+window.minimumHeight()-1))
        if self._ratio:
            result=keep_ratio(result,rect,self._ratio,self.edges,window.minimumWidth(),window.minimumHeight())
        window.setGeometry(result);event.accept()

    def mouseReleaseEvent(self,event):self._press=None;self._ratio=None;event.accept()


class WindowResizeHandles:
    def __init__(self,window,proportional=False):
        self.window=window
        self.proportional=bool(proportional)
        E=Qt.Edge;C=Qt.CursorShape
        self.items=[ResizeHandle(window,e,c) for e,c in (
            (E.LeftEdge,C.SizeHorCursor),(E.RightEdge,C.SizeHorCursor),
            (E.TopEdge,C.SizeVerCursor),(E.BottomEdge,C.SizeVerCursor),
            (E.TopEdge|E.LeftEdge,C.SizeFDiagCursor),(E.BottomEdge|E.RightEdge,C.SizeFDiagCursor),
            (E.TopEdge|E.RightEdge,C.SizeBDiagCursor),(E.BottomEdge|E.LeftEdge,C.SizeBDiagCursor))]
        self.update()

    def update(self):
        w,h=self.window.width(),self.window.height();m=6;c=12
        rects=((0,c,m,max(0,h-2*c)),(w-m,c,m,max(0,h-2*c)),(c,0,max(0,w-2*c),m),
            (c,h-m,max(0,w-2*c),m),(0,0,c,c),(w-c,h-c,c,c),(w-c,0,c,c),(0,h-c,c,c))
        for handle,rect in zip(self.items,rects):
            handle.proportional=self.proportional
            handle.setGeometry(*rect);handle.setVisible(not self.window.isMaximized() and not self.window.isFullScreen());handle.raise_()
