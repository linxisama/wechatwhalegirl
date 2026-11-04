"""1.3.88 发送事件文件：阶段/失败按天落盘、跨天读、7 天清理、接口不再依赖 backend.log。

为什么单独一份事件文件：backend.log 满 5MB 就轮转，而一次真实发送要写十几条阶段记录，
失败记录几十次发送后就被挤出日志尾部窗口——「发送诊断」于是查不到十几分钟前的失败。
事件文件把 phase/failed 按天落盘、各留各的窗口，耗时仍从日志尾部读。
本文件全部在临时目录里造数据，不碰工程真实的 logs/，也不起后台、不连微信。
"""
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import logging_setup
from src import send_history as sh
from src.agent_authority import AgentAuthority

ADMIN = {'platform': 'desktop', 'account': 'local_console', 'subject': 'owner'}


def phase_record(ts, phase='guia_input', pid=100):
    return {'ts': ts, 'kind': 'phase', 'phase': phase, 'who': '西莲寺紫…', 'len': 19,
            'reason': '', 'channel': 'guia', 'pid': pid}


def failed_record(ts, reason='send_failed', pid=100):
    return {'ts': ts, 'kind': 'failed', 'reason': reason, 'who': '西莲寺紫…', 'len': 19,
            'channel': 'guia', 'title': '西莲寺紫…', 'hint': '请核对聊天记录', 'pid': pid}


def event_file(folder, day):
    return Path(folder) / f"{logging_setup.SEND_EVENT_PREFIX}{day}{logging_setup.SEND_EVENT_SUFFIX}"


def write_events(folder, day, records):
    path = event_file(folder, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records),
                    encoding='utf-8')
    return path


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        today = datetime.date.today()
        self.today = today.isoformat()
        self.old_day = (today - datetime.timedelta(days=3)).isoformat()


class CrossDayTests(TempCase):
    """A1：阶段与失败各自留窗口，跨天也能读到。"""

    def test_failures_survive_two_hundred_phases_in_the_same_limit(self):
        write_events(self.folder, self.today,
                     [phase_record(f'{self.today} 10:{i // 60:02d}:{i % 60:02d}')
                      for i in range(200)])
        write_events(self.folder, self.old_day,
                     [failed_record(f'{self.old_day} 09:00:01'),
                      failed_record(f'{self.old_day} 09:00:02'),
                      phase_record(f'{self.old_day} 09:00:03')])

        rows = sh.recent_events(self.folder, limit=50)

        failures = [row for row in rows if row['kind'] == 'failed']
        phases = [row for row in rows if row['kind'] == 'phase']
        self.assertEqual(len(failures), 2)
        self.assertEqual(len(phases), 50)
        self.assertEqual([row['ts'] for row in failures],
                         [f'{self.old_day} 09:00:01', f'{self.old_day} 09:00:02'])
        self.assertEqual([row['ts'] for row in rows], sorted(row['ts'] for row in rows))
        self.assertEqual(sh.summarize(rows)['counts'],
                         {'phases': 50, 'failures': 2, 'timings': 0})

    def test_failed_rows_get_the_same_chinese_text_and_advice_as_log_lines(self):
        write_events(self.folder, self.today, [failed_record(f'{self.today} 10:00:00')])
        row = sh.recent_events(self.folder, limit=50)[0]
        self.assertEqual(row['text'], '「坐标+OCR」发送失败')
        self.assertEqual(row['advice'], sh.hint_for('send_failed'))

    def test_broken_lines_and_other_kinds_are_skipped(self):
        path = event_file(self.folder, self.today)
        path.write_text('\n'.join(['不是JSON', '[]', '{"kind": "timing", "ts": "x"}',
                                    json.dumps(phase_record(f'{self.today} 10:00:00'),
                                               ensure_ascii=False), '']),
                        encoding='utf-8')
        rows = sh.recent_events(self.folder, limit=50)
        self.assertEqual([row['kind'] for row in rows], ['phase'])

    def test_missing_folder_and_missing_days_yield_nothing(self):
        self.assertEqual(sh.recent_events(self.folder / 'nope', limit=50), [])
        self.assertEqual(sh.recent_events(None, limit=50), [])

    def test_limit_is_clamped_like_the_log_reader(self):
        write_events(self.folder, self.today,
                     [phase_record(f'{self.today} 10:00:{i:02d}') for i in range(5)])
        self.assertEqual(len(sh.recent_events(self.folder, limit=1)), 1)
        self.assertEqual(len(sh.recent_events(self.folder, limit=0)), 5)
        self.assertEqual(len(sh.recent_events(self.folder, limit=999)), 5)


class AppendFailureTests(unittest.TestCase):
    """A2：写不进去只返回 False，绝不抛——发送链不许被日志拖垮。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def test_parent_is_a_file_returns_false(self):
        blocker = self.folder / 'blocked'
        blocker.write_text('x', encoding='utf-8')
        target = blocker / f"{logging_setup.SEND_EVENT_PREFIX}2026-11-04{logging_setup.SEND_EVENT_SUFFIX}"
        with patch.object(logging_setup, 'send_event_path', return_value=target):
            self.assertFalse(logging_setup.append_send_event(phase_record('2026-11-04 10:00:00')))

    def test_read_only_open_returns_false(self):
        target = event_file(self.folder, '2026-11-04')
        with patch.object(logging_setup, 'send_event_path', return_value=target), \
                patch('builtins.open', side_effect=OSError('磁盘只读')):
            self.assertFalse(logging_setup.append_send_event(failed_record('2026-11-04 10:00:00')))
        self.assertFalse(target.exists())

    def test_bad_record_returns_false_instead_of_raising(self):
        target = event_file(self.folder, '2026-11-04')
        with patch.object(logging_setup, 'send_event_path', return_value=target):
            self.assertFalse(logging_setup.append_send_event(object()))


class PruneTests(unittest.TestCase):
    """A3：只留 7 天；清理只在追加成功后跑，且每天最多一次。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        saved = logging_setup._SEND_EVENT_PRUNE_DAY
        logging_setup._SEND_EVENT_PRUNE_DAY = None
        self.addCleanup(setattr, logging_setup, '_SEND_EVENT_PRUNE_DAY', saved)
        self.today = datetime.date.today()

    def test_eight_day_old_file_is_deleted_and_six_day_old_file_is_kept(self):
        stale = write_events(self.folder, (self.today - datetime.timedelta(days=8)).isoformat(),
                             [phase_record('2026-10-27 10:00:00')])
        fresh = write_events(self.folder, (self.today - datetime.timedelta(days=6)).isoformat(),
                             [phase_record('2026-10-29 10:00:00')])
        target = event_file(self.folder, self.today.isoformat())
        with patch.object(logging_setup, 'send_event_dir', return_value=self.folder), \
                patch.object(logging_setup, 'send_event_path', return_value=target):
            self.assertTrue(logging_setup.append_send_event(phase_record('2026-11-04 10:00:00')))
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(len(target.read_text('utf-8').splitlines()), 1)

    def test_names_that_are_not_dates_are_left_alone(self):
        keeper = self.folder / f"{logging_setup.SEND_EVENT_PREFIX}backup{logging_setup.SEND_EVENT_SUFFIX}"
        keeper.write_text('', encoding='utf-8')
        target = event_file(self.folder, self.today.isoformat())
        with patch.object(logging_setup, 'send_event_dir', return_value=self.folder), \
                patch.object(logging_setup, 'send_event_path', return_value=target):
            logging_setup.append_send_event(phase_record('2026-11-04 10:00:00'))
        self.assertTrue(keeper.exists())


class FakeBot:
    def __init__(self, root):
        self.root = Path(root)


class HistoryRouteTests(TempCase):
    """A4：只有事件文件、没有 backend.log 时，接口照样给出失败记录。"""

    def client(self, root):
        from fastapi.testclient import TestClient
        from src.wechat_app import create_app

        AgentAuthority(root / 'data' / 'agent_authority.json').bind(dict(ADMIN))
        app = create_app()
        app.state.bot = FakeBot(root)
        return TestClient(app)

    def test_failures_come_from_event_files_when_backend_log_is_absent(self):
        write_events(self.folder, self.today,
                     [phase_record(f'{self.today} 10:00:00'),
                      phase_record(f'{self.today} 10:00:01', phase='guia_paste'),
                      failed_record(f'{self.today} 10:00:02')])
        client = self.client(self.folder)
        with patch.object(logging_setup, 'backend_log_path',
                          return_value=self.folder / 'backend.log'):
            body = client.get('/api/send/history?limit=50').json()

        self.assertTrue(body['ok'])
        self.assertFalse((self.folder / 'backend.log').exists())
        self.assertEqual(body['counts'], {'phases': 2, 'failures': 1, 'timings': 0})
        self.assertEqual([row['kind'] for row in body['items']], ['phase', 'phase', 'failed'])
        self.assertEqual(body['last_failure']['reason'], 'send_failed')
        self.assertEqual(body['last_failure']['text'], '「坐标+OCR」发送失败')

    def test_timings_still_come_from_the_log_tail(self):
        write_events(self.folder, self.today, [failed_record(f'{self.today} 10:00:02')])
        log = self.folder / 'backend.log'
        log.write_text(
            f'{self.today} 10:00:03,000 [MainThread] INFO send timing guest total_ms=4514 '
            'receive_ms=100 agent_ms=8860\n', encoding='utf-8')
        client = self.client(self.folder)
        with patch.object(logging_setup, 'backend_log_path', return_value=log):
            body = client.get('/api/send/history?limit=50').json()

        self.assertEqual(body['counts'], {'phases': 0, 'failures': 1, 'timings': 1})
        self.assertEqual([row['kind'] for row in body['items']], ['failed', 'timing'])
        self.assertEqual(body['last_timing']['total_ms'], 4514)


class AccumulationDirectionTests(TempCase):
    """B1~B3：累积方向必须整体从新到旧，[:keep] 取到的是最新那批。

    缺陷背景：event_paths() 从新到旧遍历文件，但单个文件里的行是 append 写入的
    （旧->新）。若不把当天记录反转，phases[:keep] 取到的是当天最早的 keep 条，
    面板「最近 50 条」在一天发到第 4 次左右就永久冻结在当天最早的几次发送上，
    last_phase 也停在旧值——比只读日志尾部取最新是可见倒退。
    """

    def test_b1_same_day_phases_keep_the_newest_fifty(self):
        write_events(self.folder, self.today,
                     [phase_record(f'{self.today} 10:{i // 60:02d}:{i % 60:02d}',
                                   phase='p%03d' % i)
                      for i in range(200)])

        out = sh.recent_events(self.folder, limit=50)

        self.assertEqual(len(out), 50)
        self.assertEqual(out[0]['phase'], 'p150')
        self.assertEqual(out[-1]['phase'], 'p199')
        self.assertEqual([row['phase'] for row in out],
                         ['p%03d' % i for i in range(150, 200)])
        self.assertEqual(sh.summarize(out)['last_phase']['phase'], 'p199')

    def test_b2_failures_get_their_own_newest_fifty_window(self):
        write_events(self.folder, self.today,
                     [phase_record(f'{self.today} 10:{i // 60:02d}:{i % 60:02d}',
                                   phase='p%03d' % i)
                      for i in range(200)]
                     + [failed_record(f'{self.today} 11:{i // 60:02d}:{i % 60:02d}',
                                      reason='send_failed' if i % 2 == 0 else 'blocked_by_window')
                        for i in range(120)])

        out = sh.recent_events(self.folder, limit=50)

        failures = [row for row in out if row['kind'] == 'failed']
        self.assertEqual(len(failures), 50)
        newest = max(failures, key=lambda row: row['ts'])
        self.assertEqual(failures[-1]['ts'], newest['ts'])
        self.assertEqual(failures[-1]['ts'], f'{self.today} 11:01:59')
        self.assertEqual(sh.summarize(out)['last_failure'], failures[-1])
        self.assertEqual([row['phase'] for row in out if row['kind'] == 'phase'][:1], ['p150'])

    def test_b3_newer_day_wins_over_an_older_day(self):
        write_events(self.folder, self.today,
                     [phase_record(f'{self.today} 10:{i // 60:02d}:{i % 60:02d}',
                                   phase='today%03d' % i)
                      for i in range(60)])
        write_events(self.folder, self.old_day,
                     [phase_record(f'{self.old_day} 09:{i // 60:02d}:{i % 60:02d}',
                                   phase='old%03d' % i)
                      for i in range(60)])

        out = sh.recent_events(self.folder, limit=50)

        self.assertEqual(len(out), 50)
        self.assertEqual([row['phase'] for row in out],
                         ['today%03d' % i for i in range(10, 60)])
        self.assertEqual(sh.summarize(out)['last_phase']['phase'], 'today059')


if __name__ == '__main__':
    unittest.main()
