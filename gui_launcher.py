"""打包（PyInstaller）后的入口。

职责有三件：
1. 把启动过程中的异常写进 `%LOCALAPPDATA%\\WhaleGirlWeChat\\logs\\startup.log`，
   避免"双击没反应"查不到原因；
2. 支持 `--self-test <json>`（打包后也能离线自检）；
3. 支持 `--ocr-worker <图片> [--lines]`：冻结运行时 `sys.executable` 就是本 exe，
   如果还按源码方式 `exe ocr_worker.py 图片` 调用，等于又启动一个控制台。
   这里把参数转给 `ocr_worker` 模块的 `main()` 再退出。

源码运行时用不到这个文件（直接跑 `wechat_gui.py`）。
"""
import faulthandler
import os
import threading
import sys
import traceback
import tempfile
from pathlib import Path

APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WhaleGirlWeChat"
LOG_DIR = APP_DIR / "logs"
try:LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    LOG_DIR=Path(tempfile.gettempdir())/'WhaleGirlWeChat-logs'
    LOG_DIR.mkdir(parents=True,exist_ok=True)
log = open(LOG_DIR / "startup.log", "a", encoding="utf-8", buffering=1)
if sys.stdout is None:
    sys.stdout = log
if sys.stderr is None:
    sys.stderr = log


# 保持文件句柄到进程退出，原生崩溃和 Qt 回调异常也能留下堆栈。
crash_log = open(LOG_DIR / "crash.log", "a", encoding="utf-8", buffering=1)
faulthandler.enable(file=crash_log, all_threads=True)


def _log_exception(kind, value, tb):
    traceback.print_exception(kind, value, tb, file=log)
    log.flush()


sys.excepthook = _log_exception
threading.excepthook = lambda args: _log_exception(args.exc_type, args.exc_value, args.exc_traceback)


def _run_ocr_worker() -> int:
    """把 `--ocr-worker <图片> [--lines]` 交给 ocr_worker 的 main。"""
    # 切到可写目录：ocr_worker 会在 cwd 下建 data/ocr_temp 存中间图，
    # 安装目录可能是只读的（或从别的工作目录被拉起）。
    try:
        os.chdir(APP_DIR)
    except OSError:
        pass
    sys.argv = ["ocr_worker.py"] + sys.argv[2:]
    # 直接 import 再调 main()，比 runpy 在冻结环境里更稳（模块已打进包）
    import ocr_worker

    ocr_worker.main()
    return 0


def main() -> int:
    import multiprocessing
    multiprocessing.freeze_support()
    if len(sys.argv) >= 3 and sys.argv[1] == '--native-check':
        import json
        from src.native_dependencies import check_native_dependencies
        result = check_native_dependencies()
        Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "--ocr-worker":
        return _run_ocr_worker()
    if len(sys.argv) >= 2 and sys.argv[1] in ('--sandbox-agent','--sandbox-agent-launch'):
        from src.isolated_wechat.launcher import main as sandbox_main
        return sandbox_main(sys.argv[1:])
    if len(sys.argv) >= 3 and sys.argv[1] == "--wechat-send-worker":
        from src.wechat_send_worker import main as send_main
        return send_main(sys.argv[2])
    if len(sys.argv)>1 and sys.argv[1]=='--backend-worker':
        from src.desktop_startup import run_backend
        return run_backend(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == '--sandbox-doctor':
        from src.sandbox_doctor import main as doctor_main
        return doctor_main(sys.argv[1:])
    if getattr(sys,'frozen',False) and not any(flag in sys.argv for flag in ('--gui-worker','--self-test','--verify-upgrade-data')):
        from src.desktop_startup import supervise
        return supervise(LOG_DIR,sys.argv[1:],notify='--desktop-check' not in sys.argv)
    import wechat_gui

    if '--desktop-check' in sys.argv:
        from src.gui.desktop_check import run
        run(wechat_gui,Path(sys.argv[sys.argv.index('--desktop-check')+1]))
    elif '--verify-upgrade-data' in sys.argv:
        from src.gui.upgrade_check import run
        run(wechat_gui.ROOT,Path(sys.argv[sys.argv.index('--verify-upgrade-data')+1]))
    elif "--self-test" in sys.argv:
        from src.gui.smoke import run

        run(wechat_gui, Path(sys.argv[sys.argv.index("--self-test") + 1]))
    else:
        wechat_gui.main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc(file=log)
        for flag in ('--self-test','--verify-upgrade-data','--desktop-check','--native-check'):
            if flag in sys.argv:
                Path(sys.argv[sys.argv.index(flag) + 1]).with_suffix(".error.txt").write_text(
                    traceback.format_exc(), encoding="utf-8")
        raise SystemExit(1)
