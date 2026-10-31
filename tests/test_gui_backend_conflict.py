"""端口被"另一个自己的后台"占用时的处理逻辑。

这不是假想的场景：源码版后台（`start.bat` / `wechat_main.py`）没关，或上一次控制台被强杀，
8091 上就会留着数据目录不同的旧后台；被装的程序启动后所有请求都会被身份校验拦掉，
界面表现是三个状态卡一直"读取中"、插件与人设页空白 —— 用户完全不知道该怎么办。

所以 wechat_gui 里加了两件事，本文件把它们钉住：
1. `backend_error()` 要说清楚占用者的进程号与数据目录（只说"请关闭旧后台"没有用）；
2. `_start_backend()` 在确认身份后自动结束它（**只认 app 标识相同的**，绝不动别的程序）。
"""
import unittest
from unittest.mock import patch

import wechat_gui
from src.runtime_identity import APP_ID


class BackendConflictTests(unittest.TestCase):
    def setUp(self):
        self.foreign = {"app": APP_ID, "version": wechat_gui.APP_VERSION, "pid": 4321,
                        "data_root": r"D:\另一个目录\微信机器人", "port": wechat_gui.PORT}
        self.other_app = {"app": "SomeOtherTool", "version": "1.0", "pid": 99,
                          "data_root": r"D:\别的程序"}

    class _Response:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status

        def json(self):
            return self._payload

    def test_message_names_the_offender(self):
        """提示里必须带进程号与数据目录，否则用户不知道去关谁。"""
        with patch.object(wechat_gui._PROXY_FREE, "get", return_value=self._Response(self.foreign)), \
                patch.object(wechat_gui, "ROOT", r"E:\当前安装目录"):
            message = wechat_gui.backend_error()
        self.assertIn("4321", message)
        self.assertIn("另一个目录", message)
        self.assertIn("E:\\当前安装目录", message)

    def test_other_programs_are_never_touched(self):
        """端口上是别人的程序：能识别出来，但绝不认领（因此绝不会被自动结束）。"""
        with patch.object(wechat_gui._PROXY_FREE, "get", return_value=self._Response(self.other_app)):
            self.assertIsNone(wechat_gui.conflicting_backend())
        # 即使有人直接把它交给结束函数，也必须拒绝（结束进程不可逆，多判一次身份）
        ok, detail = wechat_gui.terminate_conflicting_backend(self.other_app)
        self.assertFalse(ok)
        self.assertIn("不是本项目", detail)

    def test_conflicting_backend_is_recognised(self):
        with patch.object(wechat_gui._PROXY_FREE, "get", return_value=self._Response(self.foreign)):
            self.assertEqual(wechat_gui.conflicting_backend()["pid"], 4321)

    def test_terminate_kills_only_after_identity_check(self):
        """结束流程：taskkill 打的是被识别出来的那个 PID，端口不再应答才算成功。"""
        calls = []

        def fake_get(url, timeout=None):
            # 第一次（识别阶段）有应答，杀掉之后再问就连接失败
            if calls:
                raise OSError("connection refused")
            return self._Response(self.foreign)

        def fake_run(args, **kwargs):
            calls.append(args)
            return None

        with patch.object(wechat_gui._PROXY_FREE, "get", side_effect=fake_get), \
                patch.object(wechat_gui.subprocess, "run", side_effect=fake_run), \
                patch.object(wechat_gui.time, "sleep", return_value=None):
            ok, detail = wechat_gui.terminate_conflicting_backend(self.foreign)

        self.assertTrue(ok, detail)
        self.assertEqual(calls, [["taskkill", "/PID", "4321", "/F"]])
        self.assertIn("4321", detail)

    def test_terminate_reports_failure_when_still_alive(self):
        """杀不掉时要如实报失败（让用户去任务管理器处理），不能假装成功。"""
        with patch.object(wechat_gui._PROXY_FREE, "get", return_value=self._Response(self.foreign)), \
                patch.object(wechat_gui.subprocess, "run", return_value=None), \
                patch.object(wechat_gui.time, "sleep", return_value=None):
            ok, detail = wechat_gui.terminate_conflicting_backend(self.foreign)
        self.assertFalse(ok)
        self.assertIn("4321", detail)


if __name__ == "__main__":
    unittest.main()
