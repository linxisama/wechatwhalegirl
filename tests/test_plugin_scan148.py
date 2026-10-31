"""插件源码分级扫描（1.3.48-B2）：AST 判定，不问正则。

为什么需要：check_source 是"命中就拒绝"的黑名单闸门，它只能回答"能不能装"，
回答不了"装上之后它会干什么"。这里给三档画像——forbidden（能执行任意代码或
起子进程）、risky（碰文件/网络/动态导入）、clean——供预检界面展示。

关键口径都写在用例里：re.compile / str.replace 这类同名常见方法不能被误判，
from os import system as _s 这种别名必须照样认得出来。
"""
import tempfile
import unittest
from pathlib import Path

from src import plugin_scan


CLEAN_PLUGIN = '''
import asyncio
import json
import re

_PATTERN = re.compile(r"a+")


class Plugin:
    def on_ingest(self, message, runtime):
        text = str(message.get("content") or "")
        if _PATTERN.search(text):
            return text.replace("a", "b")
        return json.dumps({"ok": True})
'''


def level_of(code):
    return plugin_scan.scan_source(code)["level"]


def rules_of(code):
    return [item["rule"] for item in plugin_scan.scan_source(code)["findings"]]


class ForbiddenTests(unittest.TestCase):
    """能执行任意代码或起子进程的一律 forbidden。"""

    def test_os_system_is_forbidden(self):
        self.assertEqual(level_of("import os\nos.system('dir')"), "forbidden")
        self.assertIn("os.system", rules_of("import os\nos.system('dir')"))

    def test_os_popen_and_exec_are_forbidden(self):
        self.assertEqual(level_of("import os\nos.popen('dir')"), "forbidden")
        self.assertEqual(level_of("import os\nos.execv('/bin/ls', [])"), "forbidden")

    def test_subprocess_is_forbidden(self):
        code = "import subprocess\nsubprocess.run(['cmd', '/c', 'dir'])"
        self.assertEqual(level_of(code), "forbidden")
        self.assertIn("subprocess.*", rules_of(code))

    def test_eval_exec_compile_import_are_forbidden(self):
        self.assertEqual(level_of("eval('1+1')"), "forbidden")
        self.assertEqual(level_of("exec('x = 1')"), "forbidden")
        self.assertEqual(level_of("compile('x = 1', '<s>', 'exec')"), "forbidden")
        self.assertEqual(level_of("__import__('os')"), "forbidden")

    def test_ctypes_is_forbidden(self):
        self.assertEqual(level_of("import ctypes\nctypes.windll.user32"), "forbidden")

    def test_pickle_and_marshal_loads_are_forbidden(self):
        self.assertEqual(level_of("import pickle\npickle.loads(b'')"), "forbidden")
        self.assertEqual(level_of("import marshal\nmarshal.loads(b'')"), "forbidden")

    def test_globals_escape_is_forbidden(self):
        code = "def f(fn):\n    return fn.__globals__['os']"
        self.assertEqual(level_of(code), "forbidden")
        self.assertIn("__globals__", rules_of(code))

    def test_import_alias_is_still_caught(self):
        # 别名不影响判定：from os import system as _s 之后 _s('dir') 仍然是起子进程
        code = "from os import system as _s\n_s('dir')"
        self.assertEqual(level_of(code), "forbidden")
        self.assertIn("os.system", rules_of(code))

    def test_builtin_alias_is_still_caught(self):
        code = "from builtins import eval as _e\n_e('1+1')"
        self.assertEqual(level_of(code), "forbidden")


class RiskyTests(unittest.TestCase):
    """碰文件、网络、动态导入算 risky：不拦，但要让用户看见。"""

    def test_write_mode_open_is_risky(self):
        self.assertEqual(level_of("open('a.txt', 'w')"), "risky")
        self.assertEqual(level_of("open('a.txt', mode='a')"), "risky")
        self.assertEqual(level_of("open('a.txt', 'rb')"), "clean")
        self.assertEqual(level_of("open('a.txt')"), "clean")

    def test_path_write_methods_are_risky(self):
        self.assertIn("write_text", rules_of("from pathlib import Path\nPath('a').write_text('x')"))
        self.assertIn("write_bytes", rules_of("from pathlib import Path\nPath('a').write_bytes(b'x')"))

    def test_file_removal_is_risky(self):
        self.assertEqual(level_of("import os\nos.remove('a')"), "risky")
        self.assertEqual(level_of("import os\nos.replace('a', 'b')"), "risky")

    def test_shutil_is_risky(self):
        self.assertIn("shutil.*", rules_of("import shutil\nshutil.rmtree('d')"))

    def test_network_is_risky(self):
        self.assertIn("socket.*", rules_of("import socket\nsocket.socket()"))
        self.assertIn("urllib.*", rules_of("import urllib.request\nurllib.request.urlopen('http://x')"))
        self.assertIn("requests.*", rules_of("import requests\nrequests.get('http://x')"))
        self.assertIn("httpx.*", rules_of("import httpx\nhttpx.get('http://x')"))

    def test_dynamic_import_is_risky(self):
        self.assertIn("importlib.*", rules_of("import importlib\nimportlib.import_module('os')"))


class NoFalsePositiveTests(unittest.TestCase):
    """同名常见方法不能被误判：误报会让分级变成噪音，用户就不看了。"""

    def test_clean_plugin_stays_clean(self):
        report = plugin_scan.scan_source(CLEAN_PLUGIN)
        self.assertEqual(report["level"], "clean")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["counts"], {"forbidden": 0, "risky": 0})

    def test_re_compile_is_not_eval_of_code(self):
        self.assertEqual(level_of("import re\nre.compile('a+')"), "clean")

    def test_str_replace_is_not_file_write(self):
        self.assertEqual(level_of("text = 'a'\ntext = text.replace('a', 'b')"), "clean")

    def test_comment_and_docstring_are_ignored(self):
        code = '"""这里提到 os.system 与 eval 只是说明文字。"""\n# os.system("dir")\nx = 1\n'
        self.assertEqual(level_of(code), "clean")


class RobustnessTests(unittest.TestCase):
    """扫描本身绝不能把控制台带崩。"""

    def test_syntax_error_is_unknown_not_exception(self):
        report = plugin_scan.scan_source("def broken(:\n")
        self.assertEqual(report["level"], "unknown")
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["error"])

    def test_missing_file_is_unknown_not_exception(self):
        report = plugin_scan.scan_file(Path(tempfile.gettempdir()) / "no_such_plugin_148.py")
        self.assertEqual(report["level"], "unknown")
        self.assertTrue(report["error"])

    def test_findings_are_capped_and_deduped(self):
        code = "\n".join("eval('1')" for _ in range(20))
        report = plugin_scan.scan_source(code)
        self.assertEqual(report["level"], "forbidden")
        self.assertLessEqual(len(report["findings"]), plugin_scan.MAX_FINDINGS_PER_RULE)

    def test_real_plugins_all_scan_without_exception(self):
        root = Path(__file__).resolve().parents[1] / "src" / "plugins"
        files = sorted(root.glob("*.py"))
        self.assertGreater(len(files), 30)          # 32 个在用插件，别在空目录上假装通过
        levels = {}
        for path in files:
            report = plugin_scan.scan_file(path)
            self.assertIn(report["level"], plugin_scan.LEVELS + ("unknown",), path.name)
            levels[report["level"]] = levels.get(report["level"], 0) + 1
        self.assertGreater(levels.get("clean", 0), 20)   # 绝大多数插件本来就干净


if __name__ == "__main__":
    unittest.main()
