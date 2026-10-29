import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from interaction_dashboard import render
from interaction_drafts import source as draft_source
from interaction_import import import_manual
from interaction_inbox import open_db
from interaction_observations import record
from publish_core import PublishError
from weibo_official_inbox import ingest, normalize, overview, sync, validate_config, _reader_lock
from weibo_official_probe import Client


class OfficialInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'
        self.credentials = Path(self.temp.name) / 'auth.json'
        self.credentials.write_text(json.dumps({'app_id': 'example-app', 'app_secret': 'example-secret'}))
        self.credentials.chmod(0o600)
        self.config = {'schema': 'weibo-official-inbox.v1', 'enabled': True, 'account': 'trial',
                       'uid': '101', 'credentials': str(self.credentials), 'work_ids': ['201'],
                       'not_before': '2026-10-27T00:00:00+00:00'}
        self.comment = {'id': '301', 'mid': '301', 'createdAt': 'Wed Oct 28 08:00:00 +0800 2026',
                        'postJumpLink': 'https://weibo.com/101/201', 'text': 'hello',
                        'user': {'id': '102', 'name': 'viewer'}}
        self.result = {'schema': 'weibo-official-probe-result.v1', 'account': 'trial', 'uid': '101',
                       'started_at': '2026-10-28T01:00:00+00:00',
                       'finished_at': '2026-10-28T01:00:01+00:00',
                       'state': 'read_probe_complete', 'social_writes': 0, 'automatic_sync': False,
                       'requests': [{'kind': 'received', 'work_id': None, 'comments': [copy.deepcopy(self.comment)], 'count': 1},
                                    {'kind': 'work', 'work_id': '201', 'comments': [copy.deepcopy(self.comment)], 'count': 1}]}
        self.calls = []

    def request(self, method, path, params):
        self.calls.append((method, path))
        if method == 'POST':
            return {'code': 0, 'data': {'uid': '101', 'token': 'in-memory-token'}}
        return {'code': 0, 'data': {'comments': [copy.deepcopy(self.comment)], 'count': 1}}

    def store(self, result=None, config=None):
        return ingest(self.root, config or self.config, result or self.result, '/private/probe.json')

    def summary(self):
        with open_db(self.root) as db:
            return overview(db)[0]

    def test_disabled_unknown_empty_future_configuration_refused(self):
        cases = [{}, {**self.config, 'enabled': False}, {**self.config, 'extra': 1},
                 {**self.config, 'work_ids': []}, {**self.config, 'not_before': '2026-10-27'},
                 {**self.config, 'not_before': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}]
        for config in cases:
            with self.subTest(config=config), self.assertRaises(PublishError):
                validate_config(config)

    def test_duplicate_endpoint_rows_count_once_and_cannot_enter_draft_queue(self):
        receipt = self.store()
        self.assertEqual((receipt['returned_rows'], receipt['distinct_comments']), (2, 1))
        self.assertEqual(receipt['canonical_imports'], 0)
        self.assertEqual(self.summary()['needs_context'], 1)
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM interaction_items').fetchone()[0], 0)
            with self.assertRaises(PublishError):
                draft_source(db, 'weibo', 'trial', '101', '301')

    def test_mid_is_not_the_work_id(self):
        data = normalize(self.result, self.config)
        self.assertEqual(data['items'][0]['work_id'], '201')
        self.assertEqual(data['items'][0]['comment_id'], '301')

    def test_different_bodies_preserved_and_never_overwrite_native_record(self):
        body = self.result['requests'][1]['comments'][0]
        body['text'] += ' https://example.org/audio'
        import_manual(self.root, {'platform': 'weibo', 'account': 'trial', 'channel_id': '101',
            'items': [{'comment_id': '301', 'video_id': '201', 'body': 'native original',
                       'is_own': False, 'thread_id': '300', 'parent_id': '300',
                       'author_channel': '102', 'published_at': '2026-10-28T00:00:00+00:00'}]})
        self.store()
        summary = self.summary()
        self.assertEqual(summary['representation_differences'], 1)
        self.assertEqual(summary['needs_context'], 0)
        with open_db(self.root) as db:
            item = db.execute('SELECT * FROM interaction_items').fetchone()
            self.assertEqual((item['body'], item['thread_id'], item['parent_id']), ('native original', '300', '300'))
            views = json.loads(db.execute('SELECT views_json FROM weibo_official_items').fetchone()[0])
            self.assertNotEqual(views['received']['body'], views['work']['body'])

    def test_historical_and_own_comments_not_new_viewer_candidates(self):
        for request in self.result['requests']:
            request['comments'][0]['createdAt'] = 'Mon Oct 26 08:00:00 +0800 2026'
        self.store()
        self.assertEqual((self.summary()['historical'], self.summary()['needs_context']), (1, 0))
        for request in self.result['requests']:
            request['comments'][0] = {**self.comment, 'id': '302', 'user': {'id': '101', 'name': 'self'}}
        self.store()
        self.assertEqual((self.summary()['own'], self.summary()['needs_context']), (1, 0))

    def test_empty_valid_result_is_partial_and_keeps_previously_discovered_items(self):
        self.store()
        for request in self.result['requests']:
            request.update(comments=[], count=0)
        receipt = self.store()
        self.assertEqual(receipt['state'], 'partial')
        self.assertEqual(receipt['distinct_comments'], 0)
        self.assertEqual(self.summary()['needs_context'], 1)

    def test_reimport_is_idempotent(self):
        self.store()
        self.assertEqual(self.store()['new_local_candidates'], 0)
        self.assertEqual(self.summary()['unique_seen'], 1)

    def test_incomplete_empty_document_and_write_claim_refused(self):
        for mutate in ('empty', 'running', 'written', 'auto', 'missing_request'):
            d = copy.deepcopy(self.result)
            if mutate == 'empty':d = {}
            if mutate == 'running':d['state'] = 'started'
            if mutate == 'written':d['social_writes'] = 1
            if mutate == 'auto':d['automatic_sync'] = True
            if mutate == 'missing_request':d['requests'] = []
            with self.subTest(mutate=mutate), self.assertRaises(PublishError):
                normalize(d, self.config)

    def test_wrong_uid_external_link_and_query_work_refused(self):
        for link in ('https://weibo.com/999/201', 'https://weibo.com.evil/101/201',
                     'https://weibo.com/101/202', 'https://weibo.com/101/201?token=example',
                     'http://weibo.com/101/201'):
            d = copy.deepcopy(self.result)
            d['requests'][1]['comments'][0]['postJumpLink'] = link
            with self.subTest(link=link), self.assertRaises(PublishError):
                normalize(d, self.config)
        with self.assertRaises(PublishError):
            normalize({**self.result, 'uid': '999'}, self.config)

    def test_same_id_author_work_or_time_conflict_refused_atomically(self):
        self.store()
        for key, value in (('author', '999'), ('work', '202'), ('time', 'Wed Oct 28 08:00:01 +0800 2026')):
            d = copy.deepcopy(self.result)
            for request in d['requests']:
                c = request['comments'][0]
                if key == 'author':c['user']['id'] = value
                if key == 'work':c['postJumpLink'] = 'https://weibo.com/101/' + value
                if key == 'time':c['createdAt'] = value
            with self.subTest(key=key), self.assertRaises(PublishError):
                self.store(d)
        with open_db(self.root) as db:
            r = db.execute('SELECT * FROM weibo_official_items').fetchone()
            self.assertEqual((r['work_id'], r['author_id'], r['published_at']), ('201', '102', '2026-10-28T00:00:00+00:00'))

    def test_conflicting_endpoint_identity_rejected(self):
        for key in ('author', 'time'):
            d = copy.deepcopy(self.result)
            c = d['requests'][1]['comments'][0]
            if key == 'author':c['user']['id'] = '999'
            else:c['createdAt'] = 'Wed Oct 28 08:00:01 +0800 2026'
            with self.subTest(key=key), self.assertRaises(PublishError):
                normalize(d, self.config)

    def test_wrong_query_work_even_without_duplicate_comment_rejected(self):
        self.result['requests'][1]['comments'][0].update(id='302', postJumpLink='https://weibo.com/101/202')
        with self.assertRaises(PublishError):self.store()

    def test_empty_body_invalid_time_and_future_evidence_rejected(self):
        for text in ('', '   ', '<br>'):
            d = copy.deepcopy(self.result)
            d['requests'][0]['comments'][0]['text'] = text
            with self.subTest(text=text), self.assertRaises(PublishError):normalize(d, self.config)
        d = copy.deepcopy(self.result)
        d['finished_at'] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with self.assertRaises(PublishError):normalize(d, self.config)
        d = copy.deepcopy(self.result)
        d['requests'][0]['comments'][0]['createdAt'] = 'unknown time'
        with self.assertRaises(PublishError):normalize(d, self.config)

    def test_native_identity_conflict_does_not_create_candidates(self):
        import_manual(self.root, {'platform': 'weibo', 'account': 'trial', 'channel_id': '101',
            'items': [{'comment_id': '301', 'video_id': '201', 'body': 'native original',
                       'is_own': False, 'author_channel': '999',
                       'published_at': '2026-10-28T00:00:00+00:00'}]})
        with self.assertRaises(PublishError):self.store()
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM weibo_official_items').fetchone()[0], 0)

    def test_duplicate_within_one_endpoint_rejected(self):
        self.result['requests'][0]['comments'].append(copy.deepcopy(self.comment))
        with self.assertRaises(PublishError):
            self.store()

    def test_cutoff_uid_and_alias_cannot_rebind(self):
        self.store()
        for changed in ({'not_before': '2026-10-26T00:00:00Z'}, {'uid': '999'}, {'account': 'alias'}):
            with self.subTest(changed=changed), self.assertRaises(PublishError):
                sync(self.root, {**self.config, **changed}, Client(self.request))
        self.assertEqual(self.calls, [])

    def test_old_evidence_cannot_replace_new_read(self):
        self.store()
        old = copy.deepcopy(self.result)
        old['started_at'] = '2026-10-28T00:30:00+00:00'
        old['finished_at'] = '2026-10-28T00:30:01+00:00'
        with self.assertRaises(PublishError):self.store(old)

    def test_sync_only_auth_and_two_gets_and_private_evidence(self):
        receipt = sync(self.root, self.config, Client(self.request))
        self.assertEqual([c[0] for c in self.calls], ['POST', 'GET', 'GET'])
        self.assertEqual(receipt['social_writes'], 0)
        p = Path(receipt['evidence'])
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('in-memory-token', p.read_text())
        self.assertEqual(receipt['canonical_imports'], 0)

    def test_wrong_auth_uid_stops_before_comment_reads(self):
        def wrong(method, path, params):
            self.calls.append((method, path))
            return {'code': 0, 'data': {'uid': '999', 'token': 'in-memory-token'}}
        with self.assertRaises(PublishError):sync(self.root, self.config, Client(wrong))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.summary()['state'], 'error')

    def test_failure_retains_old_candidates_and_last_success_without_retry(self):
        self.store()
        previous = self.summary()['last_success_at']
        def fail(method, path, params):
            self.calls.append((method, path))
            raise PublishError('a diagnostic containing private material must not reach output')
        with self.assertRaises(PublishError) as cm:sync(self.root, self.config, Client(fail))
        self.assertNotIn('private material', str(cm.exception))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.summary()['state'], self.summary()['needs_context'], self.summary()['last_success_at']),
                         ('error', 1, previous))
        self.assertIn('官方检查失败', render(self.root / 'interaction-inbox.sqlite3'))
        self.assertIn('已有记录可能过时', render(self.root / 'interaction-inbox.sqlite3'))

    def test_lock_blocks_second_sync_before_network(self):
        self.store()
        with _reader_lock(self.root, '101'):
            with self.assertRaises(PublishError):sync(self.root, self.config, Client(self.request))
        self.assertEqual(self.calls, [])

    def test_dashboard_private_text_optin_and_escapes_both_representations(self):
        for request in self.result['requests']:
            request['comments'][0]['user']['name'] = '<untrusted>'
            request['comments'][0]['text'] = 'only-in-private &lt;script&gt;alert(1)&lt;/script&gt;'
        self.store()
        plain = render(self.root / 'interaction-inbox.sqlite3')
        full = render(self.root / 'interaction-inbox.sqlite3', True)
        self.assertIn('官方已读（范围有限）', plain)
        self.assertIn('待补上下文 1', plain)
        self.assertNotIn('only-in-private', plain)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', full)
        self.assertNotIn('<script>alert(1)</script>', full)
        self.assertIn('https://weibo.com/101/201', full)
        self.assertIn('未进入回复草稿', full)

    def test_dashboard_one_account_row_with_supplemental_observation(self):
        import_manual(self.root, {'platform': 'weibo', 'account': 'trial', 'channel_id': '101',
            'items': [{'comment_id': '301', 'video_id': '201', 'body': 'native',
                       'is_own': False, 'author_channel': '102',
                       'published_at': '2026-10-28T00:00:00+00:00'}]})
        self.store()
        record(self.root, {'platform': 'weibo', 'account': 'trial', 'observed_at': self.result['finished_at'],
            'state': 'partial', 'scope': 'sample', 'reason': 'observation', 'next_action': 'check',
            'evidence': '/private/example.json', 'replies': []})
        page = render(self.root / 'interaction-inbox.sqlite3')
        self.assertEqual(page.count('<td>trial</td>'), 1)
        self.assertIn('官方已读（范围有限）', page)


if __name__ == '__main__':
    unittest.main()
