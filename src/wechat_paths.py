"""只读、有限范围的数据目录发现，不依赖微信窗口或递归扫描磁盘。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


def clean_path(value: str) -> Path:
    text = os.path.expandvars(os.path.expanduser(str(value).strip().strip('"')))
    if re.fullmatch(r'[A-Za-z]:', text):
        text += '\\'
    elif re.match(r'^[A-Za-z]:[^\\/]', text):
        raise ValueError('请填写绝对路径，例如 E:\\微信数据，不能使用盘符相对路径')
    return Path(text).resolve()


def account_root(value: str) -> str:
    """只接受实际包含账号/db_storage 的结构，安装目录和图片目录不作为数据库。"""
    if not str(value).strip():
        return ''
    path = clean_path(value)
    selected = path_account(value)
    if selected:
        # 选择具体库文件或库内目录时仍保留其所属账号，不能误扫到旁边的旧账号。
        current = path.parent if path.is_file() else path
        for parent in (current, *list(current.parents)[:4]):
            if parent.name.lower() == 'db_storage':
                path = parent.parent.parent
                break
        else:
            if (path / 'db_storage').is_dir():
                path = path.parent
    elif (path / 'db_storage').is_dir():
        path = path.parent
    for candidate in (path, *(path / name for name in
                              ('xwechat_files', 'WeChat Files', 'xwechat_files_data'))):
        if not candidate.is_dir():
            continue
        # 只查看候选目录的一层；不能为了找数据遍历整个 C/D/E 盘。
        with os.scandir(candidate) as entries:
            for index, entry in enumerate(entries):
                if index >= 2048:
                    break
                if entry.is_dir() and (Path(entry.path) / 'db_storage').is_dir():
                    return str(candidate)
    return ''


def path_account(value: str) -> str:
    """从用户明确选择的账号/数据库路径读取账号，不根据文件时间猜测。"""
    if not str(value).strip():
        return ''
    path = clean_path(value)
    if not path.exists():
        return ''
    if path.is_file():
        if path.suffix.lower() != '.db':
            return ''
        path = path.parent
    for candidate in (path, *list(path.parents)[:4]):
        if candidate.name.lower() == 'db_storage' and candidate.is_dir():
            return candidate.parent.name
    if (path / 'db_storage').is_dir():
        return path.name
    return ''


def _config_paths(text: str):
    """支持 JSON 内嵌路径和含中文空格的 INI 路径，不截断到第一个空格。"""
    try:
        data = json.loads(text.lstrip('\ufeff'))
    except (ValueError, TypeError):
        data = None
    stack = [data]
    for _ in range(512):
        if not stack:
            break
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(list(item.values())[:128])
        elif isinstance(item, list):
            stack.extend(item[:128])
        elif isinstance(item, str) and re.match(r'^(?:[A-Za-z]:[\\/]|\\\\|%[^%]+%)', item):
            yield item
    for match in re.finditer(r'[A-Za-z]:[\\/][^\x00\r\n\"<>|]*', text):
        yield match.group(0).strip().rstrip("';")
    for line in text.splitlines():
        value = line.split('=', 1)[-1].strip().strip('"')
        if value.startswith('%'):
            yield value


def _known_candidates():
    for env in ('APPDATA', 'LOCALAPPDATA'):
        base = os.environ.get(env)
        if not base:
            continue
        for relative in ('Tencent/xwechat', 'Tencent/xwechat/config',
                         'Tencent/WeChat', 'Tencent/WeChat/All Users/config'):
            directory = Path(base) / relative
            if not directory.is_dir():
                continue
            # 配置目录文件数量/大小都有限制，不读取用户消息或媒体文件。
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 64:
                        break
                    if not entry.is_file() or Path(entry.name).suffix.lower() not in (
                            '.ini', '.json', '.txt', '.config', '.cfg', '.data'):
                        continue
                    try:
                        with open(entry.path, 'rb') as stream:
                            content = stream.read(262145)
                        if len(content) > 262144:
                            continue
                        encodings = ('utf-16',) if content.startswith((b'\xff\xfe', b'\xfe\xff')) else (
                            ('utf-16-le', 'utf-8-sig', 'gb18030') if b'\x00' in content else ('utf-8-sig', 'gb18030'))
                        for encoding in encodings:
                            try:
                                text = content.decode(encoding)
                                break
                            except UnicodeError:
                                continue
                        else:
                            continue
                        yield from _config_paths(text)
                    except OSError:
                        continue
    if os.name == 'nt':
        import winreg
        for subkey in (r'Software\Tencent\WeChat', r'Software\Tencent\xwechat',
                       r'Software\Tencent\xwechat\config',
                       r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                    for index in range(128):
                        try:
                            name, value, _ = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        if isinstance(value, str) and any(token in name.lower() for token in
                                                          ('path', 'dir', 'save', 'personal')):
                            yield value
            except OSError:
                continue
    home = Path(os.environ.get('USERPROFILE') or Path.home())
    yield str(home / 'Documents')
    yield str(home)


def resolve_database_dir(configured: str = '', detector=None) -> str:
    """用户显式指定的无效路径不能降级到另一个账号的数据目录。"""
    if str(configured).strip():
        try:
            found = account_root(configured)
        except (OSError, ValueError) as exc:
            raise ValueError(f'微信数据目录无法读取：{exc}') from exc
        if found:
            return found
        raise ValueError('指定目录中没有找到 <账号>/db_storage；请选择微信数据根目录、账号目录或 db_storage')
    environment = os.environ.get('WECHATAUTO_DB_DIR', '')
    if environment:
        try:
            found = account_root(environment)
            if found:
                return found
        except (OSError, ValueError):
            pass
    if detector is not None:
        try:
            detected = detector()
            found = account_root(detected) if detected else ''
            if found:
                return found
        except (OSError, ValueError, RuntimeError):
            pass
    try:
        for candidate in _known_candidates():
            try:
                found = account_root(candidate)
                if found:
                    return found
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    raise ValueError('未找到微信数据库目录；请在设置中选择实际数据目录（支持 D/E 等盘符），不要选择微信安装目录或图片缓存')
