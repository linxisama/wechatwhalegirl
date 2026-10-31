"""plugin_installer.py - 从网址/仓库安装插件，以及卸载自定义插件。

⚠️ 安全前提（很重要，不要弱化）
远程安装等于**把外部代码装进本程序并会被 import 执行**。所以这里的原则是：

1. **只允许 https**（拒绝 http/其它协议，防中间人替换代码）；
2. **必须显式确认**：调用方（界面）要先拿到 ``inspect_source()`` 的预检结果给用户看，
   再由用户点"确认安装"才走 ``install_from_url(..., confirmed=True)``；
3. **拒绝明显的危险内容**（``os.system`` / ``subprocess`` / ``eval`` / ``exec`` /
   ``socket`` / ``shutil.rmtree`` / 反引号 shell 等），命中就拒绝并说明命中了哪一条；
4. **大小与数量上限**，防止塞一个超大包；
5. 只允许 **单个 .py 文件** 或 **包含 .py 与 plugin.json 的 zip**，其它一律拒绝；
6. 自定义插件与内置插件**分开记账**（``custom_plugins.json``），
   内置插件不允许通过这里删除。

安装器不"理解"插件语义：它只负责把**一个合规的 .py** 落到 ``src/plugins/<id>.py``
并登记到自定义清单；是否真的能用由插件自身与运行时决定。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from .plugin_manifest import (MANIFEST_NAME, entry_file, load_manifest,
                              manifest_from_source, validate_manifest)
from .plugin_policy import RETIRED_PLUGINS
from .plugin_scan import scan_source
from .plugins import PluginBase

# ── 限额 ──────────────────────────────────────────────────────────────────────
MAX_FILE_BYTES = 512 * 1024          # 单个 .py 上限 512KB
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024  # zip 上限 8MB
MAX_ARCHIVE_FILES = 200              # zip 内文件数上限
MAX_MANIFEST_BYTES = 64 * 1024       # plugin.json 上限 64KB
FETCH_TIMEOUT = 20

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")

# 命中即拒绝的用法 → 人类可读的原因
FORBIDDEN_PATTERNS: List[Tuple[str, str]] = [
    (r"\bos\.system\s*\(", "调用系统命令 os.system"),
    (r"\bos\.popen\s*\(", "调用系统命令 os.popen"),
    (r"\bsubprocess\b", "启动子进程 subprocess"),
    (r"\beval\s*\(", "动态执行代码 eval"),
    (r"\bexec\s*\(", "动态执行代码 exec"),
    (r"\b__import__\s*\(", "动态导入 __import__"),
    (r"\bshutil\.rmtree\s*\(", "递归删除目录 shutil.rmtree"),
    (r"\bos\.remove\s*\(", "删除文件 os.remove"),
    (r"\bos\.unlink\s*\(", "删除文件 os.unlink"),
    (r"\bsocket\b", "原始网络套接字 socket"),
    (r"\bctypes\b", "调用底层 API ctypes"),
    (r"pickle\.loads?\s*\(", "反序列化 pickle（可执行任意代码）"),
    (r"\bwinreg\b", "修改注册表 winreg"),
    (r"\b(?:wechatauto|wxauto4?|uiautomation|pyautogui)\b", "绕过消息契约直接控制微信或键鼠"),
    (r"\b(?:SendToLabel|ForwardToLabel|AddLabelMembers|ForwardMessage|ForwardVoiceMessage|SwitchToMoments|Poke|VoiceCall|RecallLastMessage|RecallGuard|AtAll)\s*\(", "调用已禁止的微信能力"),
    (r"\.(?:queue_outgoing|can_send_proactive|send_to|send_text|send_files)\s*\(", "插件直接外发；请通过被动回复契约返回内容"),
    (r"base64\.b64decode\s*\([^)]*\)\s*\)?\s*\)?\s*$", "可疑的 base64 载荷"),
]


class InstallError(Exception):
    """安装失败（原因面向用户，可直接显示）。"""


# ── 源码检查 ──────────────────────────────────────────────────────────────────
def _code_without_comments(code: str) -> str:
    """去掉注释与文档字符串后的代码，用于"禁用用法"扫描。

    为什么需要：注释/文档字符串里提到 `wechatauto.media` 这类字眼（本仓库
    `sticker_steal.py` 的说明里就写着）会被正则当成真的调用，导致自家插件也过不了预检。
    `ast.unparse` 天然丢掉注释；再把模块/类/函数的首个字符串表达式（docstring）摘掉，
    剩下的就是真正会执行的代码——字符串参数仍保留，`eval("...")` 这类照样能被查到。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    class _StripDocstrings(ast.NodeTransformer):
        def _strip(self, node):
            body = getattr(node, 'body', None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
            return self.generic_visit(node)

        visit_Module = _strip
        visit_FunctionDef = _strip
        visit_AsyncFunctionDef = _strip
        visit_ClassDef = _strip

    try:
        return ast.unparse(_StripDocstrings().visit(tree))
    except (ValueError, AttributeError, TypeError):
        return code


def check_source(code: str) -> List[str]:
    """检查源码是否可接受；返回命中的问题列表（空 = 通过）。"""
    problems: List[str] = []
    try:
        tree=ast.parse(code)
    except SyntaxError as exc:
        problems.append(f"不是合法的 Python 代码：第 {exc.lineno} 行 {exc.msg}")
        return problems

    scanned = _code_without_comments(code)
    for pattern, reason in FORBIDDEN_PATTERNS:
        if re.search(pattern, scanned):
            problems.append(f"含被禁止的用法：{reason}")
    for node in ast.walk(tree):
        if isinstance(node,ast.Call) and (getattr(node.func,'id','')=='PluginMeta' or getattr(node.func,'attr','')=='PluginMeta'):
            if any(k.arg=='id' and isinstance(k.value,ast.Constant) and k.value.value in RETIRED_PLUGINS for k in node.keywords):
                problems.append('插件标识属于已移除的主动插话或刷屏能力')

    # 必须是插件：要能看出它继承了 PluginBase（宽松判断，避免误拒）
    if "PluginBase" not in code:
        problems.append("看起来不是插件：没有引用 PluginBase")
    if "PluginMeta" not in code:
        problems.append("看起来不是插件：没有引用 PluginMeta")
    return problems


def normalize_plugin_id(raw: str) -> str:
    """规范化插件标识；不合法就抛 InstallError（标识会作为文件名）。"""
    value = str(raw or "").strip().lower().replace("-", "_")
    if not PLUGIN_ID_RE.match(value):
        raise InstallError("插件标识只能用「小写字母开头 + 小写字母/数字/下划线」，长度 2-48")
    if value in RETIRED_PLUGINS:
        raise InstallError('此插件已因主动插话或刷屏风险移除，不允许重新安装')
    return value


# ── 本地文件 ──────────────────────────────────────────────────────────────────
LOCAL_SUFFIXES = (".py", ".zip")


def _read_local(path: Path, limit: int) -> bytes:
    """读取本地文件；路径不合法/读不到都抛 InstallError（消息面向用户）。"""
    if not path.is_file():
        raise InstallError(f"找不到文件：{path}")
    size = path.stat().st_size
    if size > limit:
        raise InstallError(f"文件过大（{size // 1024}KB），上限 {limit // 1024}KB")
    try:
        return path.read_bytes()
    except Exception as exc:
        raise InstallError(f"读取失败：{type(exc).__name__}: {exc}") from None


def _resolve_local(path: Path) -> str:
    """判断本地文件类型，返回 ``"file"`` 或 ``"archive"``。"""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "file"
    if suffix == ".zip":
        return "archive"
    raise InstallError("本地文件只支持 .py 或 .zip")


def inspect_local(path, plugin_id: str = "") -> Dict:
    """检查本地文件，**不写入任何文件**。返回给用户确认用的信息。

    与远程版本的区别只有"来源"：安全检查（危险用法、必须是插件）完全一样，
    本地文件同样是要被 import 执行的代码，不能因为"是本地的"就放松。
    """
    target = Path(path)
    kind = _resolve_local(target)
    wanted = normalize_plugin_id(plugin_id) if plugin_id else ""

    manifest: Optional[dict] = None
    manifest_problems: List[str] = []
    if kind == "file":
        data = _read_local(target, MAX_FILE_BYTES)
        suggested = wanted or normalize_plugin_id(target.stem)
        code = _decode(data)
    else:
        data = _read_local(target, MAX_ARCHIVE_BYTES)
        payload = _archive_payload(data, wanted, strict=False)
        manifest = payload["manifest"]
        manifest_problems = payload["problems"]
        code = payload["code"]
        suggested = (wanted or _manifest_id(manifest)
                     or str((manifest_from_source(code) or {}).get("id") or "")
                     or Path(payload["member"]).stem)

    problems = check_source(code) + manifest_problems
    return {
        "ok": not problems,
        "kind": kind,
        "resolved_url": f"本地文件：{target}",
        "plugin_id": normalize_plugin_id(suggested),
        "version": str((manifest or {}).get("version") or
                       (manifest_from_source(code) or {}).get("version") or ""),
        "api_version": str((manifest or {}).get("api_version") or ""),
        "manifest": manifest or {},
        "bytes": len(data),
        "lines": code.count("\n") + 1,
        "problems": problems,
        # 分级风险画像：problems 决定"能不能装"，scan 只回答"装上去它会干什么"。
        # 附加键，不参与 ok 判定，老调用方读不到也不受影响。
        "scan": scan_source(code, str(suggested)),
        "preview": "\n".join(code.splitlines()[:40]),
    }


# ── 试加载（在临时副本上跑不可信代码） ────────────────────────────────────────
def _describe_load_error(exc: BaseException) -> str:
    """把试加载时的异常翻成用户看得懂的一句话。"""
    if isinstance(exc, SystemExit):
        return f"插件在导入阶段直接退出（SystemExit: {getattr(exc, 'code', None)!r}）"
    if isinstance(exc, ModuleNotFoundError):
        return f"缺少依赖模块：{getattr(exc, 'name', '') or exc}"
    if isinstance(exc, ImportError):
        return f"导入失败：{exc}"
    first = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {first[0] if first else ''}".strip()


def _plugin_classes_error(module, expected_id: str) -> Optional[str]:
    """试加载通过后必须能找到一个 PluginBase 子类，且 META.id 与清单 id 一致。"""
    metas = []
    for value in list(vars(module).values()):
        if not isinstance(value, type):
            continue
        if getattr(value, "__module__", "") != module.__name__:
            continue  # 只认这个文件里定义的类，导入进来的 PluginBase 之类不算
        try:
            is_plugin = issubclass(value, PluginBase) and value is not PluginBase
        except TypeError:
            continue
        if not is_plugin:
            continue
        meta = getattr(value, "META", None)
        if meta is None:
            continue
        metas.append(meta)
    if not metas:
        return "没有找到 PluginBase 子类（插件必须定义一个继承 PluginBase 的类）"
    if expected_id:
        ids = [str(getattr(meta, "id", "")) for meta in metas]
        if expected_id not in ids:
            return f"清单 id={expected_id!r} 与插件 META.id={ids[0]!r} 不一致"
    return None


def _trial_load(path: Path, expected_id: str = "") -> Optional[str]:
    """在**临时副本**上试加载插件；成功返回 None，失败返回可读原因。

    为什么必须在副本上做：这里执行的是不可信代码，它会 import、会写文件、会 sys.exit。
    副本让失败的半成品不落在 plugins_dir，也绝不污染 src/plugins/。
    异常一律接住（连 SystemExit/KeyboardInterrupt 也接），安装器本身不许崩。
    """
    alias = "src_install_probe_%s" % uuid.uuid4().hex
    probe_dir: Optional[str] = None
    module = None
    try:
        probe_dir = tempfile.mkdtemp(prefix="plugin_probe_")
        # 副本必须是 .py 结尾：spec_from_file_location 靠后缀选 loader，
        # 直接拿 <id>.py.tmp 当文件名会拿不到 loader。
        probe = Path(probe_dir) / "plugin_probe.py"
        shutil.copyfile(str(path), str(probe))
        source = probe.read_text("utf-8")
        code = compile(source, str(probe), "exec")
        spec = importlib.util.spec_from_file_location(alias, str(probe))
        if spec is None or spec.loader is None:
            return "无法为插件文件建立模块加载器"
        module = importlib.util.module_from_spec(spec)
        # 项目里的插件习惯用相对导入（from . import PluginBase）；按文件路径加载的
        # 模块默认没有包上下文，不补 __package__ 就会 ImportError。
        module.__package__ = "src.plugins"
        sys.modules[alias] = module
        exec(code, module.__dict__)
    except SyntaxError as exc:
        return f"语法错误：第 {exc.lineno} 行 {exc.msg}"
    except BaseException as exc:  # noqa: BLE001 - 插件代码，连 SystemExit 都要接住
        return _describe_load_error(exc)
    finally:
        sys.modules.pop(alias, None)
        if probe_dir:
            shutil.rmtree(probe_dir, ignore_errors=True)
    if module is None:
        return "插件模块没有加载成功"
    return _plugin_classes_error(module, expected_id)


def _write_plugin_atomically(code: str, destination: Path,
                             manifest: Optional[dict]) -> None:
    """原子落盘：写 <id>.py.tmp → 试加载 → 通过才 os.replace 到最终路径。

    任何一步失败都删掉 tmp 并抛 InstallError；原文件字节一个都不动
    （os.replace 之前根本没碰过它）。
    """
    tmp = destination.with_name(destination.name + ".tmp")
    try:
        tmp.write_text(code, encoding="utf-8", newline="\n")
        reason = _trial_load(tmp, _manifest_id(manifest))
        if reason:
            raise InstallError("新插件无法加载，已保留原文件不动：" + reason)
        os.replace(str(tmp), str(destination))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _install_code(code: str, plugins_dir: Path, suggested: str, source: str,
                  manifest: Optional[dict] = None,
                  overwrite: bool = False) -> Dict:
    """本地安装与远程安装共用的落盘流程（安全检查 → 标识 → 原子写 + 试加载）。"""
    problems = check_source(code)
    if problems:
        raise InstallError("安全检查未通过：" + "；".join(problems))

    plugin_id = normalize_plugin_id(suggested)
    plugins_dir = Path(plugins_dir)
    plugins_dir.mkdir(parents=True, exist_ok=True)
    destination = plugins_dir / f"{plugin_id}.py"
    overwritten = destination.exists()
    if overwritten and not overwrite:
        raise InstallError(f"已存在同名插件 {plugin_id}.py；要覆盖请明确选择覆盖")

    version = str((manifest or {}).get("version") or "")
    if not version:
        # 裸 .py 没有清单：能从 META 抽到版本就记上，抽不到就空着（不因此拒绝安装）。
        version = str((manifest_from_source(code) or {}).get("version") or "")
    _write_plugin_atomically(code, destination, manifest)
    return {"plugin_id": plugin_id, "path": str(destination), "bytes": len(code),
            "source": source, "overwritten": overwritten, "version": version,
            "api_version": str((manifest or {}).get("api_version") or "")}

def install_from_file(path, plugins_dir: Path, plugin_id: str = "",
                      confirmed: bool = False, overwrite: bool = False) -> Dict:
    """安装本地插件文件。同样必须 ``confirmed=True``。

    为什么本地文件也要确认：它和远程下载的代码一样会被 import 执行，
    而且界面上要先把"这是个什么插件、有没有危险用法"给用户看过。
    """
    if not confirmed:
        raise InstallError("未经确认：请先预检并把结果展示给用户，确认后再安装")

    target = Path(path)
    kind = _resolve_local(target)
    wanted = normalize_plugin_id(plugin_id) if plugin_id else ""
    manifest: Optional[dict] = None

    if kind == "file":
        code = _decode(_read_local(target, MAX_FILE_BYTES))
        suggested = wanted or normalize_plugin_id(target.stem)
    else:
        payload = _archive_payload(_read_local(target, MAX_ARCHIVE_BYTES), wanted,
                                   strict=True)
        code, manifest = payload["code"], payload["manifest"]
        # 清单没写 id（规格方言就是这样）时，退一步用源码里 META 的 id：
        # manager 要求模块名与 META.id 一致，用错名字装进去等于装了个加载不了的插件。
        suggested = (wanted or _manifest_id(manifest)
                     or str((manifest_from_source(code) or {}).get("id") or "")
                     or Path(payload["member"]).stem)

    return _install_code(code, plugins_dir, suggested, f"本地文件：{target}",
                         manifest, overwrite)


# ── 来源解析 ──────────────────────────────────────────────────────────────────
def resolve_source(url: str) -> Tuple[str, str]:
    """把用户填的地址规范化，返回 ``(kind, url)``。

    ``kind`` 是 ``"file"``（单个 .py）或 ``"archive"``（zip）。
    支持：
    - ``https://.../xxx.zip`` → archive
    - ``https://.../xxx.py``  → file
    - GitHub 仓库页 / raw 页 → 自动转成默认分支的 zip 或 raw 文件
    """
    raw = str(url or "").strip()
    if not raw:
        raise InstallError("请填写插件地址")
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise InstallError("只允许 https 地址（防止代码在传输中被替换）")
    if not parsed.netloc:
        raise InstallError("地址不完整")

    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")

    # GitHub 仓库主页 → 默认分支 zip
    if host in ("github.com", "www.github.com"):
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1].removesuffix(".git")
            # /blob/<branch>/<file>.py → raw
            if len(parts) >= 4 and parts[2] == "blob":
                branch = parts[3]
                rest = "/".join(parts[4:])
                return "file", f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{rest}"
            if len(parts) >= 4 and parts[2] in ("tree", "releases"):
                return "archive", f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{parts[3]}"
            return "archive", f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/main"
        raise InstallError("GitHub 地址格式不对")

    if path.lower().endswith(".zip"):
        return "archive", raw
    if path.lower().endswith(".py"):
        return "file", raw
    # 末段没有扩展名时按仓库/homepage 处理，尝试当 zip
    raise InstallError("地址需要指向 .py 文件、.zip 压缩包，或 GitHub 仓库页")


def _fetch(url: str, limit: int) -> bytes:
    import httpx

    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url)
    except Exception as exc:
        raise InstallError(f"下载失败：{type(exc).__name__}: {exc}") from None
    if response.status_code != 200:
        raise InstallError(f"下载失败：HTTP {response.status_code}")
    data = response.content
    if len(data) > limit:
        raise InstallError(f"内容过大（{len(data) // 1024}KB），上限 {limit // 1024}KB")
    return data


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise InstallError("文件不是 UTF-8 文本")


def _open_archive(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InstallError("这不是一个有效的 zip 压缩包") from None


def _archive_names(archive: zipfile.ZipFile) -> List[str]:
    """zip 内的文件名单（去掉目录项），顺带做数量与路径穿越检查。"""
    names = [n for n in archive.namelist() if not n.endswith("/")]
    if len(names) > MAX_ARCHIVE_FILES:
        raise InstallError(f"压缩包内文件过多（{len(names)} 个）")
    # 防目录穿越
    for name in names:
        if ".." in Path(name).parts or Path(name).is_absolute():
            raise InstallError(f"压缩包内含可疑路径：{name}")
    return names


def _manifest_member(names: List[str]) -> Optional[str]:
    """挑出最浅的那个 plugin.json（根目录优先）。"""
    members = [n for n in names if PurePosixPath(n).name.lower() == MANIFEST_NAME]
    if not members:
        return None
    members.sort(key=lambda n: (len(PurePosixPath(n).parts), n))
    return members[0]


def _read_manifest(archive: zipfile.ZipFile, names: List[str], strict: bool):
    """读 zip 里的 plugin.json，返回（成员名, 归一化清单, 问题列表）。

    strict=True（安装路径）：清单读不懂或校验不过就直接 InstallError，不装；
    strict=False（预检路径）：把它记成 problems 返回，让用户先看到问题。
    """
    member = _manifest_member(names)
    if member is None:
        return None, None, []
    try:
        raw = archive.read(member)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError(f"清单超过 {MAX_MANIFEST_BYTES // 1024}KB")
        manifest = load_manifest(raw)
    except (ValueError, TypeError, KeyError) as exc:
        message = f"plugin.json 无法解析：{exc}"
        if strict:
            raise InstallError(message) from None
        return member, None, [message]
    problems = validate_manifest(manifest)
    if not problems:
        return member, manifest, []
    message = "插件清单校验未通过：" + "；".join(problems)
    if strict:
        raise InstallError(message)
    return member, None, [message]


def _legacy_pick(archive: zipfile.ZipFile, names: List[str],
                 wanted: Optional[str]) -> Tuple[str, str]:
    """没有清单时的老挑法：点名优先 index/main/plugin，其次第一个非 __init__。"""
    pys = [n for n in names if n.lower().endswith(".py")]
    if not pys:
        raise InstallError("压缩包里没有 .py 文件")

    target = None
    if wanted:
        for name in pys:
            if Path(name).stem.lower() == wanted:
                target = name
                break
    if target is None:
        # 优先 index.py / main.py，其次任何不是 __init__ 的
        for preferred in ("index.py", "main.py", "plugin.py"):
            for name in pys:
                if Path(name).name.lower() == preferred:
                    target = name
                    break
            if target:
                break
    if target is None:
        candidates = [n for n in pys if Path(n).name != "__init__.py"]
        target = (candidates or pys)[0]

    if archive.getinfo(target).file_size > MAX_FILE_BYTES:
        raise InstallError("插件源文件过大")
    return target, _decode(archive.read(target))


def _entry_pick(archive: zipfile.ZipFile, names: List[str], manifest: dict,
                manifest_member: Optional[str]) -> Tuple[str, str]:
    """按清单 entry 取源码；允许包外层多一层目录（GitHub 的 zip 就是这样）。"""
    wanted_path = entry_file(str(manifest.get("entry") or ""))
    base = PurePosixPath(manifest_member).parent if manifest_member else PurePosixPath("")
    relatives = [wanted_path]
    if str(base) not in ("", "."):
        relatives.insert(0, f"{base.as_posix()}/{wanted_path}")
    ordered = sorted(names, key=lambda n: (len(PurePosixPath(n).parts), n))
    for relative in relatives:
        for name in ordered:
            if name == relative or name.endswith("/" + relative):
                if archive.getinfo(name).file_size > MAX_FILE_BYTES:
                    raise InstallError("插件源文件过大")
                return name, _decode(archive.read(name))
    raise InstallError(f"压缩包里找不到清单 entry 指向的文件：{wanted_path}")


def _archive_payload(data: bytes, wanted: Optional[str], *, strict: bool) -> Dict:
    """解析一个插件 zip：清单（有则校验）+ 要落盘的那份源码。"""
    archive = _open_archive(data)
    names = _archive_names(archive)
    member, manifest, problems = _read_manifest(archive, names, strict)
    if manifest is not None and entry_file(str(manifest.get("entry") or "")):
        code_member, code = _entry_pick(archive, names, manifest, member)
    else:
        # 裸 .py / 无清单 / 清单没写 entry：一律保持老挑法，绝不因此变严。
        code_member, code = _legacy_pick(archive, names, wanted)
    return {"manifest": manifest, "manifest_member": member,
            "member": code_member, "code": code, "problems": problems}


def _manifest_id(manifest: Optional[dict]) -> str:
    return str((manifest or {}).get("id") or "").strip()


def _pick_from_archive(data: bytes, wanted: Optional[str]) -> Tuple[str, str]:
    """从 zip 里挑出插件源文件，返回 ``(filename, code)``。"""
    payload = _archive_payload(data, wanted, strict=False)
    return Path(payload["member"]).stem, payload["code"]


# ── 预检（给界面显示，不落盘） ────────────────────────────────────────────────
def inspect_source(url: str, plugin_id: str = "") -> Dict:
    """下载并检查，但**不写入任何文件**。返回给用户确认用的信息。"""
    kind, resolved = resolve_source(url)
    wanted = normalize_plugin_id(plugin_id) if plugin_id else ""

    manifest: Optional[dict] = None
    manifest_problems: List[str] = []
    if kind == "file":
        data = _fetch(resolved, MAX_FILE_BYTES)
        name = Path(urlparse(resolved).path).stem
        suggested = wanted or normalize_plugin_id(name or "download")
        code = _decode(data)
    else:
        data = _fetch(resolved, MAX_ARCHIVE_BYTES)
        payload = _archive_payload(data, wanted, strict=False)
        manifest = payload["manifest"]
        manifest_problems = payload["problems"]
        code = payload["code"]
        suggested = (wanted or _manifest_id(manifest)
                     or str((manifest_from_source(code) or {}).get("id") or "")
                     or Path(payload["member"]).stem)

    problems = check_source(code) + manifest_problems
    return {
        "ok": not problems,
        "kind": kind,
        "resolved_url": resolved,
        "plugin_id": normalize_plugin_id(suggested),
        "version": str((manifest or {}).get("version") or
                       (manifest_from_source(code) or {}).get("version") or ""),
        "api_version": str((manifest or {}).get("api_version") or ""),
        "manifest": manifest or {},
        "bytes": len(data),
        "lines": code.count("\n") + 1,
        "problems": problems,
        # 分级风险画像：problems 决定"能不能装"，scan 只回答"装上去它会干什么"。
        # 附加键，不参与 ok 判定，老调用方读不到也不受影响。
        "scan": scan_source(code, str(suggested)),
        "preview": "\n".join(code.splitlines()[:40]),
    }


# ── 安装 / 卸载 ───────────────────────────────────────────────────────────────
def install_from_url(url: str, plugins_dir: Path, plugin_id: str = "",
                     confirmed: bool = False, overwrite: bool = False) -> Dict:
    """下载并安装插件。

    ``confirmed`` 必须为 True —— 这是有意设置的"人工闸门"：远程代码会被 import 执行，
    不允许静默安装。
    """
    if not confirmed:
        raise InstallError("未经确认：请先预检并把结果展示给用户，确认后再安装")

    kind, resolved = resolve_source(url)
    wanted = normalize_plugin_id(plugin_id) if plugin_id else ""
    manifest: Optional[dict] = None

    if kind == "file":
        code = _decode(_fetch(resolved, MAX_FILE_BYTES))
        suggested = wanted or normalize_plugin_id(Path(urlparse(resolved).path).stem)
    else:
        payload = _archive_payload(_fetch(resolved, MAX_ARCHIVE_BYTES), wanted,
                                   strict=True)
        code, manifest = payload["code"], payload["manifest"]
        suggested = (wanted or _manifest_id(manifest)
                     or str((manifest_from_source(code) or {}).get("id") or "")
                     or Path(payload["member"]).stem)

    return _install_code(code, plugins_dir, suggested, resolved,
                         manifest, overwrite)
