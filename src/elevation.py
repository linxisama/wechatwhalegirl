"""管理员权限：能不能发、以及怎么自己提权重启。

为什么单独一个模块：微信 4.x 的发送是像素点击 + 剪贴板，非管理员进程会被系统挡掉，
表现为「能收不能发」。用户双击图标启动时默认不是管理员，代码里只能给提示，
让他自己去找「以管理员身份运行」——所以这里提供一键重启，并且把判断与启动做成可测的纯函数。
"""
import ctypes
import os
import sys

SW_SHOWNORMAL = 1
SHELL_EXECUTE_OK = 32  # ShellExecuteW 返回值大于 32 才算成功


def is_elevated():
    """当前进程是不是管理员。判断不了就当不是（宁可多提示一次，不要误报能用）。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def launch_command(argv=None, executable=None, frozen=None):
    """返回 (程序, 参数串)。冻结态直接重启 exe；源码态用 python + 脚本路径。"""
    frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else bool(frozen)
    exe = executable or sys.executable
    args = list(sys.argv if argv is None else argv)
    if frozen:
        return exe, ''
    script = os.path.abspath(args[0]) if args and args[0] else ''
    rest = args[1:]
    parts = ['"' + script + '"'] if script else []
    parts.extend('"' + str(item) + '"' if ' ' in str(item) else str(item) for item in rest)
    return exe, ' '.join(parts)


def relaunch_elevated(argv=None, executable=None, frozen=None, result_hook=None):
    """以管理员身份重启自己，返回 (是否已发起, 给用户看的一句话)。

    只发起、不等待：提权要用户在 UAC 弹窗里点「是」，等下去只会把界面卡住。
    """
    if is_elevated():
        return False, '当前已经是管理员权限，不需要重启'
    exe, params = launch_command(argv, executable, frozen)
    if not exe:
        return False, '找不到当前程序的路径，无法自动提权；请手动右键「以管理员身份运行」'
    try:
        hook = result_hook or ctypes.windll.shell32.ShellExecuteW
        code = hook(None, 'runas', exe, params, os.getcwd(), SW_SHOWNORMAL)
        code = int(code or 0)
        if code <= SHELL_EXECUTE_OK:
            # 取消 UAC 时返回 1223 之类的错误码；这里不区分，统一让用户手动提权。
            return False, f'提权请求没有成功（返回码 {code}）；请手动右键「以管理员身份运行」'
        return True, '已请求以管理员身份重启：在弹窗里点「是」，旧窗口会自动关闭'
    except Exception as exc:
        return False, f'提权失败：{exc}；请手动右键「以管理员身份运行」'
