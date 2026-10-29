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
import interaction_bilibili_watch as w
from interaction_monitor import status
from publish_core import PublishError


class BilibiliWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name); self.root = self.folder / 'state'
        self.cfg = {'enabled': True, 'account': 'creator', 'uid': '101',
                    'credentials': str(self.folder / 'missing-auth.json'),
                    'not_before': '2026-01-01T00:00:00+00:00',
                    'works': [{'aid': '201', 'bvid': 'BV1AA411A7gJ'}]}
        self.time = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
        self.factory = Mock(return_value=object()); self.rows = []; self.sleeps = []
        self.result = {'platform': 'bilibili', 'scope': 'configured_works', 'state': 'complete',
                       'works': 1, 'added': 0, 'baseline': 0, 'parents_scanned': 1,
                       'parents_total': 1, 'writes': 0, 'send_enabled': False, 'official_api': False}
        self.sync = Mock(side_effect=lambda *a, **k: dict(self.result))

    def sleep(self, seconds):
        self.sleeps.append(seconds); self.time += timedelta(seconds=seconds)

    def watch(self, **kwargs):
        options = dict(interval=1800, cycles=2, client_factory=self.factory, sync_fn=self.sync,
                       sleep_fn=self.sleep, clock=lambda: self.time, emit=self.rows.append)
        options.update(kwargs)
        return w.watch(self.root, options.pop('loader', lambda: copy.deepcopy(self.cfg)), **options)

    def status(self):
        return status(self.root, 'bilibili', 'creator', '101', clock=lambda: self.time)

    def test_finite_reads_reuse_scope_record_real_cycle_times_and_never_send(self):
        outcome = self.watch()
        self.assertEqual((outcome['state'], outcome['cycles'], outcome['writes']), ('completed', 2, 0))
        self.assertEqual((self.sync.call_count, self.factory.call_count, self.sleeps), (2, 2, [1800]))
        saved = self.status()
        self.assertEqual((saved['complete_cycles'], saved['error_cycles'], saved['lock_held']), (2, 0, False))
        self.assertEqual(saved['max_start_gap_seconds'], 1800)
        for row in self.rows:
            self.assertFalse(row['send_enabled']); self.assertFalse(row['official_api'])
            self.assertEqual(row['scope'], 'configured_works')
        self.assertFalse(any(p.name == 'interaction-dispatches' for p in self.root.iterdir()))

    def test_partial_coverage_is_retained_and_followed_by_next_bounded_cycle(self):
        self.sync.side_effect = [dict(self.result, state='partial'), dict(self.result)]
        self.watch()
        self.assertEqual((self.status()['partial_cycles'], self.status()['complete_cycles']), (1, 1))
        self.assertEqual(self.rows[0]['state'], 'partial')

    def test_failure_stops_without_retry_or_secret_in_output(self):
        self.sync.side_effect = PublishError('sensitive-cookie-value')
        outcome = self.watch()
        self.assertEqual((outcome['state'], self.sync.call_count, self.sleeps), ('error', 1, []))
        self.assertEqual((self.status()['state'], self.status()['error_cycles']), ('failed', 1))
        self.assertNotIn('sensitive-cookie-value', json.dumps(outcome) + ''.join(p.read_text()
                         for p in self.root.glob('*.json')))

    def test_disabled_during_run_stops_before_credentials(self):
        loader = Mock(side_effect=[copy.deepcopy(self.cfg), dict(self.cfg, enabled=False)])
        outcome = self.watch(loader=loader)
        self.assertEqual((outcome['state'], outcome['cycles']), ('stopped', 0))
        self.factory.assert_not_called(); self.sync.assert_not_called()

    def test_identity_scope_cutoff_or_credential_change_requires_explicit_new_run(self):
        changes = {'account': 'other', 'uid': '102', 'credentials': '/private/new-auth.json',
                   'not_before': '2026-02-01T00:00:00+00:00',
                   'works': [{'aid': '202', 'bvid': 'BV1AA411A7gK'}]}
        for key, value in changes.items():
            with self.subTest(key=key):
                self.factory.reset_mock(); self.sync.reset_mock()
                changed = dict(self.cfg); changed[key] = value
                loader = Mock(side_effect=[copy.deepcopy(self.cfg), changed])
                outcome = self.watch(loader=loader)
                self.assertEqual(outcome['reason'], 'reader_config_changed')
                self.factory.assert_not_called(); self.sync.assert_not_called()

    def test_duplicate_uid_with_another_alias_is_rejected_before_credentials(self):
        other = dict(self.cfg, account='alias')
        second_factory = Mock()
        def first(*args, **kwargs):
            self.assertTrue(self.status()['lock_held'])
            with self.assertRaises(PublishError):
                w.watch(self.root, lambda: other, client_factory=second_factory,
                        sync_fn=self.sync, cycles=1, clock=lambda: self.time)
            return dict(self.result)
        self.watch(sync_fn=first, cycles=1)
        second_factory.assert_not_called()

    def test_invalid_intervals_cycles_and_budgets_rejected_before_loading_config(self):
        for option, value in [('interval', 1799), ('interval', True), ('interval', 86401),
                              ('cycles', 0), ('cycles', 50), ('cycles', True),
                              ('max_pages', 1), ('max_parents', 0), ('max_reply_pages', 11)]:
            loader = Mock(return_value=self.cfg)
            with self.subTest(option=option, value=value), self.assertRaises(PublishError):
                self.watch(loader=loader, **{option: value})
            loader.assert_not_called()
        self.factory.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_empty_config_rejected_before_state_or_credentials(self):
        for cfg in ({}, dict(self.cfg, works=[]), dict(self.cfg, enabled=False)):
            with self.subTest(cfg=cfg), self.assertRaises(PublishError):
                self.watch(loader=lambda: cfg)
        self.factory.assert_not_called(); self.assertFalse(self.root.exists())

    def test_public_state_directory_rejected_before_credentials(self):
        self.root = Path(w.__file__).resolve().parents[1] / 'should-never-be-created'
        with self.assertRaises(PublishError): self.watch()
        self.factory.assert_not_called(); self.assertFalse(self.root.exists())

    def test_lock_symlink_rejected_before_credentials(self):
        self.root.mkdir(mode=0o700)
        target = self.folder / 'target'; target.write_text('retain'); target.chmod(0o600)
        key = hashlib.sha256(b'bilibili-reader:101').hexdigest()
        (self.root / ('bilibili-reader-' + key + '.lock')).symlink_to(target)
        with self.assertRaises(OSError): self.watch()
        self.factory.assert_not_called(); self.assertEqual(target.read_text(), 'retain')

    def test_unrecognized_or_writing_adapter_result_stops_once(self):
        for result in ({}, dict(self.result, writes=1), dict(self.result, writes=False),
                       dict(self.result, send_enabled=True), dict(self.result, official_api=True),
                       dict(self.result, scope='entire_account'), dict(self.result, works=2),
                       dict(self.result, added=-1)):
            self.sync.reset_mock(); self.sync.side_effect = [result]
            with self.subTest(result=result):
                outcome = self.watch()
                self.assertEqual((outcome['state'], self.sync.call_count), ('error', 1))
                self.assertEqual((self.status()['complete_cycles'], self.status()['error_cycles']), (0, 1))

    def test_interrupt_preserves_stopped_evidence_and_does_not_retry(self):
        self.sync.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.watch()
        saved = self.status()
        self.assertEqual((saved['state'], saved['reason'], saved['cycles']), ('stopped', 'interrupted', 0))
        self.assertEqual(self.sync.call_count, 1); self.assertEqual(self.sleeps, [])

    def test_disabled_config_still_allows_local_status_without_reading_credentials(self):
        self.watch(cycles=1)
        cfg = self.folder / 'config.json'; cfg.write_text(json.dumps(dict(self.cfg, enabled=False)));cfg.chmod(0o600)
        output = io.StringIO()
        with patch.object(sys, 'argv', ['watch', '--config', str(cfg), '--state-dir', str(self.root), 'status']), contextlib.redirect_stdout(output):
            self.assertEqual(w.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['complete_cycles'], 1)
        self.assertFalse(Path(self.cfg['credentials']).exists())


if __name__ == '__main__': unittest.main()
