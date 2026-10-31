"""诊断包导出：把界面上的报错、运行记录和后台日志打成一个 zip。

用户反馈问题时不需要懂日志目录在哪：点一下"导出诊断包"，把 zip 发过来就行。
打包全程用标准库 zipfile，任何单个文件缺失/被占用都跳过，绝不因此中断导出。
"""
import ctypes
import os
import platform
import sys
import time
import zipfile
from pathlib import Path

from .runtime_identity import APP_VERSION
from .diagnostic_redaction import redact_diagnostic_text

BOM = b"\xef\xbb\xbf"  # Windows 记事本靠它识别 UTF-8，否则中文全乱码
REPORT_NAME = "report.txt"
LOG_DIR_NAME = "logs"
AUTO_LOG_DIR_NAME = "wechatauto_logs"
# 启动诊断三件套：由 gui_launcher / desktop_startup 写在日志目录（源码模式下另有一份在 LOCALAPPDATA）
STARTUP_FILES = ("startup-diagnostics.json", "startup.log", "crash.log", "send-worker-stacks.log")
# 单个文件超过这个尺寸就不塞进包里：用户要的是诊断，不是把几百 MB 传上去
MAX_BUNDLE_FILE_BYTES = 32 * 1024 * 1024


def write_text_with_bom(path, text: str, *, root=None) -> Path:
    """按 UTF-8 带 BOM 落盘：Windows 记事本双击打开不乱码。"""
    target = Path(path)
    if root is not None:
        _guard_export_target(target, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(BOM + redact_diagnostic_text(text).replace("\r\n", "\n").encode("utf-8"))
    return target


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def system_info() -> list:
    """报告开头的环境信息：版本/系统/Python/是否管理员，排查先看这几行。"""
    return [
        f"程序版本：{APP_VERSION}",
        f"系统：{platform.platform()}",
        f"Python：{platform.python_version()} ({platform.machine() or '未知架构'})",
        f"运行方式：{'安装包（冻结）' if getattr(sys, 'frozen', False) else '源码运行'}",
        f"管理员权限：{'是' if _is_admin() else '否'}",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]


def compose_report_text(*, notice: str = "", events: str = "", version: str = APP_VERSION,
                        bundled=(), skipped=()) -> str:
    """拼报告正文：环境信息 + 报错条原文 + 运行记录全文 + 打包清单。"""
    lines = ["鲸鱼娘 · 微信控制台 诊断报告"]
    info = system_info()
    if version != APP_VERSION:
        info[0] = f"程序版本：{version}"
    lines.extend(info)
    lines.extend(["", "===== 当前报错 / 提示 =====",
                  (notice or "").strip() or "（暂无）",
                  "", "===== 运行记录（界面可见全文）=====",
                  (events or "").strip() or "（暂无）"])
    lines.extend(["", "===== 已打包文件 ====="])
    lines.extend(bundled or ["（没有可打包的日志）"])
    if skipped:
        lines.extend(["", "===== 未打包（过大或读取失败）====="])
        lines.extend(skipped)
    return redact_diagnostic_text("\n".join(lines) + "\n")


def _log_files(folder: Path) -> list:
    """目录下全部日志，含 backend.log.1 这类轮换文件。"""
    try:
        return sorted(p for p in folder.iterdir() if p.is_file() and ".log" in p.name.lower())
    except OSError:
        return []


def _startup_dirs(root: Path) -> list:
    """启动诊断三件套可能出现的位置：安装/源码根、logs，以及冻结态 LOCALAPPDATA 的日志目录。"""
    candidates = [root / LOG_DIR_NAME, root]
    local = (Path(os.environ["LOCALAPPDATA"]) / "WhaleGirlWeChat" if os.environ.get("LOCALAPPDATA")
             else Path.home() / "WhaleGirlWeChat")
    candidates.append(local / LOG_DIR_NAME)
    seen, dirs = set(), []
    for item in candidates:
        key = str(item).lower()
        if key not in seen:
            seen.add(key)
            dirs.append(item)
    return dirs


def collect_bundle_files(root) -> list:
    """收集要打包的文件，返回 [(包内路径, 磁盘路径)]；缺失的目录/文件一律跳过。"""
    root = Path(root)
    items, names = [], set()

    def add(arcname: str, path: Path):
        if arcname in names or not path.is_file():
            return
        try:
            if path.stat().st_size > MAX_BUNDLE_FILE_BYTES:
                return
        except OSError:
            return
        names.add(arcname)
        items.append((arcname, path))

    for path in _log_files(root / LOG_DIR_NAME):
        add(f"{LOG_DIR_NAME}/{path.name}", path)
    for folder in _startup_dirs(root):
        for name in STARTUP_FILES:
            add(name, folder / name)
    for path in _log_files(root / AUTO_LOG_DIR_NAME):
        add(f"{AUTO_LOG_DIR_NAME}/{path.name}", path)
    return items


def _guard_export_target(target: Path, root):
    """导出副本不能写回日志输入，别名也不能绕过；检查必须先于打开输出。"""
    root = Path(root)
    folders = [root / LOG_DIR_NAME, root / AUTO_LOG_DIR_NAME]
    startup_dirs = _startup_dirs(root)
    folders.extend(folder for folder in startup_dirs if folder.name == LOG_DIR_NAME)
    # 不按打包大小筛选，过大的原日志同样不能被导出覆盖。
    sources = [path for folder in folders for path in _log_files(folder)]
    sources.extend(folder / name for folder in startup_dirs for name in STARTUP_FILES)
    sources.append(root / LOG_DIR_NAME / 'backend.log')
    resolved = os.path.normcase(str(target.resolve()))
    for source in sources:
        if os.path.normcase(str(source.resolve())) == resolved:
            raise OSError('导出目标不能覆盖后台日志或启动诊断，请选择新的 TXT/ZIP 文件')
        try:
            same_file = target.exists() and source.exists() and target.samefile(source)
        except OSError:
            same_file = False
        if same_file:
            raise OSError('导出目标与后台日志指向同一文件，请选择新的 TXT/ZIP 文件')


def build_bundle(zip_path, *, root, notice: str = "", events: str = "",
                 version: str = APP_VERSION) -> Path:
    """把报告和日志压成诊断包，返回 zip 路径。"""
    target = Path(zip_path)
    _guard_export_target(target, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    items = collect_bundle_files(root)
    report = compose_report_text(notice=notice, events=events, version=version,
                                 bundled=[arc for arc, _ in items])
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(REPORT_NAME, BOM + report.encode("utf-8"))
        for arcname, path in items:
            try:
                # 导出副本净化，原文件保留。不能把历史日志或第三方驱动日志
                # 当成天然安全；一次有界读取也避免收集后文件继续增长越过上限。
                with path.open('rb') as stream:
                    raw = stream.read(MAX_BUNDLE_FILE_BYTES + 1)
                if len(raw) > MAX_BUNDLE_FILE_BYTES:
                    continue
                had_bom = raw.startswith((BOM, b'\xff\xfe', b'\xfe\xff'))
                if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
                    text = raw.decode('utf-16', 'replace')
                else:
                    try:
                        text = raw.decode('utf-8-sig')
                    except UnicodeDecodeError:
                        try:
                            text = raw.decode('gb18030')
                        except UnicodeDecodeError:
                            text = raw.decode('utf-8', 'replace')
                encoded = redact_diagnostic_text(text).encode('utf-8')
                bundle.writestr(arcname, (BOM if had_bom else b'') + encoded)
            except OSError:
                continue  # 文件正被后台占用时不拖垮整个导出
    return target
