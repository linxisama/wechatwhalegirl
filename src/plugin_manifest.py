"""plugin_manifest.py - 插件清单（plugin.json）的解析与校验。

为什么需要这一层：插件包在社区里有两套方言在流通——

- **规格方言**：``{"name": ..., "version": ..., "entry": "main.py"}``（没有 id，
  标识由包里的文件名决定）；
- **内核方言**：``{"id": ..., "name": ..., "version": ..., "api_version": ...,
  "author": {"name": ..., "contact": ...}, "entry": "main.py:setup",
  "permissions": [...]}``。

两种都必须能读（宽容优先）：``load_manifest`` 把它们归一成同一份内部结构，
``validate_manifest`` 只把**真正会出事的三件事**判死——标识形状、entry 逃逸、
API 版本不受支持。其余字段缺省即可，因为**裸 .py 没有清单也是合法的老形态**，
不能因为加了清单就把老插件判成不能装。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Union

# ── 常量 ──────────────────────────────────────────────────────────────────────
MANIFEST_NAME = "plugin.json"
SUPPORTED_API_VERSIONS = ("1",)   # 新增 API 版本时往这里加，老版本永远保留
DEFAULT_VERSION = "0.0.0"
DEFAULT_API_VERSION = "1"
MAX_ENTRY_DEPTH = 3               # entry 最多允许 3 层（含文件名）

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
ENTRY_FUNC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DRIVE_RE = re.compile(r"^[A-Za-z]:")   # C:/x.py 这类盘符写法

FIELDS = ("id", "name", "version", "api_version", "author", "description",
          "entry", "permissions")

# manifest_from_source 的正则兜底：源码没法 ast 解析时（或写法太怪）还能抽到 META
_META_CALL_RE = re.compile(r"PluginMeta\s*\((?P<body>[^)]*)\)", re.S)
_META_KV_RE = re.compile(
    r"(?P<key>id|name|version)\s*=\s*[rRuUbB]{0,2}[\"'](?P<val>[^\"']*)[\"']")


# ── 小工具 ────────────────────────────────────────────────────────────────────
def _text(value) -> str:
    """把任意值取成去空白的字符串；取不到就是空串（清单字段一律宽容）。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _api_version_text(value) -> str:
    """api_version 归一：内核方言写 int 1，规格方言根本不写，都收成 "1"。"""
    if value is None or value == "":
        return DEFAULT_API_VERSION
    return str(value).strip()


def _author(value) -> Dict[str, str]:
    """author 两种形态都收：字符串，或 {name, contact}。"""
    if isinstance(value, dict):
        return {"name": _text(value.get("name")),
                "contact": _text(value.get("contact"))}
    return {"name": _text(value), "contact": ""}


def _permissions(value) -> List[str]:
    """permissions 归一成字符串列表：数组/单个字符串/{"net": true} 都收。"""
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [str(key).strip() for key, flag in value.items() if flag]
    text = _text(value)
    return [text] if text else []


def _decode_json_bytes(raw: bytes):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("插件清单不是 UTF-8 文本：%s" % exc) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("插件清单不是合法 JSON：%s" % exc) from None


# ── 读取 ──────────────────────────────────────────────────────────────────────
def load_manifest(data: Union[bytes, bytearray, dict, str]) -> Dict[str, object]:
    """解析并归一一份插件清单，返回统一内部结构。

    参数可以是 JSON 字节串，也可以是已经 json.loads 过的 dict。
    顶层不是对象、或字节串不是合法 UTF-8 JSON 时抛 ``ValueError``
    （消息面向用户）；字段缺失一律走缺省值，不在这里判生死。
    """
    if isinstance(data, (bytes, bytearray)):
        data = _decode_json_bytes(bytes(data))
    elif isinstance(data, str):
        data = _decode_json_bytes(data.encode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("插件清单顶层必须是 JSON 对象，收到 %s" % type(data).__name__)

    manifest = {
        "id": _text(data.get("id")),
        "name": _text(data.get("name")),
        "version": _text(data.get("version")) or DEFAULT_VERSION,
        "api_version": _api_version_text(data.get("api_version")),
        "author": _author(data.get("author")),
        "description": _text(data.get("description")),
        "entry": _text(data.get("entry")),
        "permissions": _permissions(data.get("permissions")),
    }
    if not manifest["name"]:
        manifest["name"] = manifest["id"]
    return manifest


def _entry_errors(entry: str) -> List[str]:
    """entry 的形状与逃逸检查（只有 entry 存在时才判）。"""
    raw = entry.strip()
    if "\\" in raw:
        return ["entry 不许包含反斜杠（路径分隔符请用 /）：%r" % (raw,)]
    if DRIVE_RE.match(raw):
        return ["entry 不许带盘符或 URL 前缀：%r" % (raw,)]
    path_part, _, func_part = raw.partition(":")
    if not path_part:
        return ["entry 缺少文件部分（<文件>.py 或 <文件>.py:<函数名>）：%r" % (raw,)]
    if PurePosixPath(path_part).is_absolute() or path_part.startswith("/"):
        return ["entry 不许是绝对路径（不许逃出插件目录）：%r" % (raw,)]
    if not path_part.lower().endswith(".py"):
        return ["entry 必须是 <文件>.py 或 <文件>.py:<函数名>，收到 %r" % (raw,)]
    parts = [item for item in path_part.split("/") if item not in ("", ".")]
    if any(item == ".." for item in parts):
        return ["entry 不许含 ..（不许逃出插件目录）：%r" % (raw,)]
    if not parts or len(parts) > MAX_ENTRY_DEPTH:
        return ["entry 目录层级过深（最多 %d 层）：%r" % (MAX_ENTRY_DEPTH, raw)]
    if func_part and not ENTRY_FUNC_RE.match(func_part):
        return ["entry 的函数名不合法（只允许字母/数字/下划线）：%r" % (raw,)]
    return []


def validate_manifest(manifest: Dict[str, object]) -> List[str]:
    """校验归一化后的清单；返回错误列表（空 = 通过）。

    只判三件事，且都是"装了就会出事"的：插件标识形状、entry 逃逸、API 版本。
    标识缺失**不算错**——规格方言本来就没有 id，由包里的文件名兜底；
    但写了就必须合法。API 版本不受支持时 fail-closed（宿主不装自己不会跑的插件）。
    """
    if not isinstance(manifest, dict):
        return ["插件清单必须是对象"]

    errors: List[str] = []

    plugin_id = _text(manifest.get("id"))
    if plugin_id and not PLUGIN_ID_RE.match(plugin_id):
        errors.append("插件标识不合法：%r（只允许小写字母开头、2-48 位小写字母/数字/下划线）"
                      % (plugin_id,))

    api_version = _text(manifest.get("api_version")) or DEFAULT_API_VERSION
    if api_version not in SUPPORTED_API_VERSIONS:
        errors.append("宿主不支持该插件 API 版本：%r（本宿主支持 %s）"
                      % (api_version, "、".join(SUPPORTED_API_VERSIONS)))

    entry = _text(manifest.get("entry"))
    if entry:
        errors.extend(_entry_errors(entry))
    return errors


def entry_file(entry: str) -> str:
    """entry 里的文件部分（去掉 ":函数名"）；不校验，只切分。"""
    return str(entry or "").partition(":")[0].strip()


# ── 从裸 .py 源码里尽力抽 META ────────────────────────────────────────────────
def _meta_fields_from_ast(text: str) -> Optional[Dict[str, str]]:
    """ast 找 ``META = PluginMeta(...)``，取 id/name/version。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None

    found: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "META" for t in targets):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "PluginMeta":
            continue
        for keyword in call.keywords:
            if keyword.arg not in ("id", "name", "version"):
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                found[keyword.arg] = keyword.value.value
        # 位置写法：PluginMeta("id", "名字", "说明")
        for index, key in enumerate(("id", "name")):
            if key in found or len(call.args) <= index:
                continue
            arg = call.args[index]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found[key] = arg.value
    return found or None


def _meta_fields_from_regex(text: str) -> Optional[Dict[str, str]]:
    """正则兜底：源码语法都解析不了时，仍尽力认出 PluginMeta(...) 里的字段。"""
    call = _META_CALL_RE.search(text)
    if call is None:
        return None
    found: Dict[str, str] = {}
    for match in _META_KV_RE.finditer(call.group("body")):
        found.setdefault(match.group("key"), match.group("val"))
    return found or None


def manifest_from_source(text: str) -> Optional[Dict[str, object]]:
    """从纯 .py 源码里尽力抽出一份清单形状的字典；抽不出返回 None。

    **抽不出不得因此拒绝安装**：裸 .py 没有清单是合法的老形态，调用方拿到 None
    就按老办法处理（标识用文件名）。抽出来的东西只用来补 version/name 这类记录字段。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    fields = _meta_fields_from_ast(text)
    if fields is None:
        fields = _meta_fields_from_regex(text)
    if not fields:
        return None
    return load_manifest(fields)
