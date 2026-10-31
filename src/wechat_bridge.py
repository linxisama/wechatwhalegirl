"""微信接入桥：**读消息走本地数据库（只读）**，**发消息走桌面自动化**。

为什么这么分（2026-10 在本机微信 4.1.15.13 上实测的结论）：

- 收消息：微信 4.x 的会话界面是实时渲染的，UI 自动化只能看到屏幕上的几条，还依赖「窗口在前台
  / 分辨率 / 主题」这些易变条件。而 `wechatauto`（wechatauto-replica）提供了基于本地 SQLCipher
  数据库的增量监听（`WeChatDB` + `Listener`），带水位持久化、重启不重复推送，且**全程只读**，
  不点界面、不切窗口 —— 这既稳又安全。
- 发消息：往数据库写消息不是官方支持的行为（而且要碰加密写回），所以发送仍走键鼠自动化，
  由库负责搜索会话、输入、回车。发送是风险最高的一环，因此本桥加了：
  串行闸门、发送间隔、超时、失败退避、连续失败判定与重连。

**明确不做的事**（见 `封号风险与功能取舍.md`）：不做群发、不做标签群发、不做朋友圈互动、
不做主动加好友/通过好友、不做语音通话、不做「@所有人」、不做批量转发。这些能力即使库里
有（`SendToLabel`/`ForwardToLabel`/`AddLabelMembers`/`SwitchToMoments`/`VoiceCall`/`Poke`），
本项目也不调用。

对外只暴露同步方法（内部会被 async 侧用线程池包起来），失败一律返回 `(False, 原因)`
或 `None`，绝不抛异常打断机器人。
"""
from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .wechat_paths import resolve_database_dir, path_account
from .send_outcome import SendOutcome, not_submitted

# ── 后端标识 ────────────────────────────────────────────────────────────────────
BACKEND_AUTO = "auto"
BACKEND_WECHATAUTO = "wechatauto"   # 微信 4.1.12+，本项目首选
BACKEND_WXAUTO4 = "wxauto4"         # 微信 4.0.x（免费版仅支持到 4.1.8.107）
BACKEND_WXAUTO = "wxauto"           # 微信 3.9.x（经典 wxauto）
BACKEND_MOCK = "mock"               # 模拟后端：不连微信，用于离线测试与控制台自检

SUPPORTED_BACKENDS = (BACKEND_WECHATAUTO, BACKEND_WXAUTO4, BACKEND_WXAUTO, BACKEND_MOCK)

# 群聊会话 username 的后缀（微信约定）
GROUP_SUFFIX = "@chatroom"
# 上次成功连接的账号目录名，记在 data 目录里。
# 为什么要记：多账号机器上"哪个账号在登录"要靠进程句柄猜（见 active_database_account），
# 猜不出来时库会退化成"按 .db 最近修改时间挑"，很容易挑到已经退出的旧账号 ——
# 表现就是"连上了但收不到消息、发出去也失败"。记住上一次真正连通的账号，能把这个抖动压掉。
ACCOUNT_HINT_FILE = "account_hint.json"
# 微信消息类型名 → 我们内部的媒体类型
MEDIA_KINDS = {
    "图片": "image",
    "语音": "voice",
    "视频": "video",
    "动画表情": "emotion",
}
# 系统消息不进入聊天管线
SYSTEM_TYPES = {"系统消息"}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _weixin_process_running() -> bool:
    """本机是否在跑微信 4.x（`weixin.exe`）或旧版 3.9（`wechat.exe`）。"""
    try:
        import psutil
    except Exception:
        return False
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in {"weixin.exe", "wechat.exe"}:
                return True
    except Exception:
        pass
    return False


def _client_executable() -> str:
    """返回正在运行的微信主程序名（用于选择后端）；拿不到返回空串。"""
    try:
        import psutil
    except Exception:
        return ""
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in {"weixin.exe", "wechat.exe"}:
                return name
    except Exception:
        pass
    return ""


def is_admin() -> bool:
    """当前进程是否有管理员权限。

    微信 4.x 的 UIA 发送在低完整性进程里可能被拒（能收不能发），所以控制台要把这件事说清楚。
    """
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def active_database_account(db_dir: str) -> str:
    """根据微信实际打开的数据库选账号，避免仅按文件时间选中已退出的账号。"""
    if not db_dir:
        return ""
    try:
        import psutil
        root = Path(db_dir).resolve()
        accounts = set()
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() != "weixin.exe":
                continue
            try:
                files = proc.open_files()
            except (psutil.Error, OSError):
                continue
            for item in files:
                try:
                    parts = Path(item.path).resolve().relative_to(root).parts
                except (ValueError, OSError):
                    continue
                if len(parts) >= 3 and parts[1].lower() == "db_storage":
                    accounts.add(parts[0])
        # 多账号同时运行时无法唯一判定，保留库原有选择方式。
        return next(iter(accounts)) if len(accounts) == 1 else ""
    except Exception:
        return ""


def account_dir_names(db_dir: str) -> List[str]:
    """根目录下所有"像账号"的子目录（下面有 db_storage）。``all_users`` 之类不算。"""
    if not db_dir:
        return []
    try:
        children = sorted(p for p in Path(db_dir).iterdir() if p.is_dir())
    except OSError:
        return []
    names = []
    for child in children:
        try:
            if (child / "db_storage").is_dir():
                names.append(child.name)
        except OSError:
            continue
    return names


def newest_message_account(db_dir: str) -> str:
    """按"哪个账号最近还有会话在动"兜底判断登录中的账号；判断不出来返回空串。

    为什么需要它：判断"微信正在登录哪个账号"最可靠的信号是微信进程打开的库文件
    （``active_database_account``），但那需要管理员权限 —— 普通权限下 psutil 读不到
    高完整性进程的句柄，只能返回空。退回库自带的"按 .db 修改时间挑"会踩一个恶性循环：
    **我们自己打开某个旧账号的库，就会更新它的 -wal/-shm 时间**，于是旧账号看起来
    "更新"、被反复选中（本机实测就是这样一路连到已退出的"西莲寺紫"上）。

    会话时间是我们读库不会改变的量，所以它比文件时间可靠。代价是要把每个账号的库都
    打开一次（只在进程信号拿不到、且账号多于一个时才做）。
    """
    names = account_dir_names(db_dir)
    if len(names) < 2:
        return ""
    try:
        from wechatauto.db import WeChatDB
    except Exception:
        return ""
    best_name, best_time = "", 0.0
    for name in names:
        try:
            db = WeChatDB(db_dir=db_dir, account=name)
            rows = db.get_sessions(limit=8) or []
        except Exception:
            continue          # 打不开（旧账号密钥没了/库损坏）→ 排除，正是我们想要的
        latest = 0.0
        for row in rows:
            try:
                latest = max(latest, float(row.get("last_time") or 0))
            except (TypeError, ValueError):
                continue
        if latest > best_time:
            best_name, best_time = name, latest
    return best_name


def account_wxid(account: str) -> str:
    """账号目录名 → wxid：去掉末尾的 4 位哈希（与 wechatauto 的规则一致）。

    目录名形如 ``wxid_rerhua6zafil22_7ba3``，而界面/文档里说的是 ``wxid_rerhua6zafil22``。
    不能简单按 ``_`` 切第一段（那会切出 "wxid"）。
    """
    return re.sub(r"_\w{4}$", "", str(account or "").strip())


def resolve_account_dir(db_dir: str, value: str) -> str:
    """把用户填的账号（允许写"账号目录名"或"wxid"）解析成账号目录名；解析不到返回空串。

    目录名形如 ``wxid_rerhua6zafil22_7ba3``，而用户从界面上抄下来的多半是不带 4 位哈希的
    ``wxid_rerhua6zafil22``，所以两种都认。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    names = account_dir_names(db_dir)
    for name in names:
        if name == text:
            return name
    for name in names:
        if account_wxid(name) == text or name.startswith(text + "_"):
            return name
    return ""


def describe_accounts(db_dir: str, strict: bool = False) -> List[dict]:
    """列出所有账号目录，附带昵称与"是否正被微信打开"，供设置页挑选。

    昵称要开一次库才拿得到，所以调用方应放到线程里（每个账号约 0.3~1 秒）。
    ``db_dir`` 留空时自己探测：绝大多数用户的"微信数据库目录"就是留空的（自动探测），
    如果这里不探测，设置页的账号列表会永远是空的。
    """
    try:
        from wechatauto.db import auto_detect_db_dir
        # 列表与连接必须使用同一个发现器，否则自定义盘符能连接却列不出账号。
        db_dir = resolve_database_dir(db_dir, auto_detect_db_dir)
    except Exception:
        if strict:
            raise
        return []
    # 直接调本模块的函数（而不是 import 一份副本）：这样测试里
    # patch("src.wechat_bridge.active_database_account") 依然能生效。
    opened = active_database_account(db_dir)
    result = []
    for name in account_dir_names(db_dir):
        nickname = ""
        try:
            from wechatauto.db import WeChatDB
            db = WeChatDB(db_dir=db_dir, account=name)
            info = db.get_self_info() or {}
            nickname = str(info.get("remark") or info.get("nick_name") or "")
        except Exception:
            nickname = ""
        result.append({
            "account": name,
            "wxid": account_wxid(name),
            "name": nickname,
            "opened": bool(opened) and opened == name,
        })
    return result


class WeChatBridge:
    """微信桥：连接、监听、发送、下载媒体。"""

    # 同一条消息里最多 @ 几个群成员（防止模型列一长串）
    MAX_AT = 5
    # 发送超时（秒）：超过就认为这次发送结果不确定，不再重试，避免重复发送
    SEND_TIMEOUT = 25.0
    # 连续失败多少次就判定掉线并触发重连
    FAIL_LIMIT = 3

    def __init__(self, data_dir: Path, backend: str = BACKEND_AUTO, db_dir: str = "",
                 note: Optional[Callable[[str], None]] = None):
        self.data_dir = Path(data_dir)
        self.media_dir = self.data_dir / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.requested_backend = (backend or BACKEND_AUTO).strip().lower() or BACKEND_AUTO
        self.db_dir = (db_dir or "").strip()
        # 用户在设置里指定的微信账号（账号目录名或 wxid）；留空=自动判断。
        self.configured_account = ""
        self.send_compat = False
        self.auto_open_chat = True
        self.background_send = False
        self.background_submit = 'invoke'
        # 热激活 UIA 只由本机设置页控制；Agent 侧写它会被 agent_control 403。
        self.hot_activate_uia = False
        self._note = note or (lambda text: None)

        self.backend = ""              # 实际使用的后端
        self.online = False
        self.self_id = ""              # 机器人 wxid
        self.account_name = ""         # 机器人昵称
        self.last_error = ""
        self.connected_at = 0.0
        self.last_message_at = 0.0
        self.listen_private = True
        self.listen_groups = True
        self.last_send_at = 0.0
        self.send_ok = 0
        self.send_fail = 0
        self.recv_count = 0
        self.consecutive_fail = 0
        self.degraded = ""             # 降级说明（能收不能发之类）
        self.send_environment = {}
        self._last_send_requires_reconnect = True

        # 需要监听的群（wxauto4 / 经典 wxauto 按会话注册监听时必须先知道它们；
        # wechatauto 走数据库全量监听，用不到这个列表）
        self.target_groups: List[str] = []
        # 模拟模式下"已发送"的记录，供控制台与离线测试查看
        self.sent_log: List[dict] = []

        self._db = None                # WeChatDB
        self._media_lock = threading.Lock()
        self._media_downloader = None
        self._wx = None                # 发送用客户端（wechatauto.WeChat / wxauto4.WeChat / wxauto.WeChat）
        self._listener = None          # wechatauto Listener
        self._poll_thread = None
        self._poll_stop = threading.Event()
        self._listener_epoch = object()
        self._callbacks: List[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()   # 串行闸门：同一时刻只发一条
        self._send_epoch = 0
        self._sends_stopped = False
        self._names_lock = threading.RLock()
        self._names_db = None
        self._names_refresh_at = 0.0
        self._db_dir_hint = ""
        self.account_dir = ""           # 实际使用的账号目录名（诊断用，状态里会显示）
        self.restored_count = 0         # 累计还原过多少个被库按下去的窗口
        self.logs: List[dict] = []

    # ── 日志 ────────────────────────────────────────────────────────────────
    def note(self, text: str) -> None:
        entry = {"time": time.time(), "text": str(text)}
        self.logs.append(entry)
        self.logs = self.logs[-200:]
        try:
            self._note(str(text))
        except Exception:
            pass

    # ── 账号记忆 ────────────────────────────────────────────────────────────
    def _hint_path(self) -> Path:
        return self.data_dir / ACCOUNT_HINT_FILE

    def read_account_hint(self, db_dir: str = "") -> str:
        """上一次真正连通的账号目录名；没有/读不出来返回空串。

        ``db_dir`` 传当前要用的根目录：只有当那个账号目录还在时才认这份记忆，
        用户已经把旧账号删掉时不能拿着一个不存在的目录去连。
        """
        return str(self.read_account_hint_record(db_dir).get("account") or "")

    def read_account_hint_record(self, db_dir: str = "") -> dict:
        """读取账号记忆：``{"account": ..., "source": ...}``。

        ``source`` 记录这个账号是**凭什么判断出来的**（见 ``_choose_account``）：
        进程打开的库最可靠，"库启发式"只是猜。挑账号时要按可靠性排序用它，
        否则一次猜测会被当成事实记下来、下次继续误导。
        """
        try:
            data = json.loads(self._hint_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        account = str(data.get("account") or "").strip()
        if not account:
            return {}
        root = str(db_dir or self._db_dir_hint or "").strip()
        if root:
            try:
                if not (Path(root) / account).is_dir():
                    return {}
            except OSError:
                return {}
        return {"account": account, "source": str(data.get("source") or "unknown")}

    def write_account_hint(self, account: str, source: str = "") -> None:
        account = str(account or "").strip()
        if not account:
            return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._hint_path().write_text(
                json.dumps({"account": account, "source": str(source or "unknown"),
                            "at": time.time()}, ensure_ascii=False),
                encoding="utf-8")
        except OSError as exc:
            self.note(f"账号记忆写入失败：{type(exc).__name__}: {exc}")

    def choose_account(self, db_dir: str) -> Tuple[str, str]:
        """决定连哪个账号目录，返回 ``(账号目录名, 依据说明)``。

        可靠性从高到低：
        0. **用户在设置里指定的账号**（最高优先：一台机器同时登录两个微信号时，
           "哪个账号在登录"没有权威信号，只有用户知道他要让机器人待在哪个号上）；
        1. ``active_database_account``：微信进程正打开着的库（**需要管理员权限**）；
        2. 账号记忆里来源可靠的那条（上次同样靠 0/1/3 判出来的）；
        3. ``newest_message_account``：哪个账号还有会话在动；
        4. 账号记忆里来源不可靠的那条；
        5. 交给库自己按文件时间挑（我们读库会污染这个信号，所以排最后）。
        """
        weak_sources = {"unknown", "", "library", "fallback"}
        configured = resolve_account_dir(db_dir, self.configured_account)
        if configured:
            return configured, "设置里指定的账号"
        if self.configured_account and not configured:
            self.note(f"设置里指定的微信账号「{self.configured_account}」没找到对应的账号目录，"
                      "本次改为自动判断")
        active = active_database_account(db_dir)
        if active:
            return active, "微信进程打开的库"
        record = self.read_account_hint_record(db_dir)
        hinted = str(record.get("account") or "")
        if hinted and str(record.get("source") or "") not in weak_sources:
            return hinted, f"上次判断记录（{record.get('source')}）"
        newest = newest_message_account(db_dir)
        if newest:
            return newest, "该账号还有会话在动"
        if hinted:
            return hinted, "上次记录（来源不确定）"
        return "", "库按数据库文件时间挑（不确定）"

    def _restore_minimized_windows(self) -> int:
        """把 wechatauto 为了操作微信而最小化的窗口还原（本控制台往往就在其中）。

        为什么必须做：`wechatauto` 发送前会**最小化所有与微信窗口重叠的窗口**，
        并且不会自动还原（它默认调用方会连着发好几条）。用户的控制台只要和微信
        有重叠就会被按下去，看起来就是"界面卡死/不见了"。

        代价与取舍：还原后要把库的"已前置"缓存作废，否则下一次发送会以为微信还在
        最前面，键鼠可能点到别的窗口上。所以宁可让下一次发送多花一次扫描时间。
        """
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return 0
        try:
            done = int(gui.release_foreground(True) or 0)
        except Exception:
            try:
                done = int(gui.restore_blockers() or 0)
            except Exception:
                return 0
        # 缓存作废：见上面"代价与取舍"。
        for attr, value in (("_last_visible_ok", False), ("_last_visible_ts", 0.0)):
            try:
                setattr(gui, attr, value)
            except Exception:
                pass
        if done:
            self.restored_count += done
            self.note(f"已还原被微信发送流程最小化的窗口 {done} 个")
        return done

    # ── 后端探测 ────────────────────────────────────────────────────────────
    def detect_backend(self) -> str:
        """按「配置 → 客户端进程 → 可用包」的顺序挑后端。

        显式配置优先，不做进程探测：低权限进程枚举可能看不到高完整性的 `weixin.exe`，
        那时"自动探测不到"会变成误导。只有 `auto` 才去探测。
        """
        if self.requested_backend in SUPPORTED_BACKENDS:
            return self.requested_backend

        client = _client_executable()
        has_wechatauto = _module_available("wechatauto")
        has_wxauto4 = _module_available("wxauto4")
        has_wxauto = _module_available("wxauto")

        if client == "weixin.exe":
            if has_wechatauto:
                return BACKEND_WECHATAUTO
            if has_wxauto4:
                return BACKEND_WXAUTO4
            return BACKEND_MOCK
        if client == "wechat.exe":
            if has_wxauto:
                return BACKEND_WXAUTO
            if has_wechatauto:
                return BACKEND_WECHATAUTO
            return BACKEND_MOCK
        # 探测不到客户端进程：能用的包优先，否则进模拟模式（不假装已连上微信）
        if has_wechatauto:
            return BACKEND_WECHATAUTO
        if has_wxauto4:
            return BACKEND_WXAUTO4
        if has_wxauto:
            return BACKEND_WXAUTO
        return BACKEND_MOCK

    # ── 连接 ────────────────────────────────────────────────────────────────
    def connect(self) -> Tuple[bool, str]:
        with self._lock:
            self.send_environment = {}
            from .wechat_safety import SafetyHold
            hold = SafetyHold(self.data_dir.parent / 'wechat_safety_hold.json').status()
            if self.requested_backend != BACKEND_MOCK and hold.get('active'):
                self.online = False
                self.last_error = hold.get('reason', '账号安全暂停，未连接微信')
                self.send_environment = {'ready':False,'reason':self.last_error,'reason_code':'safety_hold'}
                return False, self.last_error
            backend = self.detect_backend()
            self.backend = backend
            try:
                if self.background_send and backend not in (BACKEND_WECHATAUTO, BACKEND_MOCK):
                    raise ValueError('严格后台模式仅支持 wechatauto；当前后端没有后台发送通道，未启动桌面操作')
                if backend == BACKEND_WECHATAUTO:
                    return self._connect_wechatauto()
                if backend == BACKEND_WXAUTO4:
                    return self._connect_wxauto4()
                if backend == BACKEND_WXAUTO:
                    return self._connect_wxauto()
                return self._connect_mock()
            except Exception as exc:
                self.online = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                from .native_dependencies import dependency_error_hint
                hint = dependency_error_hint(exc)
                if hint:
                    self.last_error += '；' + hint
                self.note(f"微信连接失败（{backend}）：{self.last_error}")
                return False, self.last_error

    def _connect_wechatauto(self) -> Tuple[bool, str]:
        from .wechat_sender import IsolatedWeChatSender
        from wechatauto.db import WeChatDB

        # 只在本次连接中规范化，不改写配置或留下跨账号的环境变量。
        from wechatauto.db import auto_detect_db_dir
        db_dir = resolve_database_dir(self.db_dir, auto_detect_db_dir)
        selected_path_account = path_account(self.db_dir)
        names = account_dir_names(db_dir)
        if (self.configured_account and self.configured_account not in names
                and len([name for name in names if account_wxid(name) == self.configured_account]) > 1):
            raise ValueError('该 wxid 对应多个账号目录，请在账号列表中选择完整目录名，不会自动换号')
        configured = resolve_account_dir(db_dir, self.configured_account)
        if self.configured_account and not configured:
            raise ValueError('指定的微信账号在当前数据目录中不存在；请重新读取账号并选择，不会自动换号')
        if configured and selected_path_account and configured != selected_path_account:
            raise ValueError('数据库路径指定的账号与账号列表选择不一致；请统一后再连接，不会自动换号')
        # 账号选择：见 choose_account 的可靠性排序。选错账号的表现是"连上了但收不到消息、
        # 发出去也失败"，而且很容易一路错下去，所以这里把**依据**也记进运行记录。
        if selected_path_account:
            account, basis = selected_path_account, '数据库路径指定的账号'
        else:
            account, basis = self.choose_account(db_dir or "")
        options = {"account": account} if account else {}
        self._db = WeChatDB(db_dir=db_dir, **options)
        self._db_dir_hint = str(getattr(self._db, "db_dir", "") or db_dir or "")
        # 库内部还有一层"账号自愈"：当前账号解不开时会换成同一主密钥能解开的那个目录。
        # 所以要按**库最终选中的**账号记下来，否则状态里显示的账号会和实际读的库不一致。
        chosen = str(getattr(self._db, "account", "") or "")
        if (configured or selected_path_account) and chosen != account:
            self._db = None
            self._wx = None
            self.account_dir = ''
            raise ValueError('微信数据库返回的账号与指定账号不符，已停止连接；请核对目录和账号授权')
        self.account_dir = chosen
        if account and chosen and chosen != account:
            self.note(f"微信库把账号从 {account} 换成了 {chosen}（当前登录账号），已按它继续")
        if chosen:
            # 依据是"库自己挑的"时不写死记忆：那是最不可靠的一档，写进去下次会被误导。
            self.write_account_hint(chosen, "library" if basis.startswith("库按") else basis)
        names = account_dir_names(db_dir or "")
        if len(names) > 1:
            self.note(f"检测到 {len(names)} 个微信账号目录，本次使用 {chosen or '（未定）'}"
                      f"（依据：{basis}）")
            if basis.startswith("库按") or "不确定" in basis:
                self.note("提示：无法确认微信正在登录哪个账号。请用「以管理员身份启动控制台」，"
                          "否则可能连到已经退出的旧账号上（表现为收不到消息、发送失败）")

        info = {}
        try:
            info = self._db.get_self_info() or {}
        except Exception as exc:
            self.note(f"读取微信账号信息失败：{type(exc).__name__}: {exc}")
        self.self_id = str(info.get("username") or getattr(self._db, "wxid", "") or "")
        self.account_name = str(info.get("remark") or info.get("nick_name") or "")

        # 旧兼容标记不再放行任何进程写入；自动打开会话也由发送worker硬性停用。
        # Qt/COM 控件不跨线程、更不能把发送库的原生异常带进 GUI 主进程。
        self._wx = IsolatedWeChatSender(self.data_dir,
                                        str(Path(self._db_dir_hint) / chosen) if chosen else '',
                                        compat=lambda: self.send_compat,
                                        open_chat=lambda: self.auto_open_chat,
                                        background=lambda: self.background_send,
                                        submit=lambda: self.background_submit,
                                        hot=lambda: self.hot_activate_uia)

        # 发送能力体检：拿不到主窗口就只能收不能发，这里如实降级而不是假装成功。
        self.degraded = ""
        if not is_admin():
            self.degraded = ("当前进程没有管理员权限，微信 4.x 的发送可能被拒绝（能收不能发）。"
                             "请用「以管理员身份运行」启动控制台。")
            self.note(self.degraded)
        self.online = True
        self.connected_at = time.time()
        self.last_error = ""
        self.consecutive_fail = 0
        self.note(f"微信连接成功（wechatauto）｜账号：{self.account_name or self.self_id or '未知'}"
                  f"｜数据库：{self._db_dir_hint or '自动探测'}")
        return True, "微信已连接"

    def _connect_wxauto4(self) -> Tuple[bool, str]:
        import wxauto4 as wx

        # 关掉遥测：不把使用数据发给第三方。
        try:
            wx.WxParam.TELEMETRY_ENABLED = False
        except Exception:
            pass
        # ads=False 避免库自己往界面里插广告；resize=False 不动用户的窗口大小。
        self._wx = wx.WeChat(start_listener=False, ads=False, resize=False)
        online = True
        try:
            online = bool(self._wx.IsOnline())
        except Exception:
            online = True
        self.online = online
        self.connected_at = time.time()
        self.consecutive_fail = 0
        try:
            info = self._wx.GetMyInfo() or {}
            self.account_name = str(info.get("昵称") or info.get("nickname") or "")
        except Exception:
            self.account_name = ""
        if not online:
            self.last_error = "微信未登录或主窗口不可用"
            self.note(f"wxauto4 已加载但微信未就绪：{self.last_error}")
            return False, self.last_error
        self.degraded = "wxauto4 免费版只支持到微信 4.1.8；更高版本请改用 wechatauto。"
        self.note(f"微信连接成功（wxauto4）｜账号：{self.account_name or '未知'}")
        return True, "微信已连接（wxauto4）"

    def _connect_wxauto(self) -> Tuple[bool, str]:
        from wxauto import WeChat

        self._wx = WeChat()
        self.online = True
        self.connected_at = time.time()
        self.consecutive_fail = 0
        self.account_name = str(getattr(self._wx, "nickname", "") or "")
        self.degraded = "经典 wxauto 只支持微信 3.9.x；4.x 客户端请改用 wechatauto。"
        self.note(f"微信连接成功（wxauto 3.9）｜账号：{self.account_name or '未知'}")
        return True, "微信已连接（wxauto 3.9）"

    def _connect_mock(self) -> Tuple[bool, str]:
        self.online = True
        self.account_name = "模拟微信"
        self.self_id = "wxid_mock_bot"
        self.connected_at = time.time()
        self.consecutive_fail = 0
        self.backend = BACKEND_MOCK
        self.degraded = ("当前是模拟模式：没有可用的微信后端（或没检测到微信客户端），"
                         "消息不会真的发出去，只用于界面与流程验证。")
        self.note("已进入模拟模式（未连接真实微信）")
        return True, "模拟模式已就绪"

    def disconnect(self) -> None:
        """断开连接（**绝不结束用户的微信进程**）。

        踩过的坑（这是用户报的"连接不上微信 / 连接不成功"的真凶）：
        wechatauto 的 ``WeChat.ShutDown()`` 名字像"收尾清理"，实现却是
        ``os.system(f'taskkill /f /pid {微信主进程}')`` —— 直接强杀用户的微信。
        本方法原来写的是 ``for closer in ("Close", "ShutDown")``，于是：

        * 关掉控制台 → 微信被强杀；
        * 连接守护自动重连（每次重连先 disconnect）→ 微信被杀一次；
        * 微信重启期间数据库还没打开，"哪个账号在登录"判不出来 → 退化成按文件时间挑
          → 挑到已退出的旧账号 → 收不到消息、发出去也失败。

        所以这里只做"断开我们自己的连接"：停监听、放掉引用。永远不要调用客户端对象上
        任何名字带 ShutDown / Shutdown / Kill / Exit / Quit 的方法。
        """
        with self._lock:
            self._send_epoch = getattr(self, '_send_epoch', 0) + 1
        self.stop_listening()
        with self._lock:
            self.online = False
            self._wx = None
            self._db = None

    def reconnect(self) -> Tuple[bool, str]:
        self.disconnect()
        time.sleep(1.0)
        return self.connect()

    # ── 监听 ────────────────────────────────────────────────────────────────
    def start_listening(self, callback: Callable[[dict], None]) -> Tuple[bool, str]:
        from .wechat_safety import SafetyHold
        if SafetyHold(self.data_dir.parent / 'wechat_safety_hold.json').status().get('active'):
            return False, '账号安全暂停，未启动微信监听'
        """开始接收消息。`callback(我们的消息字典)` 会在后台线程被调用。"""
        with self._lock:
            # 旧线程可能还在阻塞读取，不能 clear 它仍持有的停止信号。
            self._poll_stop.set()
            self._poll_stop = threading.Event()
            self._listener_epoch = object()
            epoch = self._listener_epoch
            if callback not in self._callbacks:
                self._callbacks.append(callback)
        if self.backend == BACKEND_MOCK:
            return True, "模拟模式无需监听"
        try:
            if self.backend == BACKEND_WECHATAUTO:
                return self._listen_wechatauto(epoch)
            if self.backend == BACKEND_WXAUTO4:
                return self._listen_wxauto4()
            if self.backend == BACKEND_WXAUTO:
                return self._listen_wxauto()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            with self._lock:
                if self._listener_epoch is epoch:
                    self.stop_receiving()
                    self.last_error = detail
                    self.note(f"启动消息监听失败：{self.last_error}")
            return False, detail
        with self._lock:
            if self._listener_epoch is epoch:
                self.stop_receiving()
        return False, "当前后端不支持监听"

    def _emit(self, raw: dict, *, epoch=None) -> None:
        with self._lock:
            stop_event = self._poll_stop
            generation = self._listener_epoch
            if stop_event.is_set() or (epoch is not None and epoch is not generation):
                return
            callbacks = tuple(self._callbacks)
        raw = dict(raw)
        raw["_received_at"] = time.time()
        # 联系人库解密可能耗时，放在监听回调线程，不能堵住 HTTP/模型事件循环。
        if self._db is not None:
            username = str(raw.get("username") or "")
            sender = str(raw.get("sender_username") or raw.get("sender") or "")
            resolved_name = self.display_name(username)
            if resolved_name != username or not raw.get('target_name'):
                raw['target_name'] = resolved_name
            if not raw.get("sender_name"):
                raw["sender_name"] = self.display_name(sender)
        with self._lock:
            # 联系人查询期间可以停止/切账号。旧读取不能接上新一轮 callbacks，
            # 也不能给当前控制台增加一条假收件计数。
            if (stop_event.is_set() or self._poll_stop is not stop_event
                    or self._listener_epoch is not generation):
                return
            self.last_message_at = raw["_received_at"]
            self.recv_count += 1
        for callback in callbacks:
            if stop_event.is_set() or self._listener_epoch is not generation:
                return
            try:
                callback(raw)
            except Exception as exc:
                if not stop_event.is_set() and self._listener_epoch is generation:
                    self.note(f"消息处理回调异常：{type(exc).__name__}: {exc}")

    def _listen_wechatauto(self, epoch=None) -> Tuple[bool, str]:
        from wechatauto.db import Listener

        if epoch is None:
            epoch = self._listener_epoch
        watermark = self.data_dir / "listener_watermark.json"
        from .wechat_listener import ListenerDatabase
        watched_db = ListenerDatabase(self._db, self.target_groups, self.listen_private, self.listen_groups,
                                      note=self.note)
        listener = Listener(watched_db, interval=0.25, watermark_file=str(watermark))
        with self._lock:
            # 冷数据库/监听器构造可跨过一次停止与重启；迟到对象不能覆盖新监听。
            if self._listener_epoch is not epoch or self._poll_stop.is_set():
                return False, '监听准备期间已停止，本轮未启动'
            self._listener = listener

        def on_message(msg: dict, _listener) -> None:
            if self._listener is listener:
                self._emit(msg, epoch=epoch)

        # discover=True：运行中新建的会话也会被监听（否则新群永远不会被处理）
        listener.add_all(on_message, discover=True)
        # 播种完立刻把基线强制落盘。库只在**成功投递**后才 save_watermark()，冷启动一条都没
        # 投递就退出时 listener_watermark.json 根本不存在；下一个 Listener 于是把每个会话重新
        # 播种成「当前最新 seq」，而增量查询是 sort_seq > 水位 —— 停机窗口里到达的那条消息 seq
        # 恰好等于该值，永久跳过。先写基线，下次启动 _load_watermark 就命中它，窗口内的消息照常投递。
        # 位置在 start() 之前：此刻只有播种结果，没有轮询线程并发写同一份水位（避免旧 payload 后落盘）。
        save_baseline = getattr(listener, "save_watermark", None)
        if callable(save_baseline):
            try:
                save_baseline(force=True)
            except Exception as exc:
                # 落盘失败只影响「重启窗口不丢消息」这一层加固，不能拦住监听启动。
                self.note(f"监听水位基线落盘失败：{type(exc).__name__}: {exc}")
        with self._lock:
            if self._listener_epoch is not epoch or self._poll_stop.is_set():
                return False, '监听准备期间已停止，本轮未启动'
        listener.start()
        with self._lock:
            current = self._listener_epoch is epoch and not self._poll_stop.is_set()
        if not current:
            # start 本身也可阻塞；收发闸门已失效，只收尾当前局部对象。
            listener.stop()
            return False, '监听准备期间已停止，本轮未启动'
        self.note("消息监听已启动（wechatauto 数据库增量监听，只读）")
        return True, "监听已启动"

    def _listen_wxauto4(self) -> Tuple[bool, str]:
        """wxauto4 只能按会话注册监听，因此按配置的群列表注册。

        代价：私聊消息收不到（库没有全局监听接口）。这个限制在状态里如实说明，
        不让用户以为"私聊没反应是坏了"。
        """
        groups = [g for g in (self.target_groups or []) if g]
        if not groups:
            self.degraded = (self.degraded + " " if self.degraded else "") + \
                "wxauto4 后端需要填写要监听的群名；未填写时收不到群消息，私聊也不支持。"
            self.note("wxauto4 后端没有配置监听群，收不到消息")
            return False, "请先填写要监听的群名"

        epoch = self._listener_epoch
        def make_callback(group: str):
            def on_message(msg, chat=None):
                raw = {
                    "username": getattr(chat, "who", "") or group,
                    "type": getattr(msg, "type", ""),
                    "sender_id": getattr(msg, "sender", "") or "",
                    "sender_username": getattr(msg, "sender", "") or "",
                    "create_time": time.time(),
                    "content": getattr(msg, "content", "") or "",
                    "local_id": getattr(msg, "id", 0) or 0,
                    "shard": "",
                    "attr": getattr(msg, "attr", "friend"),
                }
                self._emit(raw, epoch=epoch)
            return on_message

        for group in groups:
            try:
                self._wx.AddListenChat(group, make_callback(group))
            except Exception as exc:
                self.note(f"监听群「{group}」失败：{type(exc).__name__}: {exc}")
        self._wx.StartListening()
        self.note(f"消息监听已启动（wxauto4，仅 {len(groups)} 个配置群）")
        return True, "监听已启动（仅配置的群）"

    def _listen_wxauto(self) -> Tuple[bool, str]:
        """经典 wxauto：注册会话后轮询 `GetListenMessage()`。"""
        groups = [g for g in (self.target_groups or []) if g]
        for group in groups:
            try:
                self._wx.AddListenChat(who=group, savepic=True, savefile=True, savevoice=True)
            except Exception as exc:
                self.note(f"监听群「{group}」失败：{type(exc).__name__}: {exc}")
        stop_event, epoch, client = self._poll_stop, self._listener_epoch, self._wx

        def poll() -> None:
            while not stop_event.is_set() and self._listener_epoch is epoch:
                try:
                    messages = client.GetListenMessage() or {}
                except Exception as exc:
                    if stop_event.is_set() or self._listener_epoch is not epoch:
                        return
                    self.note(f"读取新消息失败：{type(exc).__name__}: {exc}")
                    stop_event.wait(2)
                    continue
                for chat_name, items in messages.items():
                    for item in items or []:
                        raw = {
                            "username": chat_name,
                            "type": {"friend": "文本", "self": "文本"}.get(
                                getattr(item, "type", ""), getattr(item, "type", "") or "文本"),
                            "sender_id": getattr(item, "sender", "") or "",
                            "sender_username": getattr(item, "sender", "") or "",
                            "create_time": time.time(),
                            "content": getattr(item, "content", "") or "",
                            "local_id": 0,
                            "shard": "",
                            "attr": getattr(item, "type", "friend"),
                        }
                        self._emit(raw, epoch=epoch)
                stop_event.wait(1.0)

        self._poll_thread = threading.Thread(target=poll, name="wechat-poll", daemon=True)
        self._poll_thread.start()
        self.note("消息监听已启动（经典 wxauto 轮询）")
        return True, "监听已启动"

    def stop_listening(self) -> None:
        self.stop_receiving()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        # IsolatedWeChatSender 没有 StopListening，不能通过动态代理去定位微信。
        from .wechat_sender import IsolatedWeChatSender
        if self._wx is not None and not isinstance(self._wx, IsolatedWeChatSender):
            try:
                self._wx.StopListening(remove=False)
            except Exception:
                pass
        self._poll_thread = None

    def stop_receiving(self):
        with self._lock:
            self._poll_stop.set()
            self._listener_epoch = None
            if hasattr(self, '_runtime_receive_epoch'):
                self._runtime_receive_epoch = None
            listener, self._listener = self._listener, None
        if listener is not None:
            # 必须真的回收监听线程：start_listening 每次都新建 Listener，且新旧共用同一个
            # 水位文件，只置停止标志会让旧线程继续每 0.25s 读库并争抢水位落盘（僵尸监听）。
            # stop 放在锁外——它要 join 轮询/回调线程，而回调线程可能正卡在 _emit 里等这把锁。
            try:
                listener.stop()
            except Exception:
                # 回收失败不改变「已停止接收」这个语义，也不能拦住后续重连。
                pass

    def cancel_pending_sends(self):
        from .wechat_sender import IsolatedWeChatSender
        with self._lock:
            self._send_epoch = getattr(self, '_send_epoch', 0) + 1
            self._sends_stopped = True
            if isinstance(self._wx, IsolatedWeChatSender):
                self._wx.stop()

    def resume_sends(self):
        from .wechat_sender import IsolatedWeChatSender
        with self._lock:
            if isinstance(self._wx, IsolatedWeChatSender):
                self._wx.resume()
            self._sends_stopped = False

    # ── 发送 ────────────────────────────────────────────────────────────────
    def resolve_target_name(self, target: str) -> str:
        """能不能把会话标识翻成人能看、能搜索的名字；翻不出来返回空串。

        为什么要单独暴露：群名解析不出来时，发送阶段才报错的话，
        模型已经白算了一遍回复、界面还显示"待核实"。入口处先问一句更省事。
        """
        target = str(target or "").strip()
        if not target:
            return ""
        # 读不到数据库（mock/未连接）时返回 "unknown"：这种情况**不能**当"解析失败"处理，
        # 否则模拟模式和单元测试里所有群聊都会被误拦。
        if self._db is None:
            return "unknown"
        names = self._contact_names(refresh=True)
        current = str(names.get(target) or "").strip()
        if not current or current == target:
            current = str(self.display_name(target) or "").strip()
        from .wechat_send_worker import valid_chat_name
        if not current or current == target or not valid_chat_name(current):
            return ""
        return current

    def _chat_name(self, target: str, target_name: str = "") -> str:
        """刷新当前账号的备注/群名；不能把内部 ID 当作界面搜索词。"""
        target = str(target or '').strip()
        names = self._contact_names(refresh=True)
        current = str(names.get(target) or '').strip()
        if not current or current == target:
            current = self.display_name(target)
        from .wechat_send_worker import valid_chat_name
        if not current or current == target:
            if self._db is not None and not valid_chat_name(target):
                raise ValueError('未解析出当前账号的目标会话名称，本次未操作微信；请核对群名或联系人备注')
            current = str(target_name or '').strip() or target
        if not valid_chat_name(current):
            raise ValueError('未解析出目标会话名称，未操作微信；请核对当前账号、群名或联系人备注，不能使用 wxid/@chatroom 搜索')
        if sum(str(value).strip() == current for value in names.values()) > 1:
            raise ValueError('目标会话显示名重复，无法唯一确认，本次未操作微信；请在微信中设置可区分的备注')
        self.note(f'发送目标解析｜账号：{self.self_id or self.account_dir or "未知"}｜目标：{target}｜名称：{current}')
        return current

    def send_text(self, target: str, text: str, target_name: str = "",
                  at: Optional[List[str]] = None) -> Tuple[bool, str]:
        """发文本（可 @ 若干群成员）。返回 (是否成功, 说明)。

        @ 只对群聊有意义；`at` 传的是群成员**昵称**。
        """
        epoch = getattr(self, '_send_epoch', 0)
        if getattr(self, '_sends_stopped', False):
            return not_submitted('发送已停止，本次未操作微信')
        from .wechat_safety import service_target, SafetyHold
        if service_target({'target':target}):
            return not_submitted('官方/系统账号不允许机器人自动回复，未操作微信')
        if SafetyHold(self.data_dir.parent / 'wechat_safety_hold.json').status().get('active'):
            return not_submitted('账号安全暂停，未操作微信')
        text = "" if text is None else str(text)
        if not text.strip():
            return not_submitted("内容为空，未发送")
        if not self.online:
            return not_submitted("微信未连接")
        if self.backend == BACKEND_MOCK:
            self._record_mock_send(target, text, target_name, at)
            return True, "模拟模式已记录（未真实发送）"

        at_list = [str(x).strip() for x in (at or []) if str(x).strip()][: self.MAX_AT]
        with self._send_lock:
            if epoch != getattr(self, '_send_epoch', 0) or getattr(self, '_sends_stopped', False):
                return not_submitted('旧发送任务已停止，本次未操作微信')
            self._last_send_requires_reconnect = True
            outcome = None
            resolved = False
            try:
                who = self._chat_name(target, target_name)
                # 联系人查询可能阻塞；恢复发送只允许新任务，不能复活旧解析。
                if epoch != getattr(self, '_send_epoch', 0) or getattr(self, '_sends_stopped', False):
                    return not_submitted('旧发送任务已停止，本次未操作微信')
                resolved = True
                outcome = self._send_text_locked(who, text, at_list)
                ok, message = outcome
            except Exception as exc:
                if isinstance(exc, ValueError):
                    self._last_send_requires_reconnect = False
                ok, message = False, f"{type(exc).__name__}: {exc}"
                if isinstance(exc, ValueError) and not resolved:
                    outcome = not_submitted(message)
            self._after_send(ok, message, target)
            return SendOutcome(ok, message, getattr(outcome, 'diagnostic', {}))

    def _send_text_locked(self, who: str, text: str, at_list: List[str]) -> Tuple[bool, str]:
        from .wechat_sender import IsolatedWeChatSender
        if self.background_send and not isinstance(self._wx, IsolatedWeChatSender):
            self._last_send_requires_reconnect = False
            return not_submitted('严格后台等待：当前后端没有后台发送通道，不会退回前台操作')
        if self.backend == BACKEND_WECHATAUTO:
            # 当前依赖的真 @ 路径使用 OCR 面板且不支持昵称列表；只发普通文本。
            body = (" ".join("@" + name for name in at_list) + " " + text) if at_list else text
            result = self._wx.SendMsg(body, who=who, at=None, exact=True)
            from .wechat_sender import SendResult
            if isinstance(result, SendResult):
                self._last_send_requires_reconnect = result.requires_reconnect
                if result.diagnostic:
                    self.send_environment = result.diagnostic
            return SendOutcome(bool(result), "已发送" if result else f"发送接口返回失败：{result}",
                               getattr(result, 'diagnostic', {}))
        if self.backend == BACKEND_WXAUTO4:
            result = self._wx.SendMsg(text, who=who, at=(at_list or None), exact=True)
            return (True, "已发送") if bool(result) else (False, "发送接口返回失败")
        if self.backend == BACKEND_WXAUTO:
            self._wx.SendMsg(text, who=who, at=(at_list or None))
            return True, "已发送"
        return False, "未知后端"

    def send_files(self, target: str, paths: List[str], target_name: str = "",
                   as_image: bool = False) -> Tuple[bool, str]:
        """发图片或文件。`as_image=True` 时优先走"图片"通道（聊天里直接显示图）。"""
        epoch = getattr(self, '_send_epoch', 0)
        if getattr(self, '_sends_stopped', False):
            return not_submitted('发送已停止，本次未操作微信')
        from .wechat_safety import service_target, SafetyHold
        if service_target({'target':target}) or SafetyHold(self.data_dir.parent / 'wechat_safety_hold.json').status().get('active'):
            return not_submitted('官方/系统账号或安全暂停：未发送文件')
        if self.background_send:
            return not_submitted('严格后台模式仅支持文字，本次未发送图片或文件')
        files = [str(p) for p in (paths or []) if str(p).strip()]
        if not files:
            return not_submitted("没有要发送的文件")
        for path in files:
            if not Path(path).is_file():
                return not_submitted(f"文件不存在：{path}")
        if not self.online:
            return not_submitted("微信未连接")
        if self.backend == BACKEND_MOCK:
            for path in files:
                self._record_mock_send(target, f"[文件]{Path(path).name}", target_name, None)
            return True, "模拟模式已记录（未真实发送）"

        with self._send_lock:
            if epoch != getattr(self, '_send_epoch', 0) or getattr(self, '_sends_stopped', False):
                return not_submitted('旧发送任务已停止，本次未操作微信')
            self._last_send_requires_reconnect = True
            outcome = None
            resolved = False
            try:
                who = self._chat_name(target, target_name)
                if epoch != getattr(self, '_send_epoch', 0) or getattr(self, '_sends_stopped', False):
                    return not_submitted('旧发送任务已停止，本次未操作微信')
                resolved = True
                outcome = self._send_files_locked(who, files, as_image)
                ok, message = outcome
            except Exception as exc:
                if isinstance(exc, ValueError):
                    self._last_send_requires_reconnect = False
                ok, message = False, f"{type(exc).__name__}: {exc}"
                if isinstance(exc, ValueError) and not resolved:
                    outcome = not_submitted(message)
            self._after_send(ok, message, target)
            return SendOutcome(ok, message, getattr(outcome, 'diagnostic', {}))

    def _send_files_locked(self, who: str, files: List[str], as_image: bool) -> Tuple[bool, str]:
        from .wechat_sender import IsolatedWeChatSender
        if isinstance(self._wx, IsolatedWeChatSender):
            result = (self._wx.send_images(files, who) if as_image
                      else self._wx.SendFiles(files, who=who, exact=True))
            self._last_send_requires_reconnect = result.requires_reconnect
            if result.diagnostic:
                self.send_environment = result.diagnostic
            return SendOutcome(bool(result), str(result), result.diagnostic)
        if as_image and self.backend == BACKEND_WECHATAUTO:
            try:
                from wechatauto import quick_send_image
            except Exception:
                quick_send_image = None
            if quick_send_image is not None:
                for path in files:
                    if bool(quick_send_image(path, who, verify=False)):
                        continue
                    # 图片通道失败就用文件通道兜底（至少把图发出去）
                    result = self._wx.SendFiles(path, who=who, exact=True)
                    if not bool(result):
                        return False, "图片发送失败"
                return True, "已发送"
        send_method = getattr(self._wx, "SendFiles", None)
        if send_method is None:
            return False, "当前后端不支持发送文件"
        for path in files:
            try:
                result = send_method(path, who=who, exact=True)
            except TypeError:
                result = send_method(path, who=who)
            if not bool(result):
                return False, "文件发送失败"
        return True, "已发送"

    def _record_mock_send(self, target: str, text: str, target_name: str,
                          at: Optional[List[str]]) -> None:
        entry = {
            "time": time.time(), "target": target, "target_name": target_name,
            "text": text, "at": list(at or []),
        }
        self.sent_log.append(entry)
        self.sent_log = self.sent_log[-500:]
        # 模拟模式下"发送"没有真的发出去，但落一份本地记录文件，
        # 让用户能像看真实发送一样翻查：%LOCALAPPDATA%\WhaleGirlWeChat\data\...\mock_sent.jsonl
        try:
            path = self.data_dir / "mock_sent.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            self.note(f"模拟发送记录写入失败：{type(exc).__name__}: {exc}")

    def _after_send(self, ok: bool, message: str, target: str) -> None:
        self.last_send_at = time.time()
        if ok:
            self.send_ok += 1
            self.consecutive_fail = 0
        else:
            self.send_fail += 1
            if self._last_send_requires_reconnect:
                self.consecutive_fail += 1
            self.last_error = message
            self.note(f"发送失败（{target}）：{message}")
            if self._last_send_requires_reconnect and self.consecutive_fail >= self.FAIL_LIMIT:
                self.note(f"连续 {self.consecutive_fail} 次发送失败，判定与微信的连接已失效，稍后自动重连")
        # 发送流程会把与微信重叠的窗口按下去（含本控制台），发完立刻还原，
        # 否则用户看到的是"程序自己消失了"。成功失败都要还原。
        if not self.background_send:
            self._restore_minimized_windows()

    def mark_send_timeout(self, reason: str = "发送超时") -> None:
        """发送卡住超时时由传输层回报（那时发送线程还没返回，走不到 _after_send）。

        记一次失败并累计连败次数：连续几次之后连接守护会重连，并换掉卡死的发送线程。
        """
        self.send_fail += 1
        self.consecutive_fail += 1
        self.last_error = reason
        self.last_send_at = time.time()
        self.note(f"{reason}；本次不重发（结果不确定），等连接守护处理")

    # ── 媒体下载 ────────────────────────────────────────────────────────────
    def download_media(self, chat: str, local_id: Any, msg_type: str, shard: str = "") -> Optional[str]:
        from .wechat_safety import SafetyHold
        if SafetyHold(self.data_dir.parent / 'wechat_safety_hold.json').status().get('active'):
            return None
        """把收到的图片/语音/视频/文件落到本机，返回路径；失败返回 None。

        这是给本地 OCR 与本地语音识别用的：图片和语音都不出本机。
        """
        if self.backend == BACKEND_MOCK:
            return None
        if self._db is None:
            return None
        try:
            local_id = int(local_id)
        except (TypeError, ValueError):
            return None
        try:
            from wechatauto.media import MediaDownloader

            kind = MEDIA_KINDS.get(msg_type, msg_type)
            if kind not in {'image', 'voice', 'video', 'file'}:
                return None
            with self._media_lock:
                db = self._db
                if db is None:
                    return None
                downloader = self._media_downloader
                if downloader is None or downloader.db is not db:
                    downloader = self._media_downloader = MediaDownloader(db, save_dir=str(self.media_dir))
                # 库默认的文件名不含分片；同号消息会覆盖图片，OCR 与视觉可能读到不同内容。
                scope = hashlib.sha256(f'{self.self_id}|{chat}|{shard}'.encode('utf-8')).hexdigest()[:24]
                folder = self.media_dir / scope
                folder.mkdir(parents=True, exist_ok=True)
                kwargs = {'save_dir': str(folder)}
                if shard:
                    kwargs['shard'] = shard
                if kind == "image":
                    path = downloader.download_image(chat, local_id, tier='best', **kwargs)
                elif kind == "voice":
                    path = downloader.download_voice(chat, local_id, **kwargs)
                elif kind == "video":
                    path = downloader.download_video(chat, local_id, **kwargs)
                else:
                    path = downloader.download_file(chat, local_id, **kwargs)
            if path and Path(path).is_file():
                return str(path)
            return None
        except Exception as exc:
            self.note(f"下载媒体失败（{msg_type} #{local_id}）：{type(exc).__name__}: {exc}")
            return None

    # ── 会话信息 ────────────────────────────────────────────────────────────
    def list_sessions(self, limit: int = 50) -> List[dict]:
        if self._db is None:
            return []
        try:
            rows = self._db.get_sessions(limit=limit) or []
        except Exception as exc:
            self.note(f"读取会话列表失败：{type(exc).__name__}: {exc}")
            return []
        result = []
        for row in rows:
            username = str(row.get("username") or "")
            result.append({
                "username": username,
                "name": self.display_name(username),
                "kind": "group" if username.endswith(GROUP_SUFFIX) else "private",
                "unread": row.get("unread") or 0,
                "summary": str(row.get("summary") or "")[:60],
                "time": row.get("last_time") or 0,
            })
        return result

    def list_groups(self) -> List[dict]:
        if self._db is None:
            return []
        try:
            groups = self._db.get_groups() or []
        except Exception as exc:
            self.note(f"读取群列表失败：{type(exc).__name__}: {exc}")
            return []
        return [{"username": str(g.get("username") or ""), "name": str(g.get("name") or "")}
                for g in groups]

    def _contact_names(self, refresh: bool = False) -> dict:
        if self._db is None:
            return {}
        with self._names_lock:
            getter = getattr(self._db, 'nickname_map', None)
            if not callable(getter):
                return {}
            try:
                now = time.monotonic()
                fresh = refresh or self._names_db is not self._db or now - self._names_refresh_at >= 30
                names = getter(refresh=fresh)
                if not isinstance(names, dict):
                    return {}
                if fresh:
                    self._names_db, self._names_refresh_at = self._db, now
                return names
            except Exception as exc:
                self.note(f'读取会话名称失败：{type(exc).__name__}')
                return {}

    def display_name(self, username: str, fallback: str = "") -> str:
        """会话显示名：群名 / 好友备注，拿不到就退回 username。"""
        if not username:
            return fallback
        if self._db is not None:
            try:
                mapped = self._contact_names().get(username)
                if isinstance(mapped, str) and mapped and mapped != username:
                    return mapped
                if username.endswith(GROUP_SUFFIX):
                    name = self._db.group_id_to_name(username)
                else:
                    name = self._db.get_nickname(username)
                if isinstance(name, str) and name:
                    return str(name)
            except Exception:
                pass
        return fallback or username

    def resolve_session(self, text: str) -> str:
        """把用户填的「群名/备注/wxid」解析成会话 username；解析不到原样返回。"""
        text = (text or "").strip()
        if not text or self._db is None:
            return text
        try:
            if text.endswith(GROUP_SUFFIX):
                return text
            room = self._db.group_name_to_id(text)
            if room:
                return str(room)
            user = self._db.username_by_nickname(text)
            if user:
                return str(user)
        except Exception:
            pass
        return text

    def member_names(self, chat: str) -> List[str]:
        """群成员昵称列表（供 @ 解析与"@群友"插件用）。"""
        if self._db is None or not chat.endswith(GROUP_SUFFIX):
            return []
        try:
            members = self._db.get_group_members(chat) or []
        except Exception:
            return []
        names = []
        for member in members:
            name = str(member.get("name") or member.get("nick_name") or member.get("remark") or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    # ── 状态 ────────────────────────────────────────────────────────────────
    def inspect_send_environment(self, who=''):
        from .wechat_safety import SafetyHold
        if SafetyHold(self.data_dir.parent / 'wechat_safety_hold.json').status().get('active'):
            self.send_environment = {'ready':False, 'reason':'账号安全暂停，未检查微信窗口', 'reason_code':'safety_hold'}
            return dict(self.send_environment)
        from .wechat_sender import IsolatedWeChatSender
        if self.background_send and self.backend != BACKEND_MOCK and not isinstance(self._wx, IsolatedWeChatSender):
            self.send_environment = {'ready': False, 'background_send': True,
                'reason': '严格后台等待：当前后端没有后台发送通道，请使用 wechatauto；不会退回前台操作'}
            return dict(self.send_environment)
        if not isinstance(self._wx, IsolatedWeChatSender):
            return {'ready': None, 'reason': '当前后端不做真实窗口检查'}
        if not self._send_lock.acquire(timeout=2):
            return {'ready': False, 'reason': '发送通道正忙，请稍后检查'}
        try:
            result = self._wx.inspect_environment(who) if who else self._wx.inspect_environment()
            self.send_environment = result.diagnostic or {'ready': False, 'reason': str(result)}
            return self.send_environment
        finally:
            self._send_lock.release()

    def is_receiving(self) -> bool:
        """只检查本桥当前本地接收线程，不读取窗口、数据库或服务文件。"""
        with self._lock:
            if self._poll_stop.is_set():
                return False
            listener = self._listener
            if listener is not None:
                # 只适配本项目已验证的 wechatauto.db.Listener 私有活性字段。
                # 未知版本缺字段或检查异常时不能把对象引用当作接收存活。
                if self.backend != 'wechatauto':
                    return False
                try:
                    thread, stopped = listener._thread, listener._stop
                    return bool(thread is not None and thread.is_alive() and not stopped.is_set())
                except (AttributeError, TypeError, RuntimeError):
                    return False
            thread = self._poll_thread
            return bool(thread is not None and thread.is_alive())

    def status(self) -> dict:
        listening = self.is_receiving()
        ready = self.send_environment.get('ready')
        send_state = ('disconnected' if not self.online else 'ready' if ready is True
                      else 'waiting' if ready is False else 'unchecked')
        return {
            "backend": self.backend or "未连接",
            "requested_backend": self.requested_backend,
            "online": bool(self.online),
            "read_state": 'listening' if self.online and listening else 'connected' if self.online else 'disconnected',
            "send_state": send_state,
            # 交接指南第 5 节要求"明确原因码"：只给 waiting 这个状态没法定位，
            # 控制台与报告都需要机器可读的 code（accessibility_unavailable/minimized/…）。
            "send_code": str((self.send_environment or {}).get('waiting_code') or ''),
            "send_reason": str((self.send_environment or {}).get('reason') or ''),
            "client_login_state": 'simulated' if self.backend == BACKEND_MOCK else 'unknown',
            "account": self.account_name,
            "self_id": self.self_id,
            "account_dir": self.account_dir,
            "db_dir": self._db_dir_hint,
            "admin": is_admin(),
            "degraded": self.degraded,
            "error": self.last_error,
            "send_environment": (dict(self.send_environment) if self.online else
                                 {'ready': False, 'reason': '微信读取连接已断开，请先重新连接'}),
            "connected_at": self.connected_at,
            "last_message_at": self.last_message_at,
            "last_send_at": self.last_send_at,
            "sent": self.send_ok,
            "send_failed": self.send_fail,
            "received": self.recv_count,
            "consecutive_fail": self.consecutive_fail,
            "restored_windows": self.restored_count,
            "listening": listening,
            "logs": self.logs[-20:],
            # 模拟模式下"已发送"的记录（最多 20 条），控制台据此显示模拟来回。
            # 真实后端下为空列表，不影响正常路径。
            "mock_sent": list(self.sent_log[-20:]) if self.backend == BACKEND_MOCK else [],
        }

    def inject_mock_message(self, chat: str, text: str, sender: str = "测试用户",
                            kind: str = "group", at: bool = False) -> dict:
        """模拟模式：手动喂一条入站消息（离线验证整条链路）。"""
        raw = {
            "username": chat,
            "type": "文本",
            "sender_id": sender,
            "sender_username": sender,
            "create_time": time.time(),
            "content": text,
            "local_id": int(time.time() * 1000) % 1_000_000,
            "shard": "mock",
            "attr": "friend",
            "mock_at": at,
            "mock_kind": kind,
        }
        self._emit(raw)
        return raw
