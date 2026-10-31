"""把一次发布的两份产物放进 `发布/<版本>/`。

为什么单独做脚本：用户 2026-10-23 定的交付模式是"安装包 + DIY 指令包"两件套。
以前只出安装包，源码和提示词要手工整理，容易漏文件或带上个人数据。
这里统一按白名单行走，任何人跑一次都能得到一样的两个压缩包。

用法（项目根目录）：
    python tools/make_release.py            # 用 src/runtime_identity.py 里的版本号
    python tools/make_release.py 1.2.71     # 指定版本号（仅用于补发）
"""
from __future__ import annotations

import os
import sys

# Windows 控制台默认 GBK：脚本里的"✓"会直接抛 UnicodeEncodeError（实测踩过一次）。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, ValueError):
    pass
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_ROOT = ROOT / '发布'
DOCS_DIR = ROOT / 'diy-src'
INSTALLER_DIR = ROOT / 'installer'

# 不进包的东西：虚拟环境、打包中间产物、个人数据、临时目录
SKIP_DIRS = {'.venv-wechat', '.build-tools', 'build', 'dist', 'data', 'logs', 'out',
             '__pycache__', 'wechatauto_logs', '.git', 'node_modules', '.idea', '发布'}
SKIP_PREFIX = ('.tmp', '.workbuddy')
SKIP_EXT = {'.pyc', '.pyo', '.exe', '.dll', '.pyd', '.log', '.db', '.sqlite', '.sqlite3'}
MAX_FILE = 2 * 1024 * 1024
SOURCE_DIRS = {'assets', 'backgrounds', 'diy-src', 'docs', 'instructions',
               'src', 'styles', 'tests', 'tools'}
SOURCE_FILES = {'.gitignore', 'AGENTS.md', 'CHANGES.md', 'LICENSE', 'README.md',
                'build_installer.ps1', 'gui_launcher.py', 'icon.ico', 'installer.iss',
                'ocr_worker.py', 'requirements.gui.txt', 'requirements.wechat.txt',
                'wechat_bot.spec', 'wechat_gui.py', 'wechat_main.py',
                '打包.bat', '启动（管理员）.cmd', '清理占用端口的旧后台.bat',
                '一键安装依赖.bat', '以管理员身份启动控制台.bat', '自检.bat',
                'start_gui.bat', 'start.bat', '封号风险与功能取舍.md',
                '功能对照与使用说明.md', '构建与发布说明.md', '微信版使用说明.md', 'QQ功能移植对照表.md'}


def current_version() -> str:
    text = (ROOT / 'src/runtime_identity.py').read_text(encoding='utf-8')
    match = re.search(r"APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
    if not match:
        raise SystemExit('读不到 APP_VERSION，先检查 src/runtime_identity.py')
    return match.group(1)


def iter_source():
    """源码白名单遍历：跳过环境、产物、个人数据与超大文件。"""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(SKIP_PREFIX)
                       and (Path(dirpath) != ROOT or d in SOURCE_DIRS)
                       and not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            path = Path(dirpath) / name
            if re.search(r'\.bak(?:$|[-.])|\.backup(?:$|[-.])',name,re.I):
                continue
            if path.is_symlink() or (Path(dirpath) == ROOT and name not in SOURCE_FILES):
                continue
            if path.suffix.lower() in SKIP_EXT:
                continue
            try:
                if path.stat().st_size > MAX_FILE:
                    continue
            except OSError:
                continue
            yield path, path.relative_to(ROOT).as_posix()


def write_source_zip(dest: Path) -> int:
    count = 0
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, arc in iter_source():
            zf.write(path, arc)
            count += 1
        zf.writestr('config.wechat.yaml', default_config_text())
        count += 1
    return count


def write_diy_zip(dest: Path, version: str) -> int:
    """DIY 指令包 = 三份使用说明 + 六份 AI 指令/地图/限制 + 完整源码（放在 源码/ 下）。"""
    count = 0
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for doc in sorted(DOCS_DIR.glob('*.md')):
            zf.write(doc, doc.name)
            count += 1
        for path, arc in iter_source():
            zf.write(path, '源码/' + arc)
            count += 1
        zf.writestr('源码/config.wechat.yaml', default_config_text())
        count += 1
    return count


def default_config_text():
    """源码包只放程序默认值，绝不复制开发机的真实配置或密钥。"""
    import yaml
    sys.path.insert(0, str(ROOT))
    from src.config_manager import DEFAULT_CONFIG
    return '# 示例配置：在控制台设置页填写自己的接口。\n' + yaml.safe_dump(DEFAULT_CONFIG, allow_unicode=True, sort_keys=False)


def main() -> int:
    version = sys.argv[1].strip() if len(sys.argv) > 1 else current_version()
    out = RELEASE_ROOT / version
    out.mkdir(parents=True, exist_ok=True)

    installer = INSTALLER_DIR / f'WhaleGirlWeChat-Setup-{version}.exe'
    if not installer.exists():
        print(f'✗ 找不到安装包：{installer}')
        print('  先跑：powershell -NoProfile -ExecutionPolicy Bypass -File build_installer.ps1')
        return 1
    target_exe = out / installer.name
    shutil.copy2(installer, target_exe)

    source_zip = out / f'WhaleGirlWeChat-源码-{version}.zip'
    files = write_source_zip(source_zip)
    diy_zip = out / f'WhaleGirlWeChat-DIY指令包-{version}.zip'
    diy_files = write_diy_zip(diy_zip, version)

    note = out / '发布说明.md'
    note.write_text(
        f"""# 微信机器人 {version} 发布包

| 文件 | 给谁用 | 说明 |
| --- | --- | --- |
| `{target_exe.name}` | 普通用户 | 双击安装，覆盖升级，保留原配置与聊天记录 |
| `{diy_zip.name}` | 想自己改的人 | AI 指令包：把 `00-先读我.md` 和整包丢给 DeepSeek 即可 |
| `{source_zip.name}` | 开发者 | 纯源码，不含虚拟环境与个人数据 |

## 安装包

- 版本：{version}（版本号唯一来源 `src/runtime_identity.py`）
- 安装目录：`%LOCALAPPDATA%\\Programs\\WhaleGirlWeChat`
- 用户数据：`%LOCALAPPDATA%\\WhaleGirlWeChat`（覆盖升级不动这里）
- 建议用开始菜单「鲸鱼娘 微信控制台（管理员启动）」启动

## DIY 指令包

- 文件数：{diy_files}（说明书 + 源码）
- 用法：解压后读 `00-先读我.md`，按里面三种方式之一让 AI 按项目真实结构改代码
- 源码文件数：{files}

## 校验

- 生成发布包本身不执行测试；核验以本版本的验收报告与构建日志为准
- 发布前须运行完整单元测试、冻结程序自检及 `tools/verify_installer.ps1` 覆盖升级验证
- 前台发送、严格后台控件发送和沙盒后台发送分别验收；不能相互替代
""", encoding='utf-8')

    print(f'✓ 发布目录：{out}')
    print(f'  安装包  {target_exe.name}  {target_exe.stat().st_size / 1048576:.1f} MB')
    print(f'  DIY 包  {diy_zip.name}  {diy_zip.stat().st_size / 1048576:.2f} MB（{diy_files} 个文件）')
    print(f'  源码包  {source_zip.name}  {source_zip.stat().st_size / 1048576:.2f} MB（{files} 个文件）')
    print(f'  发布说明 {note.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
