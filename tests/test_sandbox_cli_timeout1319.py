"""CLI 超时不得等待继承标准输出的窗口；只运行本测试拥有的 Python 子进程。"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil
from src import sandbox_manager as manager


class SandboxCliOutputTests(unittest.TestCase):
    def run_python(self, code, *, timeout=3):
        with patch.object(manager, 'find_wsb', return_value=Path(sys.executable)):
            return manager._run_wsb(['-c', code], timeout=timeout)

    def test_bom_and_unicode_json_from_real_owned_process(self):
        value = self.run_python("import sys;sys.stdout.buffer.write(b'\\xef\\xbb\\xbf'+"
            "'{\"message\":\"你好\",\"ExitCode\":0}'.encode('utf-8'))")
        self.assertEqual(value, {'message': '你好', 'ExitCode': 0})

    def test_empty_success_output_remains_accepted(self):
        self.assertEqual(self.run_python('pass'), {})

    def test_nonzero_garbled_error_is_sanitized(self):
        with self.assertRaisesRegex(manager.SandboxError, '操作失败') as error:
            self.run_python("import sys;sys.stderr.buffer.write(b'\\xffPRIVATE-CREDENTIAL');sys.exit(7)")
        self.assertNotIn('PRIVATE', str(error.exception))

    def test_success_with_invalid_utf8_is_a_format_error(self):
        with self.assertRaisesRegex(manager.SandboxError, '无法识别'):
            self.run_python("import sys;sys.stdout.buffer.write(b'\\xffnot-json')")

    def test_non_object_json_remains_rejected(self):
        with self.assertRaisesRegex(manager.SandboxError, '格式不正确'):
            self.run_python("print('[]')")

    def test_stdout_over_limit_is_rejected_before_json_parsing(self):
        with self.assertRaisesRegex(manager.SandboxError, '读取上限'):
            self.run_python("import sys;sys.stdout.buffer.write(b'x'*(2*1024*1024+1))")

    def test_stderr_also_counts_towards_total_output_limit(self):
        with self.assertRaisesRegex(manager.SandboxError, '读取上限'):
            self.run_python("import sys;print('{}');sys.stderr.buffer.write(b'x'*(2*1024*1024))")

    def test_plain_process_timeout_still_returns_fixed_error(self):
        start = time.monotonic()
        with self.assertRaisesRegex(manager.SandboxError, '操作超时'):
            self.run_python('import time;time.sleep(10)', timeout=.15)
        self.assertLess(time.monotonic() - start, 2)

    def test_missing_cli_executable_is_sanitized(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(manager, 'find_wsb', return_value=Path(folder) / 'absent-cli.exe'):
                with self.assertRaisesRegex(manager.SandboxError, '无法运行'):
                    manager._run_wsb(['list'], timeout=.2)


class SandboxInheritedHandleTests(unittest.TestCase):
    def test_timeout_returns_while_owned_descendant_still_holds_both_output_handles(self):
        # 监督进程只等自己的直接测试进程。旧实现会在 TimeoutExpired 后无限
        # communicate；外层仍可按时结束回归并用专属 release 文件清理测试后代。
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            ready, release = base / 'holder-ready.json', base / 'release'
            result = base / 'result.json'
            holder = """import json,os,sys,time
from pathlib import Path
ready,release=map(Path,sys.argv[1:3])
# Only the owning test releases these inherited handles.  A fixed lifetime can
# expire while the test thread is descheduled during the full suite.
while not release.exists():time.sleep(.02)
"""
            launcher = """import json,subprocess,sys,time
from pathlib import Path
holder,ready,release=sys.argv[1:4]
child=subprocess.Popen([sys.executable,'-c',holder,ready,release],stdin=subprocess.DEVNULL,stdout=sys.stdout,stderr=sys.stderr,
    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
Path(ready).write_text(json.dumps({'pid':child.pid}))
time.sleep(20)
"""
            supervisor = """import json,sys,time
from pathlib import Path
from unittest.mock import patch
from src import sandbox_manager as manager
launcher,holder,ready,release,result=sys.argv[1:6]
started=time.monotonic()
with patch.object(manager,'find_wsb',return_value=Path(sys._base_executable)):
    try:
        manager._run_wsb(['-c',launcher,holder,ready,release],timeout=2)
        value={'raised':False}
    except manager.SandboxError as error:
        value={'raised':True,'message':str(error)}
value['elapsed']=time.monotonic()-started
value['interpreter']=sys._base_executable
Path(result).write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
"""
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            process = subprocess.Popen([sys.executable, '-c', supervisor, launcher, holder,
                str(ready), str(release), str(result)], cwd=str(Path(__file__).resolve().parents[1]),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
            returned = False
            holder_process = None
            try:
                # CreateProcess 的控制台初始化可尚未返回父 CLI，但此时内核已
                # 创建并继承输出句柄。观察真实进程关系，不依赖其 Python 启动完成。
                outer_deadline = time.monotonic() + 6
                while process.poll() is None and time.monotonic() < outer_deadline:
                    try:
                        descendants = psutil.Process(process.pid).children(recursive=True)
                        for candidate in descendants:
                            arguments = candidate.cmdline()
                            if (len(arguments) >= 5 and arguments[1:3] == ['-c', holder]
                                    and arguments[3:5] == [str(ready), str(release)]):
                                candidate.create_time()
                                holder_process = candidate
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                    time.sleep(.02)
                returned = process.poll() is not None
                self.assertIsNotNone(holder_process, 'owned descendant must have inherited the output handles; '
                    + (result.read_text('utf-8') if result.exists() else 'supervisor result missing'))
                self.assertFalse(release.exists(), 'handle holder must remain unreleased during timeout checks')
                child = holder_process
                self.assertTrue(child.is_running(), 'runner must not terminate the child holding its handles')
                self.assertTrue(returned, 'CLI timeout must not wait for descendant stdout/stderr EOF')
                self.assertEqual(process.returncode, 0)
                value = json.loads(result.read_text('utf-8'))
                self.assertTrue(value['raised'])
                self.assertIn('操作超时', value['message'])
                self.assertLess(value['elapsed'], 4)
            finally:
                release.touch()
                if process.poll() is None:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=2)
                if holder_process is not None:
                    try:
                        child = holder_process
                        if child.is_running() and str(release) in child.cmdline():
                            try:
                                child.wait(timeout=3)
                            except psutil.TimeoutExpired:
                                child.terminate(); child.wait(timeout=2)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass


if __name__ == '__main__':
    unittest.main()
