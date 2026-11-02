"""沙盒环境诊断：只读采集 + 可选的一次启动探测，输出可直接回传的文本报告。

这个模块解决的是「另一台电脑上到底为什么用不了沙盒」：控制台自己没法远程看那台机器，
所以把需要的事实（系统版本、沙盒程序、wsb.exe、容器服务、hcsdiag、可选的真实启动探测）
一次性采集下来，写成 UTF-8(BOM) 的文本，用户直接发回来即可。
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPORT_NAME = "sandbox-doctor.txt"
RUN_TIMEOUT = 20.0
FEATURE_TIMEOUT = 60.0
PROBE_TIMEOUT = 90.0
PROBE_POLL = 1.0
TAIL_CHARS = 600
SERVICES = ("CmService", "vmcompute", "hns")


def _decode(raw):
    """Windows 控制台输出既可能是 UTF-8 也可能是 GBK，按顺序试。"""
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _default_runner(command, timeout=RUN_TIMEOUT):
    """跑一条命令，返回 (退出码, 文本)。任何异常都折叠成 (-1, 说明)，绝不抛。"""
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return -1, "命令不存在"
    except subprocess.TimeoutExpired:
        return -1, "执行超时"
    except OSError as exc:
        return -1, "无法执行：%s" % exc
    return done.returncode, _decode((done.stdout or b"") + (done.stderr or b""))


def _tail(text, limit=TAIL_CHARS):
    """只留尾部：诊断只需要出错那几行，整段输出会把报告撑爆。"""
    text = (text or "").replace("\r\n", "\n").strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _reg(path, names):
    """读 HKLM 下的几个值；读不到就留 None，不抛。"""
    values = {}
    try:
        import winreg
    except Exception:
        return values
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            for name in names:
                try:
                    values[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    values[name] = None
    except OSError:
        pass
    return values


def system_info(*, reg=None, version=None):
    """系统版本与版本号：Win10 与 Win11 都靠 build 号区分，光看 ProductName 会误判。"""
    if reg is None:
        reg = _reg(
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            ("ProductName", "EditionID", "DisplayVersion", "CurrentBuild",
             "CurrentBuildNumber", "UBR", "InstallationType"),
        )
    if version is None:
        version = getattr(sys, "getwindowsversion", lambda: None)()
    info = {
        "product": reg.get("ProductName"),
        "edition": reg.get("EditionID"),
        "display_version": reg.get("DisplayVersion"),
        "build": reg.get("CurrentBuildNumber") or reg.get("CurrentBuild"),
        "ubr": reg.get("UBR"),
        "installation_type": reg.get("InstallationType"),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
    if version is not None:
        info["windows"] = "%s.%s.%s" % (
            getattr(version, "major", ""), getattr(version, "minor", ""),
            getattr(version, "build", ""))
    return info


def _file_version(path):
    """读 PE 文件版本；读不到返回空串（诊断里宁可缺，不要抛）。"""
    try:
        import ctypes
        from ctypes import wintypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return ""
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
                buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return ""
        fixed = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        return "%d.%d.%d.%d" % (
            fixed[1] >> 16, fixed[1] & 0xFFFF, fixed[0] >> 16, fixed[0] & 0xFFFF)
    except Exception:
        return ""


def _wsb_resolver():
    try:
        from .site_profile import _resolve_wsb_exe
    except Exception:
        return None
    return _resolve_wsb_exe


def binary_info(*, system_root=None, resolver=None):
    """两个关键可执行文件：沙盒程序本身，和命令行 wsb.exe。"""
    system_root = Path(system_root or os.environ.get("SystemRoot") or r"C:\Windows")
    app = system_root / "System32" / "WindowsSandbox.exe"
    if resolver is None:
        resolver = _wsb_resolver()
    path, exists, source = "", False, "none"
    if resolver is not None:
        try:
            path, exists, source = resolver()
        except Exception:
            path, exists, source = "", False, "none"
    return {
        "sandbox_app": {"path": str(app), "exists": app.is_file(),
                        "version": _file_version(app) if app.is_file() else ""},
        "wsb": {"path": str(path or ""), "exists": bool(exists), "source": source},
    }


def feature_info(*, runner=None):
    """DISM 查「Windows 沙盒」功能状态；非管理员会失败，失败本身也是证据。"""
    runner = runner or _default_runner
    code, text = runner(["dism", "/online", "/get-featureinfo",
                         "/featurename:Containers-DisposableClientVM"], FEATURE_TIMEOUT)
    state = ""
    match = re.search(r"(?:State|\u72b6\u6001)\s*[:\uff1a]\s*(\w+)", text or "")
    if match:
        state = match.group(1)
    return {"exit": code, "state": state, "tail": _tail(text, 800)}


def _sc_state(text):
    match = re.search(r"STATE\s*:\s*\d+\s+(\w+)", text or "")
    return match.group(1).upper() if match else ""


def service_info(*, runner=None):
    """容器相关服务：CmService 是沙盒虚拟机管理器，它起不来沙盒一定起不来。"""
    runner = runner or _default_runner
    info = {}
    for name in SERVICES:
        code, text = runner(["sc", "query", name], RUN_TIMEOUT)
        info[name] = {"exit": code, "state": _sc_state(text), "tail": _tail(text, 300)}
    return info


def hcsdiag_info(*, runner=None):
    """hcsdiag list 能列出正在跑的沙盒 VM；空列表说明一台都没起来过。"""
    runner = runner or _default_runner
    code, text = runner(["hcsdiag", "list"], 30.0)
    body = (text or "").strip()
    rows = [line for line in body.splitlines() if line.strip()]
    return {"exit": code, "lines": len(rows), "tail": _tail(text, 800)}


def _sandbox_windows():
    """桌面上「Windows Sandbox」窗口的数量；取不到返回 -1（非 Windows 或调用失败）。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return -1
    try:
        user32 = ctypes.windll.user32
        titles = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                text = buffer.value.strip()
                if text:
                    titles.append(text)
            return True
        user32.EnumWindows(callback_type(callback), 0)
        return sum(1 for title in titles if 'Sandbox' in title or '沙盒' in title)
    except Exception:
        return -1


def _probe_root():
    """探测目录必须是纯 ASCII：.wsb 的命令行里有中文路径时沙盒会静默不执行。"""
    for base in (os.environ.get("PUBLIC"), r"C:\Users\Public"):
        if not base:
            continue
        path = Path(base)
        if path.is_dir() and str(path).isascii():
            return str(path)
    return tempfile.gettempdir()


def _probe_wsb(host_folder):
    """最小可判定的 .wsb：映射一个临时目录，登录命令往里写一个标记文件。"""
    lines = [
        "<Configuration>",
        "  <MappedFolder>",
        "    <HostFolder>%s</HostFolder>" % host_folder,
        "    <SandboxFolder>C:\\WhaleDoctor</SandboxFolder>",
        "    <ReadOnly>false</ReadOnly>",
        "  </MappedFolder>",
        "  <LogonCommand>",
        "    <Command>cmd.exe /c echo ok&gt;C:\\WhaleDoctor\\marker.txt</Command>",
        "  </LogonCommand>",
        "</Configuration>",
    ]
    return "\r\n".join(lines) + "\r\n"


def _default_spawn(command):
    """用 DETACHED_PROCESS 拉起沙盒窗口，免得诊断进程自己挡住它。"""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(command, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags)


def _terminate(proc):
    """探测完一定要关掉沙盒窗口，否则用户桌面会留一台空 VM。"""
    if proc is None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def _sandbox_handles():
    """桌面上「Windows Sandbox」窗口的句柄列表；取不到返回空列表。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []
    handles = []
    try:
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length and user32.IsWindowVisible(hwnd):
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value.strip()
                if 'Sandbox' in title or '沙盒' in title:
                    handles.append(int(hwnd))
            return True
        user32.EnumWindows(callback_type(callback), 0)
    except Exception:
        return []
    return handles


def _sandbox_windows():
    """沙盒窗口数量；用来判断探测到底有没有把沙盒拉起来。"""
    return len(_sandbox_handles())


def close_sandbox_windows(handles, limit=4):
    """关掉探测期间新出现的沙盒窗口：WM_CLOSE 让沙盒自己收尾，不强杀。"""
    closed = 0
    try:
        import ctypes
        user32 = ctypes.windll.user32
    except Exception:
        return 0
    for hwnd in list(handles)[:limit]:
        try:
            if user32.PostMessageW(hwnd, 0x0010, 0, 0):
                closed += 1
        except Exception:
            continue
    return closed


def probe_launch(*, system_root=None, timeout=PROBE_TIMEOUT, poll=PROBE_POLL,
                 spawn=None, clock=time.monotonic, sleep=time.sleep):
    """真的启动一次沙盒：看窗口有没有出现，顺带看映射目录里能不能拿到标记文件。"""
    system_root = Path(system_root or os.environ.get("SystemRoot") or r"C:\Windows")
    exe = system_root / "System32" / "WindowsSandbox.exe"
    if not exe.is_file():
        return {"started": False, "marker": False, "window": False,
                "reason": "no_sandbox_app"}
    workdir = Path(tempfile.mkdtemp(prefix="whale-doctor-", dir=_probe_root()))
    out = workdir / "out"
    out.mkdir(parents=True, exist_ok=True)
    marker = out / "marker.txt"
    wsb = workdir / "probe.wsb"
    wsb.write_text(_probe_wsb(out), encoding="utf-8")
    spawn = spawn or _default_spawn
    before = set(_sandbox_handles())
    try:
        proc = spawn([str(exe), str(wsb)])
    except OSError as exc:
        return {"started": False, "marker": False, "window": False,
                "reason": "spawn_failed: %s" % exc, "workdir": str(workdir)}
    seen = False
    fresh = []
    try:
        deadline = clock() + timeout
        while clock() < deadline:
            if marker.is_file():
                seen = True
                break
            sleep(poll)
    finally:
        fresh = [hwnd for hwnd in _sandbox_handles() if hwnd not in before]
        _terminate(proc)
        close_sandbox_windows(fresh)  # 探测完不能把沙盒窗口留在用户桌面上
    return {
        "started": True,
        "marker": seen,
        "window": bool(fresh),
        "reason": "" if seen else "no_marker",
        "workdir": str(workdir),
        "windows": len(fresh),
    }

CONTAINER_ROOT = r"C:\ProgramData\Microsoft\Windows\Containers"
UTILITY_VM_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Virtualization\Containers\UtilityVm"


def container_store_info(*, root=None, reg=None):
    """容器组件存储：CmService 起不来最常见的两个原因就是 BaseImages/PortableBaseLayer 缺失。

    只读判断，不创建目录、不写注册表。取不到就记 None，绝不抛。
    """
    base = Path(root) if root else Path(CONTAINER_ROOT)
    info = {"root": str(base)}
    # 键名带下划线，和 verdict() 里读的 base_images / portable_base_layer 对齐。
    for key, name in (("base_images", "BaseImages"), ("portable_base_layer", "PortableBaseLayer"),
                      ("sandboxes", "Sandboxes"), ("dumps", "Dumps"), ("zygotes", "Zygotes")):
        try:
            info[key] = (base / name).exists()
        except OSError:
            info[key] = None
    getter = reg or _reg
    try:
        values = getter(UTILITY_VM_KEY, ("*",))
    except Exception:
        values = {}
    info["utility_vm"] = bool(values)
    return info


def service_events(*, runner=None, count=3):
    """最近的容器管理器服务错误事件（7023）：区分「服务没启动」和「启动时报找不到文件」。"""
    run = runner or _default_runner
    command = ["wevtutil", "qe", "System", "/q:*[System[(EventID=7023)]]",
               "/c:%d" % count, "/rd:true", "/f:text"]
    try:
        code, text = run(command, timeout=RUN_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 诊断脚本不允许因为取事件失败而崩
        return {"exit": -1, "lines": 0, "tail": str(exc)[:200], "file_not_found": False}
    lines = [line.rstrip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line.strip()]
    # 事件日志里 7023 是「某个服务启动失败」的通用码，别的服务也会报；
    # 只认提到容器管理器/CmService 的那几段，免得把无关服务的错误算到沙盒头上。
    blocks, current = [], []
    for line in lines:
        if line.lstrip().startswith("Event[") and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)
    relevant = [block for block in blocks
                if any(("容器管理器" in line or "CmService" in line
                        or "Containers" in line or "containers" in line) for line in block)]
    joined = "\n".join("\n".join(block) for block in relevant).lower()
    file_not_found = ("cannot find the file" in joined
                      or "找不到" in joined
                      or "0x80070002" in joined)
    body = relevant[-1] if relevant else ["（最近的 7023 事件里没有容器管理器相关记录）"]
    return {"exit": code, "lines": len(lines), "tail": _tail("\n".join(body), 400),
            "file_not_found": file_not_found}


def privilege_info():
    try:
        from .elevation import is_elevated
    except Exception:
        return {"elevated": None}
    try:
        return {"elevated": bool(is_elevated())}
    except Exception:
        return {"elevated": None}


NO_APP_HINT = (
    "这台机器没有 WindowsSandbox.exe：系统没有启用「Windows 沙盒」功能。"
    "需要专业版/企业版/教育版，并在「启用或关闭 Windows 功能」里勾选后重启。"
    "控制台不受影响，用本机模式即可收发消息。"
)
INCONCLUSIVE_HINT = (
    "启动探测没有看到沙盒窗口：这台机器的沙盒很可能起不来（容器服务或组件存储损坏都会这样）。"
    "报告里的服务段、hcsdiag 段与原始输出就是证据；修复需要系统级操作（sfc /scannow、DISM /RestoreHealth）。"
    "控制台不受影响，用本机模式即可收发消息。"
)

WINDOW_ONLY_HINT = (
    "沙盒窗口能出现，但映射目录里的登录命令没生效：沙盒本身可用，隔离通道还需要文件通道后端（后续版本提供）。"
    "当前请用本机模式，收发消息、插件、DSH 都不受影响。"
)
NO_CLI_HINT = (
    "没有 wsb.exe 命令行（健康的 Win10 普遍如此）：当前版本请用本机模式。"
    "本机模式下收发消息、插件、DSH 全部可用，只是没有隔离沙盒。"
)
OK_HINT = "沙盒命令行可用，隔离通道可以使用。"
BROKEN_HINT = (
    "容器管理器服务起不来（CmService 启动时报找不到文件，容器组件存储缺 BaseImages/PortableBaseLayer）："
    "这是系统层损坏，不是控制台的问题，重装或修复系统之前沙盒不可能启动。"
    "控制台请继续用本机模式——收发消息、插件、DSH 主聊、记忆全部可用；"
    "要隔离通道请把系统修好或换 Win11 24H2+。"
)


def verdict(report):
    """把采集结果压成一句结论 + 一句建议，用户看报告第一屏就知道该怎么办。"""
    binaries = report.get("binaries") or {}
    app = binaries.get("sandbox_app") or {}
    wsb = binaries.get("wsb") or {}
    services = report.get("services") or {}
    probe = report.get("probe") or {}
    if not app.get("exists"):
        return "no_sandbox_app", NO_APP_HINT
    if probe.get("started") and probe.get("marker"):
        return "ok", OK_HINT  # 真起来过就是能起来，服务状态只是旁证
    if probe.get("started") and probe.get("window"):
        return "sandbox_window_only", WINDOW_ONLY_HINT
    if probe.get("started") and not probe.get("marker"):
        return "sandbox_probe_inconclusive", INCONCLUSIVE_HINT
    if not probe.get("started") and probe.get("reason") == "no_sandbox_app":
        return "no_sandbox_app", NO_APP_HINT
    cm_service = (services.get("CmService") or {}).get("state") or ""
    store = report.get("container_store") or {}
    events = report.get("events") or {}
    store_missing = store.get("base_images") is False or store.get("portable_base_layer") is False
    if cm_service and cm_service != "RUNNING" and (store_missing or events.get("file_not_found")):
        return "container_service_broken", BROKEN_HINT
    if cm_service and cm_service != "RUNNING":
        return "container_service_down", (
            "容器服务 CmService 状态是 %s，Windows 沙盒无法创建虚拟机。" % cm_service
            + INCONCLUSIVE_HINT)
    if not wsb.get("exists"):
        return "no_cli_but_app", NO_CLI_HINT
    return "ok", OK_HINT


def collect(*, probe=False, version=None, runner=None, resolver=None,
            system_root=None, reg=None, spawn=None):
    """采集全部诊断事实；probe=True 时额外真启动一次沙盒（会有窗口一闪）。"""
    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": version or "",
        "system": system_info(reg=reg),
        "binaries": binary_info(system_root=system_root, resolver=resolver),
        "feature": feature_info(runner=runner),
        "services": service_info(runner=runner),
        "hcsdiag": hcsdiag_info(runner=runner),
        "privilege": privilege_info(),
        "container_store": container_store_info(),
        "events": service_events(runner=runner),
    }
    if probe:
        report["probe"] = probe_launch(system_root=system_root, spawn=spawn)
    code, hint = verdict(report)
    report["verdict"] = code
    report["hint"] = hint
    return report


def _flat(prefix, value, lines):
    if isinstance(value, dict):
        for key, item in value.items():
            _flat("%s.%s" % (prefix, key) if prefix else str(key), item, lines)
    elif isinstance(value, (list, tuple)):
        lines.append("%s=%s" % (prefix, "|".join(str(item) for item in value)))
    else:
        lines.append("%s=%s" % (prefix, "" if value is None else value))


def render(report):
    """报告正文：先给结论和建议，再给逐项事实，最后给原始输出。"""
    lines = [
        "鲸鱼娘 微信控制台 · 沙盒环境诊断",
        "版本：%s" % (report.get("version") or "未知"),
        "生成时间：%s" % (report.get("generated") or ""),
        "结论：%s" % (report.get("verdict") or ""),
        "建议：%s" % (report.get("hint") or ""),
        "",
        "[系统]",
    ]
    _flat("", report.get("system") or {}, lines)
    lines.append("")
    lines.append("[可执行文件]")
    _flat("", report.get("binaries") or {}, lines)
    lines.append("")
    lines.append("[功能状态]")
    _flat("", report.get("feature") or {}, lines)
    lines.append("")
    lines.append("[服务]")
    for name, item in (report.get("services") or {}).items():
        lines.append("%s=%s exit=%s" % (name, item.get("state") or "", item.get("exit")))
    lines.append("")
    lines.append("[hcsdiag]")
    hcs = report.get("hcsdiag") or {}
    lines.append("exit=%s lines=%s" % (hcs.get("exit"), hcs.get("lines")))
    lines.append("")
    lines.append("[权限]")
    _flat("", report.get("privilege") or {}, lines)
    lines.append("")
    lines.append("[容器存储]")
    _flat("", report.get("container_store") or {}, lines)
    lines.append("")
    lines.append("[服务事件]")
    events = report.get("events") or {}
    lines.append("exit=%s lines=%s file_not_found=%s" % (
        events.get("exit"), events.get("lines"), events.get("file_not_found")))
    lines.append(events.get("tail") or "")
    lines.append("")
    lines.append("[启动探测]")
    _flat("", report.get("probe") or {"note": "未执行（加 --probe-launch 才会真的启动一次沙盒）"}, lines)
    lines.append("")
    lines.append("[原始输出]")
    for name, item in (report.get("services") or {}).items():
        lines.append("--- sc query %s ---" % name)
        lines.append(item.get("tail") or "")
    lines.append("--- dism ---")
    lines.append((report.get("feature") or {}).get("tail") or "")
    lines.append("--- hcsdiag list ---")
    lines.append(hcs.get("tail") or "")
    return "\r\n".join(lines) + "\r\n"


def default_output():
    """默认写到桌面：用户回传最方便，写不进去再退回当前目录。"""
    desktop = Path(os.path.expanduser("~")) / "Desktop"
    if desktop.is_dir():
        return desktop / REPORT_NAME
    return Path.cwd() / REPORT_NAME


def write_report(report, path=None):
    """UTF-8(BOM) + CRLF：Win10 记事本双击就能看，不会乱码。"""
    path = Path(path) if path is not None else default_output()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report), encoding="utf-8-sig", newline="")
    return path


def notify(path, report):
    """把报告直接打开给用户看：冻结版没有控制台，弹模态框会卡住批处理。"""
    try:
        os.startfile(str(path))  # noqa: S606 - 用户点开自己刚生成的报告
    except Exception:
        pass

def main(argv=None):
    """命令行入口：--sandbox-doctor [--probe-launch] [--out 路径]。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    probe = "--probe-launch" in argv
    out = None
    if "--out" in argv:
        index = argv.index("--out")
        if index + 1 < len(argv):
            out = Path(argv[index + 1])
    version = ""
    try:
        from .runtime_identity import APP_VERSION
        version = APP_VERSION
    except Exception:
        pass
    report = collect(probe=probe, version=version)
    path = write_report(report, out)
    summary = ["结论：%s" % report["verdict"], "建议：%s" % report["hint"],
               "报告已写入：%s" % path]
    for line in summary:
        try:
            print(line)
        except Exception:
            pass  # 冻结版是窗口程序，没有控制台，打印失败不影响写报告
    notify(path, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
