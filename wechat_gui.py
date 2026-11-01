#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鲸鱼娘 微信机器人 · 原生 Windows 控制台
- PyQt6 无边框窗口，自定义标题栏（拖拽 / 最小化 / 最大化 / 关闭）
- Windows 11 云母片效果（Mica），默认不透明度 70%
- 背景色可自定义（叠加在 Mica 层之上）
- 通过 HTTP 轮询本地 FastAPI（http://127.0.0.1:8091/api/）
- 自动在后台启动 FastAPI 服务
"""
import ctypes
import html as _html
import base64
import json
import os
import re
import subprocess
import shutil
import sys
import time
from datetime import datetime as _datetime
from pathlib import Path
from urllib.parse import quote

import requests
# GUI 只与本机 127.0.0.1 后端通信，绝不能走系统代理：
# 机器上挂了全局代理（http_proxy/https_proxy 环境变量）时，requests 默认会把
# 发往本机端口的请求也交给代理转发 → 表现为"后台未连接 / 设置页空白 / 莫名 404"。
# 这里统一用一个不读环境变量的 session 发所有本地请求。
_PROXY_FREE = requests.Session()
_PROXY_FREE.trust_env = False
from PyQt6.QtCore import (QPoint, QRect, QSize, Qt, QTimer, QThread, pyqtSignal)
from PyQt6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen,
                         QPixmap, QTextCharFormat, QTextCursor)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSizeGrip, QLayout, QInputDialog,
    QSlider, QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget, QAbstractItemView, QSizePolicy,
)

from src.gui.config import ensure_migrated as load_gui_config, save_gui_config
from src.gui.appearance import (
    import_background, resolve_background, clear_background,
)
from src.plugin_files import import_plugin_files, merge_uploaded_files, upload_options, IMAGE_EXTENSIONS
from src.gui.keyword_rules import KeywordRulesEditor
from src.gui.session_management import SessionManagementDialog
from src.gui.dialogue_tab import DialogueTab
from src.gui.window_sizing import fit_geometry,make_scrollable,WindowResizeHandles
from src.runtime_identity import APP_ID, APP_VERSION, PORT, backend_identity_error
from src.diagnostics_bundle import build_bundle, write_text_with_bom
from src.logging_setup import (install_excepthook, read_new_errors, record_exception,
                              setup_logging)

# ── 路径 ───────────────────────────────────────────────────────────────────────
# 打包成单文件 exe（PyInstaller --onefile）后，__file__ 指向**临时解包目录**
# (_MEIxxxx)，而不是 exe 所在目录。如果还按 __file__ 推路径，配置、背景图、
# 插件状态都会写进临时目录 —— 关掉程序就全丢（用户看到的是"设置不保存"）。
# 所以：冻结运行时，可写数据统一放到当前用户 LOCALAPPDATA，只有只读资源用解包目录。
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WhaleGirlWeChat"
    ROOT.mkdir(parents=True, exist_ok=True)
    BUNDLE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    for resource in (BUNDLE / "styles").rglob("*.yaml"):
        target = ROOT / resource.relative_to(BUNDLE)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(resource, target)
else:
    ROOT = Path(__file__).resolve().parent
    BUNDLE = ROOT
GUI_CFG = ROOT / "data" / "gui_config.json"
# 端口与身份标识统一取自 src/runtime_identity.py：GUI 里再写一份就会在后端换端口时
# 静默连不上（而且 identity 校验也会对不上）。
BASE_URL = f"http://127.0.0.1:{PORT}"
# 生成回滚脚本的二次确认口令：必须与 src/install_guard.ROLLBACK_CONFIRM 完全一致。
# 后端按字符串严格比对，这里差一个字就会一直被 400 挡住（tests/test_rollback_script151.py 会断言两者相等）。
ROLLBACK_CONFIRM = "已停止微信发送并愿意重启控制台"

# 图片过滤：与 src/gui/appearance.py 的扩展名白名单保持一致
IMAGE_FILTER = "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
# 卡片上给「管理表情库…」入口的插件（对照 QQ 版：只在这几个插件卡片上出现）
STICKER_LIBRARY_PLUGINS = frozenset({"sticker_admin", "sticker_steal", "image_library"})

# ── 图标 ──────────────────────────────────────────────────────────────────────
# 图标来自 assets/icons（由 tools/setup_icons.py 从图标库随机、不重复地分配后复制进来），
# 分配表在 assets/icon-map.json。打包后这些资源在 BUNDLE 里，所以走 BUNDLE 而不是 ROOT。
ICON_DIR = BUNDLE / "assets" / "icons"
try:
    ICON_MAP = json.loads((BUNDLE / "assets" / "icon-map.json").read_text("utf-8"))
except Exception:
    ICON_MAP = {}


def slot_icon(name: str) -> QIcon:
    """按槽位名取图标；取不到返回空 QIcon（Qt 会用默认图标，不会崩）。

    名字带 slot_ 前缀是刻意的：TitleBar 里有个局部变量叫 ``icon``，
    若这里也叫 ``icon`` 容易在后续编辑中撞名（Python 会把它当局部变量）。
    """
    filename = ICON_MAP.get(name)
    if not filename:
        return QIcon()
    path = ICON_DIR / filename
    return QIcon(str(path)) if path.is_file() else QIcon()


def app_icon() -> QIcon:
    """任务栏/窗口用的主图标：优先多尺寸 ico，退回分配表里的那个 PNG。"""
    ico = BUNDLE / "icon.ico"
    if ico.is_file():
        return QIcon(str(ico))
    return slot_icon("app")

# ── 配色（银白色 / 亮色主题）─────────────────────────────────────────────────
BG       = "#F4F6F9"   # 银白：窗口与容器底色
SILVER_TOP = "#F7F9FC"  # 模糊蒙层的银白目标色
CARD     = "#FFFFFF"   # 卡片
BORDER   = "#D3DAE3"   # 银灰描边
ACCENT   = "#2FA8A0"   # 青色点缀（亮底上要够深才够对比）
ACCENT2  = "#248780"
TEXT     = "#1F2733"   # 深灰正文
MUTED    = "#5F6B7A"   # 次级文字
ERROR    = "#C0392B"
SUCCESS  = "#1E8E5A"
FIELD    = "#FFFFFF"   # 输入框底色
ACCENT_FG = "#FFFFFF"  # 青色按钮上的文字

STYLESHEET = f"""
QWidget {{ background: transparent; color: {TEXT}; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 13px; }}
QLabel {{ color: {TEXT}; background: transparent; border: none; }}
QFrame[panel="true"] {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
QLineEdit, QTextEdit, QComboBox {{
    background: {FIELD}; border: 1px solid {BORDER}; border-radius: 7px;
    color: {TEXT}; padding: 8px; selection-background-color: {ACCENT};
}}
QComboBox::drop-down {{ border: 0; }}
QComboBox::down-arrow {{ color: {MUTED}; }}
QComboBox QAbstractItemView {{ background: {FIELD}; border: 1px solid {BORDER}; color: {TEXT}; }}
QPushButton {{
    background: {ACCENT}; color: {ACCENT_FG}; border: 0; border-radius: 7px;
    padding: 8px 16px; font-weight: bold;
}}
QPushButton:hover {{ background: #37bdb4; }}
QPushButton:pressed {{ background: {ACCENT2}; }}
QPushButton:disabled {{ background: #E3E8EE; color: {MUTED}; }}
QPushButton.secondary {{
    background: #E8EDF3; color: {TEXT}; border: 1px solid {BORDER};
}}
QPushButton.secondary:hover {{ background: #DDE5EE; }}
QPushButton.danger {{ background: #C0392B; color: #FFFFFF; }}
QScrollBar:vertical {{ background: #EDF1F5; width: 8px; border-radius: 4px; }}
QScrollBar::handle:vertical {{ background: #C2CCD8; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; border: 0; padding: 10px 18px;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
}}
QTabBar::tab:selected {{ background: {CARD}; color: {ACCENT}; border: 1px solid {BORDER}; border-bottom: 0; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QCheckBox {{ color: {TEXT}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; background: {FIELD};
    border: 1px solid {BORDER}; border-radius: 4px;
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QSlider::groove:horizontal {{
    height: 4px; background: {BORDER}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {ACCENT}; width: 16px; height: 16px; margin: -6px 0;
    border-radius: 8px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
"""

# 弹窗用的样式。
# 为什么需要单独一份：STYLESHEET 是**继承**的，只应用在 MainWindow 上，
# 而 QDialog 是独立顶层窗口、不是 MainWindow 的子控件，继承不到 ——
# 结果就是"主界面很漂亮、弹窗却是系统默认黑底白输入框"。
# 这里在 STYLESHEET 之上补 QDialog 自身的背景与输入框提示色。
DIALOG_STYLESHEET = STYLESHEET + f"""
QDialog {{ background: {BG}; }}
QLineEdit {{ background: {FIELD}; color: {TEXT}; }}
QLineEdit::placeholder {{ color: {MUTED}; }}
QTextEdit {{ background: {FIELD}; color: {TEXT}; }}
"""


def backend_error(timeout=2):
    """返回"当前后台能不能用"的说明；能用的返回空串。

    这里要把"端口被别的后台占着"说清楚：只说"请关闭旧后台"，用户不知道该关谁。
    所以尽量把占用者的**数据目录与进程号**带出来（这些信息只在本机可见）。
    """
    try:
        response=_PROXY_FREE.get(f'{BASE_URL}/api/runtime',timeout=timeout)
        if response.status_code!=200:
            # 端口用 PORT 拼：这里通常意味着 8091 上蹲着别的程序（或旧版后台）。
            return f'检测到旧版或其他后台占用 {PORT}。请关闭旧后台后重新打开当前控制台。'
        identity = response.json()
        error = backend_identity_error(identity, ROOT)
        if error and isinstance(identity, dict) and identity.get('app') == APP_ID:
            # 是我们自己的后台，但数据目录不一样（八成是源码版/另一份安装还在跑）
            error += (f"（占用者：PID {identity.get('pid')}，"
                      f"数据目录 {identity.get('data_root')}；"
                      f"当前控制台期望 {ROOT}）")
        return error
    except Exception:
        return '当前后台尚未连接，请稍等；若持续出现，请重新打开控制台。'


def conflicting_backend(timeout=2):
    """占用端口的进程是不是"我们自己的另一个后台"；是就返回它的 /api/runtime 信息。

    只认 APP_ID 相同的后端 —— 绝不因为端口被占用就去杀别的程序。
    """
    try:
        response = _PROXY_FREE.get(f'{BASE_URL}/api/runtime', timeout=timeout)
        if response.status_code != 200:
            return None
        identity = response.json()
    except Exception:
        return None
    if isinstance(identity, dict) and identity.get('app') == APP_ID:
        return identity
    return None


def terminate_conflicting_backend(identity) -> tuple:
    """结束"另一个自己的后台"，返回 (是否成功, 说明)。

    为什么可以自动做：同一个端口只能有一个后台，而它用的是**别处的数据目录**，
    当前控制台无法安全复用（会把配置写错地方）。用户遇到这个只能手关，
    但普通用户不知道该关哪个进程 —— 所以这里在确认身份后替他了结。
    """
    pid = identity.get('pid') if isinstance(identity, dict) else None
    # 再确认一次身份：本函数只应作用于"我们自己的另一个后台"。
    # 调用方虽然已经用 conflicting_backend() 筛过，但这里不省这一步 ——
    # 结束进程是不可逆操作，宁可多判一次，也不能因为将来某处误用而杀掉别人的程序。
    if not isinstance(identity, dict) or identity.get('app') != APP_ID:
        return False, "占用端口的进程不是本项目的后台，不会去动它"
    if not pid:
        return False, "拿不到占用进程号"
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           creationflags=0x08000000, capture_output=True, check=False)
        else:
            os.kill(int(pid), 9)
    except Exception as exc:
        return False, f"结束进程失败：{exc}"
    for _ in range(20):
        time.sleep(0.25)
        if conflicting_backend(timeout=1) is None:
            return True, f"已关闭占用 {PORT} 的旧后台（PID {pid}）"
    return False, f"PID {pid} 没有被关掉，请手动结束它"


def _response_result(response, list_key=None) -> dict:
    """响应 → 字典；非 2xx 时把后端返回的 detail 提升成 message。

    为什么必须这么做：FastAPI 的错误体是 ``{"detail": "..."}``，原来的实现不管状态码
    直接把 JSON 交给界面 —— 界面上只会看到默认的"操作完成"，真正的失败原因（配置校验
    不通过、404、没有权限）全被吞掉。
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400:
        detail = None
        if isinstance(body, dict):
            detail = body.get("detail")
            if detail is None:
                detail = body.get("message")
        if isinstance(detail, (list, dict)):
            # 422 的校验错误 detail 是列表，直接 str() 出来是一坨 repr
            detail = json.dumps(detail, ensure_ascii=False)
        message = str(detail) if detail else f"后端返回 HTTP {response.status_code}"
        return {"success": False, "message": message}
    if isinstance(body, list) and list_key:
        return {list_key: body}
    if not isinstance(body, dict):
        return {"success": False, "message": f"后端返回了非预期的响应（HTTP {response.status_code}）"}
    return body


def api_get(path: str, timeout: int = 3) -> dict:
    error=backend_error(min(timeout,2))
    if error:
        return {'success':False,'message':error}
    try:
        return _response_result(_PROXY_FREE.get(f"{BASE_URL}/api/{path}", timeout=timeout),
                                list_key='plugins' if path == 'plugins' else None)
    except requests.Timeout:
        return {'success': False, 'message': '读取后台响应超时，请稍后重试；当前显示的数据可能尚未更新。'}
    except requests.ConnectionError:
        return {'success': False, 'message': '无法连接后台，请点击“重连后台”重试。'}
    except Exception:
        return {'success': False, 'message': '读取后台响应失败，请稍后重试。'}


def api_post(path: str, data: dict = None, timeout: int = 10) -> dict:
    error=backend_error(min(timeout,2))
    if error:
        return {'success':False,'message':error}
    try:
        return _response_result(_PROXY_FREE.post(f"{BASE_URL}/api/{path}", json=data or {}, timeout=timeout))
    except requests.Timeout:
        return {"success": False, "message": "等待后台响应超时，操作可能仍在执行；请查看运行状态和记录，不要重复点击。"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def api_delete(path: str, timeout: int = 15) -> dict:
    """DELETE 请求。失败时返回 {success: False, message}，不抛异常。"""
    error=backend_error(min(timeout,2))
    if error:
        return {'success':False,'message':error}
    try:
        return _response_result(_PROXY_FREE.delete(f"{BASE_URL}/api/{path}", timeout=timeout))
    except Exception as e:
        return {"success": False, "message": str(e)}


# ── 非阻塞调用（用户实测的"界面异常卡死"就出在这里）────────────────────────────
# 微信连接要十几到二十秒的键鼠自动化（找窗口 + 校准布局），安装插件、读群列表也可能
# 等好几秒。只要这段等待发生在 Qt 主线程，整个窗口就不再重绘、不再响应点击 —— 看起来
# 就是"卡死"。所以交互路径一律走 `call_api()`：请求在后台线程跑，结果排队回主线程。
#
# 没有顶层窗口的场合（单测、脚本、离线自检里直接 new 出来的控件）自动退化成同步调用，
# 这样这些代码不用为一个回调写两遍，既有测试的语义也不变。

def _request(method: str, path: str, data: dict = None, timeout: float = 10) -> dict:
    if method == "GET":
        return api_get(path, timeout=timeout)
    if method == "DELETE":
        return api_delete(path, timeout=timeout)
    return api_post(path, data, timeout=timeout)


def action_controls(window,busy):
    console=getattr(window,'console_tab',None)
    for name in ('btn_start','btn_stop','btn_test_ai','btn_test_wechat'):
        button=getattr(console,name,None)
        if button is not None:button.setEnabled(not busy)
    settings=getattr(window,'settings_tab',None)
    button=getattr(settings,'btn_save',None)
    if button is not None:button.setEnabled(not busy and getattr(window,'_settings_loaded',True) and not getattr(window,'_settings_loading',False))


class ApiJob(QThread):
    """一次后台 HTTP 调用。

    结果的派发刻意走 ``self.deliver``（本对象自己的方法）而不是外部传入的
    lambda：Qt 的自动连接要拿**接收者对象的线程归属**来决定是否排队，普通函数
    没有线程归属，会在**工作线程**里直接执行回调 —— 那样一碰控件就是跨线程操作，
    轻则界面随机崩，重则再也刷不出来。ApiJob 对象是在主线程创建的，所以信号排队
    回到主线程执行，回调里可以安全操作控件。
    """

    done = pyqtSignal(object)

    def __init__(self, method: str, path: str, data: dict = None, timeout: float = 10):
        super().__init__()
        self._method, self._path, self._data, self._timeout = method, path, data, timeout
        self._callback = None

    def set_callback(self, callback):
        self._callback = callback

    def run(self):
        try:
            result = _request(self._method, self._path, self._data, self._timeout)
        except Exception as exc:
            result = {"success": False, "message": str(exc)}
        self.done.emit(result)

    def deliver(self, result):
        if self._callback is not None:
            self._callback(result)


class TaskJob(QThread):
    """把任意阻塞函数丢到后台线程跑，返回值排队回主线程。

    与 :class:`ApiJob` 同一套派发约定（见那里的说明）；区别只是跑的不是 HTTP 请求，
    而是"探测端口 / 关掉旧后台 / 起后端"这类同样会把界面拖僵的活。
    """

    done = pyqtSignal(object)

    def __init__(self, func):
        super().__init__()
        self._func = func
        self._callback = None

    def set_callback(self, callback):
        self._callback = callback

    def run(self):
        try:
            result = self._func()
        except Exception as exc:      # 线程里抛错没人接，转成结果交给界面
            result = exc
        self.done.emit(result)

    def deliver(self, result):
        if self._callback is not None:
            self._callback(result)


def apply_backend_probe(window, result):
    """按 :func:`ensure_backend` 的结果给界面提示并按需在进程内起后端。

    写成模块级函数而不是窗口方法：单测里 ``self`` 常常是 SimpleNamespace，
    不能假设它有窗口的那些方法。
    """
    # TaskJob 将探测异常作为结果送回主线程；不能把它或损坏结果当作连接成功。
    if isinstance(result, Exception):
        record_exception(result, message='后台连接检查失败')
        message, ok, in_process = f'后台连接检查失败（{type(result).__name__}），请查看运行日志后重试。', False, False
    elif (not isinstance(result, tuple) or len(result) != 3
            or not isinstance(result[0], str) or type(result[1]) is not bool
            or type(result[2]) is not bool):
        message, ok, in_process = '后台连接检查返回无效结果，请重连后台后重试。', False, False
    else:
        message, ok, in_process = result
        if not ok and not message:
            message = '后台连接检查未成功，请重连后台后重试。'
    if message:
        window.console_tab.set_notice(message, ok=ok)
    if not ok:
        return
    if in_process:
        window._start_backend_inprocess()


def ensure_backend(window):
    """探测端口并（必要时）启动 FastAPI 服务；返回 ``(提示, 成功, 是否进程内起)``。

    三种运行方式差别很大：
    - 直接跑脚本：用 `sys.executable -m uvicorn ...` 起一个子进程；
    - 打包成 exe：`sys.executable` 就是**这个 exe 自己**，再 spawn 一次等于又开一个
      控制台（后端永远不会起来）。所以冻结运行时改为**在进程内**用 uvicorn 起服务；
    - 端口上已经有"另一个自己的后台"（源码版没关、或另一份安装还在跑）：
      它用的是别处的数据目录，本控制台无法复用，先把它关掉再起自己的。

    全程可能要等好几秒（关旧后台含 20×0.25s 的等待），所以由调用方放到后台线程里跑 ——
    以前这段在主线程里 sleep，窗口一打开就是僵的。
    """
    if window._backend_ready():
        return "", True, False
    conflict = conflicting_backend(timeout=1)
    message = ""
    if conflict is not None and not window._backend_ready():
        ok, detail = terminate_conflicting_backend(conflict)
        if not ok:
            return detail, False, False
        message = detail
    try:
        _PROXY_FREE.get(f'{BASE_URL}/api/status', timeout=1)
    except Exception:
        pass
    else:
        # 端口被**别的程序**占着：绝不去动它，如实说明并让用户处理
        return backend_error(), False, False
    if FROZEN:
        return message, True, True
    try:
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        window._backend_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "src.wechat_app:create_app",
             "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=str(ROOT),
            creationflags=flags,
        )
    except Exception as exc:
        return f"后端启动失败：{exc}", False, False
    return message, True, False


def call_api(owner, method: str, path: str, data: dict = None, timeout: float = 10,
             on_done=None):
    """发起一次接口调用：能异步就异步，否则同步执行并立即回调。

    返回 ``None``（异步，结果走 ``on_done``）或结果字典（同步）。
    """
    window = None
    try:
        window = owner.window() if owner is not None else None
    except Exception:
        window = None
    runner = getattr(window, "_run_api", None)
    if not callable(runner):
        # 模态 QDialog 自己就是 window，仍应使用宿主线程，不能退回主线程阻塞 HTTP。
        ancestor = owner
        while ancestor is not None:
            runner = getattr(ancestor, '_run_api', None)
            if callable(runner):
                break
            ancestor = ancestor.parentWidget() if hasattr(ancestor, 'parentWidget') else None
    if callable(runner):
        def deliver(result):
            from PyQt6 import sip
            if isinstance(owner, QWidget) and sip.isdeleted(owner):
                return
            if on_done is not None:
                on_done(result)
        runner(method, path, data, timeout, deliver)
        return None
    result = _request(method, path, data, timeout)
    if on_done is not None:
        on_done(result)
    return result


# DWM 属性号
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38
# 系统背景类型：1=无 2=云母 3=亚克力 4=标签页云母
BACKDROP_NONE, BACKDROP_MICA, BACKDROP_ACRYLIC, BACKDROP_TABBED = 1, 2, 3, 4


def apply_mica(hwnd: int, dark: bool = False, backdrop: int = BACKDROP_MICA) -> bool:
    """对窗口句柄应用 Windows 11 云母片（Mica）。

    ``backdrop`` 选系统背景类型（见上面的 BACKDROP_*）。系统的云母片本身**没有**
    强度参数，所以"模糊度"由两层实现：这里决定用哪种背板（0 = 不用），
    窗口内的银白蒙层再按模糊度决定透出多少（见 BackgroundFrame）。
    """
    try:
        dwm = ctypes.windll.dwmapi
        mode = ctypes.c_int(1 if dark else 0)
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(mode), ctypes.sizeof(mode))
        value = ctypes.c_int(int(backdrop))
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(value), ctypes.sizeof(value))
        return True
    except Exception:
        return False


def backdrop_for_blur(blur: float, available: bool = True) -> int:
    """按模糊度挑系统背景类型：0 → 不用背板；低模糊用亚克力；其余用云母。

    系统只提供几种固定背板，没有连续强度参数，所以这里做离散映射，
    连续变化由窗口内的蒙层负责。
    """
    if not available or blur <= 0.0:
        return BACKDROP_NONE
    if blur < 0.5:
        return BACKDROP_ACRYLIC
    return BACKDROP_MICA


def scrim_alpha_for_blur(blur: float) -> float:
    """模糊度 → 银白蒙层的不透明度。

    0 = 完全看得清桌面/背景图；1 = 基本只剩银白，细节全被盖住（观感上就是"很糊"）。
    """
    try:
        value = float(blur)
    except (TypeError, ValueError):
        value = 0.0
    value = min(max(value, 0.0), 1.0)
    return value * 0.85


def overlay_color(base: str, blur: float) -> QColor:
    """把"底色 + 模糊蒙层"合成成一个纯色，供自绘使用。

    返回**不透明**颜色：模糊度 0 时是底色本身，随模糊度升高向银白（#F7F9FC）靠拢
    —— 在完全不用云母片（没有真正背景模糊可看）时，"模糊度"就靠这个体现。
    """
    color = QColor(base)
    if not color.isValid():
        color = QColor(BG)
    mix = scrim_alpha_for_blur(blur)
    if mix <= 0:
        return color
    silver = QColor(SILVER_TOP)
    blend = min(mix * 0.45, 0.45)  # 上限 45%：保持"银白"而不是越调越白得发飘
    return QColor(
        int(color.red() * (1 - blend) + silver.red() * blend),
        int(color.green() * (1 - blend) + silver.green() * blend),
        int(color.blue() * (1 - blend) + silver.blue() * blend),
    )


def window_opacity_from_transparency(transparency: float) -> float:
    """透明度 → Qt 的窗口不透明度。

    ⚠️ Qt 在不透明度 == 1.0 时把窗口当作完全不透明，会丢掉 WA_TranslucentBackground
    的逐像素透明，云母片背板也随之消失。所以"0% 透明"映射到 0.999（视觉上完全不透明，
    但保留透明通道）。

    注意副作用：正因为映射到 0.999，"0% 透明"时背板仍会极轻微地透出来。想要纯银白，
    把背景叠加色设成不透明的银白即可完全盖住。
    """
    try:
        value = float(transparency)
    except (TypeError, ValueError):
        value = 0.0
    value = min(max(value, 0.0), 1.0)
    return round(min(max(1.0 - value, 0.0), 0.999), 4)


class BackgroundFrame(QFrame):
    """容器：先铺纯色底，再按 cover 语义绘制背景图，保证不变形且不留黑边。

    为什么不用样式表 background-image：Qt 样式表不支持 background-size，
    且读取中文/空格路径的图片会失败（实测 Could not create pixmap）。
    QPixmap 对同样的路径加载正常，所以这里自绘。
    """

    def __init__(self):
        super().__init__()
        self._color = BG
        self._pixmap: QPixmap | None = None
        self._blur = 0.70
        self._alpha = 1.0  # 容器自身填充的不透明度，由"透明度"反推

    def set_background_color(self, color: str):
        self._color = color or BG
        self.update()

    def set_background_pixmap(self, pixmap: QPixmap | None):
        self._pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        self.update()

    def set_blur(self, blur: float):
        """0~1：越高，盖在背景上的银白蒙层越厚 = 看起来越糊。"""
        try:
            value = float(blur)
        except (TypeError, ValueError):
            value = 0.0
        self._blur = min(max(value, 0.0), 1.0)
        self.update()

    def set_fill_alpha(self, alpha: float):
        """容器填充的不透明度 = 1 - 窗口透明度。

        必须跟着窗口透明度走：容器如果永远画成不透明，把"透明度"调高也只会
        穿透到桌面被这层挡住 —— 滑块看起来就没用了。
        """
        try:
            value = float(alpha)
        except (TypeError, ValueError):
            value = 1.0
        self._alpha = min(max(value, 0.0), 1.0)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        # 底色 = 叠加色与银白蒙层按模糊度混合。0% 透明（窗口不透明）时，
        # 这个底就是用户看到的全部，所以模糊度必须在这里就体现出来。
        base = overlay_color(self._color, self._blur)
        base.setAlphaF(self._alpha)
        painter.fillRect(self.rect(), base)
        pixmap = self._pixmap
        if pixmap is not None:
            area = self.rect()
            if area.width() > 0 and area.height() > 0:
                scale = max(area.width() / pixmap.width(), area.height() / pixmap.height())
                width = max(1, int(pixmap.width() * scale))
                height = max(1, int(pixmap.height() * scale))
                target = QRect(
                    int((area.width() - width) / 2),
                    int((area.height() - height) / 2),
                    width,
                    height,
                )
                if self._alpha < 1.0:
                    painter.setOpacity(self._alpha)
                painter.drawPixmap(target, pixmap)
                painter.setOpacity(1.0)
                # 有背景图时，蒙层改画在图片之上，否则"模糊度"对图片毫无作用。
                alpha = scrim_alpha_for_blur(self._blur)
                if alpha > 0:
                    scrim = QColor(255, 255, 255)
                    scrim.setAlphaF(alpha * self._alpha)
                    painter.fillRect(area, scrim)
        painter.end()
        super().paintEvent(event)


# ── 自定义标题栏 ──────────────────────────────────────────────────────────────
class TitleBar(QWidget):
    """可拖拽的自定义标题栏，包含最小化/最大化/关闭按钮。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedHeight(44)
        self._drag_pos = None
        self._maximized = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 8, 0)
        layout.setSpacing(8)

        # 图标 + 标题（图标来自图标库，不再是 emoji）
        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(22, 22))
        mark.setFixedSize(24, 24)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel(f"鲸鱼娘 · 微信控制台 {APP_VERSION}")
        title.setStyleSheet(f"color: {TEXT}; font-size: 14px; font-weight: bold;")

        layout.addWidget(mark)
        layout.addWidget(title)
        layout.addStretch()

        # 窗口按钮
        for text, slot, cls in [("─", self._minimize, ""), ("□", self._toggle_max, ""), ("✕", self._close, "danger")]:
            btn = QPushButton(text)
            btn.setFixedSize(38, 32)
            btn.setProperty("class", cls)
            btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {MUTED}; border: 0; border-radius: 6px; font-size: 14px; }}
                QPushButton:hover {{ background: {'#7a1f1f' if cls=='danger' else '#253748'}; color: {TEXT}; }}
            """)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

    def _minimize(self):
        self.window().showMinimized()

    def _toggle_max(self):
        w = self.window()
        if w.isMaximized():
            w.showNormal()
        else:
            w.showMaximized()
        self._maximized = not self._maximized

    def _close(self):
        self.window().close()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, event):
        self._toggle_max()


# ── 状态卡片 ──────────────────────────────────────────────────────────────────
class StatusCard(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setProperty("panel", True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self.value_lbl = QLabel("读取中")
        self.value_lbl.setStyleSheet(f"color: {TEXT}; font-size: 15px; font-weight: bold;")
        self.value_lbl.setWordWrap(True)
        self.detail_lbl = QLabel("")
        self.detail_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.detail_lbl.setWordWrap(True)
        for label in (self.value_lbl,self.detail_lbl):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title_lbl)
        layout.addWidget(self.value_lbl)
        layout.addWidget(self.detail_lbl)

    def update(self, value: str, detail: str = "", ok: bool | None = None):
        self.value_lbl.setText(value)
        self.detail_lbl.setText(detail)
        self.detail_lbl.setToolTip(detail)
        if ok is True:
            self.value_lbl.setStyleSheet(f"color: {SUCCESS}; font-size: 15px; font-weight: bold;")
        elif ok is False:
            self.value_lbl.setStyleSheet(f"color: {ERROR}; font-size: 15px; font-weight: bold;")
        else:
            self.value_lbl.setStyleSheet(f"color: {TEXT}; font-size: 15px; font-weight: bold;")


# ── 控制台标签页 ──────────────────────────────────────────────────────────────
def memory_card(data: dict):
    """"记忆与省 token"卡：标题是记忆规模，副标题是用量与省下的量。

    字段缺失时全部降级成 0，绝不因为后台是老版本就崩（覆盖升级时后台可能还没重启）。
    """
    memory = data.get("memory") or {}
    usage = data.get("usage") or {}
    manual = int(memory.get("manual") or 0)
    confirmed = int(memory.get("confirmed") or 0)
    candidate = int(memory.get("candidate") or 0)
    learned = int(memory.get("learned") or 0)
    people = int(memory.get("people") or 0)
    title = f"记忆 {confirmed + manual} 条 · 学习 {learned} 条" if (confirmed or manual or learned) else "记忆为空"
    parts = []
    if people:
        parts.append(f"{people} 位群友")
    if candidate:
        parts.append(f"{candidate} 条待确认")
    tokens = int(usage.get("total_tokens") or 0)
    requests = int(usage.get("request_count") or 0)
    if tokens:
        parts.append(f"已用 {tokens} token / {requests} 次调用")
    else:
        parts.append("还没有调用记录")
    return title, " · ".join(parts)


def model_subtitle(data: dict) -> str:
    """「模型 / API」卡片的副标题：测试结论 + 用量 + 估算花费。

    用户看"用了多少 token"没有成本感；这里按设置的计费方式折成金额，
    和 QQ 版「群友花费统计」的口径一致（单价留 0 就只报用量）。
    """
    tested = str(data.get("ai_test") or "")
    if '尚未测试' in tested:
        tested = '主动连接测试：' + tested
    parts = [tested]
    billing = data.get("billing") or {}
    tokens = int(billing.get("tokens") or (data.get("usage") or {}).get("total_tokens") or 0)
    requests = int(billing.get("requests") or (data.get("usage") or {}).get("request_count") or 0)
    usage = f"已用 {tokens:,} token / {requests} 次请求"
    cost = billing.get("cost")
    if isinstance(cost, (int, float)):
        usage += f" ≈ ¥{float(cost):.4f}"
    parts.append(usage)
    return " ｜ ".join(part for part in parts if part)


def _file_log_caption(block, today):
    """只按首行有效日志日期区分早于今日的记录，不推断后台启动边界。"""
    kind = '警告' if getattr(block, 'level', '') == 'WARNING' else '错误'
    first = str(block).partition('\n')[0].lstrip('\ufeff')
    try:
        if not re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d{3})?(?:\s|$)', first):
            raise ValueError('no log timestamp')
        day = _datetime.strptime(first[:19], '%Y-%m-%d %H:%M:%S').date()
    except ValueError:
        return f'⚠ 日志{kind}（日期未确认）\n'
    if day < today:
        return f'⚠ 历史{kind} · {day.isoformat()}\n'
    if day == today:
        return f'⚠ 今日{kind} · {day.isoformat()}\n'
    return f'⚠ 日志{kind} · {day.isoformat()}（日期晚于今日，待核对）\n'


class ConsoleTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # 状态卡片行
        cards_layout = QHBoxLayout()
        self.card_running = StatusCard("机器人服务")
        self.card_conn = StatusCard("微信接入")
        self.card_model = StatusCard("模型 / API")
        self.card_memory = StatusCard("记忆与省 token")
        for c in [self.card_running, self.card_conn, self.card_model, self.card_memory]:
            cards_layout.addWidget(c,1)
        layout.addLayout(cards_layout)

        # 操作按钮
        btn_layout = QHBoxLayout()
        self.btn_start = QPushButton("▶ 启动机器人")
        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.setProperty("class", "secondary")
        self.btn_test_ai = QPushButton("测试 AI")
        self.btn_test_ai.setProperty("class", "secondary")
        self.btn_test_wechat = QPushButton("测试微信")
        self.btn_test_wechat.setProperty("class", "secondary")
        self.btn_retry_backend=QPushButton('重连后台');self.btn_retry_backend.setProperty('class','secondary')
        self.notice = QLabel("正在连接后端服务…")
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        # 报错条必须能用鼠标拖选：只让用户截图，开发者拿不到原文就只能猜
        self.notice.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.notice.setMinimumWidth(0)
        self.notice.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        for b in [self.btn_start, self.btn_stop, self.btn_test_ai, self.btn_test_wechat,self.btn_retry_backend]:
            btn_layout.addWidget(b)
        self.btn_safety = QPushButton('账号安全')
        def open_safety():
            from src.gui.safety_panel import SafetyDialog
            SafetyDialog(call_api,self).exec()
        self.btn_safety.clicked.connect(open_safety)
        btn_layout.addWidget(self.btn_safety)
        self.btn_sandbox = QPushButton('隔离后台')
        self.btn_sandbox.setProperty('class','secondary')
        def open_sandbox():
            from src.gui.sandbox_panel import SandboxDialog
            SandboxDialog(call_api,self).exec()
        self.btn_sandbox.clicked.connect(open_sandbox)
        btn_layout.addWidget(self.btn_sandbox)
        self.btn_hook = QPushButton('协议候选')
        self.btn_hook.setProperty('class','secondary')
        def open_hook():
            from src.gui.hook_panel import HookDialog
            HookDialog(call_api,self).exec()
        self.btn_hook.clicked.connect(open_hook)
        btn_layout.addWidget(self.btn_hook)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        notice_row = QHBoxLayout()
        notice_row.setContentsMargins(0, 0, 0, 0)
        self.btn_copy_notice = QPushButton("复制报错")
        self.btn_copy_notice.setProperty("class", "secondary")
        self.btn_copy_notice.setToolTip("把当前这条提示/报错连同时间一起复制到剪贴板")
        self.btn_copy_notice.clicked.connect(self._copy_notice)
        self.copy_hint = QLabel("")
        self.copy_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        notice_row.addWidget(self.notice, 1)
        notice_row.addWidget(self.btn_copy_notice)
        notice_row.addWidget(self.copy_hint)
        layout.addLayout(notice_row)

        # 消息 + 事件
        splitter = QSplitter(Qt.Orientation.Horizontal)
        msg_widget = QWidget()
        msg_layout = QVBoxLayout(msg_widget)
        msg_layout.setContentsMargins(0, 0, 0, 0)
        msg_layout.addWidget(icon_row("消息与回复", "card_messages"))
        self.msg_view = QTextEdit()
        self.msg_view.setReadOnly(True)
        self.msg_view.setStyleSheet(f"background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px;")
        msg_layout.addWidget(self.msg_view)

        evt_widget = QWidget()
        evt_layout = QVBoxLayout(evt_widget)
        evt_layout.setContentsMargins(0, 0, 0, 0)
        evt_layout.addWidget(icon_row("运行记录", "card_events"))
        log_row = QHBoxLayout()
        log_row.setContentsMargins(0, 0, 0, 0)
        self.btn_open_logs = QPushButton("打开后台日志目录")
        self.btn_open_logs.clicked.connect(self._open_logs)
        self.btn_copy_events = QPushButton("复制全部")
        self.btn_copy_events.setProperty("class", "secondary")
        self.btn_copy_events.setToolTip("把运行记录全文（含完整报错堆栈）复制到剪贴板")
        self.btn_copy_events.clicked.connect(self._copy_events)
        self.btn_export_txt = QPushButton("导出 TXT")
        self.btn_export_txt.setProperty("class", "secondary")
        self.btn_export_txt.setToolTip("存成 txt 发给开发者，记事本双击直接可读")
        self.btn_export_txt.clicked.connect(self._export_events_txt)
        self.btn_export_bundle = QPushButton("导出诊断包")
        self.btn_export_bundle.setProperty("class", "secondary")
        self.btn_export_bundle.setToolTip("把报错、运行记录和后台日志压成一个 zip，反馈问题时直接发这个")
        self.btn_export_bundle.clicked.connect(self._export_bundle)
        self.btn_open_bundle = QPushButton("打开所在目录")
        self.btn_open_bundle.setProperty("class", "secondary")
        self.btn_open_bundle.setToolTip("打开最近一次导出的诊断包所在文件夹")
        self.btn_open_bundle.clicked.connect(self._open_bundle_folder)
        self.btn_env_check = QPushButton("兼容性自检")
        self.btn_env_check.setProperty("class", "secondary")
        self.btn_env_check.setToolTip("读一遍这台机器的缩放、屏幕、微信版本、系统 OCR、沙盒和端口，判断能不能搬过去跑")
        self.btn_env_check.clicked.connect(self._show_env_profile)
        for button in (self.btn_open_logs, self.btn_copy_events, self.btn_export_txt,
                       self.btn_export_bundle, self.btn_open_bundle, self.btn_env_check):
            log_row.addWidget(button)
        log_row.addStretch()
        evt_layout.addLayout(log_row)
        log_filters = QHBoxLayout()
        self.log_source = QComboBox()
        self.log_source.addItem('运行摘要', 'summary')
        self.log_source.addItem('后台完整日志', 'backend')
        self.log_source.setToolTip('后台完整日志包含发送阶段；有界显示最近 500 条，完整文件可导出')
        log_filters.addWidget(self.log_source)
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText('搜索当前日志')
        self.log_search.setMinimumWidth(80)
        log_filters.addWidget(self.log_search, 1)
        self.log_level = QComboBox()
        for title, level in (('全部级别', ''), ('普通记录', 'INFO'), ('警告', 'WARNING'), ('错误', 'ERROR')):
            self.log_level.addItem(title, level)
        log_filters.addWidget(self.log_level)
        self.log_pause = QCheckBox('暂停滚动')
        log_filters.addWidget(self.log_pause)
        self.btn_clear_log = QPushButton('清空显示')
        self.btn_clear_log.setToolTip('只清空当前显示；后台日志文件与聊天记录保留')
        log_filters.addWidget(self.btn_clear_log)
        evt_layout.addLayout(log_filters)
        self.log_summary = QLabel('暂无记录')
        self.log_summary.setStyleSheet(f'color: {MUTED}; font-size: 11px;')
        evt_layout.addWidget(self.log_summary)
        # 兼容性自检结果：点按钮才出现，只读，不占常驻高度
        self.env_view = QTextEdit()
        self.env_view.setReadOnly(True)
        self.env_view.setMaximumHeight(150)
        self.env_view.setVisible(False)
        self.env_view.setStyleSheet(
            f"background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px; font-size: 11px;")
        evt_layout.addWidget(self.env_view)
        self.evt_view = QTextEdit()
        self.evt_view.setReadOnly(True)
        self.evt_view.setStyleSheet(f"background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px;")
        evt_layout.addWidget(self.evt_view)
        # 增量跟读 backend.log：记已读偏移，只把新错误追加进来，不重复刷屏
        self._log_offset = 0
        self._error_blocks = []
        self._error_shown = 0
        self._runtime_events = []
        self._backend_records = []
        self._event_seen = set()
        self._event_seen_order = []
        self._cleared_error_ids = set()
        self._log_rendering = False
        self._log_follow_tail = True
        self._log_follow_range = self.evt_view.verticalScrollBar().maximum()
        self.evt_view.verticalScrollBar().rangeChanged.connect(self._log_range_changed)
        self.evt_view.verticalScrollBar().valueChanged.connect(self._log_scroll_changed)
        self.evt_view.verticalScrollBar().sliderMoved.connect(self._log_user_scroll)
        self.evt_view.verticalScrollBar().actionTriggered.connect(
            lambda _action: self._log_user_scroll(self.evt_view.verticalScrollBar().sliderPosition()))
        self.log_search.textChanged.connect(lambda: self._render_logs(force=True))
        self.log_source.currentIndexChanged.connect(lambda: self._render_logs(force=True))
        self.log_level.currentIndexChanged.connect(lambda: self._render_logs(force=True))
        self.log_pause.toggled.connect(lambda: self._render_logs())
        self.btn_clear_log.clicked.connect(self._clear_log_display)
        self.evt_view.selectionChanged.connect(self._render_logs)
        self._last_bundle = None
        self._tail_timer = QTimer(self)
        self._tail_timer.timeout.connect(self._poll_log_errors)
        self._tail_timer.start(5000)
        self._poll_log_errors()

        splitter.addWidget(msg_widget)
        splitter.addWidget(evt_widget)
        splitter.setSizes([600, 400])
        layout.addWidget(splitter)

        # 环境与自检：换机器、覆盖升级前后先看这里；只有「清理残留通道」会写，其余全只读
        layout.addWidget(self._build_selfcheck_panel())

        # 模拟对话面板：仅"模拟模式"后端显示，让用户不连微信也能端到端跑一遍链路
        self.mock_panel = QWidget()
        mp = QVBoxLayout(self.mock_panel)
        mp.setContentsMargins(0, 4, 0, 0)
        mp.setSpacing(6)
        mock_title = QLabel("模拟对话（离线验证：手动喂一条消息，看机器人怎么回）")
        mock_title.setStyleSheet(f"color: {ACCENT}; font-size: 12px; font-weight: bold;")
        mp.addWidget(mock_title)
        row = QHBoxLayout()
        self.mock_input = QLineEdit()
        self.mock_input.setPlaceholderText("要发给机器人的话，例如：在吗？")
        self.mock_at = QCheckBox("@我")
        self.mock_send = QPushButton("发送")
        self.mock_send.setProperty("class", "primary")
        row.addWidget(self.mock_input, 1)
        row.addWidget(self.mock_at)
        row.addWidget(self.mock_send)
        mp.addLayout(row)
        self.mock_panel.hide()
        layout.addWidget(self.mock_panel)
        self.mock_send.clicked.connect(self._send_mock_message)

    def _open_logs(self):
        folder = ROOT / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    # ── 运行记录：复制 / 导出 / 错误堆栈跟读 ────────────────────────────────
    ERROR_BLOCK_LIMIT = 20  # 界面上最多留最近 20 段堆栈，再多用户也没法看

    def _notice_copy_text(self) -> str:
        """复制报错的正文：时间戳 + 报错条原文，粘到聊天窗口里就能对上时间。"""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        return f"[{stamp}] {self.notice.text().strip() or '（当前没有报错）'}"

    def _copy_notice(self):
        try:
            QApplication.clipboard().setText(self._notice_copy_text())
        except Exception as exc:
            self.set_notice(f"复制失败：{exc}", ok=False)
            return
        self.copy_hint.setText("已复制报错")

    def _copy_events(self):
        text = self.evt_view.toPlainText().strip()
        if not text:
            self.copy_hint.setText("运行记录还是空的")
            return
        try:
            QApplication.clipboard().setText(self.evt_view.toPlainText())
        except Exception as exc:
            self.set_notice(f"复制失败：{exc}", ok=False)
            return
        self.copy_hint.setText("已复制运行记录")

    def _events_text(self) -> str:
        """导出 TXT 的正文：报错条 + 运行记录（含红色堆栈），与界面所见一致。"""
        return "\n".join([
            f"鲸鱼娘 · 微信控制台 {APP_VERSION}",
            f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "【当前报错 / 提示】",
            self.notice.text().strip() or "（暂无）",
            "",
            "【运行记录】",
            self.evt_view.toPlainText().strip() or "（暂无）",
            "",
        ])

    def _export_events_txt(self):
        stamp = time.strftime("%Y%m%d-%H%M")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出运行记录", str(Path.home() / f"运行记录-{stamp}.txt"), "文本文件 (*.txt)")
        if not path:
            return
        try:
            # UTF-8 带 BOM：Windows 记事本双击打开不会乱码
            saved = write_text_with_bom(path, self._events_text(), root=ROOT)
        except OSError as exc:
            self.set_notice(f"导出失败：{exc}（换一个目录再试，比如桌面）", ok=False)
            return
        self.copy_hint.setText(f"已导出 {saved.name}")

    def _export_bundle(self):
        stamp = time.strftime("%Y%m%d-%H%M")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出诊断包", str(Path.home() / f"诊断包-{stamp}.zip"), "压缩包 (*.zip)")
        if not path:
            return
        try:
            self._last_bundle = build_bundle(
                path, root=ROOT, notice=self.notice.text(), events=self.evt_view.toPlainText())
        except Exception as exc:
            # 槽函数里抛异常会被 Qt 吞掉，必须自己落堆栈
            record_exception(exc, message="诊断包导出失败")
            self.set_notice(f"诊断包导出失败：{exc}", ok=False)
            return
        self.set_notice(f"诊断包已导出：{self._last_bundle}（可点「打开所在目录」）", ok=True)

    def _open_bundle_folder(self):
        folder = self._last_bundle.parent if self._last_bundle else ROOT / "logs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except OSError as exc:
            self.set_notice(f"打开目录失败：{exc}", ok=False)

    def _show_env_profile(self):
        """机器兼容性自检：只读展示结果，不改任何设置、不启动微信。

        探测本身要等本机端口一次（约 1.5 秒上限），走 call_api() 免得点一下窗口僵住。
        """
        self.btn_env_check.setEnabled(False)
        self.set_notice("正在读取本机环境…", ok=True)

        def apply(profile):
            self.btn_env_check.setEnabled(True)
            items = profile.get("items") if isinstance(profile, dict) else None
            if not isinstance(items, list):
                message = str((profile or {}).get("message") or "后台没有返回探测结果")
                self.env_view.setPlainText(f"兼容性自检失败：{message}")
                self.env_view.setVisible(True)
                self.set_notice(f"兼容性自检失败：{message}", ok=False)
                return
            lines = [str(profile.get("summary") or "兼容性自检完成")]
            for item in items:
                if isinstance(item, dict):
                    lines.append(f"{item.get('key')}: {item.get('status')} — {item.get('detail')}")
            self.env_view.setPlainText("\n".join(lines))
            self.env_view.setVisible(True)
            self.set_notice(str(profile.get("summary") or "兼容性自检完成"),
                            ok=bool(profile.get("ok", True)))

        call_api(self, "GET", "env", timeout=8, on_done=apply)

    def _relaunch_elevated(self):
        """以管理员身份重启自己。

        为什么做成按钮：普通用户双击启动默认不是管理员，微信发送会被系统挡掉，
        而「右键 → 以管理员身份运行」这一步绝大多数人想不到。
        只发起、不等待：UAC 弹窗由用户点，点完旧窗口自己退出把 8091 让出来。
        """
        from src.elevation import relaunch_elevated
        ok, message = relaunch_elevated()
        self.set_notice(message, ok=ok)
        if ok:
            self.btn_elevate.setEnabled(False)
            QTimer.singleShot(1200, QApplication.quit)

    def _warn_if_not_elevated(self):
        """启动后只提示一次：不是管理员就直说要重启，别等用户发现「能收不能发」。"""
        try:
            from src.elevation import is_elevated
            elevated = is_elevated()
        except Exception:
            return
        if not elevated:
            self.set_notice("当前不是管理员：微信发送可能被系统拒绝；点「环境与自检 → 以管理员身份重启」", ok=False)

    def _open_update(self):
        """打开更新窗口；对话框自己负责异步调用后端，这里只管把窗口弹起来。"""
        try:
            from src.gui.maintenance_dialogs import open_update_dialog
            open_update_dialog(self, api_get, api_post)
        except Exception as error:
            QMessageBox.warning(self, "检查更新", f"打不开更新窗口：{type(error).__name__}")

    def _open_sandbox_provision(self):
        """沙盒启用向导：检测 → 提权启用 → 重启后继续 → 创建隔离通道。"""
        try:
            from src.gui.maintenance_dialogs import SandboxProvisionDialog
            SandboxProvisionDialog(self, api_get, api_post).exec()
        except Exception as error:
            QMessageBox.warning(self, "Windows 沙盒", f"打不开沙盒窗口：{type(error).__name__}")

    def _open_account_guard(self):
        """账号与登录：机器人账号和微信当前登录的账号不一致时，一键改绑。"""
        try:
            from src.gui.maintenance_dialogs import AccountGuardDialog
            AccountGuardDialog(self, api_get, api_post).exec()
        except Exception as error:
            QMessageBox.warning(self, '账号与登录', f'打不开账号窗口：{type(error).__name__}')

    def _run_send_selftest(self):
        """发送自检：真的发一条到「文件传输助手」，验证整条发送链路。

        为什么必须真发：剪贴板、草稿、标题核验、发送键这几步只有真跑一次才知道
        通不通，只读体检永远看不出「能收不能发」。目标写死「文件传输助手」，
        不会发给任何人；标题对不上就不输入、不按发送键。
        """
        answer = QMessageBox.question(
            self, "发送自检",
            "这会用当前微信账号往「文件传输助手」真的发一条消息（不会发给任何人）。\n"
            "发送前会自己打开这个会话；标题对不上就不输入、不按发送键。\n\n继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.btn_selftest.setEnabled(False)
        self.set_notice("正在自检发送链路（会打开「文件传输助手」并真的发一条）…", ok=True)

        def done(data):
            self.btn_selftest.setEnabled(True)
            data = data or {}
            ok = bool(data.get("ok", data.get("success")))
            parts = ["发送自检通过" if ok else "发送自检未通过"]
            if data.get("elapsed") is not None:
                parts.append("用时 %s 秒" % data.get("elapsed"))
            detail = str(data.get("message") or data.get("reason") or "").strip()
            if detail:
                parts.append(detail)
            text = "\n".join(parts)
            self.set_notice("；".join(parts), ok=ok)
            if ok:
                QMessageBox.information(self, "发送自检", text)
            else:
                QMessageBox.warning(self, "发送自检", text)

        call_api(self, "POST", "wechat/selftest-send", data={"confirm": True},
                 timeout=120, on_done=done)

    def _build_selfcheck_panel(self):
        """「环境与自检」区域：三类只读结果放一处，删残留通道单独二次确认。

        构造期只建控件、不发任何请求；所有网络调用都走 call_api，点按钮时不会卡住界面。
        """
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 8, 0, 0)
        box.setSpacing(6)
        title = QLabel("环境与自检（换机器、覆盖升级前后先看这里）")
        title.setStyleSheet(f"color: {ACCENT}; font-size: 12px; font-weight: bold;")
        box.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_compat_advice = QPushButton("兼容性建议")
        self.btn_compat_advice.setProperty("class", "secondary")
        self.btn_compat_advice.setToolTip("读这台机器缺什么、影响什么、怎么修；只读，不改设置、不启动微信")
        self.btn_compat_advice.clicked.connect(self._load_compat_advice)
        row.addWidget(self.btn_compat_advice)
        self.btn_selfcheck = QPushButton("安装自检")
        self.btn_selfcheck.setProperty("class", "secondary")
        self.btn_selfcheck.setToolTip("读数据目录状态、升级自检摘要、残留通道列表；只读")
        self.btn_selfcheck.clicked.connect(self._load_install_guard)
        row.addWidget(self.btn_selfcheck)
        self.btn_elevate = QPushButton("以管理员身份重启")
        self.btn_elevate.setProperty("class", "secondary")
        self.btn_elevate.setToolTip("微信 4.x 的发送需要管理员权限；不是管理员时点这里重启自己")
        self.btn_elevate.clicked.connect(self._relaunch_elevated)
        row.addWidget(self.btn_elevate)
        self.btn_update = QPushButton("检查更新")
        self.btn_update.setProperty("class", "secondary")
        self.btn_update.setToolTip("从配置的更新源检查新版本，下载后校验 SHA256，校验不通过绝不安装")
        self.btn_update.clicked.connect(self._open_update)
        row.addWidget(self.btn_update)
        self.btn_sandbox_provision = QPushButton("Windows 沙盒")
        self.btn_sandbox_provision.setProperty("class", "secondary")
        self.btn_sandbox_provision.setToolTip("检测 Windows 沙盒是否可用；未启用时提权启用，然后创建隔离通道")
        self.btn_sandbox_provision.clicked.connect(self._open_sandbox_provision)
        row.addWidget(self.btn_sandbox_provision)
        self.btn_selftest = QPushButton("发送自检")
        self.btn_selftest.setProperty("class", "secondary")
        self.btn_selftest.setToolTip("真的往「文件传输助手」发一条消息，验证剪贴板/草稿/标题/发送键整条链路；不会发给任何人")
        self.btn_selftest.clicked.connect(self._run_send_selftest)
        row.addWidget(self.btn_selftest)
        self.btn_account_guard = QPushButton('账号与登录')
        self.btn_account_guard.setProperty('class', 'secondary')
        self.btn_account_guard.setToolTip('看机器人账号和微信当前登录的是不是同一个；不一致时能一键改绑')
        self.btn_account_guard.clicked.connect(self._open_account_guard)
        row.addWidget(self.btn_account_guard)
        row.addStretch(1)
        box.addLayout(row)

        view_style = (f"background: {CARD}; border: 1px solid {BORDER}; "
                      f"border-radius: 10px; font-size: 11px;")
        self.compat_view = QTextEdit()
        self.compat_view.setReadOnly(True)
        self.compat_view.setMaximumHeight(190)
        self.compat_view.setVisible(False)
        self.compat_view.setStyleSheet(view_style)
        box.addWidget(self.compat_view)

        self.guard_view = QTextEdit()
        self.guard_view.setReadOnly(True)
        self.guard_view.setMaximumHeight(150)
        self.guard_view.setVisible(False)
        self.guard_view.setStyleSheet(view_style)
        box.addWidget(self.guard_view)

        # 残留通道清理：下拉框只能装「安装自检」读到的未核验通道，编号对不上就不发请求
        pick_row = QHBoxLayout()
        pick_row.setSpacing(8)
        self.channel_pick = QComboBox()
        self.channel_pick.setMinimumWidth(260)
        self.channel_pick.setEnabled(False)
        self.channel_pick.setToolTip("只能清理「安装自检」列出来的未核验通道")
        pick_row.addWidget(self.channel_pick)
        QTimer.singleShot(1500, self._warn_if_not_elevated)
        self.channel_confirm = QLineEdit()
        self.channel_confirm.setPlaceholderText("把所选通道编号完整再填一遍，才会发清理请求")
        self.channel_confirm.setEnabled(False)
        pick_row.addWidget(self.channel_confirm, 1)
        self.btn_channel_remove = QPushButton("清理残留通道")
        self.btn_channel_remove.setProperty("class", "secondary")
        self.btn_channel_remove.setEnabled(False)
        self.btn_channel_remove.setToolTip("只删目录核验不过的那一条；编号对不上什么都不发")
        self.btn_channel_remove.clicked.connect(self._remove_channel)
        pick_row.addWidget(self.btn_channel_remove)
        box.addLayout(pick_row)

        # 回滚到上一版：后端只写脚本，绝不自己执行 —— 控制台没法替换自己正在运行的 exe。
        rollback_title = QLabel("回滚到上一版（只生成脚本，由你关掉控制台后自己双击执行）")
        rollback_title.setStyleSheet(f"color: {ACCENT}; font-size: 12px; font-weight: bold;")
        box.addWidget(rollback_title)
        rollback_row = QHBoxLayout()
        rollback_row.setSpacing(8)
        self.rollback_state_label = QLabel("还没读过上一版备份状态")
        self.rollback_state_label.setWordWrap(True)
        self.rollback_state_label.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        rollback_row.addWidget(self.rollback_state_label, 1)
        self.btn_rollback_state = QPushButton("读取备份状态")
        self.btn_rollback_state.setProperty("class", "secondary")
        self.btn_rollback_state.setToolTip(
            "只读检查安装目录里有没有 WhaleGirlWeChat.prev.exe（覆盖升级时自动备份的那份）")
        self.btn_rollback_state.clicked.connect(self._load_rollback_state)
        rollback_row.addWidget(self.btn_rollback_state)
        self.btn_rollback_script = QPushButton("生成回滚脚本")
        self.btn_rollback_script.setProperty("class", "secondary")
        self.btn_rollback_script.setEnabled(False)
        self.btn_rollback_script.setToolTip("先点「读取备份状态」；没有上一版备份就不能生成")
        self.btn_rollback_script.clicked.connect(self._generate_rollback_script)
        rollback_row.addWidget(self.btn_rollback_script)
        box.addLayout(rollback_row)

        self._unverified_channels = []
        self._rollback_state = {}
        return panel

    def _load_compat_advice(self):
        """读 /api/compat/advice：按 severity 分组显示「影响」与「怎么修」。"""
        self.btn_compat_advice.setEnabled(False)
        self.set_notice("正在读兼容性建议…", ok=True)

        def apply(result):
            self.btn_compat_advice.setEnabled(True)
            items = result.get("items") if isinstance(result, dict) else None
            if not isinstance(items, list):
                reason = result.get("message") if isinstance(result, dict) else None
                message = str(reason or "后台没有返回建议")
                self.compat_view.setPlainText(f"兼容性建议读取失败：{message}")
                self.compat_view.setVisible(True)
                self.set_notice(f"兼容性建议读取失败：{message}", ok=False)
                return
            groups = (("blocking", "必须先解决"),
                      ("degraded", "降级（能跑，但会退化）"),
                      ("info", "说明（不影响使用）"))
            lines = []
            for severity, heading in groups:
                bucket = [item for item in items
                          if isinstance(item, dict) and item.get("severity") == severity]
                if not bucket:
                    continue
                lines.append(f"【{heading}】{len(bucket)} 条")
                for item in bucket:
                    lines.append(f"· {item.get('title')}")
                    detail = str(item.get("detail") or "").strip()
                    fix = str(item.get("fix") or "").strip()
                    if detail:
                        lines.append(f"  影响：{detail}")
                    if fix:
                        lines.append(f"  怎么修：{fix}")
                lines.append("")
            self.compat_view.setPlainText("\n".join(lines).strip() or "没有可显示的建议")
            self.compat_view.setVisible(True)
            summary = str(result.get("summary") or "兼容性建议读取完成")
            self.set_notice(summary, ok=not result.get("blocking_count"))

        call_api(self, "GET", "compat/advice", timeout=20, on_done=apply)

    def _load_install_guard(self):
        """安装自检：数据目录、升级自检摘要、残留通道列表，三个只读接口串起来读一遍。"""
        self.btn_selfcheck.setEnabled(False)
        self.set_notice("正在跑安装自检…", ok=True)
        lines = []

        def finish():
            self.btn_selfcheck.setEnabled(True)
            self.guard_view.setPlainText("\n".join(lines).strip() or "安装自检没有返回内容")
            self.guard_view.setVisible(True)
            self.set_notice("安装自检完成", ok=True)

        def step_channels(channels):
            try:
                lines.append(self._format_channels(channels))
                self._fill_channels(channels)
            finally:
                finish()

        def step_self_check(report):
            try:
                lines.append(self._format_self_check(report))
            finally:
                call_api(self, "GET", "install-guard/channels", timeout=15,
                         on_done=step_channels)

        def step_data_root(report):
            try:
                lines.append(self._format_data_root(report))
            finally:
                call_api(self, "GET", "install-guard/self-check", timeout=15,
                         on_done=step_self_check)

        call_api(self, "GET", "install-guard/data-root", timeout=10, on_done=step_data_root)

    def _format_data_root(self, report):
        if not isinstance(report, dict):
            return "数据目录：后台没有返回结果"
        active = report.get("active") or report.get("primary") or "（未知）"
        writable = report.get("writable")
        atomic = report.get("atomic_replace")
        text = f"数据目录：{active}"
        text += "\n  可写：" + ("是" if writable else ("否" if writable is False else "没测出来"))
        text += "\n  原子替换：" + ("可以" if atomic else
                                   ("被占用，退化为就地重写" if atomic is False else "没测出来"))
        if report.get("fallback_used"):
            text += "\n  说明：主目录不可用，已临时切到兜底目录"
        reason = str(report.get("reason") or "").strip()
        if reason:
            text += f"\n  原因：{reason}"
        return text

    def _format_self_check(self, report):
        if not isinstance(report, dict):
            return "升级自检：后台没有返回结果"
        text = "升级自检：" + str(report.get("message") or "（没有摘要）")
        for problem in (report.get("problems") or []):
            text += f"\n  待修：{problem}"
        for note in (report.get("notes") or []):
            text += f"\n  提示：{note}"
        items = report.get("items")
        if isinstance(items, dict):
            for key in ("config", "history", "plugin_settings", "personas", "isolated_channels"):
                entry = items.get(key)
                if isinstance(entry, dict):
                    text += f"\n  {key}: {entry.get('status')} — {entry.get('message')}"
        return text

    def _format_channels(self, report):
        if not isinstance(report, dict):
            return "隔离通道：后台没有返回结果"
        verified = [item for item in (report.get("verified") or []) if isinstance(item, dict)]
        unverified = [item for item in (report.get("unverified") or []) if isinstance(item, dict)]
        text = f"隔离通道：核验通过 {len(verified)} 个，残留 {len(unverified)} 个"
        for item in unverified:
            text += f"\n  残留：{item.get('channel_id')}（{item.get('reason')}）"
            suggestion = str(item.get("suggestion") or "").strip()
            if suggestion:
                text += f"\n    建议：{suggestion}"
        message = str(report.get("message") or "").strip()
        if message:
            text += f"\n  {message}"
        return text

    def _fill_channels(self, report):
        """把未核验通道填进下拉框；列表为空就禁用清理入口，避免空发 remove。"""
        unverified = []
        if isinstance(report, dict):
            unverified = [item for item in (report.get("unverified") or [])
                          if isinstance(item, dict) and item.get("channel_id")]
        self._unverified_channels = unverified
        self.channel_pick.clear()
        self.channel_confirm.clear()
        for item in unverified:
            ident = str(item.get("channel_id"))
            short = str(item.get("short_id") or ident[:12])
            self.channel_pick.addItem(f"{ident}（{short}）", ident)
        ready = bool(unverified)
        self.channel_pick.setEnabled(ready)
        self.channel_confirm.setEnabled(ready)
        self.btn_channel_remove.setEnabled(ready)
        if not ready:
            self.channel_pick.addItem("没有残留通道（先点「安装自检」）")

    def _remove_channel(self):
        """清理一条残留通道：必须把所选编号原样再填一遍，对不上就一个请求都不发。"""
        ident = str(self.channel_pick.currentData() or "").strip()
        typed = self.channel_confirm.text().strip()
        if not ident:
            self.set_notice("还没有读到残留通道列表，先点「安装自检」", ok=False)
            return
        if typed != ident:
            self.set_notice("二次确认没对上：把所选通道编号完整再填一遍才会发请求，这次什么都没删", ok=False)
            return
        self.btn_channel_remove.setEnabled(False)
        self.set_notice(f"正在清理残留通道 {ident[:12]}…", ok=True)

        def apply(result):
            self.btn_channel_remove.setEnabled(bool(self._unverified_channels))
            ok = bool(result.get("success")) if isinstance(result, dict) else False
            message = str(result.get("message") or "后台没有返回结果") if isinstance(result, dict) \
                else "后台没有返回结果"
            if ok:
                self.channel_confirm.clear()
                self.set_notice(f"清理完成：{message}", ok=True)
                self._load_install_guard()
            else:
                self.set_notice(f"清理失败：{message}", ok=False)

        call_api(self, "POST", "install-guard/channels/remove",
                 data={"channel_id": ident, "confirm": typed}, timeout=20, on_done=apply)

    def _load_rollback_state(self):
        """只读读 /api/install-guard/rollback：有没有上一版备份、多大、什么时候。"""
        self.btn_rollback_state.setEnabled(False)
        self.set_notice("正在读上一版备份状态…", ok=True)

        def apply(result):
            self.btn_rollback_state.setEnabled(True)
            state = result if isinstance(result, dict) else {}
            if not state.get("install_dir"):
                message = str(state.get("message") or "后台没有返回备份状态")
                self._rollback_state = {}
                self.rollback_state_label.setText(f"读取备份状态失败：{message}")
                self.btn_rollback_script.setEnabled(False)
                self.set_notice(f"读取备份状态失败：{message}", ok=False)
                return
            self._rollback_state = state
            available = bool(state.get("available"))
            if available:
                text = (f"上一版备份：{state.get('prev_path')}"
                        f"（{state.get('prev_size')} 字节，{state.get('prev_mtime') or '时间未知'}）")
            else:
                text = "没有可回滚的上一版备份"
            note = str(state.get("note") or "").strip()
            if note:
                text += f"\n{note}"
            self.rollback_state_label.setText(text)
            self.btn_rollback_script.setEnabled(available)
            self.btn_rollback_script.setToolTip(
                "生成回滚脚本（会先弹一次确认）" if available
                else "没有 WhaleGirlWeChat.prev.exe：先做过一次覆盖升级才有可回滚的版本")
            self.set_notice("上一版备份：" + ("有，可以生成回滚脚本" if available else "没有"),
                            ok=available)

        call_api(self, "GET", "install-guard/rollback", timeout=10, on_done=apply)

    def _generate_rollback_script(self):
        """生成回滚脚本：先弹确认框，确认后才发请求；后端只写 .cmd，绝不执行。"""
        if not isinstance(self._rollback_state, dict):
            self._rollback_state = {}
        if not self._rollback_state.get("available"):
            self.set_notice("还没有读到可回滚的上一版备份，先点「读取备份状态」", ok=False)
            return
        confirmed = QMessageBox.question(
            self, "确认生成回滚脚本",
            "即将在安装目录里生成 rollback-prev.cmd。\n\n"
            "脚本不会自动运行：需要你先停止微信发送、关掉控制台窗口，再自己双击它。\n"
            "脚本会结束 WhaleGirlWeChat.exe 进程，把 WhaleGirlWeChat.prev.exe 复制成\n"
            "WhaleGirlWeChat.exe，然后重新启动上一版。\n\n"
            "确定要生成这个脚本吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirmed != QMessageBox.StandardButton.Yes:
            self.set_notice("已取消：没有生成回滚脚本", ok=True)
            return
        self.btn_rollback_script.setEnabled(False)
        self.set_notice("正在生成回滚脚本…", ok=True)

        def apply(result):
            data = result if isinstance(result, dict) else {}
            ok = bool(data.get("success"))
            message = str(data.get("message") or "后台没有返回结果")
            self.btn_rollback_script.setEnabled(
                bool(isinstance(self._rollback_state, dict) and self._rollback_state.get("available")))
            if ok:
                self.set_notice(f"{message}（执行前先停止微信发送；脚本要关掉控制台才复制得动 exe）",
                                ok=True)
            else:
                self.set_notice(f"生成回滚脚本失败：{message}", ok=False)

        call_api(self, "POST", "install-guard/rollback-script",
                 data={"confirm": ROLLBACK_CONFIRM}, timeout=20, on_done=apply)

    def _poll_log_errors(self):
        """把 backend.log 里新出现的 ERROR/CRITICAL 堆栈追加进运行记录。

        后端是独立进程，界面只能从日志文件里增量读：记住已读偏移，第二次只拿新增部分。
        """
        try:
            blocks, self._log_offset = read_new_errors(ROOT / "logs" / "backend.log", self._log_offset, include_info=True)
        except Exception:
            return  # 日志读不到只是"暂时没有新错误"，绝不能影响界面
        if not blocks:
            return
        for block in blocks:
            identity = getattr(block, 'record_id', None)
            if identity in self._cleared_error_ids:
                continue
            backend_index = next((i for i, old in enumerate(self._backend_records)
                                  if identity is not None and getattr(old, 'record_id', None) == identity), None)
            if backend_index is None:
                self._backend_records.append(block)
            else:
                self._backend_records[backend_index] = block
            if getattr(block, 'level', 'ERROR') not in ('WARNING', 'ERROR', 'CRITICAL'):
                continue
            index = next((i for i, old in enumerate(self._error_blocks)
                          if identity is not None and getattr(old, 'record_id', None) == identity), None)
            if index is None:
                self._error_blocks.append(block)
            else:
                self._error_blocks[index] = block
        self._backend_records = self._backend_records[-500:]
        overflow = len(self._error_blocks) - self.ERROR_BLOCK_LIMIT
        if overflow > 0:
            del self._error_blocks[:overflow]
            self._error_shown = max(0, self._error_shown - overflow)
        self._render_error_blocks()

    def _render_error_blocks(self, reset: bool = False):
        """保留旧调用入口；记录统一渲染，分次写入的堆栈原位更新。"""
        self._error_shown = len(self._error_blocks)
        self._render_logs()

    def _remember_log_events(self, events):
        for event in events:
            stamp, text = event.get('time', 0), str(event.get('text', ''))
            key = (stamp, text)
            if key in self._event_seen:
                continue
            self._event_seen.add(key)
            self._event_seen_order.append(key)
            self._runtime_events.append((stamp, text))
        self._runtime_events = self._runtime_events[-500:]
        while len(self._event_seen_order) > 2000:
            self._event_seen.discard(self._event_seen_order.pop(0))
        self._render_logs()

    def _clear_log_display(self):
        self._cleared_error_ids.update(getattr(block, 'record_id', None) for block in self._error_blocks)
        self._cleared_error_ids.update(getattr(block, 'record_id', None) for block in self._backend_records)
        if len(self._cleared_error_ids) > 2000:
            self._cleared_error_ids = {getattr(block, 'record_id', None) for block in self._backend_records + self._error_blocks}
        self._runtime_events.clear()
        self._backend_records.clear()
        self._error_blocks.clear()
        self._error_shown = 0
        self._render_logs(force=True)
        self.copy_hint.setText('已清空显示，磁盘日志保留')

    def _render_logs(self, force=False):
        if self._log_rendering:
            return
        query = self.log_search.text().strip().casefold()
        level = self.log_level.currentData()
        records = [('INFO', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp)) + ' · 记录\n' + text)
                   for stamp, text in self._runtime_events]
        today = _datetime.now().date()
        records += [(getattr(block, 'level', 'ERROR'),
                     _file_log_caption(block, today) + str(block))
                    for block in self._error_blocks]
        backend = self.log_source.currentData() == 'backend'
        if backend:
            records = [(getattr(block, 'level', 'INFO'), str(block)) for block in self._backend_records]
        shown = [text for severity, text in records
                 if (not level or severity == level or (level == 'ERROR' and severity == 'CRITICAL'))
                 and (not query or query in text.casefold())]
        self.log_summary.setText(f'显示 {len(shown)} / {len(records)} 条 · '
                                 + ('最近 500 条后台日志（含发送阶段）' if backend else '最近 500 条运行记录、20 段文件警告/错误')
                                 + (' · 已暂停滚动' if self.log_pause.isChecked() else ''))
        self.log_summary.setToolTip('历史仅按文件日志日期标注，不表示问题已解决；原文与读取顺序保留。')
        text = '\n\n'.join(shown) or ('没有符合条件的记录' if records else '暂无记录')
        if self.evt_view.toPlainText() == text:
            return
        cursor = self.evt_view.textCursor()
        # 后台刷新不得打断复制；选区取消后 selectionChanged 会补上已收集的新记录。
        if cursor.hasSelection() and not force:
            return
        self._log_rendering = True
        try:
            bar = self.evt_view.verticalScrollBar()
            scroll = bar.value()
            follow = not self.log_pause.isChecked() and (self._log_follow_tail or bar.maximum() - scroll <= 3)
            self._log_follow_tail = follow
            position = cursor.position()
            self.evt_view.setPlainText(text)
            if follow:
                bar.setValue(bar.maximum())
            else:
                cursor = self.evt_view.textCursor()
                cursor.setPosition(min(position, self.evt_view.document().characterCount() - 1))
                self.evt_view.setTextCursor(cursor)
                bar.setValue(min(scroll, bar.maximum()))
        finally:
            self._log_rendering = False

    def showEvent(self, event):
        super().showEvent(event)
        if self._log_follow_tail:
            QTimer.singleShot(0, self._follow_log_tail)

    def _log_scroll_changed(self, value):
        if self._log_rendering:
            return
        maximum = self.evt_view.verticalScrollBar().maximum()
        # 布局刚展开时，最大值先变、当前位置仍是 0；不能当成用户上滚。
        if self._log_follow_tail and self._log_follow_range != maximum:
            return
        self._log_follow_tail = maximum - value <= 3
        self._log_follow_range = maximum

    def _log_range_changed(self, _minimum, maximum):
        if self._log_follow_tail:
            QTimer.singleShot(0, self._follow_log_tail)
        else:
            self._log_follow_range = maximum

    def _log_user_scroll(self, value):
        # 用户拖动/翻页即使发生在首次布局定时器之前，也优先于自动滚尾。
        self._log_follow_range = self.evt_view.verticalScrollBar().maximum()
        self._log_follow_tail = self._log_follow_range - value <= 3

    def _follow_log_tail(self):
        if not self._log_follow_tail or not self.evt_view.isVisible():
            return
        bar = self.evt_view.verticalScrollBar()
        if self.log_pause.isChecked() or self.evt_view.textCursor().hasSelection():
            self._log_follow_tail = False
        else:
            bar.setValue(bar.maximum())
        self._log_follow_range = bar.maximum()

    def _send_mock_message(self):
        """模拟模式：把输入框内容作为一条假入站消息推给后端，触发完整回复链路。"""
        text = self.mock_input.text().strip()
        if not text:
            return
        payload = {
            "text": text, "chat": "mock_room", "sender": "测试用户",
            "kind": "group", "at": self.mock_at.isChecked(),
        }

        def apply(result):
            if result.get("success"):
                self.mock_input.clear()
                self.set_notice(result.get("message") or "已发送模拟消息", ok=True)
                self._refresh()
            else:
                self.set_notice(result.get("message") or "发送失败", ok=False)

        self.set_notice("正在投递模拟消息…")
        call_api(self, "POST", "mock/recv", payload, timeout=30, on_done=apply)

    def refresh(self, data: dict):
        running = data.get("running", False)
        # "正在连接/正在重连"必须显式说出来：微信连接要定位并校准窗口，通常 5~20 秒，
        # 以前这段时间界面是死的，用户只会以为程序卡死了。
        if data.get("connecting") or data.get("reconnecting"):
            label = "正在连接微信…" if data.get("connecting") else "正在重新连接微信…"
            self.card_running.update(label, detail="要定位/校准微信窗口，通常 5~20 秒；界面不会卡住",
                                     ok=None)
        else:
            pipeline = data.get("pipeline") or {}
            stages = {"idle": "等待消息", "waiting_desktop": "等待解锁 Windows / 恢复远程桌面", "waiting_wechat_input": "等待微信输入控件 · 可点测试微信复查", "prepare": "合并消息 / 插件准备", "waiting_ai": "等待模型空闲",
                      "model": "模型生成回复", "post_reply": "整理回复", "sending": "发送回复"}
            stage = str(pipeline.get("stage") or "idle")
            elapsed = max(0, time.time() - float(pipeline.get("since") or time.time()))
            detail = f"{stages.get(stage, stage)} · 待处理 {data.get('pending_current', data.get('pending', 0))} 条"
            if data.get('pending_other_accounts'):
                detail += f" · 其他账号 {data['pending_other_accounts']} 条未处理"
            if stage != "idle":
                detail += f" · 已用 {elapsed:.0f} 秒"
            self.card_running.update("运行中" if running else "已停止", detail=detail if running else "", ok=running)

        # 微信接入：后端 status() 的 connection{} 就是全部可读字段
        conn = data.get("connection") or {}
        receive_stopped = (running and conn.get('backend') in ('sandbox', 'wechatauto')
                           and conn.get('listening') is not True)
        if receive_stopped and not data.get('connecting') and not data.get('reconnecting'):
            receive_reason = str(conn.get('error') or '收件循环未运行，等待重新连接')
            self.card_running.update('服务运行 · 接收已停止', detail=receive_reason, ok=False)
        online = bool(conn.get("online"))
        if online:
            detail = (f"{conn.get('backend', '')} · {conn.get('account', '')} · "
                      f"收 {conn.get('received', 0)} / 发 {conn.get('sent', 0)}")
            failed = int(conn.get("send_failed") or 0)
            if failed:
                detail += f" · 发失败 {failed}"
        else:
            detail = str(conn.get("error") or "")
            if not detail:
                # 没有报错信息时至少告诉用户后端在看哪个数据库目录，方便自查
                detail = f"数据库：{conn.get('db_dir') or '自动探测'}"
        # 只在"真的连着微信、又不是模拟模式"时提示权限：模拟模式不需要管理员，
        # 没连上时提示权限只会让人以为这就是失败原因。
        if online and conn.get("backend") != "mock" and not conn.get("admin"):
            detail += "｜无管理员权限"
        degraded = str(conn.get("degraded") or "").strip()
        if degraded:
            detail += f"｜{degraded[:60]}"
        waiting_desktop = str((data.get('pipeline') or {}).get('stage')) == 'waiting_desktop'
        connection_label = '读消息在线 · 等待解锁' if online and waiting_desktop else ('已连接' if online else '未连接')
        environment = conn.get('send_environment') or {}
        if online and conn.get('backend') == 'sandbox':
            connection_label = ('隔离后台 · 当前会话就绪' if environment.get('ready') is True
                                else '隔离读库可用 · 待检查发送')
        if online and conn.get('backend') == 'hook':
            connection_label = ('协议后台 · 目标已核验' if environment.get('ready') is True
                                else '协议后台 · 发送未就绪')
        if online and conn.get('client_login_state') == 'unknown' and conn.get('backend') == 'wechatauto':
            connection_label = '读消息在线 · 发送就绪' if environment.get('ready') is True else '读库可用 · 待检查发送'
            if waiting_desktop:
                connection_label = '读消息在线 · 等待解锁'
        if online and not waiting_desktop and environment.get('ready') is False:
            connection_label = '读消息在线 · 发送未就绪'
            # 带上机器可读的原因码：报告/截图里一眼能看出是"最小化"还是"没开放输入控件"，
            # 不用再从长句子里猜（交接指南第 5 节要求明确原因码）。
            code = str(conn.get('send_code') or '').strip()
            if code:
                connection_label += f'（{code}）'
            detail += '｜' + str(environment.get('reason') or '请点击测试微信检查')
        if receive_stopped:
            detail += '｜消息接收已停止：' + str(conn.get('error') or '等待重新连接')
        self.card_conn.update(connection_label, detail=detail,
                              ok=False if receive_stopped else None if online and environment.get('ready') is False else online)
        mismatch = data.get('account_mismatch') or {}
        if mismatch and running:
            # 微信换号后发送通道会在核验阶段被拦：能收不能发，必须直接说清楚怎么改。
            self.card_conn.update('账号不一致 · 发不出去',
                                  detail=(f"微信登录的是 {mismatch.get('active') or '未知'}，"
                                          f"机器人账号是 {mismatch.get('current') or '未知'}；"
                                          '点「账号与登录」改绑，或在微信里切回机器人账号'),
                                  ok=False)
        source = getattr(self, '_notice_source', '')
        if source in ('test-wechat', 'wechat-status'):
            # 微信体检只代表当时的只读结果；当前失败不能继续挂着旧绿色结论。
            # 导出、复制等其他操作通知不属于这一路，不会被轮询覆盖。
            if not online or environment.get('ready') is False or source == 'wechat-status':
                code = str(conn.get('send_code') or environment.get('waiting_code') or '').strip()
                reason = str(environment.get('reason') or conn.get('error') or '等待重新检查')
                title = '当前发送环境：' if online else '当前微信未连接：'
                self.set_notice(title + (f'[{code}] ' if code else '') + reason,
                                ok=online and environment.get('ready') is True, source='wechat-status')
        safety = data.get('safety') or {}
        self.btn_start.setEnabled(not safety.get('active'))
        self.btn_test_wechat.setEnabled(not safety.get('active'))
        if safety.get('active'):
            reason = str(safety.get('reason') or '账号安全暂停，请先在微信完成官方安全验证')
            self.card_running.update('账号安全暂停', detail='收发与自动重连已暂停，重启不会自动恢复', ok=False)
            self.card_conn.update('安全暂停 · 不连接微信', detail=reason, ok=False)

        # 模拟对话面板：只有"模拟模式"后端可用，其它后端隐藏，避免误导
        is_mock = str(conn.get("backend") or "") == "mock"
        if is_mock:
            self.mock_panel.show()
        elif self.mock_panel.isVisible():
            self.mock_panel.hide()

        self.card_model.update(data.get("model", "未配置"), model_subtitle(data))
        self.card_memory.update(*memory_card(data))

        # state 是宿主内控状态，含"连发合并"这类中间态，都要有中文说法
        state_map = {"pending": "等待", "processing": "正在处理", "sending": "发送中", "sent": "已回复", "failed": "失败",
                     "uncertain": "待核实", "expired": "过期", "merged": "已合并", "skipped": "已跳过", "cancelled": "已取消"}
        msgs = data.get("messages", [])[:15]
        html = ""
        for m in msgs:
            ts = time.strftime("%H:%M:%S", time.localtime(m.get("time", 0)))
            state = state_map.get(m.get("state"), m.get("state"))
            html += f'<p style="color:{MUTED};font-size:11px;margin:2px 0">{ts} · {_html.escape(str(m.get("room", "")))} · {_html.escape(str(state or ""))}</p>'
            html += f'<p style="margin:2px 0 6px">{_html.escape(str(m.get("sender", "")))}：{_html.escape(str(m.get("text", ""))[:120])}</p>'
            if m.get("reply"):
                html += f'<p style="color:{SUCCESS};margin:2px 0 6px">{_html.escape(str(m["reply"])[:120])}</p>'
            if m.get("error"):
                html += f'<p style="color:{ERROR};margin:2px 0 6px">{_html.escape(str(m["error"])[:120])}</p>'
        self.msg_view.setHtml(html or f'<p style="color:{MUTED}">暂无消息</p>')

        self._remember_log_events(data.get('events', []))

    def set_notice(self, text: str, ok: bool = True, *, source: str = ''):
        self._notice_source = source
        self.notice.setStyleSheet(f"color: {SUCCESS if ok else ERROR}; font-size: 12px;")
        self.notice.setText(text)
        self.notice.setToolTip(text)


# ── 设置标签页 ────────────────────────────────────────────────────────────────
class SettingsTab(QWidget):
    def __init__(self):
        super().__init__()
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: 0; }")
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        def section(title):
            frame = QFrame()
            frame.setProperty("panel", True)
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(18, 14, 18, 18)
            fl.setSpacing(8)
            lbl = QLabel(title)
            lbl.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-weight: bold;")
            fl.addWidget(lbl)
            return frame, fl

        def field(parent_layout, label_text, widget):
            lbl = QLabel(label_text)
            lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px; margin-top: 6px;")
            parent_layout.addWidget(lbl)
            parent_layout.addWidget(widget)

        # 微信接入
        sec1, fl1 = section("01 · 微信接入")
        self.backend = QComboBox()
        for value, text in [
            ("auto", "自动选择（推荐）"),
            ("wechatauto", "wechatauto · 微信 4.1.12+（推荐）"),
            ("wxauto4", "wxauto4 · 微信 4.0.x"),
            ("wxauto", "wxauto · 微信 3.9.x"),
            ("mock", "模拟模式（不连微信）"),
        ]:
            self.backend.addItem(text, value)
        field(fl1, "微信后端", self.backend)
        backend_hint = QLabel(
            "wechatauto 需要微信 4.1.12+ 且建议以管理员身份运行控制台；模拟模式不发送任何消息。"
        )
        backend_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        backend_hint.setWordWrap(True)
        fl1.addWidget(backend_hint)

        self.db_dir = QLineEdit()
        self.db_dir.setPlaceholderText(r"留空=自动探测；支持 D/E 盘、账号目录或 db_storage")
        database_row = QWidget()
        database_layout = QHBoxLayout(database_row)
        database_layout.setContentsMargins(0, 0, 0, 0)
        database_layout.addWidget(self.db_dir, 1)
        self.btn_choose_db_dir = QPushButton('选择数据目录')
        self.btn_choose_db_dir.setProperty('class', 'secondary')
        def choose_database_directory():
            selected = QFileDialog.getExistingDirectory(self, '选择微信数据目录', self.db_dir.text())
            if selected:
                self.db_dir.setText(selected)
        self.btn_choose_db_dir.clicked.connect(choose_database_directory)
        database_layout.addWidget(self.btn_choose_db_dir)
        field(fl1, "微信数据库目录", database_row)

        # 账号选择：一台机器同时登录两个微信号时，"哪个账号在登录"没有权威信号，
        # 自动判断只能猜；猜错的表现是"连上了却收不到消息、发送也失败"。
        self.account = QComboBox()
        self.account.setEditable(False)
        self.account.addItem("自动判断（推荐先试这个）", "")
        field(fl1, "机器人使用哪个微信号", self.account)
        account_row = QHBoxLayout()
        self.btn_load_accounts = decorate_button(QPushButton("读取本机微信账号"), "btn_reload")
        self.btn_load_accounts.setProperty("class", "secondary")
        self.btn_load_accounts.setToolTip("列出数据目录内的账号；进程打开数据库仅作为线索，不代表账号在线")
        self.btn_load_accounts.clicked.connect(self._load_accounts)
        self.account_hint = QLabel("")
        self.account_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.account_hint.setWordWrap(True)
        account_row.addWidget(self.btn_load_accounts)
        account_row.addWidget(self.account_hint, 1)
        fl1.addLayout(account_row)
        self.btn_weflow = QPushButton('WeFlow 只读检查 / 会话预览')
        self.btn_weflow.setProperty('class', 'secondary')
        self.btn_weflow.setToolTip('可选：读取本机 WeFlow API，不改变当前微信收发后端')
        def show_weflow_tools():
            from src.gui.weflow_dialog import WeFlowDialog
            WeFlowDialog(self, call_api).exec()
        self.btn_weflow.clicked.connect(show_weflow_tools)
        fl1.addWidget(self.btn_weflow)

        self.groups = QTextEdit()
        self.groups.setPlaceholderText("每行一个群名（与微信里显示的一致，也可填 xxx@chatroom）；留空=所有群")
        self.groups.setFixedHeight(90)
        field(fl1, "回复群列表", self.groups)

        groups_row = QHBoxLayout()
        self.btn_load_groups = decorate_button(QPushButton("读取微信群列表"), "btn_reload")
        self.btn_load_groups.setProperty("class", "secondary")
        self.btn_load_groups.setToolTip("从当前登录的微信读取群列表，追加到上面的输入框")
        self.btn_load_groups.clicked.connect(self._load_groups)
        groups_row.addWidget(self.btn_load_groups)
        groups_row.addStretch()
        fl1.addLayout(groups_row)

        self.listen_private = QCheckBox("回复私聊消息")
        self.listen_private.setChecked(True)
        fl1.addWidget(self.listen_private)

        # 发送兼容模式：微信 4.x 主界面自绘，默认不生成 UIA 控件树，
        # 发送子进程找不到输入框就会把每条回复标成"发送未就绪"并取消 —— 表现就是
        # "机器人在回，微信里什么也没有"。现只使用原生暴露的控件，不再修改进程唤醒。
        self.send_compat = QCheckBox("前台控件兼容（不修改微信进程，可能占用焦点）")
        self.send_compat.setToolTip(
            "已移除旧版修改微信进程无障碍开关的入口。\n"
            "现在只能使用微信原本暴露的控件；缺少控件就等待，不扫描或写入无障碍字节。\n"
            "账号安全暂停时不会连接或发送；前台模式仍可能占用焦点。")
        fl1.addWidget(self.send_compat)
        self.send_compat_hint = QLabel("先选择发送方式，再测试客户端控件；DSH模型连接与微信发送分别检查。")
        self.send_compat_hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self.send_compat_hint.setWordWrap(True)
        fl1.addWidget(self.send_compat_hint)
        self.background_send = QCheckBox('严格后台发送（不抢鼠标、不弹出微信）')
        self.background_send.setToolTip('仅向已打开聊天的可用后台控件发送文字；缺少控件时等待。'
                                       '前台兼容可能占用输入焦点，两种发送方式互斥；兼容开关不能生成控件。')
        fl1.addWidget(self.background_send)
        self.background_submit = QComboBox()
        self.background_submit.addItem('后台按钮调用（默认）', 'invoke')
        self.background_submit.addItem('窗口回车投递（实验，尚未实发验证）', 'posted_enter')
        self.background_submit.setToolTip('实验通道只向已核对账号和当前聊天的窗口投递回车，'
            '不移动真实鼠标、不抢前台、不切会话；缺少控件或账号匹配失败时仍等待。失败不换另一方式补发。')
        fl1.addWidget(self.background_submit)
        background_hint = QLabel('不打扰使用电脑：勾选严格后台，手动打开目标聊天，微信可以被其他窗口遮挡。'
                                 '最小化、草稿占用、会话不符或客户端未开放控件时会等待，不会自动恢复或切聊天。'
                                 '关闭兼容模式不会让微信自动开放后台控件；需要全天多会话回复时，建议在独立电脑或虚拟机运行。'
                                 '本模式跳过附加表情图片。')
        background_hint.setWordWrap(True)
        background_hint.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
        fl1.addWidget(background_hint)
        # 保留旧控件属性供旧界面调用，但移除写进程入口及误导提示。
        self.hot_activate_uia = QCheckBox(self);self.hot_activate_uia.hide();self.hot_activate_uia.setEnabled(False)
        self.hot_activate_hint = QLabel(self);self.hot_activate_hint.hide()

        self.auto_open_chat = QCheckBox("自动打开目标会话（按名字搜索并打开后再发送）")
        self.auto_open_chat.setChecked(True)
        self.auto_open_chat.setToolTip(
            "勾选后：如果微信当前停的不是要回复的会话，程序会用左上角搜索框搜索该群/联系人，"
            "打开它并**校验会话名**，确认无误后再输入正文。\n"
            "取消勾选：只发给当前已打开的会话，窗口对不上就放弃本次发送（更保守）。")
        fl1.addWidget(self.auto_open_chat)

        # 主动发消息总闸：默认关。群聊主动发言插件需要它 + 插件启用 + 群白名单三者同时成立。
        self.proactive_enabled = QCheckBox("允许主动发消息（高风险：可能被判定为营销/骚扰，默认关闭）")
        self.proactive_enabled.setToolTip(
            "打开后，「群聊主动发言」等插件才有权主动发消息（仍需插件自身启用，且只发给回复群列表内的群）。\n"
            "封号风险由使用者承担；不确定就别开。")
        fl1.addWidget(self.proactive_enabled)
        self.send_compat.stateChanged.connect(self._update_send_compat_hint)
        self.background_send.stateChanged.connect(self._update_send_compat_hint)
        self.hot_activate_uia.stateChanged.connect(self._update_send_compat_hint)
        layout.addWidget(sec1)

        # 模型 API
        sec2, fl2 = section("02 · 模型与 API")
        self.api_url = QLineEdit()
        self.api_url.setPlaceholderText("https://你的服务地址/v1")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("API Key（留空保留已保存值）")
        self.model_name = QLineEdit()
        self.model_name.setPlaceholderText("模型 id，例如 deepseek-chat")
        # 「读取可用模型」：模型名必须填服务商的 model id，而界面上看到的往往是显示名
        # （例如 "Kimi K2.7 Code"），填错的表现就是"机器人完全不回复"，只留一句 404。
        # 直接去服务商拉一份真能用的清单，用户点一下就行。
        self.btn_models = decorate_button(QPushButton("读取可用模型"), "btn_reload")
        self.btn_models.setProperty("class", "secondary")
        self.btn_models.setToolTip("按上面的 API 地址和 Key 向服务商查询可用的模型 id")
        self.btn_models.clicked.connect(self._load_models)
        self.model_pick = QComboBox()
        self.model_pick.setToolTip("选一个即可填进「模型名称」")
        self.model_pick.hide()
        self.model_pick.currentIndexChanged.connect(self._pick_model)
        self.api_style = QComboBox()
        for v, t in [("auto", "自动识别"), ("openai", "OpenAI 兼容"), ("anthropic", "Anthropic Messages")]:
            self.api_style.addItem(t, v)
        self.secrets_lbl = QLabel("")
        self.secrets_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        field(fl2, "API 地址", self.api_url)
        field(fl2, "API Key", self.api_key)
        field(fl2, "模型名称", self.model_name)
        models_row = QHBoxLayout()
        models_row.addWidget(self.btn_models)
        models_row.addWidget(self.model_pick, 1)
        models_row.addStretch()
        fl2.addLayout(models_row)
        field(fl2, "接口格式", self.api_style)

        # QQ 版「模型与 API」里那几个常用项，微信版原来只能改配置文件：
        # 回复超时 + 是否自动重试（识图/回复被拖"过期"就是重试把时间吃掉了）、
        # 计费方式 + 单价（把 token 用量折成金额，才知道贵不贵）。
        self.request_timeout = QSpinBox()
        self.request_timeout.setRange(3, 300)
        self.request_timeout.setValue(20)
        self.request_timeout.setSuffix(" 秒")
        self.request_timeout.setToolTip("单次模型请求最多等多久；超时按下面的开关决定是否重试一次")
        self.auto_retry = QCheckBox("超时后自动重试一次")
        self.auto_retry.setChecked(True)
        self.auto_retry.setToolTip("关掉后每次只试一次，回复更干脆（中转站慢时不会再拖两条超时）")
        timeout_row = QHBoxLayout()
        timeout_row.addWidget(self.request_timeout)
        timeout_row.addWidget(self.auto_retry)
        timeout_row.addStretch()
        timeout_box = QWidget()
        timeout_box.setLayout(timeout_row)
        timeout_row.setContentsMargins(0, 0, 0, 0)
        field(fl2, "回复超时", timeout_box)

        self.billing_mode = QComboBox()
        self.billing_mode.addItem("按 token 计费（官方 API / 多数云服务）", "token")
        self.billing_mode.addItem("按请求次数计费（部分中转站）", "request")
        self.unit_price = QDoubleSpinBox()
        self.unit_price.setDecimals(3)
        self.unit_price.setRange(0, 100000)
        self.unit_price.setValue(0)
        self.unit_price.setToolTip("按 token 计费填「元 / 百万 token」；按请求计费填「元 / 次」。留 0 则不折算金额")
        billing_row = QHBoxLayout()
        billing_row.addWidget(self.billing_mode, 2)
        billing_row.addWidget(self.unit_price, 1)
        billing_box = QWidget()
        billing_box.setLayout(billing_row)
        billing_row.setContentsMargins(0, 0, 0, 0)
        field(fl2, "计费方式", billing_box)
        self.reply_speed = QComboBox()
        self.reply_speed.addItem("极速（历史 2 轮 / 输出约 140 token / 识图 6 秒 / 不重试）", "turbo")
        self.reply_speed.addItem("快速（历史 4 轮 / 输出约 260 token / 识图 10 秒 / 不重试）", "fast")
        self.reply_speed.addItem("正常（历史 6 轮 / 不封顶 / 识图 15 秒 / 超时重试一次）", "normal")
        self.reply_speed.setToolTip("实测这台中转约 1.4 秒固定开销 + 每字 0.12 秒；\n"
                                   "所以想更快就选极速：少带历史、少写字、识图不多等。")
        field(fl2, "回复速度", self.reply_speed)

        self.billing_hint = QLabel("单价留 0 只统计用量；填了才会在「模型 / API」卡片上折算成金额。")
        self.billing_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.billing_hint.setWordWrap(True)
        fl2.addWidget(self.billing_hint)
        fl2.addWidget(self.secrets_lbl)
        layout.addWidget(sec2)

        # 按钮
        btn_row = QHBoxLayout()
        self.btn_save = decorate_button(QPushButton("保存全部配置"), "btn_save")
        self.btn_diagnose = decorate_button(QPushButton("测试连接"), "btn_save")
        self.btn_diagnose.setToolTip("使用当前填写的地址、模型和接口格式；Key 留空沿用已保存值。只测试模型 API，不发送微信消息。")
        self.btn_diagnose.clicked.connect(self._test_connection)
        self.save_lbl = QLabel("")
        self.save_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_diagnose)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        self.probe_lbl = QLabel('')
        self.probe_lbl.setWordWrap(True)
        self.probe_lbl.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.probe_lbl)
        self.save_lbl.setWordWrap(True)
        layout.addWidget(self.save_lbl)
        layout.addStretch()

        scroll.setWidget(inner)
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.addWidget(scroll)

    def _load_accounts(self):
        """列出本机微信账号，让用户明确指定机器人用哪个号。"""
        self.btn_load_accounts.setEnabled(False)
        self.account_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.account_hint.setText("正在读取本机微信账号（要开库拿昵称，稍慢）…")
        requested_dir = self.db_dir.text().strip()

        def apply(data):
            self.btn_load_accounts.setEnabled(True)
            if self.db_dir.text().strip() != requested_dir:
                self.account_hint.setText('目录已改变，请重新读取账号；没有应用旧目录的结果。')
                return
            rows = data.get("accounts") or []
            if not data.get("success") or not rows:
                self.account_hint.setStyleSheet(f"color: {ERROR}; font-size: 11px;")
                self.account_hint.setText(str(data.get("message")
                                              or "没找到微信账号目录，确认微信已登录且数据库目录正确"))
                return
            current = (str(data.get('path_account') or '') or self.account.currentData()
                       or str(data.get("current") or ""))
            self.account.blockSignals(True)
            self.account.clear()
            self.account.addItem("自动判断（推荐先试这个）", "")
            for row in rows:
                label = f"{row.get('name') or '（无昵称）'} · {row.get('account')}"
                if row.get("opened"):
                    label += " · 微信进程正在使用此库"
                self.account.addItem(label, str(row.get("account") or ""))
            index = self.account.findData(current)
            self.account.setCurrentIndex(index if index >= 0 else 0)
            self.account.blockSignals(False)
            active = str(data.get("active") or "")
            self.account_hint.setStyleSheet(f"color: {SUCCESS}; font-size: 11px;")
            self.account_hint.setText(
                f"共 {len(rows)} 个账号；当前连接用的是 {active or '（未连接）'}。"
                + (f" 数据根目录：{data['normalized_dir']}。" if data.get('normalized_dir') else '') +
                "选好后点「保存全部配置」才会生效。")

        call_api(self, "GET", "wechat/accounts?db_dir=" + quote(requested_dir, safe=''),
                 timeout=60, on_done=apply)

    def _load_groups(self):
        """把微信里读到的群名追加进「回复群列表」：追加不覆盖，已存在的跳过。

        微信里群显示名和 username 都可能变，手打容易错，所以给一键读取。
        读群列表要查微信本地库（可能几秒），所以走后台线程，不冻界面。
        """
        self.save_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self.save_lbl.setText("正在读取微信群列表…")

        def apply(data):
            if not data.get("success"):
                self.save_lbl.setStyleSheet(f"color: {ERROR}; font-size: 12px;")
                self.save_lbl.setText(str(data.get("message") or "读取群列表失败"))
                return
            existing = [g.strip() for g in self.groups.toPlainText().splitlines() if g.strip()]
            added = 0
            for row in data.get("groups") or []:
                if isinstance(row, dict):
                    name = str(row.get("name") or row.get("username") or "").strip()
                else:
                    name = str(row).strip()
                if name and name not in existing:
                    existing.append(name)
                    added += 1
            self.groups.setPlainText("\n".join(existing))
            self.save_lbl.setStyleSheet(f"color: {SUCCESS}; font-size: 12px;")
            self.save_lbl.setText(f"已读取 {added} 个群，记得点「保存全部配置」")

        call_api(self, "GET", "wechat/groups", timeout=30, on_done=apply)

    def _load_models(self):
        """向服务商要一份可用模型 id 清单（见字段处的说明）。"""
        self.btn_models.setEnabled(False)
        self.save_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self.save_lbl.setText("正在向服务商查询可用模型…")

        def apply(data):
            self.btn_models.setEnabled(True)
            models = [str(m) for m in (data.get("models") or []) if str(m).strip()]
            if not data.get("success") or not models:
                self.save_lbl.setStyleSheet(f"color: {ERROR}; font-size: 12px;")
                self.save_lbl.setText(str(data.get("message")
                                          or "没有取到模型列表，请按服务商文档手填 model id"))
                return
            current = self.model_name.text().strip()
            self.model_pick.blockSignals(True)
            self.model_pick.clear()
            self.model_pick.addItems(models)
            self.model_pick.setCurrentIndex(0 if current not in models
                                            else models.index(current))
            self.model_pick.blockSignals(False)
            self.model_pick.show()
            self.save_lbl.setStyleSheet(f"color: {SUCCESS}; font-size: 12px;")
            self.save_lbl.setText(f"取到 {len(models)} 个模型，请在右边选一个（会填进「模型名称」）")

        call_api(self, "POST", "ai/models", self._probe_settings(), timeout=self.request_timeout.value()+5, on_done=apply)

    def _probe_settings(self):
        data = self.collect()
        return {key:data[key] for key in ('api_url','api_key','model','api_style','request_timeout')}

    def _test_connection(self):
        self.btn_diagnose.setEnabled(False)
        self.btn_diagnose.setText('正在测试…')
        self.probe_lbl.setStyleSheet(f'color: {MUTED};')
        self.probe_lbl.setText(f'正在读取模型列表并测试最小对话（最多约 {self.request_timeout.value()} 秒，不重试）…')

        def apply(data):
            self.btn_diagnose.setEnabled(True)
            self.btn_diagnose.setText('测试连接')
            self.probe_lbl.setStyleSheet(f"color: {SUCCESS if data.get('success') else ERROR};")
            self.probe_lbl.setText(str(data.get('message') or '测试失败，后台未返回结果'))

        call_api(self, 'POST', 'ai/probe', self._probe_settings(), timeout=self.request_timeout.value()+5, on_done=apply)

    def _pick_model(self, index: int):
        name = self.model_pick.itemText(index).strip()
        if name:
            self.model_name.setText(name)

    def _update_send_compat_hint(self, *args):
        enabled = self.send_compat.isChecked()
        background = self.background_send.isChecked()
        self.hot_activate_uia.blockSignals(True);self.hot_activate_uia.setChecked(False);self.hot_activate_uia.blockSignals(False)
        self.auto_open_chat.setEnabled(False)
        self.auto_open_chat.setText('自动打开会话（已停用；旧配置不再执行）')
        self.background_submit.setEnabled(background)
        self.send_compat.setEnabled(not background)
        if background:
            # 用户明确选择不弹窗，不能因为旧的兼容选项仍勾选而撤销该选择。
            self.send_compat.blockSignals(True)
            self.send_compat.setChecked(False)
            self.send_compat.blockSignals(False)
            self.send_compat_hint.setText('严格后台模式：不执行兼容唤醒或自动打开会话；缺少控件时明确等待。')
            self.send_compat_hint.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
            return
        self.send_compat_hint.setText(
            "当前状态：前台控件兼容已开启；不修改微信进程，缺少控件仍等待" if enabled else
            "当前状态：未开启（若微信未提供输入控件，发送将等待；保存后点击测试微信检查）")
        self.send_compat_hint.setStyleSheet(
            f"color: {SUCCESS if enabled else ERROR}; font-size: 12px;")

    def populate(self, data: dict):
        idx = self.backend.findData(data.get("backend", "auto"))
        if idx >= 0:
            self.backend.setCurrentIndex(idx)
        self.db_dir.setText(data.get("db_dir", "") or "")
        account = str(data.get("account", "") or "")
        index = self.account.findData(account)
        if index >= 0:
            self.account.setCurrentIndex(index)
        elif account:
            # 配置里写了个本机列表里没有的账号（比如从别的机器抄来的）：如实显示出来，
            # 不要静默丢掉——那会让用户以为设置没保存。
            self.account.addItem(f"{account}（当前配置）", account)
            self.account.setCurrentIndex(self.account.count() - 1)
        self.groups.setPlainText("\n".join(data.get("groups", []) or []))
        self.listen_private.setChecked(bool(data.get("listen_private", True)))
        # 两个控件有关联；先完整载入再联动，不能让旧界面状态抹掉服务端新配置。
        self.send_compat.blockSignals(True)
        self.background_send.blockSignals(True)
        self.send_compat.setChecked(bool(data.get("send_compat", False)))
        self.background_send.setChecked(bool(data.get('background_send', False)))
        self.send_compat.blockSignals(False)
        self.background_send.blockSignals(False)
        self.background_submit.setCurrentIndex(max(0, self.background_submit.findData(data.get('background_submit', 'invoke'))))
        self.hot_activate_uia.setChecked(False)
        self.auto_open_chat.setChecked(bool(data.get("auto_open_chat", True)))
        self.proactive_enabled.setChecked(bool(data.get("proactive_enabled", False)))
        self._update_send_compat_hint()
        self.api_url.setText(data.get("api_url", ""))
        self.model_name.setText(data.get("model", ""))
        idx2 = self.api_style.findData(data.get("api_style", "auto"))
        if idx2 >= 0:
            self.api_style.setCurrentIndex(idx2)
        self.request_timeout.setValue(int(data.get("request_timeout", 20) or 20))
        self.auto_retry.setChecked(bool(data.get("auto_retry", True)))
        idx3 = self.billing_mode.findData(data.get("billing_mode", "token"))
        if idx3 >= 0:
            self.billing_mode.setCurrentIndex(idx3)
        self.unit_price.setValue(float(data.get("unit_price", 0) or 0))
        idx4 = self.reply_speed.findData(data.get("reply_speed", "fast"))
        if idx4 >= 0:
            self.reply_speed.setCurrentIndex(idx4)
        # 微信侧没有 AppSecret / 连接令牌，只剩模型 API Key 一个密钥
        self.secrets_lbl.setText("已保存密钥：API ✓" if data.get("api_key_set") else "尚未设置密钥")

    def collect(self) -> dict:
        # 这些键必须与后端 src/wechat_app.py 的 class Settings 严格一一对应
        return {
            "backend": self.backend.currentData(),
            "db_dir": self.db_dir.text().strip(),
            "account": str(self.account.currentData() or ""),
            "groups": [g.strip() for g in self.groups.toPlainText().splitlines() if g.strip()],
            "listen_private": self.listen_private.isChecked(),
            "send_compat": self.send_compat.isChecked(),
            "background_send": self.background_send.isChecked(),
            "background_submit": self.background_submit.currentData(),
            "hot_activate_uia": False,
            "send_mode": 'background' if self.background_send.isChecked() else 'foreground',
            "auto_open_chat": self.auto_open_chat.isChecked(),
            "proactive_enabled": self.proactive_enabled.isChecked(),
            "api_url": self.api_url.text().strip(),
            "api_key": self.api_key.text().strip(),
            "model": self.model_name.text().strip(),
            "api_style": self.api_style.currentData(),
            "request_timeout": self.request_timeout.value(),
            "auto_retry": self.auto_retry.isChecked(),
            "billing_mode": self.billing_mode.currentData(),
            "unit_price": float(self.unit_price.value()),
            "reply_speed": self.reply_speed.currentData(),
        }


# ── 外观标签页 ────────────────────────────────────────────────────────────────
class AppearanceTab(QWidget):
    appearance_changed = pyqtSignal(dict)  # {transparency, blur, bg_color, background_image}

    def __init__(self):
        super().__init__()
        self._current_image = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        frame = QFrame()
        frame.setProperty("panel", True)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(18, 16, 18, 18)
        fl.setSpacing(14)

        fl.addWidget(icon_row("外观设置", "card_appearance"))

        # 透明度（0% = 完全不透明）
        op_lbl = QLabel("窗口透明度（调高可透出桌面；文字和背景一起变淡）")
        op_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl.addWidget(op_lbl)
        op_row = QHBoxLayout()
        self.transparency_slider = QSlider(Qt.Orientation.Horizontal)
        self.transparency_slider.setRange(0, 100)
        self.transparency_slider.setValue(0)
        self.transparency_val = QLabel("0%")
        self.transparency_val.setStyleSheet(f"color: {ACCENT}; min-width: 40px;")
        self.transparency_slider.valueChanged.connect(lambda v: self.transparency_val.setText(f"{v}%"))
        op_row.addWidget(self.transparency_slider)
        op_row.addWidget(self.transparency_val)
        fl.addLayout(op_row)

        panel_lbl = QLabel("面板透明度（调高可看清背景图；文字和按钮保持清晰）")
        panel_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl.addWidget(panel_lbl)
        panel_row = QHBoxLayout()
        self.panel_transparency_slider = QSlider(Qt.Orientation.Horizontal)
        self.panel_transparency_slider.setRange(0, 100)
        self.panel_transparency_slider.setValue(35)
        self.panel_transparency_val = QLabel("35%")
        self.panel_transparency_val.setStyleSheet(f"color: {ACCENT}; min-width: 40px;")
        self.panel_transparency_slider.valueChanged.connect(lambda v: self.panel_transparency_val.setText(f"{v}%"))
        panel_row.addWidget(self.panel_transparency_slider)
        panel_row.addWidget(self.panel_transparency_val)
        fl.addLayout(panel_row)

        # 云母片模糊度
        blur_lbl = QLabel("云母片模糊度（0% = 关掉云母片，纯银白；越高越糊）")
        blur_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl.addWidget(blur_lbl)
        blur_row = QHBoxLayout()
        self.blur_slider = QSlider(Qt.Orientation.Horizontal)
        self.blur_slider.setRange(0, 100)
        self.blur_slider.setValue(70)
        self.blur_val = QLabel("70%")
        self.blur_val.setStyleSheet(f"color: {ACCENT}; min-width: 40px;")
        self.blur_slider.valueChanged.connect(lambda v: self.blur_val.setText(f"{v}%"))
        blur_row.addWidget(self.blur_slider)
        blur_row.addWidget(self.blur_val)
        fl.addLayout(blur_row)

        # 等比例缩放：拖四角时保持宽高比。做成常驻开关（而不是只认 Shift），
        # 否则用户根本不知道有这个能力；Shift 仍然可以临时生效，两者不冲突。
        self.proportional_box = QCheckBox("等比例缩放窗口（拖四角时保持宽高比；按住 Shift 可临时生效）")
        self.proportional_box.setToolTip("开启后拖动窗口四角，宽高按当前比例一起变化；拖四边仍是自由缩放。")
        fl.addWidget(self.proportional_box)

        # 「恢复默认外观」：透明度/面板透明度都能拉到看不清的程度，而配置存在数据目录里
        # （覆盖安装会保留），用户"重装也没救"。给一个一键还原，别让他去删配置文件。
        reset_row = QHBoxLayout()
        self.btn_reset_appearance = QPushButton("恢复默认外观")
        self.btn_reset_appearance.setToolTip("把窗口透明度、面板透明度、模糊度和背景色恢复默认，并清除背景图")
        self.btn_reset_appearance.clicked.connect(self.reset_appearance)
        reset_row.addWidget(self.btn_reset_appearance)
        reset_row.addStretch()
        fl.addLayout(reset_row)

        # 背景色
        bg_lbl = QLabel("背景叠加色（叠加在云母片上，用来调整色调）")
        bg_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl.addWidget(bg_lbl)
        bg_row = QHBoxLayout()
        self.color_preview = QFrame()
        self.color_preview.setFixedSize(32, 32)
        self.color_preview.setStyleSheet(f"background: {BG}; border-radius: 6px; border: 1px solid {BORDER};")
        self._current_color = BG
        btn_pick = QPushButton("选择颜色…")
        btn_pick.setProperty("class", "secondary")
        btn_pick.clicked.connect(self._pick_color)
        bg_row.addWidget(self.color_preview)
        bg_row.addWidget(btn_pick)
        bg_row.addStretch()
        fl.addLayout(bg_row)

        # 自定义背景图
        img_lbl = QLabel("自定义背景图（铺满窗口，叠加色会盖在图片之上）")
        img_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        fl.addWidget(img_lbl)
        img_row = QHBoxLayout()
        self.image_preview = QLabel("无")
        self.image_preview.setFixedSize(96, 56)
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setStyleSheet(
            f"background: #0c1625; color: {MUTED}; border-radius: 6px; border: 1px solid {BORDER}; font-size: 11px;"
        )
        btn_image = QPushButton("选择图片…")
        btn_image.setProperty("class", "secondary")
        btn_image.clicked.connect(self._pick_image)
        btn_clear = QPushButton("清除背景图")
        btn_clear.setProperty("class", "secondary")
        btn_clear.clicked.connect(self._clear_image)
        img_row.addWidget(self.image_preview)
        img_row.addWidget(btn_image)
        img_row.addWidget(btn_clear)
        img_row.addStretch()
        fl.addLayout(img_row)
        self.image_hint = QLabel("")
        self.image_hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.image_hint.setWordWrap(True)
        fl.addWidget(self.image_hint)

        # 应用按钮
        btn_apply = decorate_button(QPushButton("应用外观设置"), "btn_apply")
        btn_apply.clicked.connect(self._apply)
        fl.addWidget(btn_apply)

        note = QLabel("云母片效果仅在 Windows 11 上生效。图片会按窗口比例裁剪铺满，不会变形；面板透明度只影响卡片底色。建议窗口透明度为 0%，面板透明度为 35%–70%。")
        note.setStyleSheet(f"color: {MUTED}; font-size: 11px; margin-top: 8px;")
        note.setWordWrap(True)
        fl.addWidget(note)

        layout.addWidget(frame)
        layout.addStretch()

    def _pick_color(self):
        color = QColorDialog.getColor(QColor(self._current_color), self, "选择背景叠加色")
        if color.isValid():
            self._current_color = color.name()
            self.color_preview.setStyleSheet(
                f"background: {self._current_color}; border-radius: 6px; border: 1px solid {BORDER};"
            )

    def _pick_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择背景图片", "", IMAGE_FILTER)
        if not path:
            return
        try:
            relative = import_background(path, ROOT)
        except (ValueError, OSError) as exc:
            self.image_hint.setText(f"导入失败：{exc}")
            return
        previous = self._current_image
        self._current_image = relative
        if previous and previous != relative:
            try:
                clear_background(previous, ROOT)
            except (ValueError, OSError):
                pass
        self._show_image_hint()
        self._apply()

    def _clear_image(self):
        previous = self._current_image
        self._current_image = ""
        if previous:
            try:
                clear_background(previous, ROOT)
            except (ValueError, OSError) as exc:
                self.image_hint.setText(f"删除旧图失败（已忽略）：{exc}")
        self._show_image_hint()
        self._apply()

    def _show_image_hint(self):
        resolved = resolve_background(self._current_image, ROOT)
        if resolved is None:
            self._current_image = "" if self._current_image else self._current_image
            self._image_ready = False
            self.image_preview.setText("无")
            self.image_preview.setPixmap(QPixmap())
            self.image_preview.setStyleSheet(
                f"background: #0c1625; color: {MUTED}; border-radius: 6px; border: 1px solid {BORDER}; font-size: 11px;"
            )
            self.image_hint.setText("" if not self._current_image else "背景图文件不存在，已忽略")
            return
        self._image_ready = True
        preview = QPixmap(str(resolved))
        if preview.isNull():
            self._image_ready = False
            self.image_preview.setText("无法读取")
            self.image_preview.setPixmap(QPixmap())
            self.image_hint.setText("图片无法读取，已退回纯色背景")
            return
        self.image_preview.setText("")
        self.image_preview.setPixmap(
            preview.scaled(
                self.image_preview.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.image_hint.setText(f"当前：{self._current_image}")

    def reset_appearance(self):
        """一键把外观恢复默认：滑块回到默认值、清掉背景图，然后立刻应用并保存。"""
        self.transparency_slider.setValue(0)
        self.transparency_val.setText("0%")
        self.panel_transparency_slider.setValue(35)
        self.panel_transparency_val.setText("35%")
        self.blur_slider.setValue(70)
        self.blur_val.setText("70%")
        self._current_color = BG
        self.color_preview.setStyleSheet(
            f"background: {BG}; border-radius: 6px; border: 1px solid {BORDER};")
        self._current_image = ""
        self._show_image_hint()
        self.proportional_box.setChecked(False)
        self._apply()

    def _apply(self):
        self.appearance_changed.emit({
            "transparency": self.transparency_slider.value() / 100.0,
            "panel_transparency": self.panel_transparency_slider.value() / 100.0,
            "blur": self.blur_slider.value() / 100.0,
            "bg_color": self._current_color,
            "background_image": self._current_image,
            "proportional_resize": self.proportional_box.isChecked(),
        })

    def load(self, cfg: dict):
        transparency = int(round(float(cfg.get("transparency", 0.0)) * 100))
        self.transparency_slider.setValue(transparency)
        self.transparency_val.setText(f"{transparency}%")
        panel = int(round(float(cfg.get("panel_transparency", 0.35)) * 100))
        self.panel_transparency_slider.setValue(panel)
        self.panel_transparency_val.setText(f"{panel}%")
        blur = int(round(float(cfg.get("blur", 0.70)) * 100))
        self.blur_slider.setValue(blur)
        self.blur_val.setText(f"{blur}%")
        color = cfg.get("bg_color", BG)
        self._current_color = color
        self.color_preview.setStyleSheet(
            f"background: {color}; border-radius: 6px; border: 1px solid {BORDER};"
        )
        self._current_image = cfg.get("background_image", "") or ""
        self._show_image_hint()
        self.proportional_box.setChecked(bool(cfg.get("proportional_resize", False)))


# ── 界面小部件辅助 ────────────────────────────────────────────────────────────
ICON_PX = 18          # 卡片/标题图标字号
ICON_PX_SMALL = 14    # 按钮图标（按钮高度有限）


def icon_row(text: str, slot: str, size: int = ICON_PX, color: str = ACCENT) -> QWidget:
    """"图标 + 文字"一行，用来替换标题里的 emoji。"""
    holder = QWidget()
    holder.setStyleSheet("background: transparent;")
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(7)
    mark = QLabel()
    mark.setPixmap(slot_icon(slot).pixmap(size, size))
    mark.setFixedSize(size + 2, size + 2)
    mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label = QLabel(text)
    label.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: bold;")
    row.addWidget(mark)
    row.addWidget(label)
    row.addStretch(1)
    return holder


def decorate_button(button, slot: str, size: int = ICON_PX_SMALL):
    """给按钮加图标（图标取不到时按钮文字保持不变，不会空掉）。"""
    mark = slot_icon(slot)
    if not mark.isNull():
        button.setIcon(mark)
        button.setIconSize(QSize(size, size))
    return button


# ── 人设页 ────────────────────────────────────────────────────────────────────
def _card(title: str, slot: str = "") -> tuple:
    """统一卡片外观，返回 (frame, 内部竖排 layout)。

    ``slot`` 给了就画成"图标 + 标题"，替代以前的 emoji 前缀。
    """
    frame = QFrame()
    frame.setProperty("panel", True)
    box = QVBoxLayout(frame)
    box.setContentsMargins(18, 16, 18, 18)
    box.setSpacing(10)
    if slot:
        box.addWidget(icon_row(title, slot))
    else:
        label = QLabel(title)
        label.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-weight: bold;")
        box.addWidget(label)
    return frame, box


class PersonaTab(QWidget):
    """自定义人设：左边选、右边改，可新建/保存/切换/删除。

    默认人设是鲸鱼娘（tsundere_whale_girl）；内置人设不允许覆盖，
    改完请另存为新标识（界面会提示）。
    """

    def __init__(self):
        super().__init__()
        self._current = ""
        self._original = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：人设列表
        left, lbox = _card("人设列表", "card_persona_list")
        hint = QLabel("选择列表项后可编辑；保存修改后，点击「设为当前」让该人设用于后续回复。内置人设请用「另存为」。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        lbox.addWidget(hint)
        self.active_persona_hint = QLabel('当前生效人设：正在读取…')
        self.active_persona_hint.setWordWrap(True)
        lbox.addWidget(self.active_persona_hint)
        self.list = QListWidget()
        self.list.setStyleSheet(
            f"QListWidget {{ background: {FIELD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }}"
            f"QListWidget::item {{ padding: 6px; }}"
            f"QListWidget::item:selected {{ background: {ACCENT}; color: {ACCENT_FG}; border-radius: 6px; }}"
        )
        self.list.currentRowChanged.connect(self._on_select)
        lbox.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_new = decorate_button(QPushButton("新建"), "btn_new")
        self.btn_reload = decorate_button(QPushButton("刷新"), "btn_reload")
        self.btn_new.clicked.connect(self._new)
        self.btn_reload.clicked.connect(self.refresh)
        row.addWidget(self.btn_new)
        row.addWidget(self.btn_reload)
        lbox.addLayout(row)
        splitter.addWidget(left)

        # 右：编辑区
        right, rbox = _card("人设内容", "card_persona_edit")
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)

        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("英文标识，例如 my_persona（只能用字母数字下划线短横线）")
        self.ed_display = QLineEdit()
        self.ed_display.setPlaceholderText("显示名称，例如 傲娇鲸鱼娘")
        self.ed_desc = QLineEdit()
        self.ed_desc.setPlaceholderText("一句话说明适用场景")
        self.ed_keywords=QLineEdit()
        self.ed_keywords.setPlaceholderText('当前人设专属唤醒词，逗号分隔；留空沿用通用词表')
        self._persona_image=''
        self.ed_image_note=QLineEdit()
        self.ed_image_note.setPlaceholderText('形象说明（选填；可手填，也可保存后点击识别形象）')
        self.image_preview=QLabel('未设置形象图（选填）')
        self.image_preview.setFixedHeight(80)
        image_buttons=QHBoxLayout()
        for label,action in [('上传形象图',self._upload_persona_image),('识别形象',self._analyze_persona_image),('移除图片',self._remove_persona_image)]:
            button=QPushButton(label)
            button.clicked.connect(action)
            image_buttons.addWidget(button)

        self.sp_temp = QDoubleSpinBox()
        self.sp_temp.setRange(0.0, 2.0)
        self.sp_temp.setSingleStep(0.1)
        self.sp_temp.setDecimals(2)
        self.sp_tokens = QSpinBox()
        self.sp_tokens.setRange(16, 8000)
        self.sp_tokens.setSingleStep(16)

        self.ed_prompt = QTextEdit()
        self.ed_prompt.setPlaceholderText("人设本体：身份、性格、说话习惯、口头禅、禁止项…")
        self.ed_prompt.setMinimumHeight(140)
        self.ed_examples = QTextEdit()
        self.ed_examples.setPlaceholderText("对话示例，两行一组：\n用户：你好\n你：哼，找我干嘛啦")
        self.ed_examples.setMinimumHeight(110)

        for widget in (self.ed_name, self.ed_display, self.ed_desc, self.ed_prompt, self.ed_examples):
            widget.setStyleSheet(
                f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER};"
                "border-radius: 6px; padding: 6px;"
            )
        for spin in (self.sp_temp, self.sp_tokens):
            spin.setStyleSheet(f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px;")

        form.addRow("标识", self.ed_name)
        form.addRow("显示名", self.ed_display)
        form.addRow("说明", self.ed_desc)
        form.addRow('关键词',self.ed_keywords)
        form.addRow('形象图',self.image_preview)
        form.addRow('',image_buttons)
        form.addRow('形象说明',self.ed_image_note)
        form.addRow("温度", self.sp_temp)
        form.addRow("最大长度", self.sp_tokens)
        form.addRow("人设本体", self.ed_prompt)
        form.addRow("对话示例", self.ed_examples)
        form_body=QWidget()
        form_body.setLayout(form)
        form_scroll=QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setWidget(form_body)
        rbox.addWidget(form_scroll,1)

        btns = QHBoxLayout()
        self.btn_save = decorate_button(QPushButton("保存"), "btn_save")
        self.btn_saveas = decorate_button(QPushButton("另存为"), "btn_save")
        self.btn_activate = decorate_button(QPushButton("设为当前"), "btn_apply")
        self.btn_delete = decorate_button(QPushButton("删除"), "btn_delete")
        self.btn_save.clicked.connect(lambda: self._save(rename=False))
        self.btn_saveas.clicked.connect(lambda: self._save(rename=True))
        self.btn_activate.clicked.connect(self._activate)
        self.btn_delete.clicked.connect(self._delete)
        for b in (self.btn_save, self.btn_saveas, self.btn_activate, self.btn_delete):
            btns.addWidget(b)
        rbox.addLayout(btns)
        files=QHBoxLayout()
        self.btn_import=QPushButton('导入人设文件')
        self.btn_import.setToolTip('支持 YAML / JSON / TXT，兼容 QQ 导出、prompt 正文、UTF-8 / UTF-16 / GB18030。'
                                   '\n中文名称会保留显示，同名自动另存副本；导入后点「设为当前」才用于回复。')
        self.btn_export=QPushButton('导出人设文件')
        self.btn_import.clicked.connect(self._import_persona)
        self.btn_export.clicked.connect(self._export_persona)
        files.addWidget(self.btn_import)
        files.addWidget(self.btn_export)
        rbox.addLayout(files)

        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        rbox.addWidget(self.notice)
        splitter.addWidget(right)
        splitter.setSizes([300, 720])
        root.addWidget(splitter, 1)

    # ── 数据 ──────────────────────────────────────────────────────────────
    # 这些请求都要经过本机后端，读人设详情在慢盘上也可能是秒级；一律走 call_api()
    # 丢到后台线程，回调里再碰控件（Qt 只在主线程操作控件）。
    def refresh(self):
        call_api(self, "GET", "personas", timeout=10, on_done=self._apply_personas)

    def _apply_personas(self, data):
        personas = data.get("personas") or []
        active = data.get("active") or ""
        display = next((p.get('display_name') for p in personas if p.get('name')==active), active)
        self.active_persona_hint.setText(f'当前生效人设：{display}（{active}）')
        if not self._current:
            self._current=active
        self.list.blockSignals(True)
        self.list.clear()
        for item in personas:
            name = item.get("name", "")
            label = f"{item.get('display_name') or name}"
            if name == active:
                label += "   （当前）"
            row = QListWidgetItem(label)
            row.setData(Qt.ItemDataRole.UserRole, name)
            self.list.addItem(row)
        self.list.blockSignals(False)
        if self.list.count() and not self._current:
            self.list.setCurrentRow(0)
        elif self.list.count():
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole) == self._current:
                    self.list.setCurrentRow(i)
                    break

    def _load_detail(self, name: str):
        self._detail_target = name
        call_api(self, "GET", f"personas/{name}", timeout=10,
                 on_done=lambda detail: self._apply_detail(name, detail))

    def _apply_detail(self, name: str, detail):
        if hasattr(self, '_detail_target') and self._detail_target != name:
            return
        if not detail or "system_prompt" not in detail:
            self.notice.setText(f"读不到人设内容：{name}")
            return
        self._current = name
        self._original = name
        self.ed_name.setText(detail.get("name", ""))
        self.ed_display.setText(detail.get("display_name", ""))
        self.ed_desc.setText(detail.get("description", ""))
        self.ed_keywords.setText(', '.join(detail.get('keywords') or []))
        self._persona_image=str(detail.get('image') or '')
        self.ed_image_note.setText(detail.get('image_note') or '')
        self._render_persona_image()
        temperature = detail.get('temperature')
        self.sp_temp.setValue(float(0.7 if temperature is None else temperature))
        self.sp_tokens.setValue(int(detail.get("max_tokens", 500) or 500))
        self.ed_prompt.setPlainText(detail.get("system_prompt", ""))
        examples = detail.get("examples") or []
        lines = []
        for item in examples:
            if isinstance(item, dict):
                lines.append(f"用户：{item.get('user', '')}")
                lines.append(f"你：{item.get('assistant', '')}")
        self.ed_examples.setPlainText("\n".join(lines))
        pending = getattr(self, '_import_notice', None)
        self.notice.setText(pending[1] if pending and pending[0] == name else '')

    def _on_select(self, row: int):
        if row < 0:
            return
        name = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        if name:
            self._load_detail(name)

    def _new(self):
        self._detail_target = None
        self._import_notice = None
        self._current = ""
        self._original = ""
        self.ed_name.clear()
        self.ed_display.clear()
        self.ed_desc.clear()
        self.ed_keywords.clear()
        self._persona_image=''
        self.ed_image_note.clear()
        self._render_persona_image()
        self.sp_temp.setValue(0.7)
        self.sp_tokens.setValue(500)
        self.ed_prompt.clear()
        self.ed_examples.clear()
        self.notice.setText("正在新建人设：填好后点「保存」。标识只能用字母、数字、下划线、短横线。")

    def _payload(self, rename: bool) -> dict:
        return {
            "name": self.ed_name.text().strip(),
            "original_name": self._original if not rename else "",
            "display_name": self.ed_display.text().strip(),
            "description": self.ed_desc.text().strip(),
            'keywords':self.ed_keywords.text().strip(),
            'image':self._persona_image,'image_note':self.ed_image_note.text().strip(),
            "system_prompt": self.ed_prompt.toPlainText().strip(),
            "temperature": self.sp_temp.value(),
            "max_tokens": self.sp_tokens.value(),
            "examples": self.ed_examples.toPlainText(),
        }

    def _save(self, rename: bool):
        payload = self._payload(rename)
        if not payload["name"]:
            self.notice.setText("请先填「标识」")
            return
        self.notice.setText("正在保存…")
        call_api(self, "POST", "personas", payload, timeout=15,
                 on_done=lambda result: self._apply_save(payload, result))

    def _render_persona_image(self):
        self.image_preview.clear()
        if self._persona_image:
            path=ROOT/'data/persona_images'/self._persona_image
            pixmap=QPixmap(str(path))
            if not pixmap.isNull():
                self.image_preview.setPixmap(pixmap.scaled(180,80,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                return
        self.image_preview.setText('未设置形象图（选填）')

    def _upload_persona_image(self):
        path,_=QFileDialog.getOpenFileName(self,'上传人设形象图','',IMAGE_FILTER)
        if not path:
            return
        try:
            source=Path(path)
            if source.stat().st_size>2*1024*1024:
                raise ValueError('图片最大 2 MB')
            encoded=base64.b64encode(source.read_bytes()).decode('ascii')
        except (OSError,ValueError) as exc:
            self.notice.setText(str(exc))
            return
        def done(result):
            if result.get('success'):
                self._persona_image=result['image']
                self._render_persona_image()
                self.notice.setText('图片已上传，请保存人设；如需视觉理解，保存后点击“识别形象”。')
            else:
                self.notice.setText(str(result.get('message') or '上传失败'))
        call_api(self,'POST','persona-files/image',{'data':encoded},timeout=15,on_done=done)

    def _remove_persona_image(self):
        self._persona_image=''
        self.ed_image_note.clear()
        self._render_persona_image()

    def _analyze_persona_image(self):
        if not self._current or not self._persona_image:
            self.notice.setText('请先上传图片并保存人设。')
            return
        self.notice.setText('正在通过已配置的视觉接口识别形象…')
        def done(result):
            if result.get('success'):
                self.ed_image_note.setText(result.get('image_note',''))
            self.notice.setText(str(result.get('message') or '识别失败，请手工填写形象说明'))
        call_api(self,'POST',f'persona-files/{self._current}/analyze',timeout=90,on_done=done)

    def _import_persona(self):
        """直接导入；内部标识和同名副本由后台分配，避免中文文件名导致失败。"""
        if getattr(self, '_importing', False):
            return
        path,_=QFileDialog.getOpenFileName(self,'导入人设文件','','人设文件 (*.yaml *.yml *.json *.txt)')
        if not path:
            return
        try:
            source=Path(path)
            from src.persona_import import read_persona_file
            content=read_persona_file(source)
        except (OSError,ValueError) as exc:
            self.notice.setText(str(exc))
            return
        base=source.stem.strip() or 'imported'
        self._importing = True
        self.btn_import.setEnabled(False)
        self.notice.setText(f'正在导入「{source.name}」…')

        def attempt(name, tries=1):
            def done(result):
                result = result or {}
                if result.get('success'):
                    self._current=result['name']
                    self._detail_target = result['name']
                    summary = f'人设已导入并保存：{result.get("display_name") or result["name"]}；点击「设为当前」用于后续回复。'
                    if result.get('prompt_chars'):
                        summary += f' 正文 {result["prompt_chars"]} 字。'
                    if result.get('warnings'):
                        summary += ' 提示：' + '；'.join(str(item) for item in result['warnings'])
                    self._import_notice = (result['name'], summary)
                    self._importing = False
                    self.btn_import.setEnabled(True)
                    self.refresh()
                    self.notice.setText(summary)
                    return
                message=str(result.get('message') or result.get('detail') or '')
                # 重名：自动换一个后缀再试，最多 20 次；其它错误如实显示
                if tries<20 and ('同名' in message or '已存在' in message or 'exist' in message.lower()):
                    attempt(f'{base}_{tries+1}', tries+1)
                    return
                self.notice.setText('导入失败：'+(message or '未知原因'))
                self._importing = False
                self.btn_import.setEnabled(True)
            call_api(self,'POST','persona-files/import',{'content':content,'name':name,
                     'filename':source.name,'auto_name':True},
                     timeout=15,on_done=done)

        attempt(base)

    def _export_persona(self):
        if not self._current:
            self.notice.setText('请先保存并选中人设。')
            return
        path,_=QFileDialog.getSaveFileName(self,'导出人设文件',self._current+'.yaml','YAML (*.yaml);;JSON (*.json)')
        if not path:
            return
        def done(result):
            if not result.get('success'):
                self.notice.setText(str(result.get('message') or '导出失败'))
                return
            try:
                from src.persistence import atomic_text
                content=result['content']
                if Path(path).suffix.lower()=='.json':
                    import yaml
                    content=json.dumps(yaml.safe_load(content),ensure_ascii=False,indent=2)
                atomic_text(Path(path),content)
                self.notice.setText('人设文件已导出（包含已保存的关键词和可选图片）。')
            except (OSError,ValueError) as exc:
                self.notice.setText('导出失败：'+str(exc))
        call_api(self,'GET',f'persona-files/{self._current}/export',timeout=15,on_done=done)

    def _apply_save(self, payload: dict, result):
        ok = bool(result.get("success"))
        self.notice.setText(result.get("message") or ("已保存" if ok else "保存失败"))
        if ok:
            self._current = result.get("name") or payload["name"]
            self._original = self._current
            self.refresh()

    def _activate(self):
        if not self._current:
            self.notice.setText("先选一个人设")
            return
        call_api(self, "POST", f"personas/{self._current}/activate", timeout=15,
                 on_done=self._apply_activate)

    def _apply_activate(self, result):
        self.notice.setText(result.get("message") or ("已切换" if result.get("success") else "切换失败"))
        self.refresh()

    def _delete(self):
        if not self._current:
            return
        confirm = QMessageBox.question(
            self, "删除人设", f"确定删除「{self._current}」吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        # 删除接口是 DELETE（以前先发一次 POST 必然 404、再退化成裸 requests.delete，
        # 那条路绕过了统一的错误处理，失败原因全被吞掉）。
        call_api(self, "DELETE", f"personas/{self._current}", timeout=15,
                 on_done=self._apply_delete)

    def _apply_delete(self, result):
        self.notice.setText(result.get("message") or ("已删除" if result.get("success") else "删除失败"))
        if result.get("success"):
            self._current = ""
            self._new()
            self.refresh()


# ── 插件详细配置 ──────────────────────────────────────────────────────────────
class StickerLibraryDialog(QDialog):
    """机器人本地图库：预览 / 上传 / 改标签 / AI 识别 / 删除（对照 QQ 版同名窗口）。

    与 QQ 版的差异只有两处，且都写在界面上：
    * 顶部多一个「自动收集」开关（对应插件 `sticker_steal`），用户不用去插件页找；
    * 底部提示写明微信 4.x 的"动画表情"是加密的、本地读不出图，
      所以只有以「图片」形式发来的表情才会进库 —— 不假装支持。
    """

    PAGE_LIMIT = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.page, self.pages = 1, 1
        self._items: list = []
        self._collecting = False
        self._auto_annotating = False
        self._annotation_counts = {}
        self._annotation_polling = False
        self._busy = False
        self.setWindowTitle("表情包管理 · 机器人本地图库")
        self.setWindowIcon(slot_icon("dialog"))
        self.resize(960, 760)
        self.setStyleSheet(DIALOG_STYLESHEET)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        hint = QLabel("管理自动收集与在这里上传的图片；删除只影响机器人本地图库。"
                      "原有手动图库仍在「表情包与自定义图库」中。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        outer.addWidget(hint)

        switches = QHBoxLayout()
        self.collect_box = QCheckBox("只收集已触发回复的图片（不后台收图）")
        self.collect_box.setToolTip("打开后，群聊里别人发的图片与表情会自动收进这个图库"
                                    "（可在插件中心细调：只收小图、私聊是否收、黑名单、容量上限）")
        self.collect_box.stateChanged.connect(self._toggle_collect)
        switches.addWidget(self.collect_box)
        self.auto_box = QCheckBox('新图自动AI标注（会使用识图接口）')
        self.auto_box.stateChanged.connect(self._toggle_annotation)
        switches.addWidget(self.auto_box)
        switches.addStretch()
        outer.addLayout(switches)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索标签或图片含义，按回车查找")
        self.search.returnPressed.connect(self.search_now)
        search_row.addWidget(self.search, 1)
        self.upload_button = QPushButton("上传图片…")
        self.upload_button.clicked.connect(self.upload)
        search_row.addWidget(self.upload_button)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        search_row.addWidget(refresh)
        outer.addLayout(search_row)

        jobs = QHBoxLayout()
        self.fill_button = QPushButton('补全空白说明（最多20张）')
        self.fill_button.clicked.connect(lambda: self._queue_annotations(False))
        self.retry_button = QPushButton('重试失败项')
        self.retry_button.clicked.connect(lambda: self._queue_annotations(True))
        self.cancel_jobs_button = QPushButton('取消标注')
        self.cancel_jobs_button.clicked.connect(lambda: self._request('POST', 'stickers/annotation-jobs/cancel', {}))
        for button in (self.fill_button, self.retry_button, self.cancel_jobs_button):
            jobs.addWidget(button)
        self.job_status = QLabel('已有聊天识图结果会直接复用，不重复调用模型。')
        self.job_status.setWordWrap(True)
        jobs.addWidget(self.job_status, 1)
        outer.addLayout(jobs)

        self.table = QTableWidget(0, 4)
        # 透明全局样式会让Windows表头变黑，独立图库显式提供可读底色。
        self.table.setStyleSheet('QTableWidget { background: #FFFFFF; gridline-color: #D2DCEA; } '
            'QHeaderView::section { background: #EDF1F5; color: #18334A; padding: 6px; border: 1px solid #D2DCEA; } '
            'QTableCornerButton::section { background: #EDF1F5; border: 1px solid #D2DCEA; }')
        self.table.setHorizontalHeaderLabels(["预览", "标签", "图片含义", "使用次数"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 200)
        self.table.itemSelectionChanged.connect(self.select)
        outer.addWidget(self.table, 1)

        pager = QHBoxLayout()
        self.previous_button = QPushButton("上一页")
        self.previous_button.clicked.connect(lambda: self.move(-1))
        pager.addWidget(self.previous_button)
        self.page_label = QLabel()
        pager.addWidget(self.page_label)
        self.next_button = QPushButton("下一页")
        self.next_button.clicked.connect(lambda: self.move(1))
        pager.addWidget(self.next_button)
        pager.addStretch()
        outer.addLayout(pager)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.label = QLineEdit()
        self.label.setPlaceholderText("例如：开心、委屈、疑惑（最多40字）")
        self.label.setMaxLength(40)
        self.description = QTextEdit()
        self.description.setPlaceholderText("描述图中动作、表情及适用语境，供模型选图使用")
        self.description.setMaximumHeight(90)
        form.addRow("标签", self.label)
        form.addRow("图片含义", self.description)
        self.tags = QLineEdit()
        self.tags.setPlaceholderText('人工标签：用逗号分隔，例如 开心、点赞；优先于AI标签')
        form.addRow('人工标签', self.tags)
        self.auto_tags = QLabel('AI标签：尚未标注')
        self.auto_tags.setWordWrap(True)
        form.addRow('', self.auto_tags)
        outer.addLayout(form)

        actions = QHBoxLayout()
        self.save_button = QPushButton("保存标签与说明")
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button)
        self.recognize_button = QPushButton("AI识别这张图")
        self.recognize_button.clicked.connect(self.recognize)
        actions.addWidget(self.recognize_button)
        self.delete_button = QPushButton("删除选中图片")
        self.delete_button.clicked.connect(self.remove)
        actions.addWidget(self.delete_button)
        # 没选中任何一行时这三个按钮必须是灰的（首次读取还没回来时也一样）
        for button in (self.save_button, self.recognize_button, self.delete_button):
            button.setEnabled(False)
        actions.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        outer.addLayout(actions)

        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        outer.addWidget(self.notice)
        self._job_timer = QTimer(self)
        self._job_timer.setInterval(1500)
        self._job_timer.timeout.connect(self._poll_annotations)
        self.refresh()

    # ── 数据 ────────────────────────────────────────────────────────────────
    def refresh(self):
        self._request("GET", f"stickers?q={quote(self.search.text())}&page={self.page}",
                      on_done=self._fill)

    def _fill(self, result):
        if not isinstance(result, dict) or "items" not in result:
            self.notice.setText((result or {}).get("message") or
                                "图库读取失败：后台可能还没起来，点「刷新」重试")
            return
        self._items = result["items"]
        self._auto_annotating = bool(result.get('annotation_auto'))
        self.auto_box.blockSignals(True)
        self.auto_box.setChecked(self._auto_annotating)
        self.auto_box.blockSignals(False)
        self.pages = max(1, int(result.get("pages") or 1))
        self.page = max(1, min(self.pages, int(result.get("page") or 1)))
        # 刷新前记住选中的是哪一条：改完标签/识别完刷新后，选中的还得是同一条，
        # 否则用户每次操作完都要重新找那一行。
        previous = self.current()
        previous_id = str(previous.get("id")) if previous else ""
        collecting = bool(result.get("collecting"))
        if collecting != self._collecting:
            self._collecting = collecting
            self.collect_box.blockSignals(True)
            self.collect_box.setChecked(collecting)
            self.collect_box.blockSignals(False)
        self.table.setRowCount(len(self._items))
        for row, entry in enumerate(self._items):
            preview = QLabel()
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pixmap = QPixmap()
            pixmap.loadFromData(base64.b64decode(entry.get("thumbnail") or ""))
            preview.setPixmap(pixmap.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))
            self.table.setCellWidget(row, 0, preview)
            self.table.setRowHeight(row, 104)
            self.table.setItem(row, 1, QTableWidgetItem(str(entry.get("label") or "")))
            self.table.setItem(row, 2, QTableWidgetItem(str(entry.get("description") or "")))
            self.table.setItem(row, 3, QTableWidgetItem(str(entry.get("uses", 0))))
        self.page_label.setText(f"{self.page}/{self.pages} 页 · 共 {result.get('total', 0)} 张")
        self.previous_button.setEnabled(self.page > 1)
        self.next_button.setEnabled(self.page < self.pages)
        # 默认选中第一行：只 setRowCount 不会选中任何行，三个操作按钮就一直是灰的
        if self._items:
            index = next((row for row, entry in enumerate(self._items)
                          if str(entry.get("id")) == previous_id), 0)
            self.table.selectRow(index)
        self.select()
        if isinstance(result.get('annotation_jobs'), dict):
            self._annotation_status(result['annotation_jobs'], refresh=False)

    def current(self):
        row = self.table.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def select(self):
        entry = self.current()
        self.label.setText(str(entry.get("label") or "") if entry else "")
        self.description.setPlainText(str(entry.get("description") or "") if entry else "")
        self.tags.setText('、'.join(entry.get('manual_tags') or []) if entry else '')
        self.auto_tags.setText('AI标签：' + ('、'.join(entry.get('auto_tags') or []) or '尚未标注') if entry else 'AI标签：尚未标注')
        for button in (self.save_button, self.recognize_button, self.delete_button):
            button.setEnabled(bool(entry) and not self._busy)

    def search_now(self):
        self.page = 1
        self.refresh()

    def move(self, amount):
        self.page = max(1, min(self.pages, self.page + amount))
        self.refresh()

    def show_result(self, result):
        result = result if isinstance(result, dict) else {}
        self.notice.setText(str(result.get("message") or "操作未完成"))
        if result.get("success"):
            self.refresh()

    # ── 操作 ────────────────────────────────────────────────────────────────
    def _request(self, method, path, data=None, timeout=15, on_done=None):
        """所有请求都走宿主的后台线程（`call_api`），模态窗口也不能卡住界面。"""
        call_api(self, method, path, data, timeout=timeout,
                 on_done=on_done or self.show_result)

    def _toggle_collect(self, state):
        enabled = bool(state)
        self.collect_box.setEnabled(False)
        self._request("POST", "plugins/sticker_steal", {"enabled": enabled},
                      on_done=self._collect_done)

    def _collect_done(self, result):
        self.collect_box.setEnabled(True)
        result = result if isinstance(result, dict) else {}
        if not result.get("success"):
            self.notice.setText(f"自动收集开关没保存成功：{result.get('message') or '未知原因'}")
            # 回到"上一次确认过的状态"，而不是取反 —— 取反会在连续失败时来回跳
            self.collect_box.blockSignals(True)
            self.collect_box.setChecked(self._collecting)
            self.collect_box.blockSignals(False)
            return
        self._collecting = self.collect_box.isChecked()
        self.notice.setText("回复图片收集已" + ("开启：仅收集已触发回复的图片，闲聊不下载"
                                          if self._collecting else "关闭"))

    def upload(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "上传表情图片（可多选）", "",
                                                "图片 (*.png *.jpg *.jpeg *.gif *.webp)")
        if not paths:
            return
        try:
            spec = {"file_picker": "files",
                    "file_extensions": [".png", ".jpg", ".jpeg", ".gif", ".webp"],
                    "file_max_mb": 6}
            items = import_plugin_files(ROOT / "data/plugin_uploads", "sticker_admin",
                                        "imports", paths, spec)
        except (OSError, ValueError) as exc:
            self.notice.setText(f"上传失败：{exc}")
            return
        self.notice.setText(f"正在导入 {len(items)} 张…")
        self._request("POST", "stickers/import", {"items": items}, timeout=60)

    def save(self):
        entry = self.current()
        if not entry:
            return
        body = {"label": self.label.text(), "description": self.description.toPlainText()}
        if self.tags.text() != '、'.join(entry.get('manual_tags') or []):
            body['tags'] = self.tags.text()
        self._request("POST", "stickers/" + str(entry["id"]), body)

    def _toggle_annotation(self, state):
        self.auto_box.setEnabled(False)
        def done(result):
            self.auto_box.setEnabled(True)
            if result.get('success'):
                self._auto_annotating = self.auto_box.isChecked()
                self.notice.setText('新图自动标注已' + ('开启；旧图请点击补全空白说明' if self._auto_annotating else '关闭'))
            else:
                self.auto_box.blockSignals(True)
                self.auto_box.setChecked(self._auto_annotating)
                self.auto_box.blockSignals(False)
                self.notice.setText(result.get('message') or '自动标注设置保存失败')
        self._request('POST', 'plugins/sticker_admin', {'autoRecognize': bool(state)}, on_done=done)

    def _queue_annotations(self, retry):
        self._request('POST', 'stickers/annotation-jobs', {'retry_failed': retry})

    def _poll_annotations(self):
        if self._annotation_polling:
            return
        self._annotation_polling = True
        self._request('GET', 'stickers/annotation-jobs', on_done=self._annotation_status)

    def _annotation_status(self, result, refresh=True):
        self._annotation_polling = False
        if not isinstance(result.get('counts'), dict):
            self.job_status.setText(result.get('message') or '标注服务暂不可用')
            return
        counts = result.get('counts') or {}
        self.job_status.setText('排队 %s · 识别 %s · 完成 %s · 失败 %s' % tuple(
            counts.get(key, 0) for key in ('pending', 'running', 'done', 'failed')))
        self.job_status.setToolTip('\n'.join(str(item.get('id', ''))[:8] + ': ' +
            str(item.get('error') or item.get('state') or '') for item in result.get('items', [])))
        changed = counts != self._annotation_counts
        self._annotation_counts = counts
        entry = self.current() or {}
        dirty = (self.label.text() != str(entry.get('label') or '') or
                 self.description.toPlainText() != str(entry.get('description') or '') or
                 self.tags.text() != '、'.join(entry.get('manual_tags') or []))
        if changed and refresh and not counts.get('running') and not dirty:
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._job_timer.start()

    def hideEvent(self, event):
        self._job_timer.stop()
        super().hideEvent(event)

    def recognize(self):
        entry = self.current()
        if not entry:
            return
        self._busy = True
        self.select()
        self.notice.setText("正在识别，完成后会更新图片说明…")
        self._request("POST", f"stickers/{entry['id']}/recognize", {}, timeout=120,
                      on_done=self._recognize_done)

    def _recognize_done(self, result):
        self._busy = False
        self.select()
        self.show_result(result)

    def remove(self):
        entry = self.current()
        if not entry:
            return
        answer = QMessageBox.question(
            self, "删除本地表情", f"删除「{entry.get('label')}」？\n只删除机器人自己的本地收藏，"
                                 "不影响微信里的表情与手动图库。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._request("DELETE", "stickers/" + str(entry["id"]))


class PluginConfigDialog(QDialog):
    """按插件的 config_schema 生成表单：布尔→勾选框、数字→数值框、选项→下拉框、
    多行文本→大文本框。保存时把整份设置写回 /api/plugins/{id}。"""

    def __init__(self, plugin: dict, parent=None):
        super().__init__(parent)
        meta = plugin or {}
        self.plugin_id = meta.get("id", "")
        # /api/plugins 返回的键是 schema / defaults（不是 config_schema），这里做兼容
        self._schema = meta.get("schema") or meta.get("config_schema") or {}
        self._defaults = meta.get("defaults") or meta.get("default_settings") or {}
        self._initial_settings = dict(meta.get('settings') or self._defaults)
        self._widgets: dict = {}
        self._file_buttons = {}
        self.saved = False

        self.setWindowTitle(f"插件设置 · {meta.get('name') or self.plugin_id}")
        self.setWindowIcon(slot_icon("dialog"))
        self.resize(680, 620)
        if self.plugin_id == 'keyword_trigger':
            area = self.screen().availableGeometry()
            self.resize(min(740, area.width()-40), min(760, area.height()-60))
        if any(isinstance(spec,dict) and spec.get('editor')=='keyword_rules' for spec in self._schema.values()):
            self.resize(780,660)
        self.setStyleSheet(DIALOG_STYLESHEET)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        head = QLabel((meta.get('name') or self.plugin_id) if self.plugin_id == 'keyword_trigger'
                      else f"{meta.get('name') or self.plugin_id}  ({self.plugin_id})")
        head.setStyleSheet(f"color: {ACCENT}; font-size: 15px; font-weight: bold;")
        outer.addWidget(head)
        desc = QLabel(meta.get("description") or "")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        outer.addWidget(desc)

        if self.plugin_id == 'image_understanding':
            hint=QLabel('仅保存需要机器人回复的图片。超过 50 张自动删除最旧一半；聊天记录和表情库保留。')
            hint.setWordWrap(True)
            outer.addWidget(hint)
            self.image_store_button=QPushButton('打开图片储藏区')
            self.image_store_button.setObjectName('open_received_images')
            self.image_store_button.clicked.connect(self._open_received_images)
            outer.addWidget(self.image_store_button)

        if self.plugin_id == 'keyword_trigger':
            guide = QLabel('第一次设置可以这样填：保持三个开关开启，选择“保留原有词表”，'
                           '在“额外唤醒词”里每行写一个称呼，例如“小鲸鱼”。保存后，'
                           '群友发“小鲸鱼，在吗”就能触发 AI 回答。')
            guide.setObjectName('keyword_quick_guide')
            guide.setWordWrap(True)
            guide.setTextFormat(Qt.TextFormat.PlainText)
            guide.setStyleSheet(f'color: {TEXT}; background: {CARD}; padding: 10px; border-radius: 8px;')
            outer.addWidget(guide)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        body = QWidget()
        body.setStyleSheet(f"background: {CARD}; border-radius: 10px;")
        form = QFormLayout(body)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        if self.plugin_id == 'keyword_trigger':
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        settings = meta.get("settings") or self._defaults
        groups: dict = {}
        fields = list(self._schema.items())
        if self.plugin_id == 'keyword_trigger':
            # 最常填写的词表放在开头，小屏幕也先看到填写入口。
            order = {'keywords': 0, 'mode': 1, 'includePersona': 2, 'keywordsEnabled': 3, 'enabled': 4}
            fields.sort(key=lambda item: order.get(item[0], 5))
        for key, spec in fields:
            if not isinstance(spec, dict):
                continue
            if spec.get('editor') == 'internal':
                continue
            group = spec.get("group") or ""
            if key not in settings and key in self._defaults:
                settings = dict(settings)
                settings[key] = self._defaults[key]
            widget = self._make_widget(spec, settings.get(key))
            self._widgets[key] = (widget, spec)
            if spec.get('editor') == 'admin_owners':
                widget.setReadOnly(True)
            field_widget = self._file_field(key,widget,spec) if spec.get('file_picker') else widget
            if self.plugin_id == 'keyword_trigger':
                field_widget = self._keyword_field(key, widget, spec)
            label = QLabel(spec.get("label") or key)
            tip = spec.get("description") or ""
            if tip:
                label.setToolTip(tip)
                widget.setToolTip(tip)
            row_label = QLabel(spec.get("label") or key)
            if tip:
                row_label.setToolTip(tip)
            if group:
                if group not in groups:
                    box = QGroupBox(group)
                    box.setStyleSheet(
                        f"QGroupBox {{ color: {TEXT}; border: 1px solid {BORDER};"
                        "border-radius: 8px; margin-top: 10px; padding: 10px; }}"
                        f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; color: {ACCENT}; }}"
                    )
                    inner = QFormLayout(box)
                    inner.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
                    groups[group] = inner
                    form.addRow(box)
                groups[group].addRow(row_label, field_widget)
            else:
                if spec.get('editor')=='keyword_rules':
                    form.addRow(row_label)
                    form.addRow(field_widget)
                else:
                    form.addRow(row_label, field_widget)

        if not self._schema:
            form.addRow(QLabel("这个插件没有可配置项。"))
        if self.plugin_id == 'keyword_trigger':
            self.keyword_summary = QLabel()
            self.keyword_summary.setObjectName('keyword_summary')
            self.keyword_summary.setWordWrap(True)
            self.keyword_summary.setTextFormat(Qt.TextFormat.PlainText)
            priority = QLabel('注意：人设页若填写了专属“关键词”，会优先使用人设词表。想使用本页词表，请将人设页的“关键词”留空。群监控名单和其他回复限制仍然生效。')
            priority.setWordWrap(True)
            priority.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
            form.addRow(priority)
            for widget, spec in self._widgets.values():
                signal = (widget.toggled if spec.get('type') == 'bool' else
                          widget.currentIndexChanged if spec.get('type') == 'select' else widget.textChanged)
                signal.connect(self._update_keyword_summary)
            self._update_keyword_summary()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        if self.plugin_id == 'keyword_trigger':
            outer.addWidget(self.keyword_summary)

        if self.plugin_id == 'group_monitor':
            select = QHBoxLayout()
            self.observed_groups = QComboBox()
            self.observed_groups.addItem('选择已经观察到的群…','')
            select.addWidget(self.observed_groups,1)
            add_group = QPushButton('加入监控名单')
            def append_group():
                target = self.observed_groups.currentData()
                if not target:
                    return
                widget = self._widgets['groups'][0]
                values = [v.strip() for v in widget.toPlainText().splitlines() if v.strip()]
                if target not in values:
                    values.append(target)
                widget.setPlainText('\n'.join(values))
                mode = self._widgets['mode'][0]
                mode.setCurrentIndex(mode.findData('selected'))
            add_group.clicked.connect(append_group)
            select.addWidget(add_group)
            outer.addLayout(select)
            def loaded_groups(result):
                for item in result.get('sessions',[]):
                    if item.get('kind') == 'group':
                        self.observed_groups.addItem(f"{item['name']} ({item['target']})",item['target'])
            call_api(self,'GET','management/sessions',timeout=10,on_done=loaded_groups)

        note = QLabel("鼠标悬停在标签上可以看该项的说明。修改后点「保存设置」立即生效。")
        if self.plugin_id == 'keyword_trigger':
            note.setText('示例只修改当前表单；点击“保存设置”后才生效，取消不会修改原配置。')
            note.setWordWrap(True)
        note.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        outer.addWidget(note)

        buttons = QDialogButtonBox()
        btn_save = buttons.addButton("保存设置", QDialogButtonBox.ButtonRole.AcceptRole)
        btn_reset = buttons.addButton("恢复默认", QDialogButtonBox.ButtonRole.ResetRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        btn_save.clicked.connect(self._save)
        btn_reset.clicked.connect(self._reset)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _open_received_images(self):
        try:
            folder=ROOT/'data/received_images'
            folder.mkdir(parents=True,exist_ok=True)
            os.startfile(str(folder))
        except OSError:
            QMessageBox.warning(self,'无法打开储藏区','请检查本机数据目录的权限和磁盘空间。')

    def _keyword_field(self, key, widget, spec):
        """只改善唤醒设置的可读性，所有值仍走原来的 schema 和保存接口。"""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        labels = {'enabled': '使用本页设置', 'keywordsEnabled': '允许通过关键词唤醒（@ 也可以）',
                  'includePersona': '把人设名称和追加昵称也作为唤醒词'}
        if key in labels:
            widget.setText(labels[key])
        if key == 'mode':
            widget.setStyleSheet(f'QComboBox {{ background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 28px 4px 6px; }} QComboBox::drop-down {{ border: none; width: 24px; }} QComboBox::down-arrow {{ image: none; }}')
            indicator = QLabel(widget)
            indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            indicator.setStyleSheet('background: transparent;')
            chevron = QPixmap(12, 9); chevron.fill(Qt.GlobalColor.transparent)
            painter = QPainter(chevron); painter.setPen(QPen(QColor(TEXT), 2))
            painter.drawLine(2, 2, 6, 6); painter.drawLine(6, 6, 10, 2); painter.end()
            indicator.setPixmap(chevron)
            arrow = QHBoxLayout(widget); arrow.setContentsMargins(0, 0, 8, 0)
            arrow.addStretch(); arrow.addWidget(indicator)
        if key == 'keywords':
            example = QPushButton('添加示例')
            self._keyword_examples = example
            example.setObjectName('keyword_examples')
            example.setStyleSheet(f'background: {ACCENT}; color: {ACCENT_FG}; border: none; border-radius: 6px; padding: 6px 10px;')
            example.clicked.connect(self._append_keyword_examples)
            row = QHBoxLayout()
            label = QLabel('示例：鲸鱼娘、小鲸鱼、帮我看看')
            label.setWordWrap(True)
            row.addWidget(label, 1); row.addWidget(example)
            layout.addLayout(row)
            widget.setPlaceholderText('填写你希望群友使用的称呼，例如“小鲸鱼”；每行一个。')
            widget.setMinimumHeight(105)
            widget.setMaximumHeight(115)
        layout.addWidget(widget)
        if spec.get('description'):
            hint = QLabel(spec['description'])
            hint.setWordWrap(True)
            hint.setObjectName('keyword_help_' + key)
            hint.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
            layout.addWidget(hint)
        return box

    def _append_keyword_examples(self):
        from src.text_matching import configured_terms
        widget = self._widgets['keywords'][0]
        current = widget.toPlainText()
        words = {str(word).strip() for word in configured_terms(current)}
        additions = [word for word in ('鲸鱼娘', '小鲸鱼', '帮我看看') if word not in words]
        if additions:
            widget.setPlainText(current.rstrip() + ('\n' if current.strip() else '') + '\n'.join(additions))

    def _update_keyword_summary(self, *_):
        from src.text_matching import configured_terms
        values = self.values()
        policy_enabled = bool(values.get('enabled'))
        keywords_enabled = policy_enabled and bool(values.get('keywordsEnabled'))
        # 禁用不相关的输入但保留草稿，避免用户填写后误以为关闭的规则也会生效。
        self._widgets['keywordsEnabled'][0].setEnabled(policy_enabled)
        for key in ('mode', 'includePersona', 'keywords'):
            self._widgets[key][0].setEnabled(keywords_enabled)
        self._keyword_examples.setEnabled(keywords_enabled)
        words = list(dict.fromkeys(word.strip() for word in configured_terms(values.get('keywords', '')) if word.strip()))
        invalid = [word for word in words if len(word) < 2]
        if invalid:
            summary = '请修改：' + '、'.join(invalid[:5]) + '。每个唤醒词至少需要 2 个字符。'
        elif not values.get('enabled'):
            summary = '保存后：本页策略关闭，恢复原来的唤醒规则；机器人不会因此停止。'
        elif not values.get('keywordsEnabled'):
            summary = '保存后：群聊 AI 只由 @ 唤醒。关键词固定回复单独设置。'
        else:
            sources = ['原有词表'] if values.get('mode') == 'append' else []
            if words:
                sources.append(f'本页 {len(words)} 个词（' + '、'.join(words[:4]) + ('…' if len(words)>4 else '') + '）')
            if values.get('includePersona'):
                sources.append('人设名称及追加昵称')
            summary = '保存后：' + (' + '.join(sources) + '，或 @ 唤醒。' if sources else '没有通用唤醒词，群聊需 @ 唤醒。')
            summary += ' 人设专属词表如有设置，仍会优先生效。'
        self.keyword_summary.setText(summary)
        self.keyword_summary.setStyleSheet(f'color: {"#C93D45" if invalid else ACCENT}; font-size: 12px;')

    def _file_field(self,key,editor,spec):
        container=QWidget()
        layout=QVBoxLayout(container)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(6)
        mode=spec.get('file_picker')
        images=bool(spec.get('file_extensions')) and set(spec['file_extensions'])<=IMAGE_EXTENSIONS
        button=QPushButton('选择文件夹…' if mode=='directory' else '上传图片…' if images else '上传文件…')
        button.setObjectName('upload_'+key)
        button.setStyleSheet(f'background: {ACCENT}; color: #FFFFFF; border: none; border-radius: 7px; padding: 8px 16px; font-weight: bold;')
        button.clicked.connect(lambda checked=False,k=key:self._choose_plugin_files(k))
        self._file_buttons[key]=button
        controls=QHBoxLayout()
        controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)
        if spec.get('description'):
            hint=QLabel(spec['description'])
            hint.setWordWrap(True)
            hint.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
            layout.addWidget(hint)
        layout.addWidget(editor,1)
        return container

    def _choose_plugin_files(self,key):
        editor,spec=self._widgets[key]
        try:
            mode,extensions,_=upload_options(spec)
            if mode=='directory':
                path=QFileDialog.getExistingDirectory(self,'选择插件使用的文件夹')
                if path:
                    editor.setText(str(Path(path).resolve()))
                return
            file_filter=('支持的文件 ('+' '.join('*'+v for v in extensions)+')') if extensions else '所有文件 (*)'
            if mode=='file':
                path,_=QFileDialog.getOpenFileName(self,'上传插件文件','',file_filter)
                paths=[path] if path else []
            else:
                paths,_=QFileDialog.getOpenFileNames(self,'上传插件文件（可多选）','',file_filter)
            if not paths:
                return
            items=import_plugin_files(ROOT/'data/plugin_uploads',self.plugin_id,key,paths,spec)
            value=merge_uploaded_files(self._read_widget(editor,spec),items,mode)
            if spec.get('multiline') or spec.get('type')=='text' or mode in {'files','labeled_files'}:
                editor.setPlainText(value)
            else:
                editor.setText(value)
        except (OSError,ValueError) as exc:
            QMessageBox.warning(self,'上传失败',str(exc))

    def _make_widget(self, spec: dict, value):
        if spec.get('editor')=='keyword_rules':
            return KeywordRulesEditor(value)
        kind = str(spec.get("type") or "string")
        if kind == "bool":
            widget = QCheckBox("启用")
            widget.setChecked(bool(value))
            return widget
        if kind == "select":
            widget = QComboBox()
            values = list(spec.get("values") or [])
            options = spec.get("options") or []
            for option in options:
                if isinstance(option, dict):
                    widget.addItem(str(option.get("label", option.get("value", ""))), option.get("value"))
            if not options:
                for item in values:
                    widget.addItem(str(item), item)
            index = widget.findData(value)
            if index < 0:
                index = widget.findText(str(value))
            if index >= 0:
                widget.setCurrentIndex(index)
            widget.setStyleSheet(f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px;")
            return widget
        if kind in ("int", "float"):
            if kind == "int":
                widget = QSpinBox()
                widget.setRange(int(spec.get("min", -10 ** 9) or -10 ** 9), int(spec.get("max", 10 ** 9) or 10 ** 9))
            else:
                widget = QDoubleSpinBox()
                widget.setRange(float(spec.get("min", -10 ** 9) or -10 ** 9), float(spec.get("max", 10 ** 9) or 10 ** 9))
                widget.setDecimals(3)
            if spec.get("step"):
                widget.setSingleStep(spec["step"])
            try:
                widget.setValue(value if value is not None else 0)
            except (TypeError, ValueError):
                pass
            widget.setStyleSheet(f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px;")
            return widget
        if kind == "text" or spec.get("multiline") or spec.get('file_picker') in {'files','labeled_files'}:
            widget = QTextEdit()
            widget.setPlainText("" if value is None else str(value))
            widget.setMinimumHeight(90)
            widget.setStyleSheet(f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px;")
            return widget
        widget = QLineEdit()
        if isinstance(value, (list, tuple)):
            widget.setText(", ".join(str(v) for v in value))
        else:
            widget.setText("" if value is None else str(value))
        widget.setStyleSheet(f"background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px;")
        return widget

    def _read_widget(self, widget, spec: dict):
        if spec.get('editor')=='keyword_rules':
            return widget.value()
        kind = str(spec.get("type") or "string")
        if kind == "bool":
            return widget.isChecked()
        if kind == "select":
            data = widget.currentData()
            return data if data is not None else widget.currentText()
        if kind == "int":
            return int(widget.value())
        if kind == "float":
            return float(widget.value())
        if kind == "text" or spec.get("multiline") or spec.get('file_picker') in {'files','labeled_files'}:
            return widget.toPlainText()
        return widget.text()

    def values(self) -> dict:
        result = {}
        for key, (widget, spec) in self._widgets.items():
            result[key] = self._read_widget(widget, spec)
        return result

    def _save(self):
        # 不要把 plugin_id 放进 body：后端 update_settings 是**严格 schema 校验**，
        # 任何不在该插件 config_schema 里的键都会抛
        # `unknown setting field 'plugin_id'`，导致保存整体失败。
        # 插件 id 已经在 URL 里了。
        try:
            payload = self.values()
            if self.plugin_id == 'keyword_trigger':
                from src.text_matching import configured_terms
                if any(len(word.strip()) == 1 for word in configured_terms(payload.get('keywords', ''))):
                    raise ValueError('每个唤醒词至少需要 2 个字符，例如“小鲸鱼”；多个词请分行填写。')
            # 旧关键词客户端只提交 enabled/rules；新增选项未修改时继续使用增量保存。
            if self.plugin_id == 'keyword_reply':
                for key in ('scope','match'):
                    if payload.get(key) == self._initial_settings.get(key,self._defaults.get(key)):
                        payload.pop(key,None)
        except ValueError as exc:
            QMessageBox.warning(self,'请检查填写的内容',str(exc))
            return
        call_api(self, "POST", f"plugins/{self.plugin_id}", payload, timeout=20,
                 on_done=self._apply_save)

    def _apply_save(self, result):
        if not result.get("success"):
            QMessageBox.warning(self, "保存失败", str(result.get("message") or result))
            return
        self.saved = True
        self.accept()

    def _reset(self):
        for key, (widget, spec) in self._widgets.items():
            kind = str(spec.get("type") or "string")
            default = self._defaults.get(key, spec.get("default"))
            if spec.get('editor') == 'admin_owners':
                continue
            if spec.get('editor')=='keyword_rules':
                widget.set_rules(default)
            elif kind == "bool":
                widget.setChecked(bool(default))
            elif kind == "select":
                index = widget.findData(default)
                if index >= 0:
                    widget.setCurrentIndex(index)
            elif kind in ("int", "float"):
                try:
                    widget.setValue(default if default is not None else 0)
                except (TypeError, ValueError):
                    pass
            elif kind == "text" or spec.get("multiline") or spec.get('file_picker') in {'files','labeled_files'}:
                widget.setPlainText("" if default is None else str(default))
            else:
                widget.setText("" if default is None else str(default))


class PluginInstallDialog(QDialog):
    """从网址/仓库安装插件。

    ⚠️ 安全设计：远程安装等于**把外部代码装进程序、并且会被 import 执行**。
    所以这里刻意分成两步，中间必须由**用户点确认**：

    1. 「检查」→ 后端只下载并检查（不写盘），把结果、来源、大小、命中的危险用法
       展示在下面；
    2. 「确认安装」→ 才真正落盘并加载。

    检查不通过时"确认安装"按钮保持禁用，用户没法绕过。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加插件")
        self.setWindowIcon(slot_icon("dialog"))
        self.resize(720, 640)
        self.installed = False
        self._inspection = None
        self.setStyleSheet(DIALOG_STYLESHEET)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(10)
        outer.addWidget(icon_row("安装插件", "btn_new"))

        # 用 HTML 加粗，不要写 Markdown 的 ** —— QLabel 不解析 Markdown，
        # 星号会原样显示出来（之前就是这样）。
        warn = QLabel(
            "注意：插件是<b>外部代码</b>，安装后会被本程序加载并执行。"
            "请只安装你信任的来源。安装前请先点「检查」看清它是什么。")
        warn.setWordWrap(True)
        warn.setTextFormat(Qt.TextFormat.RichText)
        warn.setStyleSheet(f"color: {ERROR}; background: rgba(200,60,60,18);"
                           f" padding: 10px; border-radius: 6px; font-size: 12px;")
        outer.addWidget(warn)

        form = QFormLayout()
        form.setSpacing(8)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://…/plugin.py、https://…/plugin.zip 或 GitHub 仓库地址")
        self.url.setAccessibleName("插件地址")
        form.addRow("网址安装", self.url)
        # 本地文件：给"浏览…"按钮，不让用户手打路径（打错的失败最难查）
        local_row = QHBoxLayout()
        local_row.setSpacing(8)
        self.local_path = QLineEdit()
        self.local_path.setPlaceholderText("选择本地的 .py 或 .zip 插件文件")
        self.local_path.setAccessibleName("本地插件文件")
        local_row.addWidget(self.local_path, 1)
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setProperty("class", "secondary")
        self.btn_browse.clicked.connect(self._browse)
        local_row.addWidget(self.btn_browse)
        form.addRow("本地文件", local_row)
        self.plugin_id = QLineEdit()
        self.plugin_id.setPlaceholderText("留空则按文件名自动决定")
        self.plugin_id.setAccessibleName("插件标识")
        form.addRow("插件标识", self.plugin_id)
        outer.addLayout(form)

        actions = QHBoxLayout()
        self.btn_check = QPushButton("检查")
        self.btn_check.setProperty("class", "secondary")
        self.btn_check.clicked.connect(self._check)
        actions.addWidget(self.btn_check)
        self.btn_install = QPushButton("确认安装")
        self.btn_install.setEnabled(False)
        self.btn_install.clicked.connect(self._install)
        actions.addWidget(self.btn_install)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setProperty("class", "secondary")
        self.btn_close.clicked.connect(self.reject)
        actions.addWidget(self.btn_close)
        actions.addStretch(1)
        outer.addLayout(actions)

        self.result = QLabel("还没有检查。先点「检查」看看这个插件是什么。")
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        self.result.setStyleSheet(f"color: {TEXT}; font-size: 12px;")
        outer.addWidget(self.result)

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("这里会显示插件源码前 40 行")
        self.preview.setStyleSheet(
            f"QTextEdit {{ background: {FIELD}; border: 1px solid {BORDER};"
            f" border-radius: 8px; padding: 8px; font-family: Consolas, monospace; }}")
        outer.addWidget(self.preview, 1)

    def _source(self) -> tuple:
        """当前要装的是哪个来源？返回 ``("url"|"local", value)``。

        两个输入框都填了时以**本地文件**优先（用户刚点了"浏览…"的意图更明确），
        并在结果里说明用了哪个，避免"我明明填了网址"这种困惑。
        """
        local = self.local_path.text().strip()
        if local:
            return "local", local
        return "url", self.url.text().strip()

    def _browse(self):
        """打开原生文件选择框。后端在同一台机器上，所以让它弹对话框拿准确路径。"""
        self.btn_browse.setEnabled(False)
        call_api(self, "POST", "plugins/browse", {}, timeout=180,
                 on_done=self._apply_browse)

    def _apply_browse(self, data):
        self.btn_browse.setEnabled(True)
        if not data.get("success"):
            self.result.setText(
                f"打开文件选择框失败：{data.get('message') or data.get('detail') or '未知原因'}")
            return
        path = str(data.get("path") or "")
        if not path:
            return                      # 用户取消
        self.local_path.setText(path)
        self.result.setText(f"已选择本地文件：{path}\n现在点「检查」看看它是什么。")

    def _check(self):
        kind, value = self._source()
        if not value:
            self.result.setText("请填写插件网址，或用「浏览…」选择一个本地插件文件。")
            return
        self.btn_check.setEnabled(False)
        self.btn_install.setEnabled(False)
        self.result.setText("正在检查…")

        if kind == "local":
            endpoint = "plugins/inspect-local"
            payload = {"path": value, "plugin_id": self.plugin_id.text().strip()}
        else:
            endpoint = "plugins/inspect"
            payload = {"url": value, "plugin_id": self.plugin_id.text().strip()}

        call_api(self, "POST", endpoint, payload, timeout=60,
                 on_done=lambda data: self._apply_check(kind, data))

    def _apply_check(self, kind: str, data):
        self.btn_check.setEnabled(True)

        if not data.get("ok"):
            self._inspection = None
            message = str(data.get("message") or data.get("detail") or "检查失败")
            # 纯文本：后端消息里可能有 < > 等字符，别被当 HTML 解析
            self.result.setTextFormat(Qt.TextFormat.PlainText)
            self.result.setText(f"检查未通过：{message}")
            self.preview.setPlainText("")
            return

        self._inspection = data
        source_label = "本地文件" if kind == "local" else "网址"
        if data.get("problems"):
            # HTML 加粗（QLabel 不解析 Markdown）；问题列表也用等宽列表排版
            self.result.setTextFormat(Qt.TextFormat.RichText)
            self.result.setText(
                f"<b>安全检查未通过</b>（来源：{source_label}），不允许安装：<br>"
                + "<br>".join(f"&nbsp;&nbsp;· {p}" for p in data["problems"]))
            self.btn_install.setEnabled(False)
        else:
            self.result.setTextFormat(Qt.TextFormat.PlainText)
            self.result.setText(
                f"检查通过，可以安装。（来源：{source_label}）\n"
                f"  类型：{'压缩包' if data.get('kind') == 'archive' else '单个 .py 文件'}\n"
                f"  实际来源：{data.get('resolved_url')}\n"
                f"  插件标识：{data.get('plugin_id')}\n"
                f"  大小：{data.get('bytes', 0) // 1024} KB，约 {data.get('lines')} 行\n"
                f"  确认无误后再点「确认安装」。")
            self.btn_install.setEnabled(True)
            if not self.plugin_id.text().strip():
                self.plugin_id.setText(str(data.get("plugin_id") or ""))
        self.preview.setPlainText(str(data.get("preview") or ""))

    def _install(self):
        if not self._inspection:
            return
        kind, value = self._source()
        confirmed = QMessageBox.question(
            self, "确认安装",
            f"即将安装插件「{self._inspection.get('plugin_id')}」。\n\n"
            "它来自" + ("本地文件" if kind == "local" else "外部网址")
            + "，安装后会随程序一起加载执行。\n确定要继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        self.btn_install.setEnabled(False)
        if kind == "local":
            endpoint = "plugins/install-local"
            payload = {"path": value, "plugin_id": self.plugin_id.text().strip(),
                       "confirmed": True, "overwrite": False}
        else:
            endpoint = "plugins/install"
            payload = {"url": value, "plugin_id": self.plugin_id.text().strip(),
                       "confirmed": True, "overwrite": False}
        self.result.setText("正在安装…")
        call_api(self, "POST", endpoint, payload, timeout=90, on_done=self._apply_install)

    def _apply_install(self, data):
        if not data.get("success"):
            message = str(data.get("message") or data.get("detail") or "安装失败")
            self.result.setText(f"安装失败：{message}")
            self.btn_install.setEnabled(True)
            return
        self.installed = True
        QMessageBox.information(self, "安装完成", str(data.get("message") or "已安装"))
        self.accept()


class AgentTab(QWidget):
    """Agent单列对话工作台；旧设置操作保留为显式模式。

    这里只做界面：能不能改、改成什么值全部由后端 `wechat_agent` 决定，
    界面不直接碰设置，也不存在任何"发消息"的入口（那是 Agent 的硬约束）。
    """

    def __init__(self):
        super().__init__()
        self.session_id = ''
        self.cards = []
        self._chat_busy = False
        self.attachments=[]
        self._history_epoch=0
        self._card_epoch=0
        self._workspace_key=None
        self._authority_epoch=0
        self._binding_scope=None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        title = QLabel("Agent · 主人工作台")
        title.setStyleSheet(f"font-size: 18px; font-weight: 600; color: {TEXT};")
        layout.addWidget(title)
        hint = QLabel("选一个人设开始对话。聊天保存在本机，使用 /记住 内容 保存长期记忆。"
                      "笔记与共享记忆在「主人工作区」管理；改设置请切换模式。这里聊天不会发往微信。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED};")
        layout.addWidget(hint)

        row = QHBoxLayout()
        self.sessions = QComboBox()
        self.sessions.setMinimumWidth(220)
        self.sessions.currentIndexChanged.connect(self._session_changed)
        row.addWidget(self.sessions, 1)
        for label, slot in (("新建会话", self._new_session), ("删除会话", self._delete_session)):
            button = QPushButton(label)
            button.setProperty("class", "secondary")
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)

        binding_row = QHBoxLayout()
        self.authority_status = QLabel('Agent修改操作需要先绑定管理员')
        self.authority_status.setWordWrap(True)
        binding_row.addWidget(self.authority_status, 1)
        binding_button = QPushButton('管理员与DSH接入')
        binding_button.clicked.connect(self._binding)
        binding_row.addWidget(binding_button)
        dsh_button = QPushButton('DSH对话引擎')
        def open_dsh():
            from src.gui.dsh_panel import DshDialog
            DshDialog(call_api, self).exec()
        dsh_button.clicked.connect(open_dsh)
        binding_row.addWidget(dsh_button)
        dsh_connect_button = QPushButton('DSH一键连接')
        dsh_connect_button.setToolTip('自动写入接入脚本与 DSH 配置，让新建的会话直接能用 whale_wechat 工具')
        def open_dsh_connect():
            from src.gui.maintenance_dialogs import DshConnectDialog
            DshConnectDialog(self, api_get, api_post).exec()
        dsh_connect_button.clicked.connect(open_dsh_connect)
        binding_row.addWidget(dsh_connect_button)
        layout.addLayout(binding_row)

        # 只读一行：最近一轮 DSH 记忆注入统计（条数/字数，不含正文），跟随下面的管理员刷新。
        self.dsh_memory = QLabel('DSH记忆注入：绑定管理员后显示最近一轮统计')
        self.dsh_memory.setWordWrap(True)
        self.dsh_memory.setTextFormat(Qt.TextFormat.PlainText)
        self.dsh_memory.setStyleSheet(f'color: {MUTED}; font-size: 11px;')
        layout.addWidget(self.dsh_memory)

        mode_row = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem('人设对话', 'chat')
        self.mode.addItem('修改控制台设置', 'settings')
        self.personas = QComboBox()
        self.personas.addItem('当前人设', '')
        self.personas.currentIndexChanged.connect(lambda *_: self._reload_chat())
        mode_row.addWidget(self.mode)
        mode_row.addWidget(self.personas, 1)
        workspace_button = QPushButton('主人工作区')
        def open_workspace():
            from src.gui.agent_workspace import WorkspaceDialog
            WorkspaceDialog(call_api, self).exec()
        workspace_button.clicked.connect(open_workspace)
        mode_row.addWidget(workspace_button)
        layout.addLayout(mode_row)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet(f"background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px;")
        left_layout.addWidget(self.log)
        self.input = QLineEdit()
        self.input.setPlaceholderText("和当前人设聊天，或输入 /记住 我喜欢……")
        self.input.returnPressed.connect(self._plan)
        left_layout.addWidget(self.input)
        attachment_row=QHBoxLayout()
        self.attach_button=QPushButton('文件 / 语音')
        self.attach_button.clicked.connect(self._attachments)
        attachment_row.addWidget(self.attach_button)
        self.attachment_status=QLabel('本轮未附加资料')
        self.attachment_status.setWordWrap(True);self.attachment_status.setTextFormat(Qt.TextFormat.PlainText)
        attachment_row.addWidget(self.attachment_status,1)
        self.clear_attachments=QPushButton('取消附加')
        self.clear_attachments.clicked.connect(lambda:self._set_attachments([]))
        attachment_row.addWidget(self.clear_attachments);left_layout.addLayout(attachment_row)
        self.send = QPushButton("发送给 Agent")
        self.send.clicked.connect(self._plan)
        left_layout.addWidget(self.send)
        layout.addWidget(left, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_view = QListWidget()
        self.cards_view.hide()
        self.cards_view.setStyleSheet(f"background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px;")
        # 旧卡片保存在后台，右侧方案栏不再展示；最新设置操作在对话中说明。
        self.cards_view.setParent(self)
        actions = QHBoxLayout()
        self.btn_apply = QPushButton("应用")
        self.btn_apply.clicked.connect(lambda: self._act("apply"))
        self.btn_undo = QPushButton("撤销")
        self.btn_undo.setProperty("class", "secondary")
        self.btn_undo.clicked.connect(lambda: self._act("undo"))
        self.btn_schedule = QPushButton("定时任务")
        self.btn_schedule.setProperty("class", "secondary")
        self.btn_schedule.clicked.connect(lambda: ScheduleDialog(self).exec())
        for button in (self.btn_apply, self.btn_undo, self.btn_schedule):
            actions.addWidget(button)
        layout.addLayout(actions)
        right.deleteLater()
        split.deleteLater()
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed()

        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.result)

    # ── 数据 ────────────────────────────────────────────────────────────
    def refresh_sessions(self, select=None):
        self.refresh_authority()
        call_api(self, "GET", "agent/sessions", timeout=10,
                 on_done=lambda data: self._apply_sessions(data, select))

    def refresh_authority(self):
        self._authority_epoch+=1;epoch=self._authority_epoch
        def done(data):
            if epoch==self._authority_epoch:self._authority(data)
        call_api(self, 'GET', 'agent/authority', timeout=10, on_done=done)

    def _authority(self, data):
        bound = bool(data.get('bound'))
        admin = data.get('admin') or {}
        scope=(bound,data.get('revision'),str(sorted(admin.items())))
        changed=self._binding_scope is not None and scope!=self._binding_scope
        self._binding_scope=scope
        if not bound or changed:
            self._history_epoch+=1;self._card_epoch+=1
            self.log.clear();self.cards_view.clear();self.cards=[];self.input.clear();self._set_attachments([])
            self._workspace_key=''
        self.authority_status.setText(('管理员：'+admin.get('platform', '')+' / '+admin.get('subject', ''))
            if bound else data.get('message', '尚未绑定管理员：可预览方案，应用/撤销前请先绑定。'))
        self.btn_apply.setEnabled(bound)
        self.btn_undo.setEnabled(bound)
        if bound:
            def loaded(result):
                if scope==self._binding_scope:self._workspace_loaded(result)
            call_api(self, 'GET', 'agent/workspace', timeout=10, on_done=loaded)
        self._refresh_dsh_memory(bound)

    def _refresh_dsh_memory(self, bound=None):
        """最近一轮记忆注入统计；跟随管理员刷新走，不另起定时器，也不显示任何记忆正文。"""
        if bound is False:
            self.dsh_memory.setText('DSH记忆注入：绑定管理员后显示最近一轮统计')
            return
        def done(data):
            rows=(data or {}).get('items') or []
            last=rows[-1] if rows else None
            if not last:
                self.dsh_memory.setText('DSH记忆注入：还没有 DSH 回合记录')
                return
            self.dsh_memory.setText('DSH记忆注入：最近一轮 manual=%d notes=%d learned=%d archive=%d chars=%d empty=%d；共 %d 轮' % (
                int(last.get('manual') or 0),int(last.get('notes') or 0),int(last.get('learned') or 0),
                int(last.get('archive') or 0),int(last.get('chars') or 0),1 if last.get('empty') else 0,
                int((data or {}).get('total') or len(rows))))
        call_api(self,'GET','dsh/memory-injections',timeout=10,on_done=done)

    def _workspace_loaded(self, data):
        if not data.get('success'):
            return
        key=data.get('workspace')
        if self._workspace_key is not None and key!=self._workspace_key:
            self._history_epoch+=1;self._card_epoch+=1
            self.log.clear();self.cards_view.clear();self.cards=[];self.input.clear();self._set_attachments([])
        self._workspace_key=key
        selected = self.personas.currentData()
        self.personas.blockSignals(True)
        self.personas.clear()
        for row in data.get('personas') or []:
            self.personas.addItem(row.get('display_name') or row['name'],row['name'])
        target = selected or data.get('active_persona')
        self.personas.setCurrentIndex(max(0,self.personas.findData(target)))
        self.personas.blockSignals(False)
        self._reload_chat()

    def _mode_changed(self, *_):
        settings = self.mode.currentData() == 'settings'
        self.btn_apply.setVisible(settings)
        self.btn_undo.setVisible(settings)
        self.btn_schedule.setVisible(settings)
        self.send.setText('生成设置操作' if settings else '发送给 Agent')
        self.attach_button.setVisible(not settings)
        self.clear_attachments.setVisible(not settings)
        self.attachment_status.setVisible(not settings)
        self._reload_chat()

    def _reload_chat(self):
        self._history_epoch+=1;epoch=self._history_epoch
        if not self.session_id or self.mode.currentData() != 'chat':
            return
        from urllib.parse import urlencode
        sid, persona = self.session_id, self.personas.currentData() or ''
        def done(data):
            if epoch!=self._history_epoch or sid != self.session_id or persona != (self.personas.currentData() or '') or self.mode.currentData() != 'chat':
                return
            if data.get('success'):
                self.log.setPlainText('\n\n'.join(('我：' if r['role']=='user' else 'Agent：')+r['content']
                    for r in data.get('messages') or []))
            else:
                self.result.setText(str(data.get('detail') or data.get('message') or '读取对话失败，请稍后刷新。'))
        call_api(self, 'GET', 'agent/history?'+urlencode({'session_id':sid,'persona':persona}), timeout=10,on_done=done)

    def _binding(self):
        from src.gui.agent_binding import AgentBindingDialog
        AgentBindingDialog(call_api, self).exec()
        self.refresh_authority()

    def _apply_sessions(self, data, select=None):
        if not data.get('success'):
            self.result.setText(str(data.get('detail') or data.get('message') or '读取会话失败，请稍后刷新。'))
            return
        sessions = data.get("sessions") or []
        self.sessions.blockSignals(True)
        self.sessions.clear()
        for item in sessions:
            self.sessions.addItem(f"{item.get('title') or '会话'}",
                                  item["id"])
        self.sessions.blockSignals(False)
        if not sessions:
            self._new_session()
            return
        index = self.sessions.findData(select) if select else 0
        self.sessions.setCurrentIndex(max(0, index))
        self._session_changed()

    def _session_changed(self, *_):
        chosen=self.sessions.currentData() or ''
        if chosen!=self.session_id:
            self.log.clear();self._set_attachments([])
        self.session_id = chosen
        if self.session_id:
            self._reload_cards()
            self._reload_chat()

    def _new_session(self):
        def created(data):
            if not data.get('success'):
                self.result.setText(str(data.get('detail') or data.get('message') or '创建会话失败，请稍后重试。'))
                return
            self.refresh_sessions((data.get('session') or {}).get('id'))
        call_api(self, "POST", "agent/sessions", {"title": "新会话"}, timeout=10,
                 on_done=created)

    def _delete_session(self):
        if not self.session_id:
            return
        call_api(self, "DELETE", f"agent/sessions/{self.session_id}", None, timeout=10,
                 on_done=lambda data: self.refresh_sessions())

    def _reload_cards(self):
        if not self.session_id:
            return
        self._card_epoch+=1;epoch=self._card_epoch;sid=self.session_id
        def done(data):
            if epoch==self._card_epoch and sid==self.session_id:self._apply_cards(data)
        call_api(self, "GET", f"agent/sessions/{self.session_id}", timeout=10,on_done=done)

    def _set_attachments(self,rows):
        self.attachments=list(rows)
        self.attachment_status.setText('、'.join(r['name'] for r in self.attachments) if self.attachments else '本轮未附加资料')

    def _attachments(self):
        from src.gui.agent_attachments import AttachmentsDialog
        sid,key=self.session_id,self._workspace_key
        dialog=AttachmentsDialog(call_api,self.attachments,self)
        if dialog.exec()==QDialog.DialogCode.Accepted and sid==self.session_id and key==self._workspace_key:
            self._set_attachments(dialog.chosen)

    def _apply_cards(self, data):
        session = data.get("session") or {}
        self.cards = session.get("cards") or []
        self.cards_view.clear()
        for card in self.cards:
            labels = "；".join(a.get("label") or a.get("action") for a in (card.get("actions") or []))
            state = {"preview": "待应用", "applied": "已应用", "undone": "已撤销", "answered": "已回答"}.get(
                card.get("state"), card.get("state") or "")
            item = QListWidgetItem(f"{card.get('title') or '方案'}［{state}］\n{card.get('reply') or labels}")
            item.setData(Qt.ItemDataRole.UserRole, card.get("id"))
            self.cards_view.addItem(item)
        if self.cards_view.count():
            self.cards_view.setCurrentRow(self.cards_view.count()-1)

    def _plan(self):
        if self._chat_busy:return
        text = self.input.text().strip()
        if not text and not (self.attachments and self.mode.currentData()=='chat'):
            return
        if not self.session_id:
            self.result.setText("正在创建会话…")
            return
        if self.mode.currentData() == 'chat':
            if self._chat_busy:
                return
            import uuid
            self._chat_busy = True
            self._history_epoch+=1
            self.send.setEnabled(False)
            sid = self.session_id
            persona = self.personas.currentData() or ''
            key=self._workspace_key
            files=list(self.attachments)
            from PyQt6.QtGui import QTextCursor
            self.log.moveCursor(QTextCursor.MoveOperation.End)
            self.log.insertPlainText('\n\n我：'+(text or '请根据附件内容分析并回答。')+(' [附件：'+ '、'.join(r['name'] for r in files)+']' if files else ''))
            self.input.clear()
            self._set_attachments([])
            self.result.setText('Agent正在回答…')
            def done(data):
                self._chat_busy = False
                self.send.setEnabled(True)
                if key!=self._workspace_key or sid != self.session_id or persona != (self.personas.currentData() or '') or self.mode.currentData()!='chat':
                    return
                if data.get('success'):
                    self.result.setText('对话已保存到本地主人工作区。')
                    context=data.get('context')
                    if isinstance(context,dict):
                        self.result.setText(f"已保存：上下文 {context.get('history_turns',0)} 轮，相关旧对话 {context.get('recalled',0)} 条，"
                            f"记忆 {context.get('memories',0)} 条；历史输入 {context.get('chars',0)}/{context.get('max_chars',8000)} 字。")
                    self._reload_chat()
                else:
                    self.result.setText(str(data.get('message') or data.get('detail') or '本轮未完成；请检查记录，不自动重试。'))
            call_api(self, 'POST', 'agent/chat', {'session_id':sid,'request_id':uuid.uuid4().hex,
                'text':text,'persona':persona,'attachments':[r['id'] for r in files]}, timeout=70, on_done=done)
            return
        from PyQt6.QtGui import QTextCursor
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        self.log.insertPlainText('\n\n我：'+text)
        self.input.clear()
        sid=self.session_id;key=self._workspace_key
        def planned(data):
            if sid==self.session_id and key==self._workspace_key and self.mode.currentData()=='settings':self._planned(data)
        call_api(self, "POST", "agent/plan",
                 {"session_id": self.session_id, "text": text}, timeout=20,
                 on_done=planned)

    def _planned(self, data):
        if not data.get("success"):
            self.result.setText(f"没能生成方案：{data.get('message') or '未知原因'}")
            return
        card = data.get("card") or {}
        from PyQt6.QtGui import QTextCursor
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        self.log.insertPlainText('\n\nAgent：' + (card.get('reply') or "；".join(a.get("label") or "" for a in (card.get("actions") or []))))
        self.result.setText("已回答，无需应用。" if card.get('state') == 'answered' else "方案已生成，点「应用」执行；可用「撤销」恢复。")
        self._reload_cards()

    def _act(self, action):
        item = self.cards_view.currentItem()
        if item is None or not self.session_id:
            self.result.setText("请先生成设置方案")
            return
        card_id = item.data(Qt.ItemDataRole.UserRole)
        sid,key=self.session_id,self._workspace_key
        def acted(data):
            if sid==self.session_id and key==self._workspace_key:self._acted(action,data)
        call_api(self, "POST", f"agent/{action}",
                 {"session_id": self.session_id, "card_id": card_id}, timeout=30,
                 on_done=acted)

    def _acted(self, action, data):
        if not data.get("success"):
            self.result.setText(f"{'应用' if action == 'apply' else '撤销'}失败："
                                f"{data.get('message') or '未知原因'}")
            return
        self.result.setText("已应用。想还原就点「撤销」。" if action == "apply" else "已撤销。")
        self._reload_cards()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.sessions.count():
            self.refresh_sessions()


class ScheduleDialog(QDialog):
    """定时任务面板：建/跑/启停/删。动作只有连接检查与本地日报，都不会发微信。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(DIALOG_STYLESHEET)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle('定时任务（只写本地报告，不发微信）')
        self.resize(620, 420)
        layout = QVBoxLayout(self)
        hint = QLabel('只有两种动作：连接检查（只读状态）、每日简报（写到 data/agent_reports/）。'
                      '任何定时任务都不会向微信发送消息。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        for label, slot in (('新建连接检查', lambda: self._create('connection_check')),
                            ('新建日报', lambda: self._create('daily_digest')),
                            ('立即运行', self._run), ('启用/停用', self._toggle),
                            ('删除', self._delete)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)
        self.result_label = QLabel('')
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        self.reload()

    def _call(self, method, path, body, done):
        call_api(self, method, path, body, timeout=60, on_done=done)

    def reload(self):
        self._call('GET', 'agent/schedules', None, self._apply)

    def _apply(self, data):
        self.list.clear()
        for task in data.get('tasks') or []:
            text = (f"{task.get('name')} · {task.get('action')} · 每 {task.get('every_minutes')} 分钟"
                    f" · {'启用' if task.get('enabled') else '停用'}")
            if task.get('last_result'):
                text += f"\n   上次：{str(task['last_result'])[:80]}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, task.get('id'))
            self.list.addItem(item)
        if not self.list.count():
            self.result_label.setText('还没有定时任务。点「新建日报」每天写一份本地简报。')

    def _current(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ''

    def _create(self, action):
        name, ok = QInputDialog.getText(self, '新建任务', '任务名称：',
                                        text='每日简报' if action == 'daily_digest' else '连接检查')
        if not ok or not name.strip():
            return
        minutes, ok = QInputDialog.getInt(self, '间隔', '每隔多少分钟运行：', 60, 5, 1440)
        if not ok:
            return
        self._call('POST', 'agent/schedules',
                   {'name': name.strip(), 'action': action, 'every_minutes': minutes},
                   lambda data: (self.result_label.setText(
                       str(data.get('message') or '已创建' if data.get('success') else data.get('message') or '创建失败')),
                                 self.reload()))

    def _run(self):
        task_id = self._current()
        if not task_id:
            return
        self._call('POST', 'agent/schedules/run', {'task_id': task_id},
                   lambda data: (self.result_label.setText(str(data.get('result') or data.get('message') or '已运行')),
                                 self.reload()))

    def _toggle(self):
        task_id = self._current()
        if task_id:
            self._call('POST', 'agent/schedules/toggle', {'task_id': task_id},
                       lambda data: self.reload())

    def _delete(self):
        task_id = self._current()
        if task_id:
            self._call('DELETE', f'agent/schedules/{task_id}', None, lambda data: self.reload())


class PluginTab(QWidget):
    """Searchable plugin catalogue with filters and confirmed toggle state."""
    CATEGORIES = {
        "management": "机器人管理",
        "rhythm": "回复节奏", "message": "消息处理", "social": "互动交流",
        "personality": "个性表达", "intelligence": "智能判断",
        "media": "图片与媒体", "style": "回复样式", "utility": "实用工具",
    }

    def __init__(self):
        super().__init__()
        self._rows = {}
        self._plugins = []
        self._loaded = False
        self._loading = False
        self._load_id = 0
        self._pending_management = None
        # 用户自己装的插件（只有这些允许删除；内置插件永远不在这个集合里）
        self._custom_ids = set()
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        heading = QHBoxLayout()
        title = QLabel("插件中心")
        title.setStyleSheet(f"font-size: 21px; font-weight: bold; color: {TEXT};")
        heading.addWidget(title)
        heading.addStretch()
        self.summary = QLabel("等待加载")
        self.summary.setStyleSheet(f"color: {MUTED};")
        heading.addWidget(self.summary)
        self.btn_add = decorate_button(QPushButton("添加插件"), "btn_new")
        self.btn_add.setToolTip("从网址或 GitHub 仓库安装插件")
        self.btn_add.clicked.connect(self._add_plugin)
        heading.addWidget(self.btn_add)
        self.btn_batch = QPushButton("批量导入…")
        self.btn_batch.setToolTip("一次选多个插件文件或整个文件夹；先预检再安装")
        self.btn_batch.clicked.connect(self._batch_import)
        heading.addWidget(self.btn_batch)
        self.btn_reload = QPushButton("刷新列表")
        self.btn_reload.setProperty("class", "secondary")
        self.btn_reload.clicked.connect(self.refresh)
        heading.addWidget(self.btn_reload)
        self.btn_packs = QPushButton("插件包")
        self.btn_packs.setToolTip("查看/安装/移除独立插件包（一个文件夹就是一个插件）")
        self.btn_packs.clicked.connect(self._open_packs)
        heading.addWidget(self.btn_packs)
        self.btn_retry_plugins = QPushButton('重试故障插件')
        self.btn_retry_plugins.setToolTip('先停止机器人；修复插件后重新加载，保留原配置与聊天记录')
        self.btn_retry_plugins.clicked.connect(self._retry_failed_plugins)
        heading.addWidget(self.btn_retry_plugins)
        root.addLayout(heading)

        hint = QLabel("按功能查找插件，开关立即生效；详细参数可在“配置”中调整。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED};")
        root.addWidget(hint)

        management = QHBoxLayout()
        self.btn_sessions = QPushButton('会话监控与管理员')
        self.btn_sessions.clicked.connect(self._show_sessions)
        management.addWidget(self.btn_sessions)
        for plugin_id,label in [('private_replies','私聊管理'),('keyword_trigger','唤醒关键词'),
                                ('keyword_reply','关键词固定回复'),('group_monitor','群聊监控'),
                                ('reply_preferences','称呼与速度')]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False,pid=plugin_id:self._open_management(pid))
            management.addWidget(button)
        root.addLayout(management)

        library_row = QHBoxLayout()
        self.btn_sticker_library = QPushButton('表情包管理 · 机器人本地图库')
        self.btn_sticker_library.setToolTip('查看/上传/改标签/AI 识别/删除自动收集与本地上传的表情图')
        self.btn_sticker_library.clicked.connect(self.open_sticker_library)
        library_row.addWidget(self.btn_sticker_library)
        library_row.addStretch()
        root.addLayout(library_row)

        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索插件名称、功能或标识…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索插件")
        filters.addWidget(self.search, 1)
        self.category_filter = QComboBox()
        self.category_filter.addItem("全部分类", "")
        self.category_filter.setAccessibleName("插件分类")
        self.category_filter.setMinimumWidth(125)
        filters.addWidget(self.category_filter)
        self.state_filter = QComboBox()
        for label, value in (("全部状态", "all"), ("已启用", "enabled"), ("已停用", "disabled"),('已移除','removed')):
            self.state_filter.addItem(label, value)
        self.state_filter.setAccessibleName("插件启用状态")
        self.state_filter.setMinimumWidth(110)
        filters.addWidget(self.state_filter)
        self.btn_clear = QPushButton("重置筛选")
        self.btn_clear.setProperty("class", "secondary")
        self.btn_clear.clicked.connect(self._clear_filters)
        filters.addWidget(self.btn_clear)
        root.addLayout(filters)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.holder = QWidget()
        self.rows_layout = QVBoxLayout(self.holder)
        self.rows_layout.setContentsMargins(0, 0, 8, 0)
        self.rows_layout.setSpacing(10)
        self.scroll.setWidget(self.holder)
        root.addWidget(self.scroll, 1)
        self.empty = QLabel("点击刷新加载插件列表")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"color: {MUTED}; padding: 36px; font-size: 14px;")
        root.addWidget(self.empty)
        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.notice)
        self.search.textChanged.connect(self._filter_rows)
        self.category_filter.currentIndexChanged.connect(self._filter_rows)
        self.state_filter.currentIndexChanged.connect(self._filter_rows)

    def _retry_failed_plugins(self):
        self.btn_retry_plugins.setEnabled(False)
        def done(result):
            self.btn_retry_plugins.setEnabled(True)
            self.notice.setText(str(result.get('message') or '插件复查完成'))
            self.refresh()
        call_api(self, 'POST', 'plugins/retry-failed', {}, timeout=30, on_done=done)

    def _show_sessions(self):
        window=self.window()
        cfg=getattr(window,'_cfg',{})
        SessionManagementDialog(call_api,self,theme=cfg,background=resolve_background(cfg.get('background_image'),ROOT)).exec()

    def _open_management(self, plugin_id):
        plugin = next((p for p in self._plugins if p.get('id') == plugin_id),None)
        if plugin:
            dialog = PluginConfigDialog(plugin,self)
            if dialog.exec():
                self.refresh()
        else:
            self._pending_management = plugin_id
            self.notice.setText('正在读取插件，完成后自动打开配置…')
            self.refresh()

    def refresh(self):
        """拉插件清单。两次请求都走后台线程，避免后端忙时把列表页冻住。"""
        if self._loading:
            return
        self._loading = True
        self._load_id += 1
        load_id = self._load_id
        self.btn_reload.setEnabled(False)
        self.btn_reload.setText('正在加载…')
        if not self._loaded:
            self.summary.setText('正在加载插件')
        self.notice.setText('正在读取插件，完成后自动打开配置…' if self._pending_management else '正在读取插件列表…')

        def failed(message):
            self._pending_management = None
            self.notice.setText(message)
            if not self._loaded:
                self.summary.setText('加载失败，可重试')
                self.empty.setText('插件列表加载失败，请点击“刷新列表”重试。')

        def got_custom(custom):
            if load_id == self._load_id:
                self._custom_ids = self._custom_plugin_ids(custom)
                # 来源信息只补删除入口，不能重建正在等待开关结果的控件。
                for plugin_id,entry in self._rows.items():
                    if plugin_id in self._custom_ids and not entry.get('remove'):
                        remove=self._delete_button(plugin_id,entry['plugin'])
                        entry['frame'].layout().addWidget(remove)
                        entry['remove']=remove

        def got_plugins(plugins):
            self._loading = False
            self.btn_reload.setEnabled(True)
            self.btn_reload.setText('刷新列表')
            if isinstance(plugins, dict) and plugins.get('success') is False:
                failed(str(plugins.get('message') or '后台未连接'))
                return
            if isinstance(plugins, dict):
                if isinstance(plugins.get("plugins"), list):
                    plugins = plugins["plugins"]
                elif plugins and all(isinstance(p, dict) and p.get("id") for p in plugins.values()):
                    plugins = list(plugins.values())
                else:
                    plugins = None
            if not isinstance(plugins, list):
                failed("刷新失败：暂时无法连接后端，请稍后重试。已加载的列表保留。")
                return
            if (any(not isinstance(p, dict) or not isinstance(p.get("id"), str) or not p['id'] for p in plugins)
                    or len({p['id'] for p in plugins}) != len(plugins)):
                failed("插件数据不完整，已保留上次列表，请重试。")
                return
            self._plugins = plugins
            self._loaded = True
            # 列表不必等待来源元数据；未确认自定义来源时先隐藏删除入口。
            self._render(plugins, {})
            call_api(self, "GET", "plugins/custom", timeout=10, on_done=got_custom)
            pending = self._pending_management
            self._pending_management = None
            if pending:
                if any(p['id'] == pending for p in plugins):
                    self._open_management(pending)
                else:
                    self.notice.setText(f'当前后台没有插件 {pending}，请确认已安装最新版本。')

        call_api(self, "GET", "plugins", timeout=15, on_done=got_plugins)

    def _render(self, plugins: list, custom):
        # 自定义插件清单：拿不到就当作"没有自定义插件"（宁可不给删除按钮，也不误删）
        self._custom_ids = self._custom_plugin_ids(custom)
        selected = self.category_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("全部分类", "")
        categories = sorted({str(p.get("category") or "utility") for p in plugins})
        for category in categories:
            self.category_filter.addItem(self.CATEGORIES.get(category, category), category)
        self.category_filter.setCurrentIndex(max(0, self.category_filter.findData(selected)))
        self.category_filter.blockSignals(False)
        while self.rows_layout.count():
            widget = self.rows_layout.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        self._rows.clear()
        for plugin in plugins:
            self.rows_layout.addWidget(self._make_row(plugin))
        self.rows_layout.addStretch(1)
        self.notice.setText("普通内置插件可移除并恢复；配置会保留。已移除项请在状态筛选中查看。")
        self._filter_rows()

    @staticmethod
    def _custom_plugin_ids(custom):
        if isinstance(custom,dict) and custom.get('success') is not False and isinstance(custom.get('plugins'),list):
            return {item['id'] for item in custom['plugins'] if isinstance(item,dict) and isinstance(item.get('id'),str)}
        return set()

    def _delete_button(self,plugin_id,plugin):
        restore=bool(plugin.get('removed'))
        remove=QPushButton('恢复' if restore else '移除' if plugin.get('source')=='builtin' else '删除')
        remove.setProperty('class','secondary')
        remove.setToolTip('恢复原配置与开关' if restore else '停止此内置插件，保留配置，可恢复' if plugin.get('source')=='builtin' else '删除这个自己安装的插件（会真的删掉文件）')
        remove.clicked.connect(lambda _=False:self._restore_plugin(plugin_id) if restore else self._delete_plugin(plugin_id,plugin))
        return remove

    def _clear_filters(self):
        self.search.clear()
        self.category_filter.setCurrentIndex(0)
        self.state_filter.setCurrentIndex(0)

    def _filter_rows(self, *_):
        query = self.search.text().strip().casefold()
        category = self.category_filter.currentData()
        state = self.state_filter.currentData()
        visible = 0
        for plugin in self._plugins:
            row = self._rows[plugin["id"]]["frame"]
            cat = str(plugin.get("category") or "utility")
            searchable = " ".join(str(plugin.get(k) or "") for k in ("name", "description", "id"))
            searchable += " " + self.CATEGORIES.get(cat, cat)
            enabled = bool(plugin.get("enabled"))
            show = (not query or query in searchable.casefold()) and (not category or category == cat)
            removed=bool(plugin.get('removed'))
            show = show and (removed if state=='removed' else not removed and (state == "all" or enabled == (state == "enabled")))
            row.setVisible(show)
            visible += int(show)
        total = len(self._plugins)
        enabled_count = sum(bool(p.get("enabled")) for p in self._plugins)
        removed_count=sum(bool(p.get('removed')) for p in self._plugins)
        self.summary.setText(f"共 {total} 个  ·  已启用 {enabled_count} 个  ·  已移除 {removed_count} 个  ·  显示 {visible} 个")
        self.empty.setText("没有符合条件的插件，试试其他关键词或重置筛选。" if total else "暂无插件，请确认后端已加载插件。")
        self.empty.setVisible(visible == 0)
        self.scroll.setVisible(visible > 0)

    def _make_row(self, plugin: dict) -> QWidget:
        frame = QFrame()
        frame.setProperty("panel", True)
        frame.setMinimumHeight(96)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(18)
        text = QVBoxLayout()
        text.setSpacing(6)
        heading = QHBoxLayout()
        name = QLabel(str(plugin.get("name") or plugin.get("id")))
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setStyleSheet(f"color: {TEXT}; font-size: 15px; font-weight: bold;")
        heading.addWidget(name)
        category = str(plugin.get("category") or "utility")
        badge = QLabel(self.CATEGORIES.get(category, category))
        badge.setTextFormat(Qt.TextFormat.PlainText)
        badge.setStyleSheet(f"color: {ACCENT2}; background: rgba(47,168,160,24); padding: 3px 8px; border-radius: 5px; font-size: 11px;")
        heading.addWidget(badge)
        heading.addStretch()
        text.addLayout(heading)
        description = QLabel(str(plugin.get("description") or "暂无功能说明"))
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        description.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        text.addWidget(description)
        if plugin.get('faulted'):
            fault = QLabel(str((plugin.get('fault') or {}).get('message') or '插件故障已隔离'))
            fault.setTextFormat(Qt.TextFormat.PlainText)
            fault.setWordWrap(True)
            fault.setStyleSheet(f'color: {ERROR};')
            text.addWidget(fault)
        layout.addLayout(text, 1)
        enabled = QCheckBox('已隔离' if plugin.get('faulted') else '已移除' if plugin.get('removed') else "已启用" if plugin.get("enabled") else "已停用")
        enabled.setMinimumWidth(88)
        enabled.setChecked(bool(plugin.get("enabled")))
        enabled.setEnabled(not plugin.get('removed',False) and not plugin.get('faulted',False))
        enabled.setAccessibleName(f"启用 {plugin.get('name') or plugin.get('id')}")
        enabled.toggled.connect(lambda checked, pid=plugin.get("id"): self._toggle(pid, checked))
        layout.addWidget(enabled)
        if plugin.get("schema") or plugin.get("config_schema"):
            button = QPushButton("配置")
            button.setProperty("class", "secondary")
            button.setToolTip(f"调整 {plugin.get('name') or plugin.get('id')} 的参数")
            button.clicked.connect(lambda _=False, p=plugin: self._configure(p))
            button.setEnabled(not plugin.get('removed',False))
            layout.addWidget(button)
        else:
            label = QLabel("无可配置项")
            label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            layout.addWidget(label)
        # 表情库相关的插件卡片上直接给一个入口（对照 QQ 版的做法），
        # 省得用户为了改个标签满界面找窗口在哪。
        if str(plugin.get("id") or "") in STICKER_LIBRARY_PLUGINS:
            library_button = QPushButton("管理表情库…")
            library_button.setProperty("class", "secondary")
            library_button.setToolTip("打开机器人本地图库：预览、上传、改标签、AI 识别、删除")
            library_button.clicked.connect(lambda _=False: self.open_sticker_library())
            library_button.setEnabled(not plugin.get('removed', False))
            layout.addWidget(library_button)
        # 只有自定义插件给删除按钮：内置插件删了会让功能缺失
        plugin_id = str(plugin.get("id") or "")
        remove = None
        if plugin_id in self._custom_ids or plugin.get('removable'):
            remove = self._delete_button(plugin_id,plugin)
            layout.addWidget(remove)
        frame.setToolTip(f"插件标识：{plugin.get('id', '')}")
        self._rows[plugin.get("id")] = {"frame":frame, "toggle":enabled, "plugin":plugin, 'remove':remove}
        return frame

    def _restore_plugin(self,plugin_id):
        entry=self._rows.get(plugin_id)
        if entry and entry.get('remove'):entry['remove'].setEnabled(False)
        call_api(self,'POST',f'plugins/{plugin_id}/restore',{},timeout=15,
                 on_done=lambda result:self._apply_delete(plugin_id,result))

    def _batch_import(self):
        """批量导入：多选文件或整目录 → 预检 → 确认 → 一次装完。

        为什么要先预检：插件是要被 import 执行的代码。旧流程一个一个装，
        用户看不到"这批里有没有同名冲突/危险用法"，装到一半失败还会留下半成品。
        """
        paths = QFileDialog.getOpenFileNames(
            self, "选择插件文件（可多选）", "",
            "插件文件 (*.py *.zip)")[0]
        if not paths:
            folder = QFileDialog.getExistingDirectory(self, "或选择包含插件的文件夹")
            if not folder:
                return
            paths = [folder]
        self.btn_batch.setEnabled(False)
        call_api(self, "POST", "plugins/batch-inspect", {"paths": paths}, timeout=90,
                 on_done=lambda data: self._batch_preview(paths, data))

    def _batch_preview(self, paths, data):
        self.btn_batch.setEnabled(True)
        if not data.get("success"):
            QMessageBox.warning(self, "预检失败",
                                str(data.get("message") or data.get("detail") or "未知原因"))
            return
        items = data.get("items") or []
        if not items:
            QMessageBox.information(self, "没有可安装的文件", "选中的位置里没有 .py 或 .zip 插件。")
            return
        lines = []
        for item in items:
            flag = "✓" if item.get("ok") else "✗"
            note = []
            if item.get("exists"):
                note.append("同名已存在")
            if item.get("error"):
                note.append(item["error"])
            if item.get("problems"):
                note.append("；".join(item["problems"]))
            lines.append(f"{flag} {item.get('plugin_id') or '?'}（{item.get('name') or '-'}）"
                         + ("  ← " + "，".join(note) if note else ""))
        bad = [item for item in items if not item.get("ok")]
        conflicts = data.get("conflicts") or []
        text = ("即将安装：\n" + "\n".join(lines) +
                ("\n\n有 %d 个未通过预检，将被跳过。" % len(bad) if bad else "") +
                ("\n\n有同名插件：覆盖会替换原文件（失败会回滚）。" if conflicts else "") +
                "\n\n安装后会自动启用吗？")
        box = QMessageBox(self)
        box.setWindowTitle("批量导入")
        box.setText(text)
        overwrite = box.addButton("覆盖同名并安装", QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton("只装不冲突的", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is cancel:
            return
        payload = {"paths": paths,
                   "overwrite": box.clickedButton() is overwrite,
                   "enable": False}
        call_api(self, "POST", "plugins/batch-install", payload, timeout=180,
                 on_done=self._batch_done)

    def _batch_done(self, data):
        if not data.get("success"):
            QMessageBox.warning(self, "安装失败",
                                str(data.get("message") or data.get("detail") or "未知原因"))
            return
        QMessageBox.information(self, "批量导入完成", str(data.get("message") or ""))
        self.refresh()

    def _open_packs(self):
        """打开插件包管理：只列出现状，装/卸都由用户手动点。"""
        try:
            from src.gui.maintenance_dialogs import PluginPacksDialog
            PluginPacksDialog(self, api_get, api_post, api_delete).exec()
            self.refresh()
        except Exception as error:
            QMessageBox.warning(self, "插件包", f"打不开插件包窗口：{type(error).__name__}")
    def _add_plugin(self):
        """打开安装对话框，装完自动刷新列表。"""
        dialog = PluginInstallDialog(self)
        dialog.exec()
        if dialog.installed:
            self.refresh()

    def _delete_plugin(self, plugin_id: str, plugin: dict):
        """删除自定义插件（二次确认 + 明确告知会删文件）。"""
        name = plugin.get("name") or plugin_id
        builtin=plugin.get('source')=='builtin' and plugin.get('removable')
        confirmed = QMessageBox.warning(
            self, "删除插件",
            (f'确定移除「{name}」吗？\n\n移除后立即停止生效，保留原配置。可在“已移除”筛选中恢复。' if builtin else f"确定要删除插件「{name}」吗？\n\n"
            f"这会删除文件 data/wechat-only/custom_plugins/{plugin_id}.py 并从插件清单移除，"
            "无法在界面里撤销。\n"
            "请确认这不是你还需要的东西。"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        call_api(self, "DELETE", f"plugins/{plugin_id}", timeout=30,
                 on_done=lambda result: self._apply_delete(plugin_id, result))

    def _apply_delete(self, plugin_id: str, result):
        if result.get("success"):
            self.notice.setText(str(result.get("message") or f"{plugin_id} 已删除"))
            self.refresh()
        else:
            entry=self._rows.get(plugin_id)
            if entry and entry.get('remove'):entry['remove'].setEnabled(True)
            QMessageBox.warning(self, "删除失败",
                                str(result.get("message") or result.get("detail") or "未知原因"))

    def _toggle(self, plugin_id: str, checked: bool):
        entry = self._rows.get(plugin_id)
        if not entry:
            return
        control = entry["toggle"]
        previous = bool(entry["plugin"].get("enabled"))
        control.setEnabled(False)
        # 同上：body 只放真正的设置字段。backend 的 update_settings 对未知键
        # 直接报错，带上 plugin_id 会让"启用/停用"也一起失败。
        call_api(self, "POST", f"plugins/{plugin_id}", {"enabled": bool(checked)}, timeout=15,
                 on_done=lambda result: self._apply_toggle(plugin_id, checked, previous, result))

    def _apply_toggle(self, plugin_id: str, checked: bool, previous: bool, result):
        entry = self._rows.get(plugin_id)
        if not entry:
            return
        control = entry["toggle"]
        try:
            if result.get("success"):
                entry["plugin"]["enabled"] = checked
                entry["plugin"].setdefault("settings", {})["enabled"] = checked
                control.setText("已启用" if checked else "已停用")
                self.notice.setText(f"{entry['plugin'].get('name') or plugin_id} 已{'启用' if checked else '停用'}")
                self._filter_rows()
            else:
                control.blockSignals(True)
                control.setChecked(previous)
                control.blockSignals(False)
                self.notice.setText(f"操作失败，已恢复原状态：{result.get('message') or result}")
        finally:
            control.setEnabled(True)

    def _configure(self, plugin: dict):
        dialog = PluginConfigDialog(plugin, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved:
            self.refresh()
            self.notice.setText(f"{plugin.get('name') or plugin.get('id')} 设置已保存")

    def open_sticker_library(self):
        """打开机器人本地图库窗口；接口调用走宿主后台线程，模态窗口也不卡界面。"""
        dialog = StickerLibraryDialog(self)
        dialog.exec()
        self.refresh()


# ── 关于作者 ──────────────────────────────────────────────────────────────────
# 作者与致谢都写在这里一处，将来要改不用翻整个文件。
AUTHOR_NAME = "林夕"
AUTHOR_QQ = "2019117802"
AUTHOR_BLOG = ""          # 有博客地址就填在这里（留空则不显示这一行）
THANKS_USERS = ["你携秋水揽星河"]
COPYRIGHT_NOTICE = "未经作者允许，禁止贩卖、转卖。"


def author_avatar_path() -> Path:
    """作者头像：打包后随 assets 一起进 _MEIPASS，所以走 BUNDLE 而不是 ROOT。"""
    return BUNDLE / "assets" / "author" / "linxi.jpg"


class AboutTab(QWidget):
    """关于作者：作者、致谢、声明，以及本程序信息与安全须知。

    做成独立一页（而不是弹窗）是为了让用户随时能翻到：
    联系方式、版本信息、免责声明、以及"哪些事这个程序不会做"。
    """

    def __init__(self):
        super().__init__()
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: 0; }")
        inner = QWidget()
        shell_row=QHBoxLayout(inner);shell_row.setContentsMargins(20,20,20,20)
        self.content=QWidget();self.content.setMaximumWidth(1120)
        layout=QVBoxLayout(self.content);layout.setContentsMargins(0,0,0,0);layout.setSpacing(16)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        heading=QHBoxLayout()
        title=QLabel('鲸鱼娘 · 微信控制台');title.setStyleSheet(f'color:{TEXT};font-size:23px;font-weight:bold;')
        heading.addWidget(title);heading.addStretch()
        version=QLabel('v'+APP_VERSION);version.setStyleSheet(f'color:{ACCENT2};background:rgba(47,168,160,24);padding:6px 12px;border-radius:12px;')
        heading.addWidget(version);layout.addLayout(heading)
        subtitle=QLabel('作者与作品信息');subtitle.setStyleSheet(f'color:{MUTED};');layout.addWidget(subtitle)
        layout.addWidget(self._author_card())
        self.card_grid=QGridLayout();self.card_grid.setSpacing(14)
        self.notice_card=self._copyright_card();self.thanks_card=self._thanks_card()
        self.card_grid.addWidget(self.notice_card,0,0);self.card_grid.addWidget(self.thanks_card,0,1)
        self.card_grid.setColumnStretch(0,1);self.card_grid.setColumnStretch(1,1)
        layout.addLayout(self.card_grid)
        layout.addWidget(self._program_card());layout.addWidget(self._safety_card());layout.addStretch(1)
        for card in self.content.findChildren(QFrame):
            if card.property('panel') and card.layout():
                card.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        shell_row.addStretch();shell_row.addWidget(self.content,1);shell_row.addStretch()

        scroll.setWidget(inner)
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.addWidget(scroll)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'card_grid'):
            narrow=self.width()<850
            self.card_grid.removeWidget(self.thanks_card)
            self.card_grid.addWidget(self.thanks_card,1 if narrow else 0,0 if narrow else 1)
            self.card_grid.setColumnStretch(1,0 if narrow else 1)
            self.content.layout().invalidate()

    # ── 各卡片 ──────────────────────────────────────────────────────────────
    def _author_card(self) -> QFrame:
        frame, box = _card("关于作者", "app")
        row = QHBoxLayout()
        row.setSpacing(16)

        self.avatar = QLabel()
        self.avatar.setFixedSize(96, 96)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 背景透明，圆形描边画在 pixmap 里（样式表的圆角不会裁掉子 pixmap，会露出方形底）
        self.avatar.setStyleSheet("QLabel { background: transparent; border: none; }")
        self._load_avatar()
        row.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(6)
        name = QLabel(AUTHOR_NAME)
        name.setStyleSheet(f"color: {ACCENT}; font-size: 20px; font-weight: bold;")
        info.addWidget(name)
        self.qq_label = QLabel(f"QQ：{AUTHOR_QQ}")
        self.qq_label.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        info.addWidget(self.qq_label)
        if AUTHOR_BLOG:
            blog = QLabel(f'博客：<a href="{AUTHOR_BLOG}" style="color:{ACCENT};">{AUTHOR_BLOG}</a>')
            blog.setOpenExternalLinks(True)
            blog.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
            info.addWidget(blog)
        role = QLabel("本程序的设计与开发 · 个人非商业作品")
        role.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        info.addWidget(role)

        buttons = QHBoxLayout()
        self.btn_copy_qq = decorate_button(QPushButton("复制 QQ 号"), "btn_save")
        self.btn_copy_qq.clicked.connect(self._copy_qq)
        buttons.addWidget(self.btn_copy_qq)
        self.btn_copy_info = decorate_button(QPushButton("复制版本信息"), "btn_reload")
        self.btn_copy_info.setToolTip("把版本号与数据目录复制到剪贴板，反馈问题时一起发来")
        self.btn_copy_info.clicked.connect(self._copy_version)
        buttons.addWidget(self.btn_copy_info)
        buttons.addStretch(1)
        info.addLayout(buttons)

        self.copy_hint = QLabel("")
        self.copy_hint.setVisible(False)
        self.copy_hint.setStyleSheet(f"color: {SUCCESS}; font-size: 12px;")
        info.addWidget(self.copy_hint)

        row.addLayout(info, 1)
        box.addLayout(row)
        return frame

    def _thanks_card(self) -> QFrame:
        frame, box = _card("特别感谢", "card_events")
        intro = QLabel("参与制作的用户（排名不分先后）：")
        intro.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        box.addWidget(intro)
        for user in THANKS_USERS:
            line = QLabel(f"· {user}")
            line.setStyleSheet(f"color: {ACCENT2}; font-size: 13px; font-weight: bold;")
            box.addWidget(line)
        more = QLabel("以及所有反馈问题、帮忙测试的朋友。")
        more.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        box.addWidget(more)
        return frame

    def _program_card(self) -> QFrame:
        frame, box = _card("关于本程序", "tab_console")

        def line(label: str, value: str):
            row = QHBoxLayout()
            left = QLabel(label)
            left.setFixedWidth(96)
            left.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            right = QLabel(value)
            right.setStyleSheet(f"color: {TEXT}; font-size: 13px;")
            right.setWordWrap(True)
            right.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(left)
            row.addWidget(right, 1)
            box.addLayout(row)

        line("程序", "鲸鱼娘 · 微信控制台")
        line("版本", APP_VERSION)
        line("形态", "安装包（覆盖安装保留配置与对话记录）")
        line("数据目录", "%LOCALAPPDATA%\\WhaleGirlWeChat")
        line("更新方式", "运行新版安装包覆盖安装即可，不必先卸载")
        line("反馈问题", f"联系作者 QQ {AUTHOR_QQ}，附上版本号与运行记录")
        tips = QLabel("版本信息、作者与致谢都取自程序自身，不联网获取；界面与后台版本不一致时，"
                      "标题栏会直接提示重新打开控制台。")
        tips.setWordWrap(True)
        tips.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        box.addWidget(tips)
        return frame

    def _safety_card(self) -> QFrame:
        frame, box = _card("使用须知", "card_persona_list")
        items = [
            "请遵守微信平台规则；因违规使用导致的账号风险由使用者自行承担。",
            "本程序不做批量主动外发、不做转发、不模拟点击面板、不读取与回复无关的聊天内容。",
            "群聊默认只在被 @ 或命中触发词时回复；回复内容由你自己的模型生成，请自行把控。",
            "配置与对话记录保存在本机；生成回复时会把必要的消息和上下文发送给你配置的模型服务。",
        ]
        for text in items:
            label = QLabel(f"· {text}")
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            box.addWidget(label)
        return frame

    def _copyright_card(self) -> QFrame:
        frame, box = _card("声明", "notice")
        notice = QLabel(COPYRIGHT_NOTICE)
        notice.setWordWrap(True)
        notice.setStyleSheet(f"color: {ERROR}; font-size: 14px; font-weight: bold;")
        box.addWidget(notice)
        extra = QLabel("· 允许个人免费使用与自行修改；请勿删除作者署名后二次分发。\n"
                       "· 本程序按「现状」提供，作者不对使用结果承担任何责任。\n"
                       "· 转载或以本程序牟利前，请先联系作者获得许可。")
        extra.setWordWrap(True)
        extra.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        box.addWidget(extra)
        return frame

    def _load_avatar(self) -> None:
        """加载头像并裁成圆形；取不到就退化成文字，不让整页崩掉。"""
        path = author_avatar_path()
        pixmap = QPixmap(str(path)) if path.is_file() else QPixmap()
        if pixmap.isNull():
            self.avatar.setText(AUTHOR_NAME)
            self.avatar.setStyleSheet(
                f"QLabel {{ color: {ACCENT}; font-size: 18px; font-weight: bold;"
                f" border: 2px solid {BORDER}; border-radius: 48px; background: {FIELD}; }}")
            return
        side = 92
        scaled = pixmap.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
        # KeepAspectRatioByExpanding 之后可能比目标大一点，居中裁掉溢出部分
        x = max(0, (scaled.width() - side) // 2)
        y = max(0, (scaled.height() - side) // 2)
        circle = QPixmap(side, side)
        circle.fill(Qt.GlobalColor.transparent)
        painter = QPainter(circle)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = QPainterPath()
        ring.addEllipse(1, 1, side - 2, side - 2)
        painter.setClipPath(ring)
        painter.drawPixmap(-x, -y, scaled)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(BORDER), 2))
        painter.drawEllipse(1, 1, side - 2, side - 2)
        painter.end()
        self.avatar.setPixmap(circle)

    def _copy_qq(self) -> None:
        QApplication.clipboard().setText(AUTHOR_QQ)
        self.copy_hint.setVisible(True)
        self.copy_hint.setText(f"已复制 QQ 号：{AUTHOR_QQ}")

    def _copy_version(self) -> None:
        info = (f"鲸鱼娘 · 微信控制台 {APP_VERSION}\n"
                f"数据目录：%LOCALAPPDATA%\\WhaleGirlWeChat\n"
                f"作者：{AUTHOR_NAME}（QQ {AUTHOR_QQ}）")
        QApplication.clipboard().setText(info)
        self.copy_hint.setVisible(True)
        self.copy_hint.setText("已复制版本信息，反馈问题时可一起发来")


# ── 主窗口 ────────────────────────────────────────────────────────────────────
class _ActionWorker(QThread):
    """微信连接与模型测试可能超过十秒，等待期间不能阻塞界面。"""
    def __init__(self, path, data=None):
        super().__init__()
        self.path, self.data = path, data
        self.response = {}

    def run(self):
        try:
            self.response = api_post(self.path, self.data, timeout=120)
        except Exception as exc:
            self.response = {'success': False, 'message': str(exc)}


class _StatusPoller(QThread):
    """把 3s 轮询的同步 requests 挪出 Qt 主线程，避免后端响应慢时整窗冻结。"""
    result = pyqtSignal(object)

    def __init__(self, window):
        super().__init__()
        self._window = window

    def run(self):
        try:
            self.result.emit(api_get("status"))
        except Exception as exc:
            self.result.emit({'success': False, 'message': str(exc)})


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._cfg = load_gui_config(GUI_CFG)
        # 首次启动套用内置浅色背景（用户自己设过背景或主动清除过就不动）。
        try:
            from src.gui.config import apply_default_background
            applied = apply_default_background(self._cfg, BUNDLE, ROOT)
            if applied.get('background_image') != self._cfg.get('background_image'):
                self._cfg = applied
                save_gui_config(GUI_CFG, self._cfg)
        except Exception:
            pass
        self._backend_proc = None
        self._busy = False
        self._polling = False
        self._poll_worker = None
        self._action_worker = None
        # 正在跑的异步接口调用（见 call_api）。关窗口前要等它们落地，否则进程会
        # 带着还在跑的 requests 线程硬退。
        self._api_jobs = set()
        # 设置页是否已经成功读到过后台的配置（后台晚就绪时用来补读一次）
        self._settings_loaded = False
        self._settings_loading = False
        self._settings_reload_requested = False
        self._backend_starting = False
        self._quitting = False
        self._quit_stop_done = False

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # 恢复窗口大小/位置
        geo = self._cfg.get("geometry", {})
        # 显示前就限制尺寸，不能先创建超大原生窗口再等定时器缩小。
        self.setGeometry(fit_geometry(geo,[s.availableGeometry() for s in QApplication.screens()]))

        # 外层容器（纯色底 + 自定义背景图，自绘）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._container = BackgroundFrame()
        self._container.setObjectName("container")
        self._apply_bg(self._cfg.get("bg_color", BG), self._cfg.get("background_image", ""))
        outer.addWidget(self._container)

        # 内层布局
        vbox = QVBoxLayout(self._container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        # 标题栏
        self.title_bar = TitleBar(self)
        vbox.addWidget(self.title_bar)

        # 分隔线
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {BORDER};")
        vbox.addWidget(line)

        # 主内容区（标签页）
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.console_tab = ConsoleTab()
        self.settings_tab = SettingsTab()
        self.appearance_tab = AppearanceTab()
        self.persona_tab = PersonaTab()
        self.plugin_tab = PluginTab()
        self.agent_tab = AgentTab()
        self.dialogue_tab=DialogueTab(call_api,self.plugin_tab._open_management)
        self.dialogue_tab.apply_theme(self._cfg)
        self.management_tab=SessionManagementDialog(call_api,self,theme=self._cfg,
            background=resolve_background(self._cfg.get('background_image'),ROOT),auto_refresh=False)
        self.management_tab.setWindowFlags(Qt.WindowType.Widget)
        self.about_tab = AboutTab()          # 关于作者：作者/致谢/声明与使用须知
        self._page_scrolls=[make_scrollable(page) for page in (self.console_tab,self.persona_tab,self.plugin_tab,self.dialogue_tab,self.management_tab)]
        self.setMinimumSize(720,480)
        # 图标：窗口/任务栏用主图标，每个标签页一枚（来自图标库的随机不重复分配）
        self.setWindowIcon(app_icon())
        self.tabs.addTab(self.console_tab, slot_icon("tab_console"), "控制台")
        self.tabs.addTab(self.settings_tab, slot_icon("tab_settings"), "设置")
        self.tabs.addTab(self.persona_tab, slot_icon("tab_persona"), "人设")
        self.tabs.addTab(self.plugin_tab, slot_icon("tab_plugin"), "插件")
        self.tabs.addTab(self.dialogue_tab,slot_icon('tab_plugin'),'对话与记忆')
        self.tabs.addTab(self.management_tab,slot_icon('tab_settings'),'机器人管理')
        self.tabs.addTab(self.appearance_tab, slot_icon("tab_appearance"), "外观")
        # Agent 紧挨"关于作者"之前：既要靠后（前面标签的索引被别处按序号引用），
        # 又要保持"关于作者"是最后一页（既有测试就是按这条断言的）。
        self.tabs.addTab(self.agent_tab, slot_icon("tab_console"), "Agent")
        self.tabs.addTab(self.about_tab, slot_icon("app"), "关于作者")
        # 切到人设/插件页时自动拉一次最新数据
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # 1.3.64：启动后自动检查一次更新（只提示，不自动下载安装；失败静默）
        # 用窗口自己的 QTimer 而不是 QTimer.singleShot：窗口销毁后一次性定时器仍会触发，
        # 会去调后端接口（GUI 离线测试就是靠彻底停掉定时器证明不联网的）。
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(9000)
        self._update_timer.timeout.connect(self._auto_check_update)
        self._update_timer.start()
        vbox.addWidget(self.tabs)

        # 调整大小手柄
        grip = QSizeGrip(self)
        grip.setFixedSize(16, 16)
        grip_layout = QHBoxLayout()
        grip_layout.setContentsMargins(0, 0, 4, 4)
        grip_layout.addStretch()
        grip_layout.addWidget(grip)
        vbox.addLayout(grip_layout)

        # 样式表
        self._apply_panel_transparency(self._cfg.get("panel_transparency", 0.35))

        # 连接信号
        self.console_tab.btn_start.clicked.connect(lambda: self._action("start"))
        self.console_tab.btn_stop.clicked.connect(lambda: self._action("stop"))
        self.console_tab.btn_test_ai.clicked.connect(lambda: self._action("test-ai"))
        self.console_tab.btn_test_wechat.clicked.connect(lambda: self._action("test-wechat"))
        self.console_tab.btn_retry_backend.clicked.connect(self._start_backend)
        self.settings_tab.btn_save.clicked.connect(self._save_settings)
        self.settings_tab.btn_save.setEnabled(False)
        self.settings_tab.save_lbl.setText('正在读取后台配置，读取成功后可保存。')
        self.appearance_tab.appearance_changed.connect(self._apply_appearance)
        self.appearance_tab.load(self._cfg)
        # 刷新定时器：轮询放到后台线程，避免同步 requests 阻塞 UI
        self._timer = QTimer()
        self._timer.timeout.connect(self._poll_status)
        self._timer.start(3000)
        self._resize_handles=WindowResizeHandles(self,proportional=bool(self._cfg.get("proportional_resize",False)))
        self._screen_connected=False

        from src.gui.tray import RobotTray
        self._tray = RobotTray(self, app_icon())
        self.title_bar.setToolTip('关闭窗口后在右下角托盘继续运行；双击托盘恢复，右键可退出程序。')

        # 启动后端
        if "--no-backend" in sys.argv:
            self.console_tab.set_notice("已禁用后端自动启动（--no-backend）：仅调试界面", ok=True)
        else:
            QTimer.singleShot(500, self._start_backend)
            QTimer.singleShot(2000, self._load_settings)

    def _on_tab_changed(self, index: int):
        """切到人设/插件页时自动拉一次最新数据（后端可能刚启动完成）。"""
        widget = self.tabs.widget(index)
        if widget is self.persona_tab:
            self.persona_tab.refresh()
        elif widget is self.plugin_tab:
            self.plugin_tab.refresh()
        elif widget is self.dialogue_tab:
            self.dialogue_tab.refresh()
        elif widget is self.management_tab:
            self.management_tab.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        # 银白主题：DWM 用亮色模式，背板按模糊度挑（0% 模糊 = 不用背板）。
        self.setWindowOpacity(window_opacity_from_transparency(self._cfg.get("transparency", 0.0)))
        self._apply_backdrop()
        if not self._screen_connected and self.windowHandle():
            self.windowHandle().screenChanged.connect(lambda screen:QTimer.singleShot(0,self._fit_screen))
            self._screen_connected=True
            QTimer.singleShot(0,self._fit_screen)

    def _fit_screen(self):
        if self.isMaximized() or self.isFullScreen():return
        areas=[s.availableGeometry() for s in QApplication.screens()]
        rect=self.geometry()
        fitted=fit_geometry({'w':rect.width(),'h':rect.height(),'x':rect.x(),'y':rect.y()},areas)
        self.setMinimumSize(min(720,fitted.width()),min(480,fitted.height()))
        self.setGeometry(fitted)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'_resize_handles'):self._resize_handles.update()

    def changeEvent(self,event):
        super().changeEvent(event)
        if hasattr(self,'_resize_handles'):self._resize_handles.update()

    def _apply_backdrop(self):
        blur = float(self._cfg.get("blur", 0.70) or 0.0)
        backdrop = backdrop_for_blur(blur)
        dark = False  # 银白主题固定亮色
        if not hasattr(self,'_backdrop_timer'):
            self._backdrop_timer=QTimer(self);self._backdrop_timer.setSingleShot(True)
            self._backdrop_timer.timeout.connect(self._apply_native_backdrop)
        self._backdrop_timer.start(100)

    def _apply_native_backdrop(self):
        apply_mica(int(self.winId()),dark=False,
            backdrop=backdrop_for_blur(float(self._cfg.get('blur',.7) or 0)))

    def _apply_bg(self, color: str, image: str = "", blur: float = None, transparency: float = None):
        """底色、背景图、模糊度与填充透明度都交给容器自绘（样式表不支持
        background-size，也读不到中文/空格路径的图片）。"""
        if blur is None:
            blur = float(self._cfg.get("blur", 0.70) or 0.0)
        if transparency is None:
            transparency = float(self._cfg.get("transparency", 0.0) or 0.0)
        self._container.setStyleSheet("#container { border-radius: 0px; }")
        self._container.set_background_color(color or BG)
        resolved = resolve_background(image, ROOT) if image else None
        pixmap = QPixmap(str(resolved)) if resolved is not None else None
        if pixmap is not None and pixmap.isNull():
            pixmap = None
        self._container.set_background_pixmap(pixmap)
        self._container.set_blur(blur)
        # 填充透明度 = 1 - 窗口透明度：否则调高透明度只会穿透到桌面被不透明的容器挡住
        self._container.set_fill_alpha(1.0 - min(max(transparency, 0.0), 1.0))

    def _apply_panel_transparency(self, value):
        alpha = round(255 * (1.0 - min(max(float(value), 0.0), 1.0)))
        self.setStyleSheet(STYLESHEET.replace(f"background: {CARD};", f"background: rgba(255, 255, 255, {alpha});"))

    def _apply_appearance(self, values: dict):
        values = values or {}
        transparency = float(values.get("transparency", self._cfg.get("transparency", 0.0)) or 0.0)
        blur = float(values.get("blur", self._cfg.get("blur", 0.70)) or 0.0)
        color = values.get("bg_color", self._cfg.get("bg_color", BG))
        image = values.get("background_image", self._cfg.get("background_image", ""))

        self.setWindowOpacity(window_opacity_from_transparency(transparency))
        self._apply_bg(color, image, blur, transparency)
        panel = float(values.get("panel_transparency", self._cfg.get("panel_transparency", 0.35)))
        self._apply_panel_transparency(panel)
        self._cfg["panel_transparency"] = panel
        self._cfg["transparency"] = transparency
        self._cfg["blur"] = blur
        self._cfg["bg_color"] = color
        self._cfg["background_image"] = image
        proportional = bool(values.get("proportional_resize", self._cfg.get("proportional_resize", False)))
        self._cfg["proportional_resize"] = proportional
        if hasattr(self, "_resize_handles"):
            self._resize_handles.proportional = proportional
            self._resize_handles.update()
        self.management_tab.apply_theme(self._cfg,resolve_background(image,ROOT))
        self.dialogue_tab.apply_theme(self._cfg)
        self._apply_backdrop()
        try:
            save_gui_config(GUI_CFG, self._cfg)
        except OSError as exc:
            self.console_tab.set_notice(f"外观设置保存失败：{exc}", ok=False)

    def _start_backend(self):
        """在后台线程里探测端口并启动 FastAPI 服务（详见 :func:`ensure_backend`）。

        这段以前在主线程里 sleep 等旧后台退出，窗口一打开就是僵的。
        """
        if getattr(self, '_quitting', False):
            return
        if getattr(self,'_backend_starting',False):
            self.console_tab.set_notice('后台正在启动，请稍候…');return
        self._backend_starting=True
        def done(result):
            self._backend_starting=False
            apply_backend_probe(self,result)
        runner = getattr(self, "_run_task", None)
        if callable(runner):
            self.console_tab.set_notice('正在连接后台…')
            runner(lambda: ensure_backend(self),
                   done)
            return
        # 单测/脚本里 self 可能是 SimpleNamespace（没有后台线程设施）：同步跑一遍，
        # 行为与改造前一致，便于断言"端口被占时既不关别人也不重复起后端"。
        done(ensure_backend(self))

    @staticmethod
    def _backend_ready() -> bool:
        return not backend_error(timeout=1)

    def _run_api(self, method: str, path: str, data: dict = None, timeout: float = 10,
                 on_done=None):
        """把一次接口调用交给后台线程（``call_api`` 找到本方法就走这条路）。"""
        job = ApiJob(method, path, data, timeout)

        def finish(result):
            if on_done is not None:
                on_done(result)

        job.set_callback(finish)
        self._api_jobs.add(job)
        job.done.connect(job.deliver)
        job.finished.connect(self._release_job)
        job.start()

    def _run_task(self, func, on_done=None):
        """把任意阻塞函数丢到后台线程（见 TaskJob）。"""
        job = TaskJob(func)

        def finish(result):
            if on_done is not None:
                on_done(result)

        job.set_callback(finish)
        self._api_jobs.add(job)
        job.done.connect(job.deliver)
        job.finished.connect(self._release_job)
        job.start()

    def _release_job(self):
        # done 只表示结果可用，finished 才保证线程已经退出。
        job = self.sender()
        self._api_jobs.discard(job)
        job.deleteLater()

    def _start_backend_inprocess(self):
        """保留旧入口名，安装版后台实际放到独立进程，隔离微信库的原生崩溃。"""
        try:
            from src.desktop_startup import worker_command,child_environment
            if self._backend_proc is not None and self._backend_proc.poll() is None:
                self.console_tab.set_notice('后台正在启动，等待连接…');return
            self._backend_proc=subprocess.Popen(worker_command('--backend-worker','--root',ROOT,'--port',PORT),
                cwd=str(ROOT),env=child_environment(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.console_tab.set_notice('后台进程已启动，正在读取配置…')
        except Exception as e:
            self.console_tab.set_notice(f"后端启动失败：{e}", ok=False)

    def _auto_check_update(self):
        """后台自动检查更新：只有发现新版本才在控制台留一句提示。

        刻意不做的事：不自动下载、不自动安装、失败不弹窗（没配更新源或离线都属正常，
        弹窗只会变成噪音）；用户想知道细节点「环境与自检 → 检查更新」。
        """
        if getattr(self, '_update_checked', False) or getattr(self, '_quitting', False):
            return
        if not self.isVisible():
            return
        self._update_checked = True

        def done(data):
            if not isinstance(data, dict) or data.get('ok') is False:
                return
            status = data.get('status') if isinstance(data.get('status'), dict) else data
            if str(status.get('state') or '') != 'available':
                return
            latest = str(status.get('latest') or '').strip()
            try:
                self.console_tab.set_notice(
                    f'发现新版本 {latest}：点「环境与自检 → 检查更新」查看更新说明并安装', ok=True)
            except Exception:
                pass

        call_api(self, 'GET', 'update/status', timeout=10, on_done=done)
    def _load_settings(self):
        if getattr(self, '_quitting', False):
            return
        if getattr(self,'_settings_loading',False):
            self._settings_reload_requested=True;return
        self._settings_loading=True
        action_controls(self,getattr(self,'_busy',False))
        def apply(data):
            self._settings_loading=False
            if data and data.get('success') is not False and 'api_url' in data and 'model' in data:
                self.settings_tab.populate(data)
                # 读到了就打个标记：后台晚就绪时，_refresh() 会补读这一次
                self._settings_loaded = True
                action_controls(self,getattr(self,'_busy',False))
                label=getattr(self.settings_tab,'save_lbl',None)
                if label is not None:label.setText('配置已读取，可以修改并保存。')
            else:
                self._settings_loaded=False;action_controls(self,getattr(self,'_busy',False))
                label=getattr(self.settings_tab,'save_lbl',None)
                if label is not None:label.setText(str(data.get('message') or '配置响应不完整或读取失败，请重连后台后重试。'))
            if getattr(self,'_settings_reload_requested',False):
                self._settings_reload_requested=False
                self._load_settings()
        call_api(self, "GET", "settings", timeout=5, on_done=apply)

    def _refresh(self):
        """保留入口（_action 等用户操作后调用）：实际网络请求走后台线程，不阻塞 UI。"""
        self._poll_status()

    def _poll_status(self):
        """把 3s 轮询的同步 requests 挪出 Qt 主线程，避免后端响应慢时整窗冻结。"""
        if self._polling or getattr(self, '_quitting', False):
            return
        self._polling = True
        self._poll_worker = _StatusPoller(self)
        self._poll_worker.result.connect(self._on_status)
        self._poll_worker.finished.connect(self._on_poll_done)
        self._poll_worker.start()

    def _on_poll_done(self):
        self._polling = False
        self._poll_worker = None

    def _on_status(self, data):
        if getattr(self, '_tray', None):
            self._tray.update(data or {})
        if data and data.get('success') is False:
            process=getattr(self,'_backend_proc',None)
            code=process.poll() if process else None
            detail=(f'后台进程异常退出（0x{code & 0xffffffff:08X}），控制台仍可使用。请查看后台日志，点击“重连后台”重试。' if code is not None else data.get('message') or '后台未连接')
            if code is not None:
                self._settings_loaded=False;action_controls(self,self._busy)
            self.console_tab.set_notice(detail, ok=False)
            return
        if data:
            self.console_tab.refresh(data)
            # 后台是导入完 wechatauto 才监听端口的，首次可能比"2 秒后读设置"更晚就绪。
            # 那样设置页会一片空白，用户以为配置丢了、一保存就把空值写回去。
            # 所以这里补一次：后台第一次真正应答时，把设置重新读进界面。
            if not self._settings_loaded:
                self._load_settings()

    def _action(self, path: str):
        if getattr(self, '_quitting', False):
            return
        if self._busy:
            self.console_tab.set_notice('已有操作正在处理中，请等待完成。')
            return
        self._busy = True
        action_controls(self,True)
        self.console_tab.set_notice("处理中，请稍候（连接或测试可能需要较长时间）…")
        self._action_worker = _ActionWorker(path)
        self._action_worker.finished.connect(self._action_finished)
        self._action_worker.start()

    def _action_finished(self):
        worker = self._action_worker
        result = worker.response
        msg = result.get("message", "操作完成")
        ok = result.get("success", True)
        if getattr(worker, 'path', '') == 'test-wechat':
            self.console_tab.set_notice(msg, ok=ok, source='test-wechat')
        else:
            self.console_tab.set_notice(msg, ok=ok)
        self._busy = False
        action_controls(self,False)
        self._action_worker = None
        worker.deleteLater()
        self._refresh()

    def _save_settings(self):
        if getattr(self, '_quitting', False):
            return
        if self._busy:
            self.settings_tab.save_lbl.setText('已有操作正在处理中，请等待完成。')
            return
        if not self._settings_loaded:
            self.settings_tab.save_lbl.setText('尚未读取原配置，请先重连后台，避免覆盖已有设置。');return
        try:data = self.settings_tab.collect()
        except Exception as exc:
            self.settings_tab.save_lbl.setText('配置填写有误：'+str(exc));return
        self._busy = True;action_controls(self,True)
        self.settings_tab.save_lbl.setText("正在保存，连接重启时请稍候…")
        self._action_worker = _ActionWorker("settings", data)
        self._action_worker.finished.connect(self._settings_finished)
        self._action_worker.start()

    def _settings_finished(self):
        worker = self._action_worker
        result = worker.response
        msg = result.get("message", "保存完成")
        ok = result.get("success", True)
        self.settings_tab.save_lbl.setStyleSheet(
            f"color: {SUCCESS if ok else ERROR}; font-size: 12px;"
        )
        self.settings_tab.save_lbl.setText(msg)
        if ok:
            self._load_settings()
        self._busy = False
        action_controls(self,False)
        self._action_worker = None
        worker.deleteLater()

    def _request_exit(self):
        if self._quitting:
            return
        self._quitting = True
        self._timer.stop()
        self._finish_exit()

    def _finish_exit(self):
        # 请求不能跨窗口销毁；等待收尾期间托盘和界面仍响应。
        if ((self._action_worker is not None and self._action_worker.isRunning())
                or any(job.isRunning() for job in self._api_jobs) or self._polling):
            QTimer.singleShot(100, self._finish_exit)
            return
        if not self._quit_stop_done:
            def stopped(result):
                if result.get('success') is False and self._backend_proc is None:
                    self._quitting = False
                    self._timer.start(3000)
                    self.showNormal()
                    self.console_tab.set_notice('停止后台失败，尚未退出：' + str(result.get('message', '请重试')), ok=False)
                    return
                self._quit_stop_done = True
                QTimer.singleShot(100, self._finish_exit)
            self._run_api('POST', 'stop', timeout=120, on_done=stopped)
            return
        self.close()
        QApplication.instance().quit()

    def closeEvent(self, event):
        tray = getattr(self, '_tray', None)
        if tray is not None and not getattr(self, '_quitting', False) and tray.hide_window():
            geo = self.geometry()
            self._cfg['geometry'] = {'x': geo.x(), 'y': geo.y(), 'w': geo.width(), 'h': geo.height()}
            try:
                save_gui_config(GUI_CFG, self._cfg)
            except OSError:
                pass
            event.ignore()
            return
        # QThread 仍在请求时销毁窗口会让进程崩溃，也会丢失操作结果。
        if self._action_worker is not None and self._action_worker.isRunning():
            self.console_tab.set_notice("后台操作仍在执行，请等待结果后关闭窗口。", ok=False)
            event.ignore()
            return
        if any(job.isRunning() for job in self._api_jobs):
            self.console_tab.set_notice("后台请求仍在执行，请等待结果后关闭窗口。", ok=False)
            event.ignore()
            return
        # 先停掉 3s 轮询，等后台线程跑完当前一次请求再退出（最长等 ~2s），
        # 否则进程会带着还在跑的 requests 线程硬退。
        try:
            self._timer.stop()
        except Exception:
            pass
        for job in list(self._api_jobs):
            try:
                job.wait(1500)
            except Exception:
                pass
        self._api_jobs.clear()
        worker = self._poll_worker
        if worker is not None:
            if not worker.wait(2000):
                self._timer.start(3000)
                event.ignore()
                return
            self._poll_worker = None
        geo = self.geometry()
        self._cfg["geometry"] = {"x": geo.x(), "y": geo.y(), "w": geo.width(), "h": geo.height()}
        try:
            save_gui_config(GUI_CFG, self._cfg)
        except OSError:
            pass
        if self._backend_proc:
            from src.desktop_startup import stop_backend_process
            stop_backend_process(self._backend_proc)
        if tray is not None:
            tray.icon.hide()
        super().closeEvent(event)


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main():
    # 日志和全局异常钩子先就位：之后任何一步崩了，logs/backend.log 里都有完整堆栈
    try:
        setup_logging(ROOT / "logs")
        install_excepthook()
    except Exception:
        pass  # 冻结后目录不可写也不能挡住启动
    # Windows 高 DPI 支持
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    # Windows 任务栏图标：不设 AppUserModelID 时任务栏会显示 python.exe 的图标，
    # 而不是我们自己的图标。
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "whalegirl.wechat.console"
            )
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("鲸鱼娘 微信控制台")
    # 应用级图标：影响任务栏、Alt+Tab，以及所有没单独设图标的子窗口
    app.setWindowIcon(app_icon())
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if "--sandbox-doctor" in sys.argv:
        from src.sandbox_doctor import main as sandbox_doctor_main
        sys.exit(sandbox_doctor_main())
    if "--self-test" in sys.argv:
        from src.gui.smoke import run
        output = Path(sys.argv[sys.argv.index("--self-test") + 1])
        try:
            run(sys.modules[__name__], output)
        except BaseException:
            import traceback
            output.with_suffix(".error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            sys.exit(1)
    else:
        main()
