import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import interaction_notify as notice
from interaction_inbox import open_db


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        with closing(open_db(self.root)) as db:
            db.execute("INSERT INTO interaction_sources(platform,account,channel_id,state) VALUES('youtube','account','channel','complete')")
            db.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, comment_id, published_at, own=0, status='new'):
        with closing(sqlite3.connect(self.root / 'interaction-inbox.sqlite3')) as db:
            db.execute('''INSERT INTO interaction_items(platform,account,channel_id,comment_id,thread_id,
                       first_seen_at,last_seen_at,is_own,local_status,published_at,body,author_name)
                       VALUES('youtube','account','channel',?,'thread','2026-01-01T00:00:00Z',
                       '2026-01-01T00:00:00Z',?,?,?,'SECRET TEXT','SECRET NAME')''',
                       (comment_id,own,status,published_at))
            db.commit()

    def test_baseline_backfill_and_new_comment_once(self):
        self.add('existing', '2020-01-01T00:00:00Z')
        with patch.object(notice, 'now', return_value='2026-01-01T00:00:00Z'):
            first = notice.check(self.root, True, sender=lambda: self.fail('baseline must not notify'))
        self.assertEqual(first['baseline_items'], 1)
        self.add('older-backfill', '2025-01-01T00:00:00Z')
        self.add('fresh', '2026-01-01T01:00:00Z')
        self.add('self', '2026-01-01T01:00:00Z', own=1)
        calls = []
        with patch.object(notice, 'now', return_value='2026-01-01T02:00:00Z'):
            disabled = notice.check(self.root, False, sender=lambda: self.fail('disabled must not notify'))
            self.assertEqual(disabled['candidates'], 2)
            self.assertEqual(disabled['historical_items'], 1)
            enabled = notice.check(self.root, True, sender=lambda: calls.append(1) or 'requested')
            again = notice.check(self.root, True, sender=lambda: calls.append(1) or 'requested')
        self.assertEqual(enabled['notification'], 'requested')
        self.assertEqual(enabled['candidates'], 2)
        self.assertEqual(enabled['historical_items'], 1)
        self.assertEqual(again['candidates'], 0)
        self.assertEqual(calls, [1])

    def test_failed_notification_does_not_claim_delivered_or_retry(self):
        with patch.object(notice, 'now', return_value='2026-01-01T00:00:00Z'):
            notice.check(self.root)
        self.add('fresh', '2026-01-01T01:00:00Z')
        result = notice.check(self.root, True, sender=lambda: 'failed')
        self.assertEqual(result['notification'], 'failed')
        self.assertIn('不证明', result['note'])
        again = notice.check(self.root, True, sender=lambda: self.fail('must not retry ambiguous attempt'))
        self.assertEqual(again['candidates'], 0)

    def test_native_request_is_fixed_and_does_not_contain_user_data(self):
        with patch.object(notice.subprocess, 'run') as run:
            run.return_value.returncode = 0
            self.assertEqual(notice._request_notification(), 'requested')
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['/usr/bin/osascript', '-e'])
        self.assertNotIn('SECRET', repr(command))
        self.assertNotIn('account', repr(command))

    def test_parallel_checks_submit_only_one_request(self):
        with patch.object(notice, 'now', return_value='2026-01-01T00:00:00Z'):
            notice.check(self.root)
        self.add('fresh', '2026-01-01T01:00:00Z')
        entered = threading.Event()
        release = threading.Event()
        calls = []
        def sender():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(3))
            return 'requested'
        worker = threading.Thread(target=notice.check, args=(self.root, True), kwargs={'sender':sender})
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            second = notice.check(self.root, True, sender=lambda: calls.append(2) or 'requested')
            self.assertEqual(second['candidates'], 0)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, [1])

    def test_no_source_does_not_claim_empty_platform(self):
        with closing(sqlite3.connect(self.root / 'interaction-inbox.sqlite3')) as db:
            db.execute('DELETE FROM interaction_sources')
            db.commit()
        result = notice.check(self.root)
        self.assertEqual(result['notification'], 'disabled')
        self.assertEqual(result['candidates'], 0)


if __name__ == '__main__':
    unittest.main()
