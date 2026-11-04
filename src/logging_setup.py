"""统一日志内核：一份 root 配置 + 全局未捕获异常落盘 + 运行记录用的错误块提取。

冻结成安装包后程序是 GUI 子系统，stdout/stderr 可能整个不存在；日志这条路
自己必须全程兜底——写不进去也只当没写，绝不能让工作循环跟着崩
（同 wechat_gui.py 里"日志功能不能因此中断工作循环"的约定）。
"""
import logging
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 与 1.3.9 及更早版本 RuntimeLog 的格式保持一致：用户/开发者手里的旧日志能直接拼着看
LOG_FORMAT = "%(asctime)s [%(threadName)s] %(levelname)s %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOG_FILE_NAME = "backend.log"
RUNTIME_LOGGER_NAME = "wechat.runtime"
# 这些库每个请求都打 INFO，留着会把 backend.log 冲满，用户看不到真正的报错
NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "requests", "asyncio")
_NOISY_LEVEL = logging.WARNING
_HANDLER_TAG = "_wechat_backend_log"
DEFAULT_TAIL_BYTES = 200 * 1024
# 正常轮换文件上限为 5MB；指纹复核留一倍余量，不扫描任意大的外部日志。
_CONTENT_FINGERPRINT_LIMIT = 10 * 1024 * 1024
_CONTENT_FINGERPRINT_CHUNK = 64 * 1024

_ERROR_HEADER = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d{3}\s+\[[^\]]*\]\s+(ERROR|CRITICAL)\b")
_LOG_HEADER = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d{3}\s+\[[^\]]*\]\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
_TRACEBACK_HEAD = "Traceback (most recent call last)"
# 异常链的过渡行不缩进，但它仍属于同一个错误块
_CHAIN_HEADS = ("During handling of the above exception",
                "The above exception was the direct cause")

_hooks_installed = False
_hook_lock = threading.Lock()


def log_folder(root=None) -> Path:
    """默认日志目录：调用方给 root 就用 root/logs，否则按源码目录推。"""
    if root is not None:
        return Path(root) / "logs"
    return Path(__file__).resolve().parent.parent / "logs"


def _key(path) -> str:
    """handler 归属判据：只用绝对化+大小写归一，不做磁盘探测（目录可能还不存在）。"""
    return os.path.normcase(os.path.abspath(str(path)))


def _resolve_level(level=None) -> int:
    if level is not None:
        return int(level)
    name = (os.environ.get("WECHAT_LOG_LEVEL") or "").strip().upper()
    value = getattr(logging, name, None) if name else None
    return value if isinstance(value, int) else logging.INFO


def _safe_target(folder: Path) -> Path:
    """日志目录不可写时退回临时目录：安装目录只读不能连累启动。"""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        return folder / LOG_FILE_NAME
    except OSError:
        pass
    fallback = Path(tempfile.gettempdir()) / "WhaleGirlWeChat-logs"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError:
        return folder / LOG_FILE_NAME
    return fallback / LOG_FILE_NAME


def _close_quietly(handler) -> None:
    try:
        handler.close()
    except Exception:
        pass


class _CloseAfterEmitHandler(RotatingFileHandler):
    """每写完一条就关文件：Windows 上不长期占用 backend.log。

    1.3.9 的 RuntimeLog 也是每次写完即关，用户（和测试）才能在程序运行中
    直接删除/复制日志；长期持有句柄会让删除报 WinError 32。
    handler 的 lock 由 Handler.handle 持有，这里关流是安全的。
    """

    def emit(self, record):
        super().emit(record)
        try:
            if self.stream is not None:
                self.stream.close()
        finally:
            self.stream = None


def _make_handler(target: Path):
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # delay=True：进程没写日志就不占用文件，用户在资源管理器里能直接删/复制日志
        handler = _CloseAfterEmitHandler(target, maxBytes=LOG_MAX_BYTES,
                                         backupCount=LOG_BACKUP_COUNT, encoding="utf-8", delay=True)
    except OSError:
        return None
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    setattr(handler, _HANDLER_TAG, _key(target))
    return handler


def _tagged_handlers(root, target: Path):
    want = _key(target)
    return [h for h in root.handlers
            if isinstance(h, RotatingFileHandler) and getattr(h, _HANDLER_TAG, None) == want]


# setup_logging 记住的目标与级别：第三方库会把 root.handlers 清空（见 restore_backend_handler）
_LAST_TARGET = None
_LAST_LEVEL = None


def restore_backend_handler() -> bool:
    """把被第三方库摘掉的 backend.log handler 原样挂回去。

    为什么需要：wechatauto/logger.py:56 直接 root_logger.handlers.clear() 再塞自己的
    控制台 handler，且把 root 降到 DEBUG。发送子进程和最外层进程导入驱动之后就再也
    写不进 backend.log——运行记录因此"什么都没有"。这里只用 setup_logging 记下的目标
    与级别把 handler 重挂回去，不动别人的 handler。
    """
    target = _LAST_TARGET
    if target is None or backend_log_path() is not None:
        return False
    root = logging.getLogger()
    handler = _make_handler(Path(target))
    if handler is None:
        return False
    root.addHandler(handler)
    if _LAST_LEVEL is not None:
        root.setLevel(_LAST_LEVEL)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(_NOISY_LEVEL)
    return True


def setup_logging(folder=None, level=None) -> Path:
    """配置 root logger 写 backend.log，返回日志文件路径。

    幂等：同一目标文件只保留一个 RotatingFileHandler。GUI 与后台真的重复调用时
    （比如后台热重启）不会变成"同一条日志写两份"。
    """
    global _LAST_TARGET, _LAST_LEVEL
    folder = Path(folder) if folder is not None else log_folder()
    target = _safe_target(folder)
    root = logging.getLogger()
    root.setLevel(_resolve_level(level))
    _LAST_TARGET, _LAST_LEVEL = target, root.level
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(_NOISY_LEVEL)

    kept = _tagged_handlers(root, target)
    if kept:
        for extra in kept[1:]:
            root.removeHandler(extra)
            _close_quietly(extra)
    else:
        handler = _make_handler(target)
        if handler is not None:
            root.addHandler(handler)

    # 换了日志目录：旧 handler 留着会把同一条日志写进两个文件
    for old in list(root.handlers):
        if not isinstance(old, RotatingFileHandler):
            continue
        owner = getattr(old, _HANDLER_TAG, None)
        if owner is not None and owner != _key(target):
            root.removeHandler(old)
            _close_quietly(old)
    return target


def is_backend_log_configured(folder=None) -> bool:
    """root 上是否已经有本目录的 backend.log handler（给 RuntimeLog 决定走哪条路）。"""
    folder = Path(folder) if folder is not None else log_folder()
    return bool(_tagged_handlers(logging.getLogger(), folder / LOG_FILE_NAME))


def _split_exception(exc):
    if exc is None:
        exc_type, exc_value, tb = sys.exc_info()
        return (exc_type, exc_value, tb) if exc_value is not None else (None, None, None)
    if isinstance(exc, BaseException):
        return type(exc), exc, exc.__traceback__
    try:
        exc_type, exc_value, tb = exc
    except (TypeError, ValueError):
        return None, None, None
    return exc_type, exc_value, tb


def record_exception(exc=None, *, message: str = "") -> str:
    """把异常的完整 traceback 写进日志并返回同一段文本；自身绝不抛异常。

    既供 sys.excepthook / threading.excepthook 使用，也是 Qt 槽函数里的兜底入口：
    Qt 会吞掉槽里的异常，出问题的地方自己 try/except 后调这里，堆栈才落得下来。
    """
    try:
        restore_backend_handler()
        exc_type, exc_value, tb = _split_exception(exc)
        logger = logging.getLogger(RUNTIME_LOGGER_NAME)
        if exc_value is None:
            if message:
                logger.error("%s（没有可用的异常对象）", message)
            return ""
        text = "".join(traceback.format_exception(exc_type, exc_value, tb))
        logger.critical("%s\n%s", message or "未捕获异常", text.rstrip())
        return text
    except BaseException:
        # 钩子里再抛异常就是二次崩溃，宁可丢掉这条日志
        return ""


def install_excepthook() -> bool:
    """装 sys/threading 的全局兜底钩子（幂等）；返回本次是否真的新装。"""
    global _hooks_installed
    with _hook_lock:
        if _hooks_installed:
            return False
        previous_main = sys.excepthook
        previous_thread = getattr(threading, "excepthook", None)
        default_thread = getattr(threading, "__excepthook__", None)

        def handle_main(exc_type, exc_value, tb):
            record_exception((exc_type, exc_value, tb), message="未捕获异常（主线程）")
            # 接着调启动器自己的钩子：它把同一份堆栈写进 startup.log，两处都能查
            if previous_main is not sys.__excepthook__ and previous_main is not handle_main:
                try:
                    previous_main(exc_type, exc_value, tb)
                except BaseException:
                    pass

        def handle_thread(args):
            name = getattr(getattr(args, "thread", None), "name", "?")
            record_exception((args.exc_type, args.exc_value, args.exc_traceback),
                             message=f"未捕获异常（线程 {name}）")
            if (previous_thread is not None and previous_thread is not handle_thread
                    and previous_thread is not default_thread):
                try:
                    previous_thread(args)
                except BaseException:
                    pass

        sys.excepthook = handle_main
        threading.excepthook = handle_thread
        _hooks_installed = True
        return True


def extract_error_blocks(text: str) -> list:
    """从日志文本里挑出 ERROR/CRITICAL 记录块（含其后的 traceback 行）。

    块边界看行首特征：统一的记录头开新块，缩进行/裸 Traceback 头/异常链过渡行续块，
    不缩进的异常类型行（如 "ValueError: boom"）是本块收尾。
    """
    blocks: list = []
    current: list = []
    in_traceback = False
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        if _ERROR_HEADER.match(line):
            if current:
                blocks.append("\n".join(current))
            current, in_traceback = [line], False
            continue
        if not current:
            if line.startswith(_TRACEBACK_HEAD):  # 没有记录头的裸堆栈也要留住
                current, in_traceback = [line], True
            continue
        if line.startswith(_TRACEBACK_HEAD):
            current.append(line)
            in_traceback = True
            continue
        if line.startswith(_CHAIN_HEADS):
            current.append(line)
            in_traceback = True
            continue
        if line.strip() and line[0] in " \t":  # 堆栈帧、链式异常的缩进行
            current.append(line)
            continue
        if in_traceback and line.strip():  # 异常类型行：本块收尾
            current.append(line)
            in_traceback = False
            continue
        if in_traceback:  # traceback 内部允许有空行
            current.append(line)
            continue
        blocks.append("\n".join(current))
        current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


# ── 发送链阶段日志 ───────────────────────────────────────────────────────
# 一次发送只留十来行：一行一个阶段码，正文绝不落盘（只写长度）。发送失败后再看
# 运行记录，能顺着 phase= 追到卡在"定位窗口"还是"按回车之后"。
SEND_PHASE_CODES = (
    'locate_window',      # 定位/恢复微信窗口
    'check_chat',         # 核对目标会话（必要时打开）
    'input_text',         # 写入正文并回读确认
    'press_send',         # 执行发送键
    'verify_watermark',   # 确认发送后输入框已清空
    'guia_probe',         # 「坐标+OCR」通道可用性探测
    'guia_check_title',   # OCR 标题栏校验会话名
    'guia_input',         # OCR 通道粘贴正文
    'guia_click_send',    # OCR 通道点击发送
    'guia_verify',        # OCR 通道确认输入框已清空
    'guia_restore_window',  # 窗口最小化，发送前自动恢复显示
    'guia_open_chat',      # 用搜索打开目标会话（只在真发送路径）
    'fallback_switch',    # 从 UIA 降级到「坐标+OCR」
)
SEND_CHANNELS = ('uia', 'guia')
SEND_NAME_KEEP = 8
# 发送事件文件：backend.log 5MB 就轮换，失败记录几十次发送后就被冲掉；
# 这里按天单独落一份、只留 7 天，让「发送诊断」在日志翻篇后仍查得到历史失败。
SEND_EVENT_PREFIX = 'send-events-'
SEND_EVENT_SUFFIX = '.jsonl'
SEND_EVENT_KEEP_DAYS = 7
_SEND_EVENT_PRUNE_DAY = None


def mask_chat_name(who) -> str:
    """会话标识只留前 8 个字符 + …：够认出是哪个群，又不把全名写进日志。"""
    name = re.sub(r'\s+', ' ', str(who or '')).strip()
    if not name:
        return '?'
    return name[:SEND_NAME_KEEP] + ('…' if len(name) > SEND_NAME_KEEP else '')


def _send_field(value, empty: str = '-') -> str:
    """日志字段不能带空白：记录被折行后 grep 和错误块提取都会断。"""
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text or empty


def _send_logger():
    """发送链 logger；测试里换成会抛异常的假对象，验证"日志坏了不许连累发送"。"""
    return logging.getLogger(RUNTIME_LOGGER_NAME)


def _event_ts() -> str:
    """事件行自带时间戳：日志行的时间戳由 formatter 加，JSONL 里必须自己写。"""
    return time.strftime('%Y-%m-%d %H:%M:%S')


def send_phase_record(phase, who='', length=0, reason='', channel='uia') -> dict:
    """阶段记录：日志行与事件行共用这一份字段，两边不会漂移。"""
    return {
        'ts': _event_ts(),
        'kind': 'phase',
        'phase': _send_field(phase),
        'who': mask_chat_name(who),
        'len': int(length),
        'reason': _send_field(reason, ''),
        'channel': _send_field(channel),
        'pid': os.getpid(),
    }


def send_phase_line(phase, who='', length=0, reason='', channel='uia') -> str:
    """单行、机器可读的阶段记录：正文只以 len= 出现，绝不写原文。"""
    row = send_phase_record(phase, who, length, reason, channel)
    return (f"send phase={row['phase']} who={row['who']} "
            f"len={row['len']} reason={row['reason']} "
            f"channel={row['channel']} pid={row['pid']}")


def send_failure_record(reason, who='', length=0, channel='uia', hint='', title='',
                        foreground='') -> dict:
    """失败记录：字段与 send_failure_line 完全一致（正文仍然只有 len=）。"""
    row = {
        'ts': _event_ts(),
        'kind': 'failed',
        'reason': _send_field(reason),
        'who': mask_chat_name(who),
        'len': int(length),
        'channel': _send_field(channel),
        'title': _send_field(mask_chat_name(title) if title else '', ''),
        'hint': _send_field(hint, ''),
        'pid': os.getpid(),
    }
    if foreground:
        row['foreground'] = _send_field(foreground)
    return row


def send_failure_line(reason, who='', length=0, channel='uia', hint='', title='',
                      foreground='') -> str:
    """失败摘要行：GUI 的 extract_error_blocks 就是靠这种单行 ERROR 找出来的。"""
    row = send_failure_record(reason, who, length, channel, hint, title, foreground)
    line = (f"send failed channel={row['channel']} reason={row['reason']} "
            f"who={row['who']} len={row['len']} "
            f"title={row['title']} "
            f"hint={row['hint']}")
    if row.get('foreground'):
        line += f" foreground={row['foreground']}"
    return line + f" pid={row['pid']}"


def send_event_dir() -> Path:
    """发送事件目录：与 backend.log 同目录，日志轮转也不会带走这些文件。"""
    return backend_log_dir()


def send_event_path(day=None) -> Path:
    """当天的事件文件；day 传 'YYYY-MM-DD'，缺省今天。"""
    stamp = str(day or time.strftime('%Y-%m-%d'))
    return send_event_dir() / f"{SEND_EVENT_PREFIX}{stamp}{SEND_EVENT_SUFFIX}"


def _prune_send_events(today=None) -> None:
    """每天最多清一次：删掉早于 today-(保留天数-1) 的事件文件，任何失败都静默。"""
    global _SEND_EVENT_PRUNE_DAY
    day = str(today or time.strftime('%Y-%m-%d'))
    if _SEND_EVENT_PRUNE_DAY == day:
        return
    # 先记日期再动手：磁盘坏了也只当今天清过，不在每次发送时反复重试。
    _SEND_EVENT_PRUNE_DAY = day
    try:
        oldest = datetime.date.fromisoformat(day) - datetime.timedelta(days=SEND_EVENT_KEEP_DAYS - 1)
        for path in send_event_dir().glob(f"{SEND_EVENT_PREFIX}*{SEND_EVENT_SUFFIX}"):
            stamp = path.name[len(SEND_EVENT_PREFIX):-len(SEND_EVENT_SUFFIX)]
            try:
                when = datetime.date.fromisoformat(stamp)
            except ValueError:
                continue  # 名字里不是日期的一律不碰
            if when < oldest:
                path.unlink()
    except BaseException:
        return


def append_send_event(record) -> bool:
    """把一条阶段/失败记录追加到当天事件文件；任何异常都吞掉，绝不连累发送。"""
    try:
        line = json.dumps(dict(record or {}), ensure_ascii=False)
        path = send_event_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8', newline='\n') as stream:
            stream.write(line + '\n')
            stream.flush()
    except BaseException:
        return False
    _prune_send_events()
    return True


def log_send_phase(phase, who='', length=0, reason='', channel='uia') -> bool:
    """写一条阶段日志；写不进去也只返回 False，绝不打断发送。"""
    try:
        restore_backend_handler()
        _send_logger().info(send_phase_line(phase, who, length, reason, channel))
        append_send_event(send_phase_record(phase, who, length, reason, channel))
        return True
    except BaseException:
        return False


def log_send_failure(reason, who='', length=0, channel='uia', hint='', title='',
                     level=None, foreground='') -> bool:
    """发送失败写 ERROR（GUI 只提取 ERROR/CRITICAL），并附一行可读诊断摘要。"""
    try:
        restore_backend_handler()
        _send_logger().log(logging.ERROR if level is None else int(level),
                           send_failure_line(reason, who, length, channel, hint, title,
                                             foreground))
        append_send_event(send_failure_record(reason, who, length, channel, hint, title,
                                              foreground))
        return True
    except BaseException:
        return False


# —— 发送耗时归因：只记阶段时刻，不改变任何既有阶段日志的行为与签名 ——
# 发送链跨进程（宿主 → 客体 → 发送子进程），耗时数字必须随返回值带回宿主才读得到；
# 这里只提供一次性收集器，谁收集谁 drain，绝不在日志里额外刷行。
_SEND_PHASES = None
_SEND_PHASES_LOCK = threading.Lock()


def start_send_phases() -> bool:
    """开始收集本次发送的阶段时刻，先落一个 begin 锚点。"""
    global _SEND_PHASES
    with _SEND_PHASES_LOCK:
        _SEND_PHASES = [('begin', round(time.perf_counter() * 1000, 3))]
        return True


def mark_send_phase(phase) -> bool:
    """记一个阶段边界时刻（毫秒，单调时钟）；没在收集时什么都不做。"""
    with _SEND_PHASES_LOCK:
        if _SEND_PHASES is None:
            return False
        _SEND_PHASES.append((_send_field(phase), round(time.perf_counter() * 1000, 3)))
        return True


def drain_send_phases() -> list:
    """取走本次发送的阶段时刻（按发生顺序）并结束收集；返回 [{'phase','at_ms'}]。"""
    global _SEND_PHASES
    with _SEND_PHASES_LOCK:
        phases, _SEND_PHASES = _SEND_PHASES or [], None
    return [{'phase': name, 'at_ms': at} for name, at in phases]


def backend_log_path():
    """当前 root 上真正生效的 backend.log；父进程没配日志就返回 None。"""
    for handler in logging.getLogger().handlers:
        target = getattr(handler, _HANDLER_TAG, None)
        if target:
            return Path(getattr(handler, 'baseFilename', '') or target)
    return None


def backend_log_dir(fallback=None):
    """父进程的日志目录：发送子进程要写同一个文件，靠 pid 分清父子进程。"""
    target = backend_log_path()
    if target is not None:
        return target.parent
    return Path(fallback) if fallback is not None else log_folder()


class ErrorBlock(str):
    """保留字符串兼容；同一堆栈分次写入时，界面按 ID 更新而不是重复追加。"""

    def __new__(cls, text, record_id, level='ERROR'):
        value = super().__new__(cls, text)
        value.record_id = record_id
        value.level = level
        return value


class ErrorReadCursor(int):
    """兼容旧整数偏移，同时记住文件身份与尚未结束的错误记录。"""

    def __new__(cls, offset=0):
        value = super().__new__(cls, offset)
        value.identity = None
        value.generation = 0
        value.anchor = b''
        value.prefix = b''
        value.fingerprint = b''
        value.fingerprint_size = -1
        value.fingerprint_stamp = None
        value.fingerprint_hasher = None
        value.partial = b''
        value.record = ''
        value.record_id = None
        value.level = ''
        value.published = ''
        return value


def _update_error_fingerprint(handle, cursor, size, stamp, *, force=False):
    """初次有界建指纹，追加时只补增量；等长且时间变更时复核中间内容。"""
    if size > _CONTENT_FINGERPRINT_LIMIT:
        cursor.fingerprint, cursor.fingerprint_hasher = b'', None
        cursor.fingerprint_size, cursor.fingerprint_stamp = -1, None
        return False
    old_size, old_digest = cursor.fingerprint_size, cursor.fingerprint
    if (not force and old_size == size and cursor.fingerprint_hasher is not None
            and cursor.fingerprint_stamp == stamp):
        return False
    if not force and cursor.fingerprint_hasher is not None and old_size < size:
        # 游标可被调用方保留；不要原地修改旧游标持有的 hash 对象。
        hasher, position = cursor.fingerprint_hasher.copy(), old_size
    else:
        hasher, position = hashlib.sha256(), 0
    handle.seek(position)
    while position < size:
        chunk = handle.read(min(_CONTENT_FINGERPRINT_CHUNK, size - position))
        if not chunk:
            # 文件在读取中被截断，不能把半份指纹登记成完整基线。
            cursor.fingerprint, cursor.fingerprint_hasher = b'', None
            cursor.fingerprint_size, cursor.fingerprint_stamp = -1, None
            return False
        hasher.update(chunk)
        position += len(chunk)
    digest = hasher.digest()
    changed = old_size == size and bool(old_digest) and digest != old_digest
    cursor.fingerprint, cursor.fingerprint_hasher = digest, hasher
    cursor.fingerprint_size, cursor.fingerprint_stamp = size, stamp
    return changed


def read_new_errors(path, offset: int = 0, max_bytes: int = DEFAULT_TAIL_BYTES, *, include_info=False):
    """有界跟读；跨轮换补读旧文件，跨轮询补全同一堆栈。

    返回值仍为 (字符串列表, 整数兼容游标)。首次只读当前文件尾部；之后每轮
    最多解析 max_bytes，不因日志写得快而跳过未读区。文件身份而非大小判断轮换，
    因此新文件长于旧偏移时也能从头读取。超出保留的五份轮换文件无法恢复。
    正常大小日志（不超过 10MB）另以分块指纹检查等长改写，首次建基线，追加仅
    补新字节，等长且修改时间变化时才重读复核。更大日志仅用前后锚点检测改写。
    """
    target = Path(path)
    budget = max(1, int(max_bytes))
    previous = offset if isinstance(offset, ErrorReadCursor) else None
    cursor = ErrorReadCursor(max(0, int(offset or 0)))
    if previous is not None:
        cursor.__dict__.update(previous.__dict__)
    files = []
    for item in [target.with_name(target.name + f'.{i}') for i in range(LOG_BACKUP_COUNT, 0, -1)] + [target]:
        try:
            stat = item.stat()
            files.append((item, (stat.st_dev, stat.st_ino), stat.st_size))
        except OSError:
            continue
    if not files:
        return [], cursor
    current_index = next((i for i, (_, identity, _) in enumerate(files)
                          if identity == cursor.identity), None)
    position = int(cursor)
    if previous is None:
        current_index = next((i for i, (item, _, _) in enumerate(files) if item == target), None)
        if current_index is None:
            return [], cursor
        size = files[current_index][2]
        position = max(0, size - budget) if position > size or size - position > budget else position
    elif current_index is None:
        # 旧文件已被轮换出保留区，只能从仍存在的最旧一份继续。
        current_index, position = 0, 0
        cursor.partial, cursor.record, cursor.published = b'', '', ''
        cursor.record_id, cursor.level = None, ''
    updates = {}

    def publish():
        if cursor.record and cursor.record != cursor.published:
            updates[cursor.record_id] = ErrorBlock(cursor.record.rstrip('\r\n'), cursor.record_id, cursor.level)
            cursor.published = cursor.record

    def accept(line, line_start):
        text = line.decode('utf-8', 'replace').rstrip('\r\n')
        header = _LOG_HEADER.match(text)
        if header or (not cursor.record and text.startswith(_TRACEBACK_HEAD)):
            publish()
            cursor.level = header[1] if header else 'ERROR'
            cursor.record = text + '\n' if include_info or cursor.level in ('WARNING', 'ERROR', 'CRITICAL') else ''
            cursor.record_id = (cursor.identity, cursor.generation, line_start)
            cursor.published = ''
        elif cursor.record:
            # 保留裸异常行、链式异常和多行说明；不要把分块后的栈帧当成无关文本。
            cursor.record += text + '\n'
            if len(cursor.record) > DEFAULT_TAIL_BYTES * 2:
                cursor.record = cursor.record[:DEFAULT_TAIL_BYTES * 2] + '\n（堆栈过长，后续请查看日志文件）\n'

    for index in range(current_index, len(files)):
        item, identity, size = files[index]
        if identity != cursor.identity:
            if previous is not None or index != current_index:
                publish()
                cursor.partial, cursor.record, cursor.published = b'', '', ''
                cursor.record_id, cursor.level = None, ''
            if index != current_index:
                position = 0
            cursor.identity, cursor.anchor, cursor.prefix, cursor.generation = identity, b'', b'', 0
            cursor.fingerprint, cursor.fingerprint_hasher = b'', None
            cursor.fingerprint_size, cursor.fingerprint_stamp = -1, None
        try:
            with item.open('rb') as handle:
                stat = os.fstat(handle.fileno())
                size = stat.st_size
                # 同一文件被清空后迅速写大，inode 和长度都可能没变；校验上次末尾。
                # 不同错误可能带相同的末尾堆栈，不能只校验末 64 字节。
                # 起始锚点同样只比较上次实际读过的前缀，短文件继续追加不误报改写。
                prefix_matches = not cursor.prefix or handle.read(len(cursor.prefix)) == cursor.prefix
                handle.seek(max(0, position - len(cursor.anchor)))
                anchor_matches = not cursor.anchor or handle.read(len(cursor.anchor)) == cursor.anchor
                edge_changed = position > size or not anchor_matches or not prefix_matches
                content_changed = _update_error_fingerprint(
                    handle, cursor, size, (stat.st_mtime_ns, stat.st_ctime_ns), force=edge_changed)
                if edge_changed or content_changed:
                    publish()
                    position = 0
                    # 同 inode 的截断重写属于新一代内容；界面清空水位不能
                    # 把新文件偏移 0 的真实错误当成已清除的旧记录。
                    cursor.generation += 1
                    cursor.partial, cursor.record, cursor.published = b'', '', ''
                    cursor.record_id, cursor.level = None, ''
                handle.seek(position)
                data = handle.read(budget)
                start = position - len(cursor.partial)
                combined = cursor.partial + data
                position += len(data)
                budget -= len(data)
                lines = combined.split(b'\n')
                cursor.partial = lines.pop()
                for line in lines:
                    accept(line, start)
                    start += len(line) + 1
                # 轮换文件已关闭，不会再补换行；把末行也留给诊断。
                if index < len(files) - 1 and position >= size and cursor.partial:
                    accept(cursor.partial, start)
                    cursor.partial = b''
                # 极端单行不能让 GUI 无限占内存，日志原文仍保存在磁盘。
                if len(cursor.partial) > DEFAULT_TAIL_BYTES * 2:
                    accept(cursor.partial[:DEFAULT_TAIL_BYTES * 2] + '\n（单行过长，详见日志文件）'.encode(), start)
                    cursor.partial = b''
                handle.seek(max(0, position - 64))
                cursor.anchor = handle.read(min(position, 64))
                handle.seek(0)
                cursor.prefix = handle.read(min(position, 64))
        except OSError:
            break
        publish()
        if budget <= 0 or position < size:
            break
    result = ErrorReadCursor(position)
    result.__dict__.update(cursor.__dict__)
    return list(updates.values()), result
