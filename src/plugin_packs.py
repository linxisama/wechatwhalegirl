"""插件包：用户手动放进来的独立插件（文件夹 / zip）的发现、校验、加载与安装。

为什么单独一个模块：src/plugin_manifest.py 服务的是「单文件自定义插件」，
它的清单允许缺 id、只做宽松校验；插件包要能被第三方分发，必须**先校验再加载**，
并且任何一个坏包都不能拖垮主程序。这里只用标准库，不 import Qt / FastAPI，
命令行工具和测试可以直接调用。

目录约定（1.3.63）：
- 发行版：程序旁边的 plugin-packs/（PyInstaller onedir 时就是 exe 同级）；
- 开发态：工程根 plugin-packs/；
- 用户数据目录：data/.../plugin_packs/（保留旧约定，安装默认落这里）。

一个插件包长这样：

    <id>/plugin.json            # 清单（必需）
    <id>/plugin.py              # 入口，默认 plugin.py
    <id>/README.md              # 给人看的说明（可选）
    <id>/settings.example.json  # 默认配置示例（可选）
"""
from __future__ import annotations

import json
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 可独立分发的可选插件：源码里仍然保留（测试覆盖），只在
# WHALE_PLUGINS_FROM_PACKS=1 的发行构建里改成从插件包加载。
# 与 src/plugins/manager.py 的 PLUGIN_MODULES 保持一致，测试会核对两边。
OPTIONAL_PLUGIN_PACKS: Tuple[str, ...] = (
    "group_summary", "image_library", "sticker_admin", "sticker_steal",
    "sticker_memory", "web_context", "web_lookup", "reply_pace",
    "member_aliases", "group_ambient",
)

PACK_DIRNAME = "plugin_packs"        # 数据目录下的安装位置
APP_PACK_DIRNAME = "plugin-packs"    # 程序旁边随发布物分发的种子包
MANIFEST_NAME = "plugin.json"
ENTRY_DEFAULT = "plugin.py"
MAX_PACK_BYTES = 20 * 1024 * 1024
MAX_FILES = 2000
MAX_ENTRY_DEPTH = 3
PACK_MODULE_PREFIX = "whale_pack_"
ENV_FROM_PACKS = "WHALE_PLUGINS_FROM_PACKS"
# 与 src/plugin_manifest.SUPPORTED_API_VERSIONS 对齐；那边服务单文件插件，这边
# 服务插件包，两个模块刻意不互相 import，避免清单语义互相绑死。
#
# 1.3.103：改成整数集合。社区写法本来就是 {"api_version": 1}（JSON 整数），
# 旧实现用字符串成员测试，整数直接被判「必须是文本」—— 这是把方言差异当成错误。
SUPPORTED_API_VERSIONS: Tuple[int, ...] = (1, 2)

# api_version 2 起是严格档：下面 13 个字段一个都不能缺。
# api_version 1（或干脆不写）走兼容档：只有 id/name/version 判死，其余降级成提示，
# 否则已装的三方包会在升级当天集体失效。
STRICT_API_VERSION = 2

# data_policy 的结构化枚举（1.3.106）。清单里原本是一段自由文本，
# 只能给人看，代码读不出「这个插件碰不碰聊天内容」。改成可选标签：
#   none            什么都不碰（纯计算）
#   own_settings    读写自己的设置
#   own_state       读写自己的状态文件
#   chat_content    接触聊天内容（消息正文、图片、语音）
#   external_network 把数据发到本机以外
# 自由文本仍然接受（存量包全是中文描述），但会被归一成空标签 + 保留原文，
# 严格档下要求至少声明一个标签。
DATA_POLICY_TAGS: Tuple[str, ...] = (
    "none", "own_settings", "own_state", "chat_content", "external_network",
)

# capabilities 的合法取值。前一组是钩子（插件在链路某一步被调用），
# 后一组是非钩子能力 —— 有些插件一个钩子都不实现也完全合法：
#   settings = 纯设置门（例如 sticker_admin 只是藏起管理入口）
#   helper   = 纯工具库（例如 sticker_memory 只导出指纹函数给别的模块用）
# 但**空列表不行**：说不清自己提供什么能力的包，用户没法判断要不要装。
# 从 src/plugin_mounts.py 派生，避免两处各写一份然后漂移。
from .plugin_mounts import HOOK_MOUNT, NON_HOOK_KINDS
CAPABILITY_HOOKS: Tuple[str, ...] = tuple(HOOK_MOUNT)
CAPABILITY_KINDS: Tuple[str, ...] = NON_HOOK_KINDS
CAPABILITY_VOCABULARY: Tuple[str, ...] = CAPABILITY_HOOKS + CAPABILITY_KINDS

STRICT_REQUIRED_FIELDS: Tuple[str, ...] = (
    "id", "name", "version", "entry", "author", "description",
    "api_version", "requires", "data_policy", "native", "side",
    "upstream", "capabilities",
)
PACK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_REMOVE_ID_RE = re.compile(r"^[a-z0-9_]+$")
_VERSION_RE = re.compile(r"^(\d{1,6})(?:\.(\d{1,6}))?(?:\.(\d{1,6}))?$")
_UNSAFE_PARTS = ("", ".", "..")

TEXT_FIELDS = ("description", "author", "upstream", "native", "side",
               "data_policy", "homepage", "settings_example")


class PackError(Exception):
    """插件包自身的错误（清单/压缩包结构），带中文提示，调用方转成 dict。"""


# ── 版本号 ────────────────────────────────────────────────────────────────
def parse_version(text: Any) -> Optional[Tuple[int, int, int]]:
    """把 "1.2.3" / "1.2" / "1" 解析成三元组；不合法返回 None。

    只接受纯数字段（最多三段），不接受 "v1.0"、预发布后缀或四段版本：
    插件清单是给普通用户看的东西，含糊的版本号宁可判非法也不要瞎猜。
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) if part is not None else 0 for part in match.groups())  # type: ignore[return-value]


def compare_versions(left: str, right: str) -> Optional[int]:
    """比较两个版本号，返回 -1/0/1；任一不合法返回 None。"""
    a, b = parse_version(left), parse_version(right)
    if a is None or b is None:
        return None
    return (a > b) - (a < b)


def version_at_least(version: str, required: str) -> Optional[bool]:
    """version >= required；版本号不合法返回 None。"""
    result = compare_versions(version, required)
    return None if result is None else result >= 0


# ── 路径 ──────────────────────────────────────────────────────────────────
def app_root() -> Path:
    """程序所在目录：冻结后是 exe 同级，开发态是工程根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_pack_dirs(data_dir: Optional[Path] = None) -> List[Path]:
    """默认扫描顺序：程序旁的 plugin-packs/ 优先，其次数据目录的 plugin_packs/。"""
    dirs: List[Path] = [app_root() / APP_PACK_DIRNAME]
    if data_dir is not None:
        dirs.append(Path(data_dir) / PACK_DIRNAME)
    result: List[Path] = []
    for path in dirs:
        if path not in result:
            result.append(path)
    return result


def _safe_rel_path(value: str) -> Optional[str]:
    """把清单里的相对路径规范化；越界或绝对路径返回 None。"""
    text = str(value).replace("\\", "/").strip()
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def normalize_api_version(value: Any) -> Optional[int]:
    """把清单里的 api_version 归一成整数；认不出来返回 None。

    接受 1 / "1" / 1.0（整数等值的浮点），拒绝 "1.0" / 1.5 / True / None。
    为什么这么宽：JSON 里写 1 和 "1" 是同一件事的两种方言，社区两种都在用；
    而 "1.0" 是版本号不是整数版本，放它进来等于把两套语义混成一个。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text, 10)
        except ValueError:
            return None
    return None


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _result(ok: bool, reason: str = "", message: str = "", **extra: Any) -> dict:
    payload = {"ok": ok, "reason": reason, "message": message}
    payload.update(extra)
    return payload


# ── 清单校验 ──────────────────────────────────────────────────────────────
def _check_manifest(data: Any, host_version: str, pack_dir: Optional[Path] = None,
                    available_ids: Optional[Iterable[str]] = None
                    ) -> Tuple[str, List[str], dict]:
    """内部实现：返回 (status, errors, meta)，status 为 ok/invalid/incompatible。"""
    errors: List[str] = []
    warnings: List[str] = []
    meta: Dict[str, Any] = {"id": "", "name": "", "version": "", "entry": ENTRY_DEFAULT,
                            "api_version": "", "dependencies": [], "permissions": [],
                            "warnings": warnings}
    if not isinstance(data, dict):
        return "invalid", ["插件清单格式无效：内容应当是一个 JSON 对象"], meta

    # id
    raw_id = data.get("id")
    if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
        errors.append("缺少必需字段 id")
    elif not isinstance(raw_id, str):
        errors.append("字段 id 必须是文本")
    elif not PACK_ID_RE.match(raw_id):
        errors.append("id 不合法：只能用小写字母、数字和下划线，以字母开头（2-48 个字符）")
    else:
        meta["id"] = raw_id

    # name
    raw_name = data.get("name")
    if raw_name is None or (isinstance(raw_name, str) and not raw_name.strip()):
        errors.append("缺少必需字段 name")
    elif not isinstance(raw_name, str):
        errors.append("字段 name 必须是文本")
    else:
        meta["name"] = raw_name.strip()

    # version
    raw_version = data.get("version")
    if raw_version is None or (isinstance(raw_version, str) and not raw_version.strip()):
        errors.append("缺少必需字段 version")
    elif not isinstance(raw_version, str) or parse_version(raw_version) is None:
        errors.append("version 不合法：应形如 1.0.0")
    else:
        meta["version"] = raw_version.strip()

    # entry（可省略，默认 plugin.py）
    raw_entry = data.get("entry", ENTRY_DEFAULT)
    if not isinstance(raw_entry, str) or not raw_entry.strip():
        errors.append("字段 entry 必须是非空文本（默认 plugin.py）")
    else:
        entry = _safe_rel_path(raw_entry)
        if entry is None:
            errors.append("entry 必须是包内相对路径，不能用绝对路径或 .. 跳出插件目录")
        elif entry.count("/") + 1 > MAX_ENTRY_DEPTH:
            errors.append("entry 层级过深：最多 3 层")
        else:
            meta["entry"] = entry
            if pack_dir is not None and not (Path(pack_dir) / entry).is_file():
                errors.append("入口文件不存在：" + entry)

    # 可选文本字段
    for field in TEXT_FIELDS:
        value = data.get(field)
        if value is None:
            continue
        if field == "data_policy":
            # 1.3.106 起 data_policy 可以是标签列表；文本形态在下面单独归一，
            # 这里不能按「必须是文本」判死，否则新写法永远过不了。
            if isinstance(value, str):
                meta[field] = value.strip()
            continue
        if not isinstance(value, str):
            errors.append("字段 " + field + " 必须是文本")
            continue
        meta[field] = value.strip()
    if "settings_example" in meta:
        normalized = _safe_rel_path(meta["settings_example"])
        if normalized is None:
            errors.append("settings_example 必须是包内相对路径")
        else:
            meta["settings_example"] = normalized

    # permissions：五项能力声明（net / fs_read / fs_write / exec / wechat_send）
    # 声明是诚实义务，不声明不代表不能用（见 src/plugin_guard.py 的两档行为），
    # 但写错名字要立刻告诉作者，否则他以为申请到了其实没有。
    raw_permissions = data.get("permissions")
    if raw_permissions is not None:
        if not isinstance(raw_permissions, list) or any(
                not isinstance(item, str) for item in raw_permissions):
            errors.append("字段 permissions 必须是权限名称的列表")
        else:
            from .plugin_guard import PERMISSION_NAMES, normalize, unknown
            bad = unknown(raw_permissions)
            if bad:
                errors.append("permissions 含未知权限：" + "、".join(bad)
                              + "（可用 " + " / ".join(PERMISSION_NAMES) + "）")
            meta["permissions"] = list(normalize(raw_permissions))

    # data_policy：结构化标签（1.3.106）。自由文本仍然收（存量包全是中文描述），
    # 但只保留原文、不打标签；严格档下要求至少一个标签，让代码读得出「碰不碰聊天内容」。
    raw_policy = data.get("data_policy")
    if isinstance(raw_policy, list):
        tags = [str(item).strip().lower() for item in raw_policy if str(item).strip()]
        bad_tags = [item for item in tags if item not in DATA_POLICY_TAGS]
        if bad_tags:
            errors.append("data_policy 含未知标签：" + "、".join(bad_tags)
                          + "（可用 " + " / ".join(DATA_POLICY_TAGS) + "）")
        else:
            meta["data_policy_tags"] = list(dict.fromkeys(tags))
    elif isinstance(raw_policy, str) and raw_policy.strip():
        meta["data_policy_tags"] = []   # 自由文本：保留原文（已在 TEXT_FIELDS 里），不打标签

    # capabilities 是能力名称列表（例如 reply / summary），只用于展示，不参与加载判断
    raw_caps = data.get("capabilities")
    if raw_caps is not None:
        if not isinstance(raw_caps, list) or any(not isinstance(item, str) for item in raw_caps):
            errors.append("字段 capabilities 必须是能力名称的列表")
        else:
            meta["capabilities"] = [item.strip() for item in raw_caps if item.strip()]


    # 降级提示：缺这些字段不影响加载，但界面要能说清「缺什么、会怎样」
    if not meta.get("author"):
        warnings.append("未填写 author：界面会显示「未知作者」")
    if not meta.get("description"):
        warnings.append("未填写 description：界面会显示「暂无说明」")
    if not meta.get("data_policy"):
        warnings.append("未填写 data_policy：无法预先告知这个插件会读写哪些数据")
    if not meta.get("capabilities"):
        warnings.append("未填写 capabilities：只能按入口脚本推断它提供什么能力")
    if not meta.get("native"):
        warnings.append("未声明 native：默认按「纯 Python、不依赖外部程序」处理")
    if not meta.get("upstream"):
        warnings.append("未声明 upstream：无法追溯这个包的上游来源")
    if not meta.get("side"):
        warnings.append("未声明 side：默认按「本机侧运行」处理")

    # api_version：归一成整数（1.3.103）
    raw_api = data.get("api_version", SUPPORTED_API_VERSIONS[0])
    api_version = normalize_api_version(raw_api)
    if api_version is None:
        errors.append("字段 api_version 必须是整数（例如 1），收到 " + repr(raw_api))
    else:
        meta["api_version"] = api_version
        meta["strict"] = api_version >= STRICT_API_VERSION
        if api_version not in SUPPORTED_API_VERSIONS:
            errors.append("插件接口版本 " + str(api_version) + " 与本机不兼容：本机支持 "
                          + "/".join(str(item) for item in SUPPORTED_API_VERSIONS))

    # 严格档（api_version >= 2）：13 个字段一个都不能缺。
    # 兼容档（api_version 1 / 未写）维持「只判死 id/name/version，其余降级成提示」，
    # 否则升级当天已装的三方包会集体失效。
    if meta.get("strict"):
        for field in STRICT_REQUIRED_FIELDS:
            if field in ("id", "name", "version", "entry", "api_version"):
                continue  # 上面已按更具体的文案判过
            value = data.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append("严格档（api_version " + str(STRICT_API_VERSION)
                              + "）缺少必需字段 " + field)
        policy_tags = meta.get("data_policy_tags")
        if policy_tags is not None and not policy_tags:
            errors.append("严格档（api_version " + str(STRICT_API_VERSION)
                          + "）的 data_policy 必须是标签列表，不能是自由文本或空：可用 "
                          + " / ".join(DATA_POLICY_TAGS))
        caps = meta.get("capabilities")
        if caps is not None and not caps:
            errors.append("严格档（api_version " + str(STRICT_API_VERSION)
                          + "）的 capabilities 不能是空列表：至少写一个能力名（可用 "
                          + " / ".join(CAPABILITY_VOCABULARY) + "）")
        elif caps:
            unknown = [item for item in caps if item not in CAPABILITY_VOCABULARY]
            if unknown:
                errors.append("严格档（api_version " + str(STRICT_API_VERSION)
                              + "）的 capabilities 含未知能力：" + "、".join(unknown)
                              + "（可用 " + " / ".join(CAPABILITY_VOCABULARY) + "）")
    # 宿主版本：requires（清单字段）与 min_host_version（PluginMeta 同义字段）取较高者
    host = parse_version(host_version)
    for field in ("requires", "min_host_version"):
        raw_required = data.get(field)
        if raw_required is None:
            continue
        if not isinstance(raw_required, str) or parse_version(raw_required) is None:
            errors.append("字段 " + field + " 不是合法版本号：应形如 1.3.63")
            continue
        meta[field] = raw_required.strip()
        if host is not None and host < parse_version(raw_required):
            errors.append("需要宿主版本 ≥ " + raw_required.strip() + "，当前是 " + host_version
                          + "，请先升级控制台")

    # dependencies
    raw_deps = data.get("dependencies")
    if raw_deps is not None:
        if not isinstance(raw_deps, list):
            errors.append("字段 dependencies 必须是插件 id 的列表")
        else:
            deps: List[str] = []
            for item in raw_deps:
                if not isinstance(item, str) or not PACK_ID_RE.match(item):
                    errors.append("依赖项不合法：" + repr(item) + "（应是插件 id）")
                    continue
                if item not in deps:
                    deps.append(item)
            meta["dependencies"] = deps
            own_id = meta.get("id") or ""
            if own_id and own_id in deps:
                errors.append("dependencies 不能包含插件自己")
            if available_ids is not None:
                known = set(available_ids)
                missing = [dep for dep in deps if dep != own_id and dep not in known]
                if missing:
                    errors.append("依赖的插件未安装：" + "、".join(missing))
    else:
        meta["dependencies"] = []

    # 目录名与 id 不一致不影响加载，但要说出来，免得用户后面找不到
    if pack_dir is not None and meta.get("id") and Path(pack_dir).name != meta["id"]:
        warnings.append("目录名与插件 id 不一致：目录 " + Path(pack_dir).name
                        + "，id " + meta["id"])

    if errors:
        # 只要有版本门槛没通过，就单独标成 incompatible，前端好区分「不兼容」和「写错了」
        incompatible = any(("不兼容" in err or "宿主版本" in err) for err in errors)
        return ("incompatible" if incompatible else "invalid"), errors, meta
    return "ok", [], meta


def validate_manifest(data: Any, host_version: str, pack_dir: Optional[Path] = None,
                      available_ids: Optional[Iterable[str]] = None
                      ) -> Tuple[bool, List[str], dict]:
    """校验插件清单。

    参数：清单数据（已 json.loads 的 dict）、当前宿主版本、可选的包目录
    （给了才会检查 entry 文件是否存在）、可选的本机已有插件 id（给了才检查依赖）。
    返回 (ok, errors, meta)：errors 全是中文、可直接显示给用户；meta 里带规范化后的
    字段和 warnings（缺 author/upstream 之类的降级提示），即使校验失败也尽量填。
    """
    status, errors, meta = _check_manifest(data, host_version, pack_dir, available_ids)
    meta["status"] = status
    return status == "ok", errors, meta


# ── 发现 ──────────────────────────────────────────────────────────────────
def _inspect_pack(pack_dir: Path, host_version: str,
                  available_ids: Optional[Iterable[str]] = None) -> dict:
    """检查单个包目录；任何异常都落在 status 里，绝不向外抛。"""
    base: Dict[str, Any] = {"id": "", "name": "", "version": "", "path": str(pack_dir),
                            "status": "error", "errors": [], "meta": {}, "warnings": [],
                            "shadowed_by": ""}
    manifest_path = Path(pack_dir) / MANIFEST_NAME
    try:
        text = manifest_path.read_text("utf-8")
    except FileNotFoundError:
        base.update(status="invalid", errors=["缺少插件清单 plugin.json"])
        return base
    except OSError as exc:
        base.update(status="error", errors=["插件清单读取失败，请检查文件是否被占用或没有读取权限"],
                    detail=_error_text(exc))
        return base
    try:
        data = json.loads(text)
    except ValueError:
        base.update(status="invalid", errors=["插件清单不是合法的 JSON（可能被手动改坏了）"])
        return base
    try:
        status, errors, meta = _check_manifest(data, host_version, Path(pack_dir), available_ids)
    except (Exception, SystemExit) as exc:  # 防御：校验逻辑自身出错也只坏这一个包
        base.update(status="error", errors=["插件清单校验时出现意外错误"],
                    detail=_error_text(exc))
        return base
    base.update(id=str(meta.get("id") or ""), name=str(meta.get("name") or ""),
                version=str(meta.get("version") or ""), status=status, errors=errors,
                meta=meta, warnings=list(meta.get("warnings") or []))
    # 挂载点（1.3.108）：控制台直接显示「这个插件在哪几步起作用」，
    # 不用自己去翻 capabilities 猜。
    try:
        from .plugin_mounts import mount_points_of, describe as describe_mounts
        base["mounts"] = list(mount_points_of(meta.get("capabilities") or ()))
        base["mount_label"] = describe_mounts(meta.get("capabilities") or ())
    except Exception:
        base["mounts"] = []
        base["mount_label"] = ""
    return base


def discover(pack_dirs: List[Path], host_version: str,
             available_ids: Optional[Iterable[str]] = None) -> List[dict]:
    """扫描若干目录，返回每个插件包的状态。

    每个条目：{id, name, version, path, status, errors, meta, warnings, shadowed_by}
    status: ok / invalid / incompatible / error / shadowed。
    单个包出错只影响它自己；同一个 id 出现在多个目录时以先出现的为准，
    后面的条目 status='shadowed' 并带 shadowed_by=先出现的路径。
    """
    entries: List[dict] = []
    roots: List[Path] = []
    for raw in pack_dirs or []:
        try:
            roots.append(Path(raw))
        except (TypeError, ValueError):
            continue
    seen: Dict[str, dict] = {}
    for root in roots:
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir() or child.name.startswith("."):
                    continue
            except OSError:
                continue
            entry = _inspect_pack(child, host_version, None)
            pack_id = entry.get("id") or ""
            if pack_id and pack_id in seen:
                entry.update(status="shadowed",
                             shadowed_by=seen[pack_id]["path"],
                             errors=["同一 id 的插件包已存在于 " + seen[pack_id]["path"] + "，本包被忽略"])
                entries.append(entry)
                continue
            if pack_id:
                seen[pack_id] = entry
            entries.append(entry)

    # 依赖要等所有包都扫完再判，否则「A 依赖 B、B 排在后面」会被误判成缺依赖
    known = set(available_ids or ())
    known.update(entry["id"] for entry in entries if entry.get("status") == "ok")
    for entry in entries:
        if entry.get("status") != "ok":
            continue
        deps = list((entry.get("meta") or {}).get("dependencies") or [])
        missing = [dep for dep in deps if dep not in known and dep != entry.get("id")]
        if missing:
            entry["status"] = "invalid"
            entry["errors"] = ["依赖的插件未安装：" + "、".join(missing)]
    return entries


# ── 加载 ──────────────────────────────────────────────────────────────────
def load_module(entry_path: Path, pack_id: str) -> dict:
    """按文件路径加载插件包入口，返回 {ok, module, reason, message, errors}。

    模块名固定 whale_pack_<id>，同一个 id 重复加载会先清掉旧模块（用户改完
    plugin.py 再点重试就能生效）。包目录会加进 sys.path，让入口脚本能 import
    自己包里的兄弟文件；异常一律转成 dict，不向调用方抛。
    """
    entry = Path(entry_path)
    if not isinstance(pack_id, str) or not PACK_ID_RE.match(pack_id):
        return _result(False, "bad_id", "插件包标识不合法", module=None, errors=["插件包标识不合法"])
    if not entry.is_file():
        return _result(False, "entry_missing", "入口文件不存在：" + entry.name,
                       module=None, errors=["入口文件不存在：" + entry.name])
    module_name = PACK_MODULE_PREFIX + pack_id
    sys.modules.pop(module_name, None)
    pack_dir = str(entry.parent)
    if pack_dir not in sys.path:
        sys.path.insert(0, pack_dir)
    try:
        spec = importlib.util.spec_from_file_location(module_name, entry)
        if spec is None or spec.loader is None:
            return _result(False, "spec_failed", "无法为这个入口文件创建加载器",
                           module=None, errors=["无法为这个入口文件创建加载器"])
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except (Exception, SystemExit) as exc:
            sys.modules.pop(module_name, None)
            message = "插件 " + pack_id + " 加载失败：" + _error_text(exc)
            return _result(False, "load_error", message, module=None, errors=[message])
        return _result(True, "", "", module=module, errors=[])
    except (Exception, SystemExit) as exc:
        sys.modules.pop(module_name, None)
        message = "插件 " + pack_id + " 加载失败：" + _error_text(exc)
        return _result(False, "load_error", message, module=None, errors=[message])


# ── 安装 / 删除 ───────────────────────────────────────────────────────────
def _copy_tree(source: Path, target: Path) -> None:
    """按上限复制目录：文件数、总体积、符号链接都要拦。"""
    total = 0
    count = 0
    for current, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            src = Path(current) / name
            if src.is_symlink():
                raise PackError("插件包里包含符号链接，出于安全考虑拒绝安装")
            count += 1
            total += src.stat().st_size
            if count > MAX_FILES:
                raise PackError("插件包文件太多（上限 " + str(MAX_FILES) + " 个）")
            if total > MAX_PACK_BYTES:
                raise PackError("插件包体积超过 20MB 上限")
    shutil.copytree(source, target, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _extract_zip(archive: Path, target: Path) -> None:
    """解压 zip 到 target，逐条防 zip-slip，并守住体积与文件数上限。"""
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_FILES:
                raise PackError("插件包文件太多（上限 " + str(MAX_FILES) + " 个）")
            target.mkdir(parents=True, exist_ok=True)
            root = target.resolve()
            written = 0
            for info in infos:
                raw_name = info.filename.replace("\\", "/")
                if raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name):
                    raise PackError("插件包里包含绝对路径，已拒绝安装")
                parts = [part for part in raw_name.split("/") if part not in _UNSAFE_PARTS]
                if not parts:
                    continue
                if ".." in raw_name.split("/"):
                    raise PackError("插件包里包含 .. 路径，已拒绝安装（防止写到包外）")
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise PackError("插件包里包含符号链接，出于安全考虑拒绝安装")
                destination = (target.joinpath(*parts)).resolve()
                if destination != root and root not in destination.parents:
                    raise PackError("插件包里的路径跳出了安装目录，已拒绝安装")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as reader, open(destination, "wb") as writer:
                    while True:
                        chunk = reader.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_PACK_BYTES:
                            raise PackError("插件包解压后超过 20MB 上限")
                        writer.write(chunk)
    except zipfile.BadZipFile:
        raise PackError("这个文件不是有效的 zip 压缩包") from None


def _find_pack_root(base: Path) -> Optional[Path]:
    """在解压/复制出来的目录里找到真正带 plugin.json 的那一层。"""
    if (base / MANIFEST_NAME).is_file():
        return base
    children = [child for child in sorted(base.iterdir()) if child.is_dir()
                and not child.name.startswith(".")] if base.is_dir() else []
    if len(children) == 1 and (children[0] / MANIFEST_NAME).is_file():
        return children[0]
    for child in children:
        if (child / MANIFEST_NAME).is_file():
            return child
    return None


def _host_version() -> str:
    try:
        from .runtime_identity import APP_VERSION
        return str(APP_VERSION)
    except Exception:
        return "0.0.0"


def install_pack(source: Path, dest_root: Path, host_version: Optional[str] = None) -> dict:
    """安装一个插件包（目录或 zip）。

    流程：先解压/复制到 dest_root 下的临时目录（同盘才能原子改名）→ 校验清单 →
    原子移动到 dest_root/<id> → 再按落盘位置校验一次（这时才查得到 entry 是否存在）。
    目标已存在时**不覆盖**，返回 reason='exists'；校验失败会把刚复制的目录删掉，
    返回 reason='invalid'，磁盘上不留半装状态，也不留临时目录。
    """
    src = Path(source)
    dest = Path(dest_root)
    host = host_version or _host_version()
    result = _result(False, "", "", id="", version="", path="", errors=[])
    if not src.exists():
        result.update(reason="not_found", message="找不到要安装的插件包：" + str(src),
                      errors=["找不到要安装的插件包"])
        return result
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.update(reason="io_error", message="安装目录无法写入，请检查磁盘空间或权限",
                      errors=["安装目录无法写入"], detail=_error_text(exc))
        return result

    temp = Path(tempfile.mkdtemp(prefix=".install-", dir=str(dest)))
    try:
        stage = temp / "stage"
        try:
            if src.is_dir():
                _copy_tree(src, stage)
            elif zipfile.is_zipfile(src):
                _extract_zip(src, stage)
            else:
                result.update(reason="unsupported",
                              message="只支持安装文件夹或 zip 压缩包",
                              errors=["只支持安装文件夹或 zip 压缩包"])
                return result
        except PackError as exc:
            result.update(reason="unsafe_zip", message=str(exc), errors=[str(exc)])
            return result
        except OSError as exc:
            result.update(reason="io_error", message="读取插件包失败，请检查文件是否被占用",
                          errors=["读取插件包失败"], detail=_error_text(exc))
            return result

        pack_root = _find_pack_root(stage)
        if pack_root is None:
            message = "插件包里没有找到 plugin.json，请确认压缩包解压后是一个插件目录"
            result.update(reason="invalid", message=message, errors=[message])
            return result
        try:
            data = json.loads((pack_root / MANIFEST_NAME).read_text("utf-8"))
        except (OSError, ValueError):
            message = "插件清单不是合法的 JSON（可能被手动改坏了）"
            result.update(reason="invalid", message=message, errors=[message])
            return result

        ok, errors, meta = validate_manifest(data, host, pack_root)
        pack_id = str(meta.get("id") or "")
        if not ok or not PACK_ID_RE.match(pack_id):
            message = "插件清单校验没通过，已取消安装"
            result.update(reason="invalid", message=message, errors=errors or [message],
                          id=pack_id, version=str(meta.get("version") or ""))
            return result

        target = dest / pack_id
        if target.exists():
            message = "已经装过同 id 的插件包（" + pack_id + "），请先删除再安装"
            result.update(reason="exists", message=message, errors=[message],
                          id=pack_id, version=str(meta.get("version") or ""), path=str(target))
            return result

        try:
            os.replace(str(pack_root), str(target))
        except OSError:
            # 跨盘或占用时退化成复制：仍然先落到临时目标再改名，避免半截目录
            shutil.copytree(pack_root, target)
            shutil.rmtree(pack_root, ignore_errors=True)

        ok, errors, meta = validate_manifest(data, host, target)
        if not ok:
            shutil.rmtree(target, ignore_errors=True)
            message = "插件清单校验没通过，已回滚删除刚复制的目录"
            result.update(reason="invalid", message=message,
                          errors=list(errors) + ["安装已回滚，磁盘上没有留下这个插件包"],
                          id=pack_id, version=str(meta.get("version") or ""))
            return result

        result.update(ok=True, reason="", message="插件包 " + str(meta.get("name") or pack_id) + " 安装完成",
                      id=pack_id, version=str(meta.get("version") or ""), path=str(target),
                      errors=[])
        return result
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def remove_pack(dest_root: Path, pack_id: str, purge: bool = False,
                state_dir: Optional[Path] = None, settings_path: Optional[Path] = None
                ) -> dict:
    """删除 dest_root 下的一个插件包目录。

    id 只允许小写字母/数字/下划线，并且解析后的目录必须正好是 dest_root 的直接子目录，
    避免 ../ 之类的写法删到别处。不存在时返回 reason='not_found'，不报错。

    purge=True（1.3.107）时连数据一起清：state_dir/<id>/ 与 settings_path 里该 id 的键。
    为什么需要它：用户的判定标准第 2 条是「删掉 jar，行为消失，不残留」。只删目录的话，
    设置留在 plugin_settings.json、状态留在 plugin_state/<id>/，重装之后「老设置又回来了」——
    那不是卸载，那是禁用。

    默认 purge=False：清数据不可逆，不能因为调用方没想清楚就替它决定。
    两个路径都由调用方传（这里不知道 manager 的内部布局），任一为 None 就跳过对应清理。
    """
    if not isinstance(pack_id, str) or not _REMOVE_ID_RE.match(pack_id) or pack_id in (".", ".."):
        return _result(False, "bad_id", "插件包标识不合法，已拒绝删除",
                       removed=False, errors=["插件包标识不合法"])
    root = Path(dest_root)
    try:
        root_resolved = root.resolve()
        target = (root / pack_id).resolve()
    except OSError as exc:
        return _result(False, "io_error", "插件包路径无法解析", removed=False,
                       errors=["插件包路径无法解析"], detail=_error_text(exc))
    if target.parent != root_resolved:
        return _result(False, "path_traversal", "插件包路径不在插件目录里，已拒绝删除",
                       removed=False, errors=["插件包路径不在插件目录里"])
    if not target.is_dir() or target.is_symlink():
        return _result(False, "not_found", "没有找到插件包 " + pack_id, removed=False,
                       errors=["没有找到插件包 " + pack_id])
    try:
        shutil.rmtree(target)
    except OSError as exc:
        return _result(False, "io_error", "删除插件包失败，请稍后再试", removed=False,
                       errors=["删除插件包失败"], detail=_error_text(exc))

    purged = []
    if purge:
        purged = _purge_plugin_data(pack_id, state_dir, settings_path)
    return _result(True, "", "插件包 " + pack_id + " 已删除"
                   + ("（连同设置与状态）" if purge else ""),
                   removed=True, id=pack_id, path=str(target), purged=purged, errors=[])


def _purge_plugin_data(pack_id: str, state_dir: Optional[Path],
                       settings_path: Optional[Path]) -> List[str]:
    """清掉这个插件留下的设置与状态；返回真的清掉了哪些（给界面显示）。

    每一步都独立 try：状态目录删不掉不该拦住设置清理，反之亦然。
    清不掉的那一项不写进返回值 —— 报告「清了什么」比报告「打算清什么」有用。
    """
    purged: List[str] = []
    if state_dir is not None:
        folder = Path(state_dir) / pack_id
        try:
            if folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder)
                purged.append("state")
        except OSError:
            pass
    if settings_path is not None:
        path = Path(settings_path)
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and pack_id in data:
            data.pop(pack_id, None)
            try:
                from .persistence import atomic_text
                atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2), backup=True)
                purged.append("settings")
            except Exception:
                pass
    return purged


def pack_dirs_for(data_dir: Optional[Path] = None) -> List[Path]:
    """对外的便利函数：默认扫描的插件包目录（程序旁优先，其次数据目录）。"""
    return default_pack_dirs(data_dir)
