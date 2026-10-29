import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import interaction_weibo_watch as w
from interaction_monitor import status
from publish_core import PublishError


class WeiboWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name); self.root = self.folder / 'state'
        self.cfg = {'schema': 'weibo-official-inbox.v1', 'enabled': True, 'account': 'creator',
                    'uid': '101', 'credentials': str(self.folder / 'missing-auth.json'),
                    'not_before': '2026-01-01T00:00:00+00:00', 'work_ids': ['201']}
        self.time = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
        self.rows = []; self.sleeps = []
        self.result = {'state': 'partial', 'returned_rows': 5, 'distinct_comments': 3,
                       'new_local_candidates': 0, 'canonical_imports': 0, 'social_writes': 0}
        self.sync = Mock(side_effect=lambda *a, **k: dict(self.result))

    def sleep(self, seconds):
        self.sleeps.append(seconds); self.time += timedelta(seconds=seconds)

    def watch(self, **kwargs):
        options = dict(interval=1800, cycles=2, sync_fn=self.sync, sleep_fn=self.sleep,
                       clock=lambda: self.time, emit=self.rows.append)
        options.update(kwargs)
        return w.watch(self.root, options.pop('loader', lambda: copy.deepcopy(self.cfg)), **options)

    def status(self):
        return status(self.root, 'weibo', 'creator', '101', clock=lambda: self.time)

    def test_finite_reads_count_partial_only_and_leave_no_send_or_draft_queue(self):
        result = self.watch()
        self.assertEqual((result['state'], result['cycles'], result['coverage']), ('completed', 2, 'partial'))
        self.assertEqual((self.sync.call_count, self.sleeps), (2, [1800]))
        saved = self.status()
        self.assertEqual((saved['partial_cycles'], saved['complete_cycles'], saved['error_cycles']), (2, 0, 0))
        self.assertEqual(saved['max_start_gap_seconds'], 1800)
        self.assertFalse(saved['lock_held'])
        for row in self.rows:
            self.assertEqual((row['social_writes'], row['canonical_imports']), (0, 0))
            self.assertFalse(row['send_enabled'])
        self.assertFalse((self.root / 'interaction-inbox.sqlite3').exists())
        self.assertFalse(Path(self.cfg['credentials']).exists())

    def test_empty_list_still_records_partial_instead_of_full_coverage(self):
        self.result.update(returned_rows=0, distinct_comments=0)
        self.watch(cycles=1)
        self.assertEqual((self.status()['partial_cycles'], self.status()['complete_cycles']), (1, 0))

    def test_notifications_are_opt_in_and_failures_do_not_stop_reader(self):
        notifier = Mock(return_value={'notification': 'requested'})
        self.watch(notify_fn=notifier)
        notifier.assert_not_called()
        self.rows.clear()
        self.watch(notify=True, notify_fn=notifier)
        self.assertEqual(notifier.call_count, 2)
        self.assertTrue(all(r['notification'] == 'requested' for r in self.rows))
        self.rows.clear()
        result = self.watch(notify=True, notify_fn=Mock(side_effect=RuntimeError('SECRET')))
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(all(r['notification'] == 'error' for r in self.rows))
        self.assertNotIn('SECRET', str(self.rows))

    def test_output_failure_counts_one_failed_round_instead_of_two_rounds(self):
        result = self.watch(cycles=1, emit=Mock(side_effect=BrokenPipeError()))
        saved = self.status()
        self.assertEqual((result['state'], saved['cycles'], saved['partial_cycles'], saved['error_cycles']),
                         ('error', 1, 0, 1))

    def test_error_stops_without_retry_or_copying_secret(self):
        self.sync.side_effect = PublishError('secret-from-transport')
        result = self.watch()
        self.assertEqual((result['state'], self.sync.call_count, self.sleeps), ('error', 1, []))
        self.assertEqual((self.status()['state'], self.status()['error_cycles']), ('failed', 1))
        self.assertNotIn('secret-from-transport', json.dumps(result) + ''.join(p.read_text() for p in self.root.glob('*.json')))

    def test_disabled_config_stops_before_api(self):
        loader = Mock(side_effect=[copy.deepcopy(self.cfg), dict(self.cfg, enabled=False)])
        self.assertEqual(self.watch(loader=loader)['reason'], 'reader_config_disabled')
        self.sync.assert_not_called()

    def test_frozen_identity_scope_cutoff_and_credential_path_stop_before_api(self):
        changes = {'account': 'alias', 'uid': '102', 'credentials': '/private/other-auth.json',
                   'not_before': '2026-02-01T00:00:00+00:00', 'work_ids': ['202']}
        for field, value in changes.items():
            with self.subTest(field=field):
                loader = Mock(side_effect=[copy.deepcopy(self.cfg), dict(self.cfg, **{field: value})])
                self.assertEqual(self.watch(loader=loader)['reason'], 'reader_config_changed')
                self.sync.assert_not_called()

    def test_alias_cannot_start_second_watcher_for_same_uid(self):
        nested = Mock()
        def sync(*args):
            self.assertTrue(self.status()['lock_held'])
            with self.assertRaises(PublishError):
                w.watch(self.root, lambda: dict(self.cfg, account='alias'), sync_fn=nested)
            return self.result
        self.watch(sync_fn=sync, cycles=1)
        nested.assert_not_called()

    def test_invalid_budget_is_rejected_before_loading_or_state_creation(self):
        for field, value in [('interval', 1799), ('interval', 86401), ('interval', True),
                             ('cycles', 0), ('cycles', 50), ('cycles', True)]:
            loader = Mock(return_value=self.cfg)
            with self.subTest(field=field, value=value), self.assertRaises(PublishError):
                self.watch(loader=loader, **{field: value})
            loader.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_empty_disabled_unknown_or_future_configuration_rejected_before_api(self):
        for config in ({}, dict(self.cfg, enabled=False), dict(self.cfg, extra=True),
                       dict(self.cfg, work_ids=[]), dict(self.cfg, not_before='2999-01-01T00:00:00Z')):
            with self.subTest(config=config), self.assertRaises(PublishError):
                self.watch(loader=lambda: config)
        self.sync.assert_not_called(); self.assertFalse(self.root.exists())

    def test_public_state_directory_rejected_before_api(self):
        self.root = Path(w.__file__).resolve().parents[1] / 'must-never-exist'
        with self.assertRaises(PublishError): self.watch()
        self.sync.assert_not_called(); self.assertFalse(self.root.exists())

    def test_symlink_watcher_lock_rejected_without_changing_target(self):
        self.root.mkdir(mode=0o700)
        target = self.folder / 'target'; target.write_text('retain'); target.chmod(0o600)
        key = hashlib.sha256(b'weibo-official-reader:101').hexdigest()
        (self.root / ('weibo-official-reader-' + key + '.lock')).symlink_to(target)
        with self.assertRaises(OSError): self.watch()
        self.sync.assert_not_called(); self.assertEqual(target.read_text(), 'retain')

    def test_incomplete_or_writing_or_impossible_result_stops_once(self):
        for result in ({}, dict(self.result, state='complete'), dict(self.result, social_writes=1),
                       dict(self.result, social_writes=False), dict(self.result, canonical_imports=1),
                       dict(self.result, new_local_candidates=4), dict(self.result, returned_rows=1),
                       dict(self.result, distinct_comments=-1)):
            self.sync.reset_mock(); self.sync.side_effect = [result]
            with self.subTest(result=result):
                self.assertEqual(self.watch()['state'], 'error')
                self.assertEqual(self.sync.call_count, 1)
                self.assertEqual(self.status()['error_cycles'], 1)
        self.assertEqual(self.sleeps, [])

    def test_interrupt_keeps_evidence_and_does_not_retry(self):
        self.sync.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.watch()
        self.assertEqual((self.status()['state'], self.status()['reason']), ('stopped', 'interrupted'))
        self.assertEqual(self.sync.call_count, 1); self.assertEqual(self.sleeps, [])

    def test_status_after_disable_never_opens_credentials(self):
        self.watch(cycles=1)
        config = self.folder / 'config.json'
        config.write_text(json.dumps(dict(self.cfg, enabled=False))); config.chmod(0o600)
        output = io.StringIO()
        with patch.object(sys, 'argv', ['watch', '--config', str(config), '--state-dir', str(self.root), 'status']), contextlib.redirect_stdout(output):
            self.assertEqual(w.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['partial_cycles'], 1)
        self.assertFalse(Path(self.cfg['credentials']).exists())


if __name__ == '__main__': unittest.main()
