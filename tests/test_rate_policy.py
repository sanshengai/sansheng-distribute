import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_core import PublishError, Store, validate, write_json, run


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state')
        self.addCleanup(lambda: self.store.close())
        (Path(self.tmp.name) / 'source.txt').write_text('example')
        self.manifest = {'schema_version': 1, 'job_id': 'pacing', 'producer': 'test',
                         'content_status': 'final', 'source_root': self.tmp.name,
                         'documents': [{'id': 'source', 'path': 'source.txt', 'format': 'text'}], 'assets': [], 'media_slots': [], 'targets': [
            {'id': ident, 'platform': platform, 'account': account, 'endpoint': 'draft',
             'content_type': 'text', 'text': 'example'}
            for ident, platform, account in [('one','x','a'), ('two','x','b'), ('three','weibo','b')]]}
        validate(self.manifest, sealing=True)
        for t in self.manifest['targets']:
            self.store.prepare(self.manifest, t['id'])
            self.store.authorize(self.manifest, t['id'], '测试草稿')

    def state(self, ident):
        target = next(t for t in self.manifest['targets'] if t['id'] == ident)
        return self.store.status(self.store.key(self.manifest, target))['state']

    def test_platform_window_is_atomic_across_accounts_and_persists(self):
        profile = {'pacing': {'x': {'platform_seconds': 30}}}
        with patch('rate_policy.time.time', return_value=1000):
            self.store.begin(self.manifest, 'one', profile=profile)
            with self.assertRaisesRegex(PublishError, '间隔尚未到期'):
                self.store.begin(self.manifest, 'two', profile=profile)
            self.assertEqual(self.state('two'), 'prepared')
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM account_locks WHERE account=?', ('b',)).fetchone()[0], 0)
            self.store.begin(self.manifest, 'three', profile=profile)
        self.store.close()
        self.store = Store(Path(self.tmp.name) / 'state')
        with patch('rate_policy.time.time', return_value=1030):
            self.store.begin(self.manifest, 'two', profile=profile)
        self.assertEqual(self.state('two'), 'submission_unknown')

    def test_account_window_is_isolated_and_unknown_still_never_retries(self):
        profile = {'pacing': {'x': {'account_seconds': 30}}}
        with patch('rate_policy.time.time', return_value=1000):
            self.store.begin(self.manifest, 'one', profile=profile)
            self.store.begin(self.manifest, 'two', profile=profile)
            with self.assertRaisesRegex(PublishError, '不重复提交'):
                self.store.begin(self.manifest, 'one', profile=profile)

    def test_existing_window_survives_profile_removal_or_changed_scope(self):
        with patch('rate_policy.time.time', return_value=1000):
            self.store.begin(self.manifest, 'one', profile={'pacing': {'x': {'platform_seconds':30}}})
            for changed in ({}, {'pacing': {'x': {}}}, {'pacing': {'x': {'account_seconds': 1}}}):
                with self.subTest(profile=changed), self.assertRaisesRegex(PublishError, '间隔尚未到期'):
                    self.store.begin(self.manifest, 'two', profile=changed)
            self.assertEqual(self.state('two'), 'prepared')
        with patch('rate_policy.time.time', return_value=1030):
            self.store.begin(self.manifest, 'two', profile={})

    def test_bad_configuration_fails_without_reservation_or_submission(self):
        for value in (0, True, -1, 86401, '5', None):
            with self.subTest(value=value), self.assertRaises(PublishError):
                self.store.begin(self.manifest, 'one', profile={'pacing': {'x': {'platform_seconds': value}}})
            self.assertEqual(self.state('one'), 'prepared')
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM pacing_windows').fetchone()[0], 0)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM account_locks').fetchone()[0], 0)

    def test_real_dispatch_does_not_call_second_adapter_during_window(self):
        adapter = Path(self.tmp.name) / 'fake_adapter.py'
        adapter.write_text("""import json,sys
from pathlib import Path
p=Path(sys.argv[-1]); req=json.loads(p.read_text()); t=req['target']
with p.with_name('adapter-calls.log').open('a') as f: f.write(t['id']+' '+sys.argv[1]+'\\n')
print(json.dumps({'manifest_digest':req['manifest_digest'],'target_id':t['id'],
                  'platform':t['platform'],'account':t['account'],'state':'draft_ready',
                  'remote_id':'remote-'+t['id'],'content_verified':True}))
""")
        path = Path(self.tmp.name) / 'manifest.json'
        write_json(path, self.manifest)
        profile = {'pacing': {'x': {'platform_seconds': 30}},
                   'accounts': {'a': {'platforms': ['x']}, 'b': {'platforms': ['x']}},
                   'adapters': {'x': {'enabled_capabilities': ['text:draft'],
                             'execute': [sys.executable, str(adapter), 'execute'],
                             'verify': [sys.executable, str(adapter), 'verify']}}}
        with patch('rate_policy.time.time', return_value=1000):
            run(path, 'one', profile, self.store)
            with self.assertRaisesRegex(PublishError, '间隔尚未到期'):
                run(path, 'two', profile, self.store)
        calls = (self.store.directory / 'requests' / 'adapter-calls.log').read_text().splitlines()
        self.assertEqual(calls, ['one execute', 'one verify'])
        self.assertEqual(self.state('two'), 'prepared')

    def test_unknown_platform_rejected_before_submission(self):
        with self.assertRaisesRegex(PublishError, '未知平台'):
            self.store.begin(self.manifest, 'one', profile={'pacing': {'unknown-platform': {'platform_seconds': 30}}})
        self.assertEqual(self.state('one'), 'prepared')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM pacing_windows').fetchone()[0], 0)

    def test_no_policy_preserves_existing_behavior(self):
        self.store.begin(self.manifest, 'one')
        self.store.begin(self.manifest, 'two')
        self.assertEqual(self.state('one'), 'submission_unknown')
        self.assertEqual(self.state('two'), 'submission_unknown')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM pacing_windows').fetchone()[0], 0)


if __name__ == '__main__': unittest.main()
