"""同目录原子替换，避免断电/重启读到半截配置；写失败向上抛出。"""
import os
import shutil
import tempfile
import time
from pathlib import Path

from loguru import logger


def _rewrite_in_place(path,text):
    """就地重写：os.replace 被占用挡住时使用，内容照样落盘但不具备原子性。"""
    with open(path,'w',encoding='utf-8',newline='\n') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_text(path,text,backup=True):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if backup and path.is_file():
            shutil.copy2(path,path.with_suffix(path.suffix+'.bak'))
        try:
            os.replace(tmp,path)
        except PermissionError as error:
            code=getattr(error,'winerror',None)
            logger.warning(f'原子替换被占用挡住，已降级为就地写入：{path}（WinError {code}）：{error}')
            _rewrite_in_place(path,text)
    finally:
        Path(tmp).unlink(missing_ok=True)


# 数据目录可写探测：真实做一次「建文件 → 写 → 改名 → 删」，而不是用 os.access 猜。
# 为什么必须真做 IO：本机已知 %LOCALAPPDATA%\WhaleGirlWeChat 这类目录会被杀毒/搜索索引/
# 旧进程挡住，os.access 说能写、真写就报 WinError 32，只有真写一次才判得准。
# 为什么把「改名」单独报一档：改名失败只说明原子替换被占用挡住（atomic_text 已就地重写兜底），
# 并不等于存不下数据；把它算成「写不进去」会让控制台误换数据目录，用户原有配置和聊天记录
# 就会在界面上「看不见」——那比失去原子性严重得多。
def probe_directory(path):
    """返回 {path, writable, atomic_replace, reason, elapsed_ms}；不抛异常，不动任何旧文件。"""
    root = Path(path)
    started = time.monotonic()
    result = {'path': str(root), 'writable': False, 'atomic_replace': False,
              'reason': '', 'elapsed_ms': 0}
    created = renamed = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd, created = tempfile.mkstemp(prefix='.write-probe-', suffix='.tmp', dir=str(root))
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write('writable\n')
            handle.flush()
            os.fsync(handle.fileno())
        result['writable'] = Path(created).is_file()
        renamed = Path(created).with_suffix('.renamed')
        try:
            os.replace(created, renamed)
        except PermissionError as error:
            result['reason'] = (f'数据目录能写文件，但改名/原子替换被占用挡住（WinError '
                                f'{getattr(error, "winerror", None)}）：{error}；'
                                '配置写入会退化为就地重写，数据不会丢')
        else:
            result['atomic_replace'] = True
    except OSError as error:
        result['reason'] = (f'数据目录写不进去（WinError {getattr(error, "winerror", None)}）：'
                            f'{error}')
    finally:
        for leftover in (created, renamed):
            if leftover is None:
                continue
            try:
                Path(leftover).unlink(missing_ok=True)
            except OSError:
                pass
        result['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    return result
