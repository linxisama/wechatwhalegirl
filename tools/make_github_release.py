#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把构建产物整理成一套可以直接上传到 GitHub Releases 的资产。

用法（在项目根目录）：
    python tools/make_github_release.py            # 用 src/runtime_identity.py 的版本
    python tools/make_github_release.py --version 1.3.70

产物目录：release/<版本>/
    WhaleGirlWeChat-Setup-<ver>.exe          安装包（= 更新包）
    WhaleGirlWeChat-Setup-<ver>.exe.sha256   单文件校验（"<sha256>  <文件名>"）
    WhaleGirlWeChat-Source-<ver>.zip         源码包
    WhaleGirlWeChat-PluginPacks-<ver>.zip    可选插件包合集
    release-notes-<ver>.md                   版本说明（复制到 GitHub Release 正文）
    update.json                              给「自定义更新源」用的清单
    SHA256SUMS.txt                           全部资产的校验值
    manifest.json                            机器可读的发布清单

设计约束：
- 只读取现有产物，不构建、不改版本号、不联网、不推送。
- 幂等：重复运行会覆盖 release/<版本>/ 下的同名文件。
- 版本号只从 src/runtime_identity.py 读，避免两处不一致。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IDENTITY = ROOT / "src" / "runtime_identity.py"

ZIP_MODE = zipfile.ZIP_DEFLATED
EXCLUDE_DIRS = {
    ".git", ".venv-wechat", "__pycache__", "build", "dist", "installer",
    "data", "logs", "wechatauto_logs", "wechatauto文件下载", "发布", "release",
    ".build-tools", ".tmp", "out", "node_modules", ".idea", ".vscode",
}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log", ".tmp", ".orig", ".bak")
_BUILD_DIR_RE = re.compile(r"^(?:build|dist)\d*[a-z]?$")


def read_version() -> str:
    for line in IDENTITY.read_text(encoding="utf-8").splitlines():
        if line.startswith("APP_VERSION"):
            m = re.search(r"""['"]([^'"]+)['"]""", line)
            if m:
                return m.group(1)
    raise SystemExit("在 src/runtime_identity.py 里读不到 APP_VERSION")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _skip(rel: Path) -> bool:
    for part in rel.parts:
        if part in EXCLUDE_DIRS or part.startswith(".tmp"):
            return True
        # 1.3.4x 起每轮构建用独立目录（dist67/build67/build51b…），按前缀一并排除，
        # 否则源码包会把上 GB 的构建产物打进去。
        if _BUILD_DIR_RE.match(part):
            return True
    return rel.suffix.lower() in EXCLUDE_SUFFIX


def zip_tree(base: Path, target: Path, prefix: str) -> int:
    count = 0
    with zipfile.ZipFile(target, "w", ZIP_MODE) as zf:
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(base)
            if _skip(rel):
                continue
            zf.write(path, str(Path(prefix) / rel).replace("\\", "/"))
            count += 1
    return count


def zip_source(target: Path) -> int:
    return zip_tree(ROOT, target, f"WhaleGirlWeChat-{target.stem}")


def zip_plugin_packs(target: Path) -> int:
    packs = ROOT / "plugin-packs"
    if not packs.is_dir():
        return 0
    return zip_tree(packs, target, "plugin-packs")


def read_notes(version: str) -> str:
    """从 CHANGES.md 里抽出当前版本的段落；抽不到就退回一句通用说明。"""
    changes = ROOT / "CHANGES.md"
    if changes.is_file():
        text = changes.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = None
        for idx, line in enumerate(lines):
            if line.strip().startswith("##") and version in line:
                start = idx
                break
        if start is not None:
            body = []
            for line in lines[start + 1:]:
                if line.strip().startswith("## "):
                    break
                body.append(line)
            block = "\n".join(body).strip()
            if block:
                return f"# 鲸鱼娘微信控制台 {version}\n\n{block}\n"
    return f"# 鲸鱼娘微信控制台 {version}\n\n本版本为功能与稳定性更新。\n"


def human_size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="整理 GitHub Releases 资产")
    parser.add_argument("--version", default="", help="覆盖版本号（默认读 src/runtime_identity.py）")
    parser.add_argument("--base-url", default="", help="写进 update.json 的下载地址前缀（可选）")
    args = parser.parse_args(argv)

    version = args.version.strip() or read_version()
    setup = ROOT / "installer" / f"WhaleGirlWeChat-Setup-{version}.exe"
    if not setup.is_file():
        raise SystemExit(
            f"找不到安装包：{setup}\n请先运行 build_installer.ps1 构建 {version} 的安装包。"
        )

    out = ROOT / "release" / version
    out.mkdir(parents=True, exist_ok=True)

    assets: list[dict] = []

    def record(path: Path, kind: str, files: int | None = None) -> None:
        digest = sha256_of(path)
        item = {
            "name": path.name,
            "kind": kind,
            "size": path.stat().st_size,
            "sha256": digest,
        }
        if files is not None:
            item["files"] = files
        assets.append(item)

    # 1) 安装包
    setup_copy = out / setup.name
    if setup_copy.resolve() != setup.resolve():
        shutil.copy2(setup, setup_copy)
    record(setup_copy, "installer")

    # 2) 校验文件（和安装包同名 + .sha256，更新器会先找它）
    checksum_text = f"{sha256_of(setup_copy)}  {setup_copy.name}\n"
    (out / (setup_copy.name + ".sha256")).write_text(checksum_text, encoding="utf-8", newline="\n")

    # 3) 源码包 / 插件包
    src_zip = out / f"WhaleGirlWeChat-Source-{version}.zip"
    files = zip_source(src_zip)
    record(src_zip, "source", files)

    packs_zip = out / f"WhaleGirlWeChat-PluginPacks-{version}.zip"
    pack_files = zip_plugin_packs(packs_zip)
    if pack_files:
        record(packs_zip, "plugin-packs", pack_files)
    else:
        try:
            packs_zip.unlink()
        except OSError:
            pass

    # 4) 版本说明
    notes = read_notes(version)
    notes_path = out / f"release-notes-{version}.md"
    notes_path.write_text(notes, encoding="utf-8", newline="\n")
    record(notes_path, "notes")

    # 5) 更新清单（给「自定义更新源」用；GitHub Releases 的接口不需要它）
    base = args.base_url.strip().rstrip("/")
    download = f"{base}/{setup_copy.name}" if base else setup_copy.name
    checksum_url = f"{base}/{setup_copy.name}.sha256" if base else setup_copy.name + ".sha256"
    update = {
        "version": version,
        "notes": notes.strip(),
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "installer": {
            "name": setup_copy.name,
            "url": download,
            "size": setup_copy.stat().st_size,
            "sha256": sha256_of(setup_copy),
            "checksum_url": checksum_url,
        },
    }
    update_path = out / "update.json"
    update_path.write_text(json.dumps(update, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    record(update_path, "update-manifest")

    # 6) 总校验文件
    lines = [f"{item['sha256']}  {item['name']}" for item in assets]
    (out / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    manifest = {
        "version": version,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": assets,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    print(f"发布目录：{out}")
    for item in assets:
        extra = f" ({item['files']} 个文件)" if item.get("files") else ""
        print(f"  {item['name']:<48}{human_size(item['size']):>10}  {item['sha256'][:16]}…{extra}")
    print("\n下一步：把 release/%s/ 里的文件全部上传到 GitHub Release（tag = v%s）。" % (version, version))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
