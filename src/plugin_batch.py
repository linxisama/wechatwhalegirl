"""批量导入插件：多选 / 文件夹 / 拖拽进来的路径，一次预检、一次重载。

为什么单独一个模块（1.3.0 P0-2）：`wechat_app` 里那些路由只该做"校验参数 + 返回 JSON"，
安装的业务流程（展开路径、去重、预检、写入、回滚、重载、按需启用）放进这里，
既能被 HTTP 调用，也能被测试和脚本直接调用，不用起一个后台服务。

设计要点：
- **预检不写盘**：用户先看到"要装什么、和已有的重名不重名、有没有危险用法"，再决定。
- **批量只重载一次**：每装一个就 `manager.reload()` 会把 30 多个插件的状态重算 N 遍，
  用户看到的是界面卡顿；这里全部写完后统一 reload。
- **装完加载不起来就回滚**：覆盖安装恢复旧文件、新装删除坏文件，不留一个"半装"状态。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List

from .plugin_installer import (InstallError, check_source, install_from_file,
                               inspect_local, normalize_plugin_id)

CANDIDATE_SUFFIXES = {".py", ".zip"}
_NAME_RE = re.compile(r"name\s*=\s*['\"]([^'\"]{1,40})['\"]")
_ID_RE = re.compile(r"id\s*=\s*['\"]([a-z0-9_]{2,40})['\"]")


def expand_paths(paths: Iterable[str]) -> List[Path]:
    """把文件、文件夹、拖进来的一堆路径展开成候选文件列表（去重、排序）。

    文件夹只往下找一层 `.py` / `.zip`：插件包本身不会嵌套多层，
    而递归整棵树可能把 `__pycache__` 里的东西也捞进来。
    """
    found: List[Path] = []
    seen = set()
    for raw in paths:
        path = Path(str(raw))
        if path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_file() and child.suffix.lower() in CANDIDATE_SUFFIXES:
                    key = str(child.resolve()).lower()
                    if key not in seen:
                        seen.add(key)
                        found.append(child)
        elif path.is_file() and path.suffix.lower() in CANDIDATE_SUFFIXES:
            key = str(path.resolve()).lower()
            if key not in seen:
                seen.add(key)
                found.append(path)
    return found


def _guess_id(code: str, fallback: str) -> str:
    """优先用代码里 `PluginMeta(id=...)` 的标识。

    文件名只是"投喂时的名字"，真正的插件标识在 META 里；两者不一致时
    `manager` 按文件名注册、模块里却声明另一个 id，会表现为"装了但加载不出来"。
    """
    match = _ID_RE.search(code or "")
    if match:
        try:
            return normalize_plugin_id(match.group(1))
        except InstallError:
            pass
    return normalize_plugin_id(fallback)


def _guess_name(code: str, plugin_id: str) -> str:
    match = _NAME_RE.search(code)
    return match.group(1) if match else plugin_id


def _inspect(path: Path) -> dict:
    """预检单个文件；文件名太短/不合规时改用代码里的 META id 再试一次。

    为什么要兜这一层：用户随手存成 `a.py`、`1.py` 很常见，`inspect_local`
    会先按文件名推断 id 并直接报错——但那不是代码问题，插件本身是好的。
    """
    try:
        return inspect_local(str(path))
    except InstallError:
        if path.suffix.lower() != ".py":
            raise
        code = path.read_text("utf-8", "replace")
        return inspect_local(str(path), _guess_id(code, path.stem))


def preview(paths: Iterable[str], plugins_dir: Path) -> dict:
    """逐个预检，返回可以直接给界面展示的列表；不写任何文件。"""
    items = []
    for path in expand_paths(paths):
        entry = {"path": str(path), "plugin_id": "", "name": "", "exists": False,
                 "ok": False, "problems": [], "error": ""}
        try:
            info = _inspect(path)
            code_preview = info.get("preview") or ""
            plugin_id = _guess_id(code_preview, path.stem)
            entry.update(plugin_id=plugin_id,
                         name=_guess_name(code_preview, plugin_id),
                         problems=list(info.get("problems") or []),
                         ok=bool(info.get("ok")))
            entry["exists"] = (Path(plugins_dir) / f"{plugin_id}.py").exists()
        except (InstallError, OSError, ValueError) as exc:
            entry["error"] = str(exc)[:200]
        items.append(entry)
    return {"items": items,
            "ok": bool(items) and all(item["ok"] for item in items),
            "conflicts": [item["plugin_id"] for item in items if item.get("exists")]}


def install(manager, paths: Iterable[str], *, overwrite: bool = False,
            enable: bool = False) -> dict:
    """批量安装；全部写完后只重载一次；加载失败的按"覆盖/新装"分别回滚。"""
    targets = expand_paths(paths)
    if not targets:
        raise InstallError("没有找到可安装的文件（只支持 .py 或 .zip）")
    plugins_dir = Path(manager.custom_plugins_dir)
    plugins_dir.mkdir(parents=True, exist_ok=True)

    results, backups = [], {}
    for path in targets:
        target_id = ""
        try:
            info = _inspect(path)
            target_id = _guess_id(info.get("preview") or "", path.stem)
            destination = plugins_dir / f"{target_id}.py"
            if destination.exists():
                backups[target_id] = destination.read_bytes()
            result = install_from_file(str(path), plugins_dir, target_id,
                                       confirmed=True, overwrite=overwrite)
            # 必须登记到自定义清单：`reload()` 只加载 PLUGIN_MODULES + 已登记的自定义插件，
            # 光把文件写进目录不会生效（这一步漏了会表现成"装了但列表里没有"）。
            manager.register_custom(target_id)
            results.append({"plugin_id": target_id, "path": str(path),
                            "ok": True, "error": "", "installed": result.get("path", "")})
        except (InstallError, OSError, ValueError) as exc:
            results.append({"plugin_id": target_id or path.stem, "path": str(path),
                            "ok": False, "error": str(exc)[:200], "installed": ""})

    manager.reload()
    loaded = set(manager._plugins)
    rolled_back = []
    for item in results:
        if not item["ok"]:
            continue
        plugin_id = item["plugin_id"]
        if plugin_id in loaded:
            item["loaded"] = True
            continue
        # 写了却加载不起来 → 回滚，别留一个"看得见但用不了"的插件。
        item["loaded"] = False
        destination = plugins_dir / f"{plugin_id}.py"
        try:
            if plugin_id in backups:
                destination.write_bytes(backups[plugin_id])
            else:
                if destination.exists():
                    destination.unlink()
                manager.unregister_custom(plugin_id)
        except OSError:
            pass
        rolled_back.append(plugin_id)
    if rolled_back:
        manager.reload()

    if enable:
        for item in results:
            if not item.get("loaded"):
                continue
            plugin = manager._plugins.get(item["plugin_id"])
            # 没有 `enabled` 字段的插件本来就没有开关，别谎报"已启用"。
            if plugin is None or "enabled" not in (plugin.META.config_schema or {}):
                item["enabled"] = None
                continue
            try:
                manager.update_settings(item["plugin_id"], {"enabled": True})
                item["enabled"] = bool(plugin.get("enabled"))
            except (ValueError, KeyError):
                item["enabled"] = False

    return {"results": results,
            "installed": sum(1 for item in results if item.get("loaded")),
            "failed": sum(1 for item in results if not item.get("loaded")),
            "rolled_back": rolled_back}
