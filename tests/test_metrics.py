"""Measure real core/subprocess paths without changing dispatch or recovery truth."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_campaign as fixtures
from campaign import authorize_campaign, run_campaign
import publish_core as core


class MetricsTests(unittest.TestCase):
    def setUp(self):
        fixtures.CampaignTests.setUp(self)
        authorize_campaign(self.cid,self.store,'offline fixture only')
        self.key=self.store.key(self.manifest,self.manifest['targets'][0])

    def calls(self):
        return fixtures.CampaignTests.calls(self)

    def test_execution_and_readonly_recovery_have_separate_durable_samples(self):
        core.run(self.path,'x',self.profile,self.store)
        first=self.store.metrics(self.key)
        self.assertEqual([r['phase'] for r in first['samples']],['preflight','execute','verify'])
        self.assertTrue(all(r['outcome']=='completed' and type(r['elapsed_ns']) is int
                            and r['elapsed_ns']>=0 and r['started_at'].endswith('+00:00')
                            for r in first['samples']))
        events=self.store.db.execute('SELECT * FROM events').fetchall()
        core.run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.calls(),[['x','execute'],['x','verify'],['x','verify']])
        latest=self.store.metrics(self.key)
        self.assertEqual(latest['sample_count'],4)
        self.assertEqual(latest['phases']['execute']['samples'],1)
        self.assertEqual(latest['phases']['verify']['samples'],2)
        self.assertEqual(latest['phases']['verify']['elapsed_ns'],sum(
            r['elapsed_ns'] for r in latest['samples'] if r['phase']=='verify'))
        self.assertEqual(len(self.store.db.execute('SELECT * FROM events').fetchall()),len(events)+1)

    def test_missing_grant_cannot_be_bypassed_by_measurement(self):
        self.store.db.execute('DELETE FROM grants');self.store.db.commit()
        with self.assertRaisesRegex(core.PublishError,'授权'):
            core.run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.calls(),[])
        self.assertEqual(self.store.status(self.key)['state'],'prepared')
        self.assertEqual(list(self.store.metrics(self.key)['phases']),['preflight'])

    def test_timing_storage_failure_preserves_success_unknown_lock_and_original_error(self):
        self.store.db.execute("CREATE TRIGGER no_timing BEFORE INSERT ON timings BEGIN SELECT RAISE(FAIL,'no timing'); END")
        self.store.db.commit()
        self.assertEqual(core.run(self.path,'x',self.profile,self.store)['state'],'draft_ready')
        core.write_json(self.root/'modes.json',{'weibo':'unknown'})
        other=self.store.key(self.manifest,self.manifest['targets'][1])
        with self.assertRaisesRegex(core.PublishError,'execute 失败'):
            core.run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.store.status(other)['state'],'submission_unknown')
        self.assertEqual(self.store.db.execute('SELECT job_key FROM account_locks').fetchone()[0],other)
        self.assertEqual(self.store.metrics(self.key)['sample_count'],0)
        self.assertEqual(self.store.metrics(other)['phases'],{})
        core.write_json(self.root/'modes.json',{})
        self.assertEqual(core.run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'draft_ready')
        self.assertEqual(self.calls().count(['weibo','execute']),1)

    def test_remote_id_is_durable_before_timing_write_and_failed_verification(self):
        self.store.db.executescript('''
          CREATE TABLE timing_witness(remote_id TEXT);
          CREATE TRIGGER witness AFTER INSERT ON timings WHEN NEW.phase='execute'
          BEGIN INSERT INTO timing_witness SELECT remote_id FROM jobs WHERE key=NEW.job_key; END;
        ''')
        original=core.invoke
        def invoke(config,action,path,*,lock_fd):
            if action=='verify':raise core.PublishError('readback unavailable')
            return original(config,action,path,lock_fd=lock_fd)
        with patch.object(core,'invoke',side_effect=invoke):
            with self.assertRaisesRegex(core.PublishError,'readback unavailable'):
                core.run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.store.db.execute('SELECT remote_id FROM timing_witness').fetchone()[0],'remote-x')
        self.assertEqual(self.store.status(self.key)['remote_id'],'remote-x')
        self.assertEqual(self.store.status(self.key)['state'],'pending')
        self.assertEqual(self.store.metrics(self.key)['phases']['verify']['failed'],1)
        core.run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.calls().count(['x','execute']),1)

    def test_old_ledger_gets_empty_metrics_without_rewriting_jobs_or_inventing_history(self):
        before=self.store.status(self.key)
        self.store.db.execute('DROP TABLE timings');self.store.db.commit()
        migrated=core.Store(self.store.directory)
        try:
            self.assertEqual(migrated.status(self.key),before)
            report=migrated.metrics(self.key)
            self.assertEqual(report['sample_count'],0);self.assertEqual(report['phases'],{})
            self.assertIn('historical_runs_before_instrumentation',report['unmeasured'])
        finally:migrated.close()

    def test_metrics_cli_only_reads_the_selected_ledger(self):
        command=[sys.executable,str(Path(core.__file__).with_name('publish.py')),
                 '--state-dir',str(self.store.directory),'--profile',str(self.root/'unused.json'),
                 'metrics','--key',self.key]
        result=subprocess.run(command,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['result']['sample_count'],0)
        self.assertEqual(self.calls(),[])
        self.assertEqual(self.store.status(self.key)['state'],'prepared')

    def test_parallel_campaign_keeps_independent_phase_counts_and_connections(self):
        report=run_campaign(self.cid,self.profile,self.store,max_parallel=2)
        self.assertTrue(report['all_endpoints_reached'])
        for target in self.manifest['targets']:
            metrics=self.store.metrics(self.store.key(self.manifest,target))
            self.assertEqual(metrics['sample_count'],3)
            self.assertEqual(set(metrics['phases']),{'preflight','execute','verify'})
        self.assertEqual(len(self.calls()),4)

    def test_unknown_key_or_phase_cannot_create_samples(self):
        with self.assertRaises(core.PublishError):self.store.metrics('not-a-job')
        with self.assertRaises(core.PublishError):
            with self.store.measure(self.key,'publish'):pass
        self.assertEqual(self.store.metrics(self.key)['sample_count'],0)


if __name__=='__main__':unittest.main()
