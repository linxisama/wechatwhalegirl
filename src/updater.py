"""自动更新核心：版本解析、发布信息读取、下载校验与安装程序启动。

这个模块只依赖 Python 标准库，刻意不导入 Qt、FastAPI 或项目运行时：
更新检查必须能在控制台后台、独立脚本和测试里复用，且离线时不能拖垮主程序。
所有对外函数都返回普通 dict（或明确抛 UpdateError），绝不把裸异常留给调用方。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

MAX_UPDATE_BYTES = 400 * 1024 * 1024
CHECK_TIMEOUT = 15.0
DOWNLOAD_TIMEOUT = 30.0
DEFAULT_FEED_URL = 'https://api.github.com/repos/linxisama/wechatwhalegirl/releases/latest'
# GitHub 未登录请求按出口 IP 限流（403/429），此时改走 Releases 直链的 update.json。
FALLBACK_FEED_URL = ('https://github.com/linxisama/wechatwhalegirl/releases/latest/download/update.json')
USER_AGENT = 'WhaleGirlWeChat-Updater/1.0'
GITHUB_ACCEPT = 'application/vnd.github+json'

_VERSION_RE = re.compile(
    r'^[vV]?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$')
_SHA256_RE = re.compile(r'(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])')


class UpdateError(Exception):
    """带稳定 reason 码与中文用户提示的更新异常。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = str(reason)
        self.message = str(message)

    def to_dict(self) -> dict:
        return {'reason': self.reason, 'message': self.message}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _clean_version(text: Any) -> str:
    value = str(text or '').strip()
    if value[:1].lower() == 'v' and parse_version(value):
        return value[1:].strip()
    return value


def parse_version(text: Any) -> tuple[int, ...]:
    """解析版本号；允许 v 前缀与数字预发布段，非法返回空元组。

    '1.3.62' -> (1, 3, 62)
    'v1.3.62' -> (1, 3, 62)
    '1.3.62-beta.1' -> (1, 3, 62, 1)
    '1.3' -> (1, 3, 0)
    """
    if not isinstance(text, str):
        return ()
    match = _VERSION_RE.match(text.strip())
    if not match:
        return ()
    try:
        parts = [int(piece) for piece in match.group(1).split('.')]
    except (TypeError, ValueError):
        return ()
    if len(parts) < 3:
        parts.extend([0] * (3 - len(parts)))
    prerelease = match.group(2)
    if prerelease:
        numbers = [int(piece) for piece in re.findall(r'\d+', prerelease)]
        parts.extend(numbers or [0])
    return tuple(parts)


def _compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    size = max(len(left), len(right))
    for index in range(size):
        a = left[index] if index < len(left) else 0
        b = right[index] if index < len(right) else 0
        if a != b:
            return 1 if a > b else -1
    return 0


def is_newer(latest: Any, current: Any) -> bool:
    """latest 是否严格新于 current；任一版本非法时返回 False。"""
    new_version = parse_version(latest)
    old_version = parse_version(current)
    if not new_version or not old_version:
        return False
    return _compare_versions(new_version, old_version) > 0


def _normalize_sha256(value: Any) -> str:
    text = str(value or '').strip().lower()
    if not text:
        return ''
    if text.startswith('sha256:'):
        text = text.split(':', 1)[1].strip()
    elif text.startswith('sha256-'):
        text = text.split('-', 1)[1].strip()
    match = _SHA256_RE.search(text)
    return match.group(1).lower() if match else ''


def _asset_name(name: Any, url: Any) -> str:
    value = str(name or '').strip()
    if value:
        return value
    path = urllib.parse.urlparse(str(url or '')).path
    return urllib.parse.unquote(path.rsplit('/', 1)[-1]) if path else ''


def _asset_kind(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith('.exe') and 'setup' in lowered:
        return 'installer'
    if lowered.endswith('.sha256') or lowered.endswith('.txt'):
        return 'checksum'
    return 'other'


def _safe_size(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _empty_release() -> dict:
    return {'version': '', 'notes': '', 'published_at': '', 'assets': []}


def _asset_entry(item: dict, *, fallback_name: str = '') -> dict | None:
    """把单个资产（GitHub asset 或 update.json installer）统一成内部形状。"""
    if not isinstance(item, dict):
        return None
    url = (item.get('browser_download_url') or item.get('url')
           or item.get('download_url') or '')
    name = _asset_name(item.get('name') or fallback_name, url)
    if not name and not url:
        return None
    return {
        'name': name,
        'url': str(url or ''),
        'size': _safe_size(item.get('size')),
        'sha256': _normalize_sha256(item.get('digest') or item.get('sha256')),
        'kind': _asset_kind(name),
    }


def parse_release(payload: Any, *, source: str = 'github') -> dict:
    """把 GitHub Releases / update.json 统一成界面可直接消费的结构。

    支持三种形状：
    - GitHub latest API：tag_name/body/assets[].browser_download_url/digest；
    - update.json：{version, notes, published_at, installer:{name,url,size,sha256,
      checksum_url}}；
    - 旧自定义形状：{version, notes, url, size, sha256}。

    无法识别的输入返回空结构而不是抛异常；check() 会据此给出 bad_payload。
    """
    if not isinstance(payload, dict):
        return _empty_release()
    version = _clean_version(payload.get('tag_name') or payload.get('version') or '')
    notes = payload.get('body')
    if not isinstance(notes, str):
        notes = payload.get('notes')
    if not isinstance(notes, str):
        notes = ''
    published_at = payload.get('published_at') or payload.get('created_at') or ''
    if not isinstance(published_at, str):
        published_at = str(published_at)

    raw_assets: list[Any] = []
    assets_value = payload.get('assets')
    if isinstance(assets_value, list):
        raw_assets = assets_value

    assets: list[dict] = []
    for item in raw_assets:
        entry = _asset_entry(item)
        if entry is not None:
            assets.append(entry)

    # update.json 的 installer 是单个对象，不放在 assets 列表里。
    installer = payload.get('installer')
    if isinstance(installer, dict):
        entry = _asset_entry(installer)
        if entry is not None:
            assets.append(entry)
        checksum_url = str(installer.get('checksum_url') or '').strip()
        if checksum_url:
            checksum_name = (str(entry.get('name') if entry else '') + '.sha256')
            if not any(str(asset.get('url') or '') == checksum_url
                       or asset.get('kind') == 'checksum' for asset in assets):
                assets.append({
                    'name': checksum_name,
                    'url': checksum_url,
                    'size': None,
                    'sha256': '',
                    'kind': 'checksum',
                })

    # 旧自定义形状 {version, notes, url, size, sha256}：没有资产时补一个。
    if not assets:
        url = payload.get('url') or payload.get('download_url') or ''
        name = _asset_name(payload.get('name'), url)
        if url:
            assets.append({
                'name': name,
                'url': str(url),
                'size': _safe_size(payload.get('size')),
                'sha256': _normalize_sha256(payload.get('sha256') or payload.get('digest')),
                'kind': _asset_kind(name),
            })

    return {'version': version, 'notes': notes, 'published_at': published_at,
            'assets': assets}


def _checksum_url_for(release: dict, installer: dict) -> str:
    """找与安装包同名的 .sha256/.txt 资产；找不到时退回第一个校验资产。"""
    if not isinstance(release, dict) or not isinstance(installer, dict):
        return ''
    name = str(installer.get('name') or '').strip().lower()
    checksums = [asset for asset in (release.get('assets') or [])
                 if isinstance(asset, dict)
                 and str(asset.get('kind') or '') == 'checksum'
                 and str(asset.get('url') or '').strip()]
    if not checksums:
        return ''
    wanted = {name + '.sha256', name + '.txt', name + '.sha256.txt'} if name else set()
    for asset in checksums:
        if str(asset.get('name') or '').strip().lower() in wanted:
            return str(asset.get('url') or '')
    if name:
        stem = name.rsplit('.', 1)[0]
        for asset in checksums:
            candidate = str(asset.get('name') or '').strip().lower()
            if candidate.startswith(stem) and ('sha256' in candidate
                                               or candidate.endswith('.txt')):
                return str(asset.get('url') or '')
    return str(checksums[0].get('url') or '')


def pick_installer(release: dict, expected_version: str) -> dict | None:
    """优先选文件名带目标版本号的 Setup exe，其次任意 Setup exe。"""
    if not isinstance(release, dict):
        return None
    assets = release.get('assets')
    if not isinstance(assets, list):
        return None
    installers = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get('name') or '')
        url = str(asset.get('url') or '')
        if not url:
            continue
        if asset.get('kind') != 'installer' and _asset_kind(name) != 'installer':
            continue
        installers.append(asset)
    wanted = _clean_version(expected_version).lower()
    if wanted:
        for asset in installers:
            if wanted in str(asset.get('name') or '').lower():
                return dict(asset)
    return dict(installers[0]) if installers else None


def _open_response(opener: Any, request: urllib.request.Request, timeout: float):
    target = opener or urllib.request.urlopen
    if hasattr(target, 'open') and not callable(target):
        return target.open(request, timeout=timeout)
    return target(request, timeout=timeout)


def _response_text(response: Any) -> str:
    data = response.read()
    if isinstance(data, bytes):
        charset = 'utf-8'
        headers = getattr(response, 'headers', None)
        if headers is not None:
            content_type = headers.get('Content-Type', '') or ''
            match = re.search(r'charset=([\w.-]+)', str(content_type), re.I)
            if match:
                charset = match.group(1)
        try:
            return data.decode(charset, errors='replace')
        except LookupError:
            return data.decode('utf-8', errors='replace')
    return str(data or '')


def _is_rate_limited(error: Any) -> bool:
    """GitHub 未认证请求按出口 IP 限流，超限时返回 403 或 429。"""
    code = int(getattr(error, 'code', 0) or 0)
    if code == 429:
        return True
    headers = getattr(error, 'headers', None)
    if headers is None:
        return False
    try:
        return str(headers.get('X-RateLimit-Remaining', '')).strip() == '0'
    except Exception:
        return False


def fetch_release(feed_url: str, timeout: float = CHECK_TIMEOUT,
                  opener: Any = None) -> dict:
    """读取发布信息并返回 parse_release 后的结构。

    网络错误按 reason='offline'，404 按 'http_404'，其他 HTTP 状态按
    'http_error'，响应不是有效发布 JSON 按 'bad_payload'。
    """
    url = str(feed_url or '').strip()
    if not url:
        raise UpdateError('offline', '尚未配置更新地址，暂时无法检查更新')
    request = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': GITHUB_ACCEPT,
    })
    response = None
    try:
        response = _open_response(opener, request, max(1.0, float(timeout)))
        text = _response_text(response)
    except urllib.error.HTTPError as error:
        code = int(getattr(error, 'code', 0) or 0)
        if code == 404:
            raise UpdateError('http_404', '更新源返回 404：没有找到发布信息，请检查更新地址') from None
        if _is_rate_limited(error):
            raise UpdateError('rate_limited',
                              '更新源接口的请求次数已用完（GitHub 未登录请求按出口 IP 限流），请稍后再试') from None
        raise UpdateError('http_error', f'更新源返回 HTTP {code or "错误"}，请稍后重试') from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
        raise UpdateError('offline', '无法连接更新源，请检查网络或稍后重试') from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise UpdateError('bad_payload', '更新源返回的内容不是有效 JSON，可能被网络代理拦截') from None
    if not isinstance(payload, dict):
        raise UpdateError('bad_payload', '更新源返回的数据格式不正确') from None
    release = parse_release(payload)
    if not release.get('version') or not parse_version(release.get('version')):
        raise UpdateError('bad_payload', '更新源没有提供有效版本号，已停止本次检查') from None
    # update.json 里的安装包地址可能只是同目录文件名；相对 feed_url 解析成绝对地址。
    for asset in release.get('assets') or []:
        if isinstance(asset, dict) and asset.get('url'):
            asset['url'] = urllib.parse.urljoin(url, str(asset['url']))
    return release


def _check_error(current: str, reason: str, message: str, latest: str = '',
                 notes: str = '') -> dict:
    return {
        'state': 'error', 'current': current, 'latest': latest, 'notes': notes,
        'installer': None, 'error': {'reason': reason, 'message': message},
        'checked_at': _now_iso(),
    }


def _fetch_with_fallback(feed_url: str, timeout: float) -> dict:
    """先按配置的地址取发布信息；只有被限流时才改走 Releases 直链的 update.json。"""
    try:
        return fetch_release(feed_url, timeout=timeout)
    except UpdateError as error:
        # 只有默认的 GitHub API 地址被限流才改走 Releases 直链；自定义更新源由用户自己负责。
        if error.reason != 'rate_limited' or not FALLBACK_FEED_URL:
            raise
        if 'api.github.com' not in str(feed_url or ''):
            raise
        return fetch_release(FALLBACK_FEED_URL, timeout=timeout)


def check(feed_url: str, current_version: str, timeout: float = CHECK_TIMEOUT) -> dict:
    """检查更新；任何失败都折叠成 state='error'，绝不抛异常。"""
    current = _clean_version(current_version)
    if not str(feed_url or '').strip():
        return _check_error(current, 'not_configured',
                            '尚未配置更新源；请在设置中填写 GitHub Releases 地址后再检查')
    try:
        release = _fetch_with_fallback(feed_url, timeout)
        if not isinstance(release, dict):
            raise UpdateError('bad_payload', '更新源返回的数据格式不正确')
        # fetch_release 已解析；再走一次 parse_release 兼容测试或自定义 fetch 返回原始 JSON。
        release = parse_release(release)
        latest = _clean_version(release.get('version'))
        notes = str(release.get('notes') or '')
        if not latest or not parse_version(latest):
            raise UpdateError('bad_payload', '更新源没有提供有效版本号，已停止本次检查')
        installer = pick_installer(release, latest)
        if not is_newer(latest, current):
            return {
                'state': 'up_to_date', 'current': current, 'latest': latest,
                'notes': notes, 'installer': None, 'error': None,
                'checked_at': _now_iso(),
            }
        if installer is None:
            return _check_error(current, 'no_installer',
                                f'发现新版本 {latest}，但发布中没有可用的 Setup 安装包',
                                latest=latest, notes=notes)
        checksum_url = _checksum_url_for(release, installer)
        if checksum_url:
            installer = dict(installer)
            installer['checksum_url'] = checksum_url
        return {
            'state': 'update_available', 'current': current, 'latest': latest,
            'notes': notes, 'installer': installer, 'error': None,
            'checked_at': _now_iso(),
        }
    except UpdateError as error:
        return _check_error(current, error.reason, error.message)
    except Exception:
        return _check_error(current, 'internal', '检查更新时发生内部错误，请稍后重试')


def _size_from_response(response: Any, expected_size: int | None) -> int | None:
    if expected_size is not None:
        return expected_size
    headers = getattr(response, 'headers', None)
    value = None
    if headers is not None:
        try:
            value = headers.get('Content-Length')
        except Exception:
            value = None
    return _safe_size(value)


def _notify_progress(progress: Callable[[int, int], Any] | None,
                     received: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(int(received), int(total))
    except Exception:
        pass


def _download_failure(reason: str, message: str, part: Path | None = None,
                      dest: Path | None = None, remove_dest: bool = False) -> dict:
    if part is not None:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
    if remove_dest and dest is not None:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
    return {'ok': False, 'reason': reason, 'message': message}


def download(url: str, dest_path: str | Path, expected_size: int | None = None,
             expected_sha256: str | None = None,
             progress: Callable[[int, int], Any] | None = None,
             timeout: float = DOWNLOAD_TIMEOUT, opener: Any = None,
             chunk: int = 262144) -> dict:
    """下载到 .part，边下边算 SHA256，全部通过后原子替换目标文件。"""
    target = Path(dest_path)
    part = target.with_name(target.name + '.part')
    expected_digest = _normalize_sha256(expected_sha256)
    expected = _safe_size(expected_size)
    if expected is not None and expected > MAX_UPDATE_BYTES:
        return _download_failure(
            'too_large', f'安装包大小 {expected} 字节，超过允许的 {MAX_UPDATE_BYTES} 字节上限')
    if not str(url or '').strip():
        return _download_failure('invalid_url', '下载地址为空，请重新检查更新')
    try:
        chunk_size = max(1024, int(chunk))
    except (TypeError, ValueError):
        chunk_size = 262144
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _download_failure('write_failed', '无法创建更新缓存目录，请检查磁盘权限')

    digest = hashlib.sha256()
    received = 0
    total = expected or 0
    response = None
    try:
        request = urllib.request.Request(str(url), headers={'User-Agent': USER_AGENT})
        response = _open_response(opener, request, max(1.0, float(timeout)))
        total = _size_from_response(response, expected) or 0
        if total > MAX_UPDATE_BYTES:
            return _download_failure(
                'too_large', f'安装包大小 {total} 字节，超过允许的 {MAX_UPDATE_BYTES} 字节上限')
        with open(part, 'wb') as handle:
            while True:
                block = response.read(chunk_size)
                if not block:
                    break
                if isinstance(block, str):
                    block = block.encode('utf-8')
                received += len(block)
                if received > MAX_UPDATE_BYTES:
                    return _download_failure(
                        'too_large',
                        f'安装包超过允许的 {MAX_UPDATE_BYTES} 字节上限，已停止下载',
                        part=part, dest=target, remove_dest=True)
                digest.update(block)
                handle.write(block)
                _notify_progress(progress, received, total)
            handle.flush()
            os.fsync(handle.fileno())
        if expected is not None and received != expected:
            return _download_failure(
                'size_mismatch',
                f'下载大小不符：预期 {expected} 字节，实际 {received} 字节',
                part=part, dest=target, remove_dest=True)
        if total and received != total:
            return _download_failure(
                'size_mismatch',
                f'下载大小不符：服务器声明 {total} 字节，实际 {received} 字节',
                part=part, dest=target, remove_dest=True)
        actual_digest = digest.hexdigest()
        if expected_digest and actual_digest != expected_digest:
            return _download_failure(
                'checksum_mismatch', 'SHA256 校验失败，安装包可能损坏或被篡改',
                part=part, dest=target, remove_dest=True)
        os.replace(part, target)
        _notify_progress(progress, received, received)
        return {'ok': True, 'path': str(target), 'size': received,
                'sha256': actual_digest}
    except urllib.error.HTTPError as error:
        code = int(getattr(error, 'code', 0) or 0)
        reason = 'http_404' if code == 404 else 'http_error'
        return _download_failure(reason, f'下载服务器返回 HTTP {code or "错误"}，请重试', part=part)
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError,
            OSError) as error:
        # OSError 也包括磁盘写入失败；对用户统一提示可重试，不暴露堆栈。
        reason = 'interrupted' if not isinstance(error, PermissionError) else 'write_failed'
        message = ('下载中断，请检查网络后重试' if reason == 'interrupted'
                   else '写入更新缓存失败，请检查磁盘空间和权限')
        return _download_failure(reason, message, part=part,
                                 dest=target, remove_dest=(reason == 'interrupted'))
    except Exception:
        return _download_failure('internal', '下载时发生内部错误，请重试', part=part)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def verify_sha256(path: str | Path, expected_hex: str) -> bool:
    """流式计算文件 SHA256；文件不存在或摘要非法返回 False。"""
    expected = _normalize_sha256(expected_hex)
    if not expected:
        return False
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except (OSError, ValueError):
        return False
    return digest.hexdigest() == expected


def fetch_checksum(url: str, timeout: float = CHECK_TIMEOUT,
                   opener: Any = None) -> dict:
    """读取发布目录里的 .sha256 校验文本，返回 {ok, sha256} 或带原因的失败。"""
    target = str(url or '').strip()
    if not target:
        return {'ok': False, 'reason': 'invalid_url', 'message': '校验文件地址为空'}
    request = urllib.request.Request(target, headers={'User-Agent': USER_AGENT})
    response = None
    try:
        response = _open_response(opener, request, max(1.0, float(timeout)))
        text = _response_text(response)
    except urllib.error.HTTPError as error:
        code = int(getattr(error, 'code', 0) or 0)
        reason = 'http_404' if code == 404 else 'http_error'
        return {'ok': False, 'reason': reason,
                'message': f'读取校验文件失败：服务器返回 HTTP {code or "错误"}'}
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
        return {'ok': False, 'reason': 'offline',
                'message': '无法下载 SHA256 校验文件，请检查网络后重试'}
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
    digest = read_checksum_text(text)
    if not digest:
        return {'ok': False, 'reason': 'bad_checksum',
                'message': '校验文件内容不是有效的 SHA256，已停止下载'}
    return {'ok': True, 'sha256': digest}


def read_checksum_text(text: str) -> str:
    """读取 sha256 文本：支持 'hex  filename'、'hex *filename' 与纯 hex。"""
    if not isinstance(text, str):
        return ''
    for line in text.splitlines():
        value = line.strip().lstrip('\ufeff')
        if not value:
            continue
        if value.lower().startswith('sha256'):
            value = re.sub(r'^sha256\s*[:=]\s*', '', value, flags=re.I)
        match = _SHA256_RE.search(value)
        if match:
            return match.group(1).lower()
    return ''


def launch_installer(path: str | Path,
                     args: Iterable[str] = ('/SILENT', '/CLOSEAPPLICATIONS', '/NORESTART'),
                     wait: bool = False) -> dict:
    """以脱离控制台的方式启动 Inno Setup 安装包；调用方随后退出旧版本。"""
    installer = Path(path)
    if not installer.is_file():
        return {'ok': False, 'reason': 'missing', 'message': '安装包不存在，请重新下载'}
    try:
        flags = 0
        if os.name == 'nt':
            flags = (getattr(subprocess, 'DETACHED_PROCESS', 0)
                     | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
            startup = None
        else:
            startup = None
        process = subprocess.Popen(
            [str(installer), *[str(item) for item in args]],
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=(os.name != 'nt'),
        )
        result = {'ok': True, 'pid': int(process.pid)}
        if wait:
            result['returncode'] = process.wait()
        return result
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'ok': False, 'reason': 'launch_failed',
                'message': '无法启动安装程序，请确认安装包完整并手动运行'}
