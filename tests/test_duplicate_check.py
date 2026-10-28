"""Offline repeat-candidate checks, using the real ledger and sealed manifests."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from duplicate_check import find_candidates
from publish_core import PublishError, Store, validate


class DuplicateCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'video.mp4').write_bytes(b'movie fixture')
        (self.root / 'other.mp4').write_bytes(b'other fixture')
        self.state = self.root / 'state'
        self.store = Store(self.state)
        self.addCleanup(self.store.close)

    def manifest(self, *, title='One', platform='x', account='owner', file='video.mp4'):
        value = {'schema_version': 1, 'job_id': 'demo', 'producer': 'test', 'content_status': 'final',
                 'source_root': str(self.root), 'documents': [], 'media_slots': [],
                 'assets': [{'id': 'video', 'path': file, 'kind': 'video'}],
                 'targets': [{'id': 'clip', 'platform': platform, 'account': account,
                              'endpoint': 'publish', 'content_type': 'video',
                              'title': title, 'text': 'example', 'asset_ids': ['video']}]}
        validate(value, sealing=True)
        return value

    def old_job(self, manifest, state='submission_unknown'):
        job = self.store.prepare(manifest, 'clip')
        self.store.db.execute('UPDATE jobs SET state=? WHERE key=?', (state, job['key']))
        self.store.db.commit()
        return job['key']

    def test_renamed_same_video_is_candidate_not_same_task(self):
        key = self.old_job(self.manifest())
        result = find_candidates(self.manifest(title='Changed title'), 'clip', self.state)
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(result['candidates'][0]['job_key'], key)
        self.assertEqual(result['candidates'][0]['category'], '需核验')
        self.assertFalse(result['candidates'][0]['same_task'])
        self.assertTrue(result['coverage_complete_for_local_rows'])

    def test_same_task_is_explicit_and_existing_published_is_separate(self):
        original = self.manifest()
        key = self.old_job(original, 'published')
        result = find_candidates(original, 'clip', self.state)
        self.assertEqual(result['candidates'][0]['job_key'], key)
        self.assertEqual(result['candidates'][0]['category'], '已记录发布')
        self.assertTrue(result['candidates'][0]['same_task'])

    def test_other_account_platform_and_other_video_do_not_match(self):
        self.old_job(self.manifest())
        for other in (self.manifest(account='other'), self.manifest(platform='weibo'),
                      self.manifest(file='other.mp4')):
            with self.subTest(target=other['targets'][0], file=other['assets'][0]['path']):
                result = find_candidates(other, 'clip', self.state)
                self.assertEqual(result['candidate_count'], 0)
                self.assertIn('候选为零不代表从未发布', result['notice'])

    def test_missing_or_changed_snapshot_cannot_mean_no_history(self):
        key = self.old_job(self.manifest())
        snapshot = self.state / 'snapshots' / (key + '.json')
        for mode in ('tamper', 'missing'):
            if mode == 'tamper':
                data = json.loads(snapshot.read_text())
                data['targets'][0]['title'] = 'modified'
                snapshot.write_text(json.dumps(data))
            else:
                snapshot.unlink()
            result = find_candidates(self.manifest(title='Changed'), 'clip', self.state)
            self.assertFalse(result['coverage_complete_for_local_rows'])
            self.assertEqual(result['unavailable_snapshot_count'], 1)
            self.assertEqual(result['candidate_count'], 0)

    def test_changed_input_and_missing_ledger_refused(self):
        manifest = self.manifest()
        (self.root / 'video.mp4').write_bytes(b'changed')
        with self.assertRaisesRegex(PublishError, '源文件已改变'):
            find_candidates(manifest, 'clip', self.state)
        (self.root / 'video.mp4').write_bytes(b'movie fixture')
        absent = self.root / 'never-created'
        with self.assertRaisesRegex(PublishError, '账本不存在'):
            find_candidates(manifest, 'clip', absent)
        self.assertFalse(absent.exists())

    def test_cli_is_read_only_and_has_no_action_path(self):
        key = self.old_job(self.manifest())
        current = self.manifest(title='Changed')
        input_file = self.root / 'manifest.json'
        input_file.write_text(json.dumps(current))
        before = self.store.status(key)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/duplicate_check.py'),
                                 '--manifest', str(input_file), '--target', 'clip',
                                 '--state-dir', str(self.state)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output['ok'])
        self.assertEqual(output['result']['candidate_count'], 1)
        self.assertEqual(self.store.status(key), before)
        self.assertIn('不能因本查询自动重发', output['result']['notice'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
