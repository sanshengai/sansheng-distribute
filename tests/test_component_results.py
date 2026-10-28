"""Component checks use existing identities, preserve uncertainty, and never publish."""
import json
import subprocess
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import test_campaign as fixture
import test_series_report as series_fixture
from campaign_report import render_report
from component_results import component_snapshot, record_component
from publish_core import PublishError, Store
from series_report import render_series_report

ROOT = Path(__file__).resolve().parents[1]


class ComponentResultsTests(unittest.TestCase):
    setUp = fixture.CampaignTests.setUp
    calls = fixture.CampaignTests.calls

    def set_remote(self):
        target = self.manifest['targets'][0]
        key = self.store.key(self.manifest, target)
        with self.store.db:
            self.store.db.execute('UPDATE jobs SET state=?, remote_id=? WHERE key=?', ('published', 'remote-1', key))
        return key

    def stamp(self, seconds=0):
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

    def test_reports_unknown_until_manual_observation_and_escapes_evidence(self):
        key = self.set_remote()
        before = self.store.status(key)
        page = render_report(self.cid, self.store)
        self.assertIn('封面：未核验', page)
        self.assertIn('合集：未核验', page)
        record_component(self.store, key, 'cover', 'matched', 'remote-1', '<script>观察来源</script>', self.stamp())
        page = render_report(self.cid, self.store)
        self.assertIn('封面：一致（人工记录', page)
        self.assertIn('合集：未核验', page)
        self.assertIn('&lt;script&gt;观察来源&lt;/script&gt;', page)
        self.assertNotIn('<script>观察来源</script>', page)
        self.assertIn('不是本报告实时核验', page)
        self.assertEqual(self.store.status(key), before)
        self.assertEqual(self.calls(), [])

    def test_wrong_identity_invalid_evidence_and_older_update_are_rejected(self):
        key = self.set_remote()
        for args in [
            ('cover', 'matched', 'other-id', '核验', self.stamp()),
            ('cover', 'matched', 'remote-1', '', self.stamp()),
            ('bogus', 'matched', 'remote-1', '核验', self.stamp()),
            ('cover', 'verified', 'remote-1', '核验', self.stamp()),
            ('cover', 'matched', 'remote-1', '核验', '2025-01-01T00:00:00'),
            ('cover', 'matched', 'remote-1', '核验', self.stamp(3600)),
        ]:
            with self.subTest(args=args), self.assertRaises(PublishError):
                record_component(self.store, key, *args)
        self.assertEqual(component_snapshot(self.store, self.store.status(key)), {})
        record_component(self.store, key, 'cover', 'mismatch', 'remote-1', '后台显示不一致', self.stamp(-10))
        with self.assertRaisesRegex(PublishError, '较旧'):
            record_component(self.store, key, 'cover', 'matched', 'remote-1', '旧观察', self.stamp(-30))
        self.assertEqual(component_snapshot(self.store, self.store.status(key))['cover']['result'], 'mismatch')
        self.assertIn('分项不一致，请处理并复核原作品', render_report(self.cid, self.store))

    def test_prepared_job_cannot_claim_remote_observation(self):
        target = self.manifest['targets'][0]
        key = self.store.key(self.manifest, target)
        with self.store.db:
            self.store.db.execute('UPDATE jobs SET remote_id=? WHERE key=?', ('remote-1', key))
        with self.assertRaisesRegex(PublishError, '尚未提交'):
            record_component(self.store, key, 'cover', 'matched', 'remote-1', '后台页面', self.stamp())
        self.assertEqual(self.store.status(key)['state'], 'prepared')

    def test_concurrent_independent_component_appends_are_serialized(self):
        key = self.set_remote()
        barrier = threading.Barrier(2)
        failures = []
        def write(field):
            local = Store(self.store.directory)
            try:
                barrier.wait()
                record_component(local, key, field, 'matched', 'remote-1', '同一作品详情页', self.stamp())
            except BaseException as exc:
                failures.append(exc)
            finally:
                local.close()
        threads = [threading.Thread(target=write, args=(field,)) for field in ('cover', 'collection')]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(set(component_snapshot(self.store, self.store.status(key))), {'cover', 'collection'})

    def test_cli_records_private_evidence_without_exposing_it_or_creating_a_job(self):
        key = self.set_remote()
        evidence = self.root / 'evidence.txt'
        evidence.write_text('来自作品编辑页的私有观察', encoding='utf-8')
        args = [sys.executable, str(ROOT / 'scripts/component_results.py'), '--state-dir', str(self.store.directory),
                'record', '--key', key, '--field', 'declaration', '--result', 'unknown', '--remote-id', 'remote-1',
                '--evidence-file', str(evidence), '--observed-at', self.stamp()]
        before = self.store.status(key)
        result = subprocess.run(args, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('来自作品编辑页的私有观察', result.stdout + result.stderr)
        self.assertNotIn(str(evidence), result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['result']['remote_checked'])
        self.assertEqual(component_snapshot(self.store, self.store.status(key))['declaration']['result'], 'unknown')
        self.assertEqual(self.store.status(key), before)
        output = self.root / 'campaign.html'
        report = subprocess.run([sys.executable, str(ROOT / 'scripts/publish.py'),
                                 '--profile', str(self.root / 'unused-profile.json'),
                                 '--state-dir', str(self.store.directory), 'batch-report', self.cid,
                                 '--output', str(output)], text=True, capture_output=True)
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        page = output.read_text(encoding='utf-8')
        self.assertIn('声明：未确认（人工记录', page)
        self.assertIn('来自作品编辑页的私有观察', page)
        self.assertEqual(self.store.status(key), before)
        invalid = list(args)
        invalid[invalid.index('--state-dir') + 1] = str(self.root / 'missing')
        result = subprocess.run(invalid, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'missing').exists())


class SeriesComponentTests(unittest.TestCase):
    setUp = series_fixture.SeriesReportTests.setUp
    make_campaign = series_fixture.SeriesReportTests.make_campaign

    def test_one_compact_warning_without_changing_publication_count(self):
        self.make_campaign()
        target = self.manifest['targets'][0]
        key = self.store.key(self.manifest, target)
        with self.store.db:
            self.store.db.execute('UPDATE jobs SET remote_id=? WHERE key=?', ('remote-1', key))
        stamp = datetime.now(timezone.utc).isoformat()
        record_component(self.store, key, 'cover', 'mismatch', 'remote-1', '人工查看作品', stamp)
        page = render_series_report(self.spec, self.store)
        self.assertEqual(page.count('分项待查'), 1)
        self.assertIn('人工分项：封面不一致', page)
        self.assertIn('1 / 3', page)
        self.assertIn('已发布', page)


if __name__ == '__main__':
    unittest.main()
