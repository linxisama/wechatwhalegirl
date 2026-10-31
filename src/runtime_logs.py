"""后台滚动日志独立于控制台 stdout，冻结程序也能留下诊断记录。

真正的配置（格式/轮转/级别/全局异常钩子）都在 src/logging_setup.py；
这里只留一个门面，让调用方继续用 RuntimeLog(folder).write(...) 这套老接口，
但它们写的是**同一个** root logger 和同一个 backend.log，不会出现两份日志。
"""
import logging

from .logging_setup import RUNTIME_LOGGER_NAME, setup_logging, restore_backend_handler


class RuntimeLog:
    def __init__(self, folder):
        # setup_logging 幂等：同一进程里再建一个 RuntimeLog 也只是复用已有 handler
        self.path = setup_logging(folder)
        self._logger = logging.getLogger(RUNTIME_LOGGER_NAME)

    def write(self, text: str):
        # 冻结态可能连 sys.stderr 都没有；日志写失败绝不能打断消息收发循环
        try:
            # 微信依赖导入会清 root handlers；监听事件也必须恢复，不能等到下次发送。
            restore_backend_handler()
            self._logger.info(str(text))
        except Exception:
            pass

    def close(self):
        """故意不摘 root handler：后台关闭时还在收尾，摘掉会把最后几行丢掉。"""
        return None
