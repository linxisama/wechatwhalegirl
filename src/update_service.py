"""自动更新服务：把检查、下载、校验、安装串成一个线程安全的状态机。

界面只读 status()，所有耗时动作都在单一后台线程里跑；任何失败都落成
status['error']（reason + 中文 message），不会把异常抛给 FastAPI 或 Qt。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import updater
from .updater import UpdateError, fetch_checksum, launch_installer, verify_sha256

try:  # 项目内已有原子写入；独立脚本运行时退化为本模块自带的临时文件写法。
    from .persistence import atomic_text as _atomic_text
except ImportError:  # pragma: no cover - 仅在脱离包结构导入时走到
    try:
        from persistence import atomic_text as _atomic_text
    except ImportError:
        _atomic_text = None


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'on', '是'):
        return True
    if text in ('0', 'false', 'no', 'off', '否'):
        return False
    return default


def _version_key(value: Any) -> tuple[int, ...]:
    return updater.parse_version(str(value or ''))


def _release_from_fetch(raw: Any) -> dict:
    """兼容测试/调用方直接返回 updater.check 结果或原始 release JSON。"""
    if isinstance(raw, dict) and raw.get('state') in (
            'up_to_date', 'update_available', 'error'):
        if raw.get('state') == 'error':
            error = raw.get('error') or {}
            raise UpdateError(str(error.get('reason') or 'bad_payload'),
                              str(error.get('message') or '更新源返回错误'))
        installer = raw.get('installer')
        assets = [dict(installer)] if isinstance(installer, dict) else []
        return {'version': str(raw.get('latest') or ''),
                'notes': str(raw.get('notes') or ''),
                'published_at': str(raw.get('checked_at') or ''),
                'assets': assets}
    return updater.parse_release(raw)


class UpdateService:
    """单实例更新服务；构造时只读本地状态文件，不访问网络。"""

    # 自动下载前的短暂停留：让 available 在界面上可见，用户也来得及点“跳过”。
    AUTO_DOWNLOAD_DELAY = 0.5

    def __init__(self, data_dir: Path, current_version: str,
                 feed_url: str = '', auto_check: bool = True,
                 auto_download: bool = True,
                 interval_hours: float = 24, clock: Callable[[], float] = time.time,
                 fetch: Callable[..., dict] = updater.fetch_release,
                 download: Callable[..., dict] = updater.download):
        self.data_dir = Path(data_dir)
        self.current_version = str(current_version or '')
        self.feed_url = str(feed_url or '').strip()
        self.auto_check = bool(auto_check)
        self.auto_download = bool(auto_download)
        try:
            self.interval_hours = max(0.0, float(interval_hours))
        except (TypeError, ValueError):
            self.interval_hours = 24.0
        self.clock = clock
        self.fetch = fetch
        self._download_impl = download

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._job = ''
        self._state = 'idle'
        self._latest = ''
        self._notes = ''
        self._error: dict | None = None
        self._progress = {'received': 0, 'total': 0, 'percent': 0}
        self._last_check_at: float | None = None
        self._next_check_at: float | None = None
        self._skipped_version = ''
        # 自动下载失败过的版本号：同一版本不再自动重试，用户手动点下载仍可重试。
        self._auto_download_failed = ''
        # 本次下载是否由自动下载触发（用于在下载线程里回填失败版本号）。
        self._auto_download_for = ''
        self._last_result: dict | None = None
        self._installer: dict | None = None
        self._downloaded_path = ''
        self._downloaded_sha = ''
        self._installing = False
        self._installer_pid = None
        self._storage_error = ''
        self.load_state()

    # ── 状态读取 ─────────────────────────────────────────────────────
    def status(self) -> dict:
        """返回界面可直接渲染的快照；本方法绝不抛异常。"""
        try:
            with self._lock:
                progress = {
                    'received': int(self._progress.get('received') or 0),
                    'total': int(self._progress.get('total') or 0),
                    'percent': int(self._progress.get('percent') or 0),
                }
                skipped = bool(self._skipped_version
                               and self._latest
                               and self._skipped_version == self._latest)
                next_check = self._next_check_at
                if next_check is None and self.auto_check and self._last_check_at is not None:
                    next_check = self._last_check_at + self.interval_hours * 3600
                return {
                    'state': self._state,
                    'current': self.current_version,
                    'latest': self._latest,
                    'notes': self._notes,
                    'progress': progress,
                    'error': dict(self._error) if self._error else None,
                    'last_check_at': self._last_check_at,
                    'next_check_at': next_check,
                    'auto': self.auto_check,
                    'auto_download': bool(self.auto_download),
                    'feed_url': self.feed_url,
                    'downloaded_path': self._downloaded_path,
                    'installing': self._installing,
                    'skipped': skipped,
                    'skipped_version': self._skipped_version,
                    'installer': dict(self._installer) if self._installer else None,
                    'installer_pid': self._installer_pid,
                    'storage_error': self._storage_error,
                }
        except Exception:
            return {
                'state': 'error', 'current': self.current_version, 'latest': '',
                'notes': '', 'progress': {'received': 0, 'total': 0, 'percent': 0},
                'error': {'reason': 'internal', 'message': '读取更新状态失败，请重启控制台'},
                'last_check_at': None, 'next_check_at': None,
                'auto': bool(self.auto_check),
                'auto_download': bool(self.auto_download), 'feed_url': self.feed_url,
                'downloaded_path': '', 'installing': False, 'skipped': False,
                'skipped_version': '', 'installer': None, 'installer_pid': None,
                'storage_error': '',
            }

    def _now(self) -> float:
        try:
            return float(self.clock())
        except Exception:
            return time.time()

    def _reject(self, reason: str, message: str) -> dict:
        result = self.status()
        result.update({'ok': False, 'reason': reason, 'message': message})
        return result

    def _set_error(self, reason: str, message: str) -> None:
        self._state = 'error'
        self._error = {'reason': str(reason), 'message': str(message)}
        self._progress = dict(self._progress or {'received': 0, 'total': 0, 'percent': 0})

    def _error_status(self) -> dict:
        result = self.status()
        result['ok'] = False
        return result

    def _record_check(self, now: float) -> None:
        self._last_check_at = now
        self._next_check_at = (now + self.interval_hours * 3600
                               if self.auto_check else None)

    # ── 检查更新 ─────────────────────────────────────────────────────
    def check(self, force: bool = False) -> dict:
        """启动一次后台检查并立即返回快照；同一时间只允许一个任务。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            if self._state in ('downloading', 'downloaded', 'installing'):
                return self._reject('busy', '当前已有更新任务在进行，请稍后再检查')
            if not self.feed_url:
                now = self._now()
                self._state = 'error'
                self._error = {'reason': 'not_configured',
                               'message': '尚未配置更新源；请在设置中填写更新地址后再检查'}
                self._record_check(now)
                self._last_result = {'state': 'error', 'reason': 'not_configured',
                                     'checked_at': now}
                return self.status()
            now = self._now()
            if not force and self._next_check_at is not None and now < self._next_check_at:
                return self.status()
            self._state = 'checking'
            self._error = None
            self._progress = {'received': 0, 'total': 0, 'percent': 0}
            self._job = 'check'
            thread = threading.Thread(target=self._check_worker,
                                      name='wechat-update-check', daemon=True)
            self._thread = thread
            thread.start()
            return self.status()

    def _fetch_release(self) -> dict:
        try:
            raw = self.fetch(self.feed_url, timeout=updater.CHECK_TIMEOUT)
        except TypeError:
            # 兼容只接受一个位置参数的注入替身。
            raw = self.fetch(self.feed_url)
        return _release_from_fetch(raw)

    def _check_worker(self) -> None:
        discovered = ''
        try:
            release = self._fetch_release()
            latest = str(release.get('version') or '').strip()
            if not _version_key(latest):
                raise UpdateError('bad_payload', '更新源没有提供有效版本号，已停止本次检查')
            notes = str(release.get('notes') or '')
            installer = updater.pick_installer(release, latest)
            if installer is not None:
                checksum_url = updater._checksum_url_for(release, installer)
                if checksum_url:
                    installer['checksum_url'] = checksum_url
            now = self._now()
            with self._lock:
                self._latest = latest
                self._notes = notes
                self._record_check(now)
                self._progress = {'received': 0, 'total': 0, 'percent': 0}
                if self._skipped_version and self._skipped_version == latest:
                    self._state = 'up_to_date'
                    self._error = None
                    self._installer = None
                elif not updater.is_newer(latest, self.current_version):
                    self._state = 'up_to_date'
                    self._error = None
                    self._installer = None
                elif installer is None:
                    self._state = 'error'
                    self._installer = None
                    self._error = {
                        'reason': 'no_installer',
                        'message': f'发现新版本 {latest}，但发布中没有可用的 Setup 安装包',
                    }
                else:
                    self._state = 'available'
                    self._error = None
                    self._installer = dict(installer)
                    discovered = latest
                self._last_result = self._status_result()
        except UpdateError as error:
            with self._lock:
                now = self._now()
                self._record_check(now)
                self._installer = None
                self._set_error(error.reason, error.message)
                self._last_result = self._status_result()
        except Exception:
            with self._lock:
                now = self._now()
                self._record_check(now)
                self._installer = None
                self._set_error('internal', '检查更新时发生内部错误，请稍后重试')
                self._last_result = self._status_result()
        finally:
            with self._lock:
                self._thread = None
                self._job = ''
            # 最后检查时间/结果要跨重启保留；写失败只记录 storage_error，不影响本次结果。
            self.save_state()
        # 必须等 _thread 归零之后再启动下载：download() 会拒绝“已有线程存活”的调用。
        self._maybe_auto_download(discovered)

    def _maybe_auto_download(self, version: str) -> None:
        """新发现的新版本 + 开了自动下载 + 该版本没自动下载失败过 → 后台下载。

        只下载、只校验，绝不自动安装：安装前必须由用户确认。
        """
        if not version:
            return
        with self._lock:
            if not self.auto_download:
                return
            if self._state != 'available' or not self._installer:
                return
            if version == self._auto_download_failed:
                # 同一版本自动下载失败过就不再自动重试，避免“检查→失败”死循环。
                return
        # 另起线程并稍等片刻再下载：available 能在界面上露一下，用户也来得及点“跳过”。
        # 注意不能占用 self._thread，否则这段等待会把界面上的手动下载挡成 busy。
        thread = threading.Thread(target=self._auto_download_worker, args=(version,),
                                  name='wechat-update-auto-download', daemon=True)
        thread.start()

    def _auto_download_worker(self, version: str) -> None:
        """自动下载：任何失败都折叠成 error 状态，绝不外抛、不留 downloading。"""
        time.sleep(self.AUTO_DOWNLOAD_DELAY)
        with self._lock:
            # 等待期间被跳过、被手动下载、被关掉开关或被新一次检查覆盖，就放弃本次自动下载。
            if (self._state != 'available' or not self._installer
                    or self._latest != version or not self.auto_download):
                return
            if version == self._auto_download_failed:
                return
            # 标记本次下载由自动下载触发；失败时由 _download_worker 记下版本号。
            self._auto_download_for = version
        try:
            result = self.download()
        except Exception:
            # 自动下载绝不能把异常抛给调用方，也不能让状态卡在 downloading。
            with self._lock:
                self._auto_download_for = ''
                self._auto_download_failed = version
                if self._state in ('available', 'downloading'):
                    self._state = 'error'
                    self._error = {'reason': 'internal',
                                   'message': '自动下载更新包时发生内部错误，请手动重试'}
            return
        with self._lock:
            if not isinstance(result, dict) or result.get('ok') is False:
                # download() 被拒绝（busy / not_available）：本次不再重试，状态保持原样。
                self._auto_download_for = ''

    def _status_result(self) -> dict:
        return {
            'state': self._state,
            'current': self.current_version,
            'latest': self._latest,
            'notes': self._notes,
            'error': dict(self._error) if self._error else None,
            'skipped': bool(self._skipped_version and self._skipped_version == self._latest),
            'auto_download': bool(self.auto_download),
        }

    # ── 下载 ─────────────────────────────────────────────────────────
    def _download_dest(self, installer: dict) -> Path:
        raw_name = str(installer.get('name') or '').replace('\\', '/').rsplit('/', 1)[-1]
        raw_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw_name).strip(' .')
        if not raw_name:
            raw_name = f'WhaleGirlWeChat-Setup-{self._latest}.exe'
        return self.data_dir / 'downloads' / raw_name

    def download(self) -> dict:
        """下载当前 available 的安装包；进度通过 status()['progress'] 暴露。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._reject('busy', '当前已有更新任务在进行，请稍候')
            # 下载失败后保留 _installer，允许用户直接点“重试”而不用重新检查。
            if self._state not in ('available', 'error') or not self._installer:
                return self._reject('not_available', '当前没有可下载的更新，请先检查更新')
            installer = dict(self._installer)
            self._state = 'downloading'
            self._error = None
            self._progress = {
                'received': 0,
                'total': int(installer.get('size') or 0),
                'percent': 0,
            }
            self._downloaded_path = ''
            self._downloaded_sha = str(installer.get('sha256') or '')
            self._job = 'download'
            thread = threading.Thread(target=self._download_worker, args=(installer,),
                                      name='wechat-update-download', daemon=True)
            self._thread = thread
            thread.start()
            return self.status()

    def _on_progress(self, received: int, total: int) -> None:
        try:
            received = max(0, int(received))
            total = max(0, int(total))
        except (TypeError, ValueError):
            return
        with self._lock:
            if total <= 0:
                percent = 0
            else:
                percent = max(0, min(100, int(received * 100 / total)))
            self._progress = {'received': received, 'total': total, 'percent': percent}

    def _download_worker(self, installer: dict) -> None:
        dest = self._download_dest(installer)
        work = dict(installer)
        expected = str(work.get('sha256') or '')
        result = None
        # GitHub 资产可能没有 digest，只有同名的 .sha256 文本；下载前先取回摘要。
        if not expected and str(work.get('checksum_url') or '').strip():
            fetched = fetch_checksum(str(work.get('checksum_url')))
            if isinstance(fetched, dict) and fetched.get('ok'):
                expected = str(fetched.get('sha256') or '')
                if expected:
                    work['sha256'] = expected
            if not expected:
                payload = fetched if isinstance(fetched, dict) else {}
                result = {
                    'ok': False,
                    'reason': str(payload.get('reason') or 'bad_checksum'),
                    'message': str(payload.get('message')
                                   or '无法获取 SHA256 校验文件，已停止下载'),
                }
        if result is None:
            try:
                result = self._download_impl(
                    str(work.get('url') or ''),
                    str(dest),
                    expected_size=work.get('size'),
                    expected_sha256=expected or None,
                    progress=self._on_progress,
                )
            except Exception:
                result = {'ok': False, 'reason': 'internal',
                          'message': '下载时发生内部错误，请重试'}
        with self._lock:
            if isinstance(result, dict) and result.get('ok'):
                self._state = 'downloaded'
                self._downloaded_path = str(result.get('path') or dest)
                self._downloaded_sha = str(result.get('sha256') or expected or '')
                self._installer = work
                self._error = None
                size = int(result.get('size') or 0)
                self._progress = {'received': size, 'total': size, 'percent': 100}
                self._last_result = self._status_result()
            else:
                self._state = 'error'
                payload = result if isinstance(result, dict) else {}
                self._error = {
                    'reason': str(payload.get('reason') or 'internal'),
                    'message': str(payload.get('message') or '下载失败，请重试'),
                }
                if self._auto_download_for:
                    # 自动下载失败：记下版本号，同一版本不再自动重试（手动点下载仍可重试）。
                    self._auto_download_failed = self._auto_download_for
                self._last_result = self._status_result()
            self._auto_download_for = ''
            self._thread = None
            self._job = ''
        self.save_state()

    # ── 校验与安装 ───────────────────────────────────────────────────
    def install(self) -> dict:
        """先复核 SHA256，再启动安装程序；校验失败绝不安装。"""
        with self._lock:
            # 启动安装程序失败时保留已校验的下载文件，允许直接重试安装。
            if self._state not in ('downloaded', 'error') or not self._downloaded_path:
                return self._reject('not_downloaded', '更新包尚未下载完成，不能安装')
            if self._thread is not None and self._thread.is_alive():
                return self._reject('busy', '当前仍有下载任务，请稍候')
            path = Path(self._downloaded_path)
            expected = str((self._installer or {}).get('sha256') or '').strip().lower()
            if not expected:
                self._set_error('checksum_missing',
                                '发布信息没有提供 SHA256，无法确认安装包完整性，已拒绝安装')
                return self._error_status()
            if not path.is_file():
                self._set_error('missing', '安装包文件不存在，请重新下载')
                return self._error_status()
            if not verify_sha256(path, expected):
                self._set_error('checksum_mismatch',
                                '安装包 SHA256 复核失败，已拒绝安装；请重新下载')
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                self._downloaded_path = ''
                return self._error_status()
            try:
                launched = launch_installer(str(path))
            except Exception:
                launched = {'ok': False, 'reason': 'launch_failed',
                            'message': '无法启动安装程序，请手动运行下载好的安装包'}
            if not isinstance(launched, dict) or not launched.get('ok'):
                payload = launched if isinstance(launched, dict) else {}
                self._set_error(str(payload.get('reason') or 'launch_failed'),
                                str(payload.get('message') or '无法启动安装程序，请重试'))
                return self._error_status()
            self._state = 'installing'
            self._installing = True
            self._error = None
            self._installer_pid = launched.get('pid')
            self._last_result = self._status_result()
            result = self.status()
            result['ok'] = True
            return result

    # ── 跳过版本 ─────────────────────────────────────────────────────
    def skip(self, version: str) -> dict:
        value = str(version or '').strip()
        if not value or not _version_key(value):
            return self._reject('bad_request', '要跳过的版本号无效')
        with self._lock:
            self._skipped_version = value[1:] if value[:1].lower() == 'v' else value
            if self._latest and self._skipped_version == self._latest:
                self._state = 'up_to_date'
                self._error = None
            self.save_state()
            return self.status()

    def unskip(self) -> dict:
        with self._lock:
            self._skipped_version = ''
            self.save_state()
            return self.status()

    # ── 配置 ─────────────────────────────────────────────────────────
    def configure(self, feed_url: str | None = None,
                  auto_check: bool | None = None,
                  interval_hours: float | None = None,
                  auto_download: bool | None = None) -> dict:
        with self._lock:
            old_feed = self.feed_url
            if feed_url is not None:
                self.feed_url = str(feed_url or '').strip()
            if auto_check is not None:
                self.auto_check = _bool_value(auto_check, self.auto_check)
            if auto_download is not None:
                self.auto_download = _bool_value(auto_download, self.auto_download)
                # 重新打开开关时忘掉上次失败，否则用户改完设置也不会再自动下载。
                if self.auto_download:
                    self._auto_download_failed = ''
            if interval_hours is not None:
                try:
                    self.interval_hours = max(0.0, float(interval_hours))
                except (TypeError, ValueError):
                    return self._reject('bad_request', '检查间隔必须是数字（小时）')
            if self._last_check_at is not None:
                self._next_check_at = (self._last_check_at + self.interval_hours * 3600
                                       if self.auto_check else None)
            if feed_url is not None and self.feed_url != old_feed and self._state not in (
                    'downloading', 'downloaded', 'installing'):
                self._state = 'idle'
                self._latest = ''
                self._notes = ''
                self._error = None
                self._installer = None
            return self.status()

    def maybe_auto_check(self) -> bool:
        """供 UI 轮询时调用：到期且空闲才真正起线程，避免启动阻塞。"""
        with self._lock:
            if not self.auto_check or not self.feed_url:
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            now = self._now()
            if self._next_check_at is not None and now < self._next_check_at:
                return False
        self.check(force=False)
        return True

    # ── 持久化 ───────────────────────────────────────────────────────
    @property
    def state_path(self) -> Path:
        return self.data_dir / 'update_state.json'

    def save_state(self) -> bool:
        with self._lock:
            payload = {
                'skipped_version': self._skipped_version,
                'last_check_at': self._last_check_at,
                'last_result': self._last_result,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                path = self.state_path
                if _atomic_text is not None:
                    _atomic_text(path, text)
                else:
                    temp = path.with_name(path.name + '.tmp')
                    temp.write_text(text, encoding='utf-8', newline='\n')
                    temp.replace(path)
                self._storage_error = ''
                return True
            except OSError as error:
                self._storage_error = f'更新状态写入失败：{error}'
                return False

    def load_state(self) -> None:
        with self._lock:
            path = self.state_path
            try:
                if not path.is_file():
                    return
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError, TypeError):
                return
            if not isinstance(data, dict):
                return
            skipped = data.get('skipped_version')
            if isinstance(skipped, str) and _version_key(skipped):
                self._skipped_version = skipped
            last = data.get('last_check_at')
            if isinstance(last, (int, float)) and not isinstance(last, bool):
                self._last_check_at = float(last)
                if self.auto_check:
                    self._next_check_at = self._last_check_at + self.interval_hours * 3600
            result = data.get('last_result')
            if isinstance(result, dict):
                self._last_result = result
