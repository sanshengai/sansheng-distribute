import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from interaction_monitor import Monitor, status
from publish_core import PublishError, write_json


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monitor(self, **kwargs):
        return Monitor(self.root, 'youtube', 'owner', 'channel', interval=1800, cycles=2,
                       clock=lambda: self.stamp, **kwargs)

    def check(self):
        return status(self.root, 'youtube', 'owner', 'channel', clock=lambda: self.stamp)

    def test_actual_lock_and_cycle_timestamps_not_pid_are_used(self):
        self.assertEqual(self.check()['state'], 'not_started')
        with self.monitor() as run:
            run.begin_cycle(); run.record_cycle('complete'); run.sleep()
            run.data['pid'] = 2147483647
            run._save()
            self.assertEqual(self.check()['health'], 'lock_observed')
            self.stamp += timedelta(seconds=1800)
            run.begin_cycle(); run.record_cycle('partial')
            run.finish('completed', 'cycle_limit_reached')
            history = run.history
        result = self.check()
        self.assertEqual(result['health'], 'terminal')
        self.assertFalse(result['lock_held'])
        self.assertEqual((result['cycles'], result['complete_cycles'], result['partial_cycles']), (2, 1, 1))
        self.assertEqual(result['elapsed_seconds'], 1800)
        self.assertEqual(result['max_start_gap_seconds'], 1800)
        self.assertEqual(history.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(history.read_text())['state'], 'completed')

    def test_separate_process_duplicate_is_rejected_before_doing_work(self):
        code = '''import sys
from interaction_monitor import Monitor
from publish_core import PublishError
try:
    with Monitor(sys.argv[1],'youtube','owner','channel',interval=1800,cycles=1):
        print('DUPLICATE WORK STARTED')
        sys.exit(9)
except PublishError:
    print('duplicate rejected')
'''
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'scripts')}
        with self.monitor() as run:
            before = run.path.read_bytes()
            child = subprocess.run([sys.executable, '-c', code, str(self.root)],
                                   capture_output=True, text=True, env=env)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(child.stdout.strip(), 'duplicate rejected')
            self.assertEqual(run.path.read_bytes(), before)
            run.finish('completed', 'test_done')

    def test_overdue_is_distinct_from_process_disappearance(self):
        run = self.monitor().__enter__()
        try:
            run.begin_cycle(); run.record_cycle('complete'); run.sleep()
            self.stamp += timedelta(seconds=2200)
            self.assertEqual(self.check()['health'], 'overdue')
        finally:
            run.file.close(); run.file = None
        self.assertEqual(self.check()['health'], 'stopped_unexpectedly')
        old_history = run.history
        with self.monitor() as replacement:
            self.assertNotEqual(replacement.data['run_id'], run.data['run_id'])
            replacement.finish('completed', 'test_done')
        old = json.loads(old_history.read_text())
        self.assertEqual(old['state'], 'stopped')
        self.assertIsNone(old['finished_at'])  # The actual crash time is unknown.
        self.assertEqual(old['reason'], 'previous_lock_released_without_terminal_record')

    def test_interrupt_and_unexpected_exception_release_lock(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.monitor(): raise KeyboardInterrupt()
        self.assertEqual(self.check()['state'], 'stopped')
        with self.assertRaises(RuntimeError):
            with self.monitor(): raise RuntimeError('not copied to evidence')
        result = self.check()
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('not copied', json.dumps(result))

    def test_empty_corrupt_identity_and_counter_mutations_rejected_on_status_path(self):
        with self.monitor() as run:
            run.finish('completed', 'test_done')
            path = run.path
            original = copy.deepcopy(run.data)
        candidates = [{}, [], {**original, 'state': []}, {**original, 'account':'wrong'},
                      {**original, 'cycles':1}, {**original, 'interval_seconds':False},
                      {**original, 'started_at':'2026-01-01'},
                      {**original, 'last_cycle_state':{}}, {**original, 'unexpected_secret':'redacted-test'}]
        for changed in candidates:
            with self.subTest(changed=changed):
                write_json(path, changed)
                with self.assertRaises(PublishError): self.check()
        path.write_text('{'); path.chmod(0o600)
        with self.assertRaises(PublishError): self.check()


if __name__ == '__main__':
    unittest.main()
