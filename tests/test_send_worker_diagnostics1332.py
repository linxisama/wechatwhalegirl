"""诊断故障不触碰 UI/剪贴板，也不能改变发送结果。"""
import gc
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

from src import send_worker_diagnostics as diag


class FakeTimer:
    instances = []

    def __init__(self, delay, callback):
        self.delay, self.callback = delay, callback
        self.daemon = False
        self.started = self.cancelled = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def join(self, *_):
        raise AssertionError('诊断取消不应等待或阻塞发送')


class Frame:
    def __init__(self, filename='worker.py', name='read', line=12, back=None):
        self.f_code = SimpleNamespace(co_filename=filename, co_name=name)
        self.f_lineno, self.f_back = line, back

    @property
    def f_locals(self):
        raise AssertionError('不能读取正文、密钥等局部变量')


class WorkerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'logs'
        FakeTimer.instances = []
        self.timer_patch = patch.object(diag.threading, 'Timer', FakeTimer)
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)

    def start(self):
        result = diag.start_stack_watchdog(self.folder)
        self.assertIsNotNone(result)
        self.addCleanup(result.cancel)
        return result

    def test_default_ten_second_daemon_timer_is_preserved(self):
        self.start()
        timer = FakeTimer.instances[-1]
        self.assertEqual(timer.delay, 10.0)
        self.assertTrue(timer.daemon and timer.started)

    def test_cancel_before_fire_does_not_capture_or_write(self):
        watchdog = self.start()
        watchdog.cancel()
        with patch.object(diag.sys, '_current_frames', side_effect=AssertionError('不要抓取')):
            FakeTimer.instances[-1].callback()
        self.assertTrue(FakeTimer.instances[-1].cancelled)
        self.assertFalse(watchdog.path.exists())

    def test_cancel_during_capture_does_not_publish(self):
        watchdog = self.start()
        def capture():
            watchdog.cancel()
            return {1: Frame()}
        with patch.object(diag.sys, '_current_frames', side_effect=capture):
            watchdog._dump()
        self.assertFalse(watchdog.path.exists())

    def test_snapshot_contains_only_locations_not_source_or_locals(self):
        watchdog = self.start()
        secret = 'do-not-log-chat-or-key-1332'
        frame = Frame()
        frame.secret = secret
        with patch.object(diag.sys, '_current_frames', return_value={7: frame}):
            watchdog._dump()
        text = watchdog.path.read_text('utf-8')
        self.assertNotIn(secret, text)
        self.assertNotIn('f_locals', text)
        self.assertEqual(json.loads(text.splitlines()[1]), [[7, [['worker.py', 'read', 12]]]])

    def test_strong_frame_references_are_released_before_file_io(self):
        watchdog = self.start()
        refs = []
        def capture():
            tail = Frame(name='tail')
            head = Frame(name='head', back=tail)
            refs.extend((weakref.ref(head), weakref.ref(tail)))
            return {9: head}
        original = Path.open
        def checked_open(path, *args, **kwargs):
            gc.collect()
            self.assertTrue(refs and all(ref() is None for ref in refs))
            return original(path, *args, **kwargs)
        with patch.object(diag.sys, '_current_frames', side_effect=capture), patch.object(Path, 'open', checked_open):
            watchdog._dump()
        self.assertTrue(watchdog.path.exists())

    def test_frame_references_are_released_when_capture_fails(self):
        refs = []
        class BrokenFrame:
            @property
            def f_code(self):
                raise OSError('diagnostic metadata unavailable')
        def capture():
            frame = BrokenFrame()
            refs.append(weakref.ref(frame))
            return {1: frame}
        with patch.object(diag.sys, '_current_frames', side_effect=capture):
            with self.assertRaises(OSError):
                diag.stack_summary()
        gc.collect()
        self.assertIsNone(refs[0]())

    def test_threads_and_depth_and_metadata_are_bounded(self):
        def capture():
            result = {}
            for ident in range(diag.MAX_THREADS + 10):
                frame = None
                for _ in range(diag.MAX_DEPTH + 10):
                    frame = Frame('f' * 900, 'n' * 900, back=frame)
                result[ident] = frame
            return result
        with patch.object(diag.sys, '_current_frames', side_effect=capture):
            rows = diag.stack_summary()
        self.assertEqual(len(rows), diag.MAX_THREADS)
        self.assertTrue(all(len(stack) == diag.MAX_DEPTH for _, stack in rows))
        self.assertTrue(all(len(filename) == diag.MAX_FILENAME and len(name) == diag.MAX_FUNCTION
                            for _, stack in rows for filename, name, _ in stack))

    def test_metadata_controls_are_escaped_on_one_json_line(self):
        watchdog = self.start()
        with patch.object(diag.sys, '_current_frames', return_value={1: Frame('a\nb.py', 'f\r\n', -3)}):
            watchdog._dump()
        lines = watchdog.path.read_text('utf-8').splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1]), [[1, [['a\nb.py', 'f\r\n', 0]]]])

    def test_rotation_preserves_prior_log_above_two_megabytes(self):
        self.folder.mkdir()
        path = self.folder / 'send-worker-stacks.log'
        old = b'x' * (2 * 1024 * 1024 + 1)
        path.write_bytes(old)
        self.start()
        self.assertEqual(path.with_suffix('.log.1').read_bytes(), old)
        self.assertFalse(path.exists())

    def test_at_two_megabytes_log_is_not_rotated(self):
        self.folder.mkdir()
        path = self.folder / 'send-worker-stacks.log'
        path.write_bytes(b'x' * (2 * 1024 * 1024))
        self.start()
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix('.log.1').exists())

    def test_setup_directory_failure_is_contained(self):
        with patch.object(Path, 'mkdir', side_effect=OSError('no diagnostic directory')):
            self.assertIsNone(diag.start_stack_watchdog(self.folder))

    def test_rotation_failure_is_contained(self):
        self.folder.mkdir()
        (self.folder / 'send-worker-stacks.log').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
        with patch.object(Path, 'replace', side_effect=OSError('locked diagnostic log')):
            self.assertIsNone(diag.start_stack_watchdog(self.folder))

    def test_timer_start_failure_is_contained_and_cancelled(self):
        with patch.object(FakeTimer, 'start', side_effect=RuntimeError('no thread')):
            self.assertIsNone(diag.start_stack_watchdog(self.folder))
        self.assertTrue(FakeTimer.instances[-1].cancelled)

    def test_capture_failure_does_not_escape_timer(self):
        watchdog = self.start()
        with patch.object(diag.sys, '_current_frames', side_effect=RuntimeError('unavailable')):
            watchdog._dump()
        self.assertFalse(watchdog.path.exists())

    def test_write_failure_does_not_escape_timer(self):
        watchdog = self.start()
        with patch.object(diag.sys, '_current_frames', return_value={1: Frame()}), patch.object(Path, 'open', side_effect=OSError('full disk')):
            watchdog._dump()

    def test_cancel_does_not_raise_when_timer_cancel_fails(self):
        watchdog = self.start()
        with patch.object(FakeTimer, 'cancel', side_effect=OSError('cancel failed')):
            watchdog.cancel()
        self.assertTrue(watchdog._cancelled.is_set())

    def test_worker_result_is_unchanged_if_diagnostics_cannot_start(self):
        from src import wechat_send_worker as worker
        request = Path(self.temp.name) / 'send_jobs' / 'sample.json'
        request.parent.mkdir()
        request.write_text(json.dumps({'action': 'window-check'}), 'utf-8')
        with patch.object(diag, 'start_stack_watchdog', return_value=None), patch.object(worker, 'setup_logging'), patch.object(worker, 'execute', return_value=(True, 'unchanged')):
            self.assertEqual(worker.main(str(request)), 0)
        result = json.loads(request.with_suffix('.result.json').read_text('utf-8'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['message'], 'unchanged')


if __name__ == '__main__':
    unittest.main()
