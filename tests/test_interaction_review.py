"""Exercise local review over real HTTP and the existing controlled queue."""
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from interaction_drafts import prepare as draft_prepare, withdraw
from interaction_import import import_manual
from interaction_inbox import open_db
from interaction_review import ReviewDesk, ReviewServer
import interaction_youtube_queue as queue
from publish_core import PublishError, digest, write_json
from youtube_comments_candidate import account_lock
from test_interaction_youtube_queue import ACCOUNT, AUTHOR, CHANNEL, VIDEO, Client, item


class MultiClient(Client):
    def call(self, method, url, **kwargs):
        query = parse_qs(urlsplit(url).query)
        if method == 'GET' and urlsplit(url).path.endswith('/commentThreads'):
            key = query['id'][0]
            root = key.removeprefix('thread-')
            self.calls.append((method, url))
            return 200, {}, json.dumps({'items': [{'id': key, 'snippet': {
                'channelId': CHANNEL, 'videoId': VIDEO, 'canReply': True,
                'topLevelComment': {'id': root}}}]}).encode()
        return super().call(method, url, **kwargs)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        self.client = MultiClient()
        self.add_comment('one')
        self.add_comment('two')
        self.profile = Path(self.tmp.name) / 'profile.json'
        write_json(self.profile, {'accounts': {ACCOUNT: {'platforms': ['youtube'],
            'public_identities': {'youtube': CHANNEL}, 'youtube_token_env': 'REVIEW_TEST_TOKEN'}}})
        self.env = patch.dict('os.environ', {'REVIEW_TEST_TOKEN': 'offline-token'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.desk = ReviewDesk(self.root, profile=str(self.profile), account=ACCOUNT,
                               client_factory=lambda _: self.client)

    def add_comment(self, key, *, platform='youtube', own=False):
        self.client.items[key] = item(key)
        import_manual(self.root, {'platform': platform, 'account': ACCOUNT,
            'channel_id': CHANNEL, 'items': [{'comment_id': key, 'video_id': VIDEO,
                'thread_id': 'thread-' + key, 'body': 'Question?', 'author_channel': AUTHOR,
                'is_own': own, 'published_at': '2026-10-29T01:00:00Z'}]})

    def selection(self, key='one', reply='An answer.', platform='youtube'):
        row = next(r for r in self.desk.snapshot()['items'] if r['comment_id'] == key and r['platform'] == platform)
        return {**{k: row[k] for k in ('platform', 'account', 'channel_id', 'comment_id', 'source_digest', 'draft_key')},
                'reply': reply}

    def test_no_credentials_without_config_and_no_network_on_view_or_confirm(self):
        desk = ReviewDesk(self.root)
        self.assertFalse(desk.snapshot()['items'][0]['sendable'])
        receipt = self.desk.confirm([self.selection()])
        self.assertEqual((receipt['confirmed'], receipt['sendable'], receipt['social_writes']), (1, 1, 0))
        self.assertEqual(self.client.calls, [])
        with open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM interaction_items WHERE local_status='handled'").fetchone()[0], 0)

    def test_two_edited_confirmed_replies_send_and_read_back_without_repeating(self):
        receipt = self.desk.confirm([self.selection('one', 'First edited answer.'), self.selection('two', 'Second answer.')])
        result = self.desk.dispatch(receipt['batch_id'])
        self.assertEqual([r['state'] for r in result['results']], ['verified', 'verified'])
        self.assertEqual([p['snippet']['textOriginal'] for p in self.client.posts], ['First edited answer.', 'Second answer.'])
        self.assertEqual([p['snippet']['parentId'] for p in self.client.posts], ['one', 'two'])
        self.desk.dispatch(receipt['batch_id'])
        self.desk.dispatch(receipt['batch_id'], reconcile=True)
        self.assertEqual(len(self.client.posts), 2)
        self.assertTrue(all(r['handled'] and not r['eligible'] for r in self.desk.snapshot()['items']))

    def test_shape_empty_duplicates_and_missing_confirmation_fail(self):
        for value in ([], None, [self.selection()] * 2, [dict(self.selection(), injected=True)]):
            with self.assertRaises(PublishError): self.desk.confirm(value)
        with self.assertRaises(PublishError): self.desk.dispatch('0' * 64)
        self.assertEqual(self.client.calls, [])

    def test_missing_and_empty_inbox_do_not_invent_items_or_approvals(self):
        empty = self.root / 'empty-inbox'
        with self.assertRaises(PublishError): ReviewDesk(empty)
        with open_db(empty): pass
        desk = ReviewDesk(empty)
        self.assertEqual(desk.snapshot()['items'], [])
        self.assertEqual(desk.snapshot()['batches'], [])
        with self.assertRaises(PublishError): desk.confirm([])

    def test_unrelated_roots_do_not_become_conversation_when_thread_identity_is_missing(self):
        with open_db(self.root) as db: db.execute("UPDATE interaction_items SET thread_id=''")
        self.assertTrue(all(not r['context'] for r in self.desk.snapshot()['items']))

    def test_history_baseline_is_never_presented_as_a_verified_reply(self):
        import_manual(self.root, {'platform': 'youtube', 'account': ACCOUNT, 'channel_id': CHANNEL,
            'items': [{'comment_id': 'history', 'video_id': VIDEO, 'body': 'Old question',
                       'is_own': False, 'published_at': '2020-01-01T00:00:00Z'}]},
            baseline_before='2026-01-01T00:00:00Z')
        row = next(r for r in self.desk.snapshot()['items'] if r['comment_id'] == 'history')
        self.assertEqual(row['status_label'], '历史基线')
        self.assertFalse(row['eligible'])
        self.assertIn('不代表已回复', row['reason'])

    def test_source_or_draft_changed_in_another_tab_cannot_be_confirmed(self):
        old = self.selection()
        draft_prepare(self.root, 'youtube', ACCOUNT, CHANNEL, 'one', 'Other tab')
        self.assertEqual(self.desk.confirm([old])['confirmed'], 0)
        fresh = self.selection()
        with open_db(self.root) as db: db.execute("UPDATE interaction_items SET body='Edited question' WHERE comment_id='one'")
        self.assertEqual(self.desk.confirm([fresh])['confirmed'], 0)
        # After seeing the changed original, an explicit fresh confirmation works.
        self.assertEqual(self.desk.confirm([self.selection()])['confirmed'], 1)

    def test_modified_and_restored_words_do_not_restore_an_old_confirmation(self):
        old = self.desk.confirm([self.selection(reply='A')])
        self.desk.confirm([self.selection(reply='B')])
        self.desk.confirm([self.selection(reply='A')])
        result = self.desk.dispatch(old['batch_id'])
        self.assertEqual(result['results'][0]['state'], 'blocked')
        self.assertEqual(self.client.posts, [])

    def test_edited_draft_revokes_old_cli_grant(self):
        accepted = self.desk.confirm([self.selection(reply='A')])['accepted'][0]
        key = accepted['draft_key']
        plan = queue.prepare(self.root, key, ACCOUNT, CHANNEL)['plan_key']
        queue.authorize(self.root, key, ACCOUNT, CHANNEL, 'Explicit test authorization')
        self.assertTrue((self.root / ('grant-' + plan + '.json')).exists())
        self.desk.confirm([self.selection(reply='B')])
        self.assertFalse((self.root / ('grant-' + plan + '.json')).exists())

    def test_authorize_checks_draft_revision_under_send_lock(self):
        approved = self.desk.confirm([self.selection()])['accepted'][0]
        key = approved['draft_key']
        queue.prepare(self.root, key, ACCOUNT, CHANNEL)
        with self.assertRaises(PublishError):
            queue.authorize(self.root, key, ACCOUNT, CHANNEL, 'Test approval', expected_created_at='older-revision')

    def test_guard_rechecks_draft_after_binding_before_post(self):
        approved = self.desk.confirm([self.selection()])['accepted'][0]
        key = approved['draft_key']
        queue.prepare(self.root, key, ACCOUNT, CHANNEL)
        queue.authorize(self.root, key, ACCOUNT, CHANNEL, 'Test approval')
        original_send = queue.send
        def intervening_send(*args, **kwargs):
            with open_db(self.root) as db: db.execute('DELETE FROM interaction_drafts WHERE draft_key=?', (key,))
            return original_send(*args, **kwargs)
        with patch.object(queue, 'send', intervening_send):
            with self.assertRaises(PublishError): queue.execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        self.assertEqual(self.client.posts, [])

    def test_withdraw_cannot_race_active_send_and_revokes_unattempted_grant(self):
        accepted = self.desk.confirm([self.selection()])['accepted'][0]
        key = accepted['draft_key']
        plan = queue.prepare(self.root, key, ACCOUNT, CHANNEL)['plan_key']
        queue.authorize(self.root, key, ACCOUNT, CHANNEL, 'Test approval')
        started, finished = threading.Event(), threading.Event()
        result = []
        def worker():
            started.set()
            result.append(withdraw(self.root, key))
            finished.set()
        with account_lock(self.root, CHANNEL):
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(.1))
        thread.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual(result[0]['withdrawn'], 1)
        self.assertFalse((self.root / ('grant-' + plan + '.json')).exists())

    def test_unknown_freezes_edits_even_if_local_draft_projection_is_lost(self):
        receipt = self.desk.confirm([self.selection()])
        self.client.mode = 'post_timeout'
        self.assertEqual(self.desk.dispatch(receipt['batch_id'])['results'][0]['state'], 'unknown')
        self.desk.dispatch(receipt['batch_id'])
        with open_db(self.root) as db: db.execute('DELETE FROM interaction_drafts')
        self.assertEqual(self.desk.confirm([self.selection(reply='Replacement')])['confirmed'], 0)
        self.assertEqual(len(self.client.posts), 1)

    def test_individual_failure_does_not_stop_the_remaining_batch(self):
        receipt = self.desk.confirm([self.selection('one'), self.selection('two')])
        self.client.items.pop('one')
        result = self.desk.dispatch(receipt['batch_id'])
        self.assertEqual([r['state'] for r in result['results']], ['unknown', 'verified'])
        self.assertEqual(len(self.client.posts), 1)

    def test_unsupported_platform_is_confirmable_but_never_sent(self):
        self.add_comment('ali', platform='alipay')
        receipt = self.desk.confirm([self.selection('ali', platform='alipay')])
        self.assertEqual(receipt['sendable'], 0)
        self.assertEqual(self.desk.dispatch(receipt['batch_id'])['results'][0]['state'], 'manual')
        self.assertEqual(self.client.calls, [])

    def test_own_handled_and_verified_records_are_excluded(self):
        self.add_comment('own', own=True)
        self.assertNotIn('own', [r['comment_id'] for r in self.desk.snapshot()['items']])
        with open_db(self.root) as db: db.execute("UPDATE interaction_items SET local_status='handled' WHERE comment_id='one'")
        with self.assertRaises(PublishError): self.desk.confirm([self.selection()])

    def test_tamper_and_expired_confirmation_fail_but_read_only_recovery_remains(self):
        receipt = self.desk.confirm([self.selection()])
        key = receipt['batch_id']
        with open_db(self.root) as db:
            original = dict(db.execute('SELECT * FROM interaction_review_batches').fetchone())
            db.execute("UPDATE interaction_review_batches SET payload_json='[]'")
        with self.assertRaises(PublishError): self.desk.dispatch(key)
        old_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        items = json.loads(original['payload_json'])
        old_key = digest({'items': items, 'confirmed_at': old_time, 'nonce': original['nonce']})
        with open_db(self.root) as db:
            db.execute('UPDATE interaction_review_batches SET batch_id=?,payload_json=?,confirmed_at=?',
                       (old_key, original['payload_json'], old_time))
        with self.assertRaises(PublishError): self.desk.dispatch(old_key)
        self.desk.dispatch(old_key, reconcile=True)
        self.assertEqual(self.client.posts, [])

    def test_loopback_http_requires_exact_origin_and_token_and_never_sends_on_get(self):
        server = ReviewServer(self.desk)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse(); data = response.read(); conn.close()
            return response.status, response.headers, data
        status, headers, html = request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(server.token.encode(), html)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(request('GET', '/api/state')[0], 403)
        auth = {'X-Review-Token': server.token, 'Origin': server.origin, 'Content-Type': 'application/json'}
        self.assertEqual(request('GET', '/api/state', headers=auth)[0], 200)
        navigation = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
        self.assertEqual(request('GET', '/', headers=navigation)[0], 200)
        self.assertEqual(request('GET', '/', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(request('GET', '/api/state', headers=dict(auth, **navigation))[0], 403)
        self.assertEqual(request('GET', '/api/state', headers=dict(auth, Host='attacker.invalid'))[0], 403)
        self.assertEqual(request('POST', '/api/confirm', '{}', dict(auth, Origin='https://attacker.invalid'))[0], 403)
        self.assertEqual(request('POST', '/api/confirm', '{}', {k: v for k, v in auth.items() if k != 'Origin'})[0], 403)
        self.assertEqual(request('POST', '/api/confirm', '{}', dict(auth, **{'X-Review-Token': 'wrong'}))[0], 403)
        self.assertEqual(request('POST', '/api/confirm', '{}', dict(auth, **{'Content-Type': 'text/plain'}))[0], 415)
        self.assertEqual(request('POST', '/api/confirm', '{bad', auth)[0], 400)
        self.assertEqual(request('POST', '/api/send', '{}', auth)[0], 400)
        self.assertEqual(request('POST', '/api/confirm', json.dumps({'items': []}), auth)[0], 400)
        self.assertEqual(self.client.calls, [])
        body = json.dumps({'items': [self.selection()]})
        status, _, response = request('POST', '/api/confirm', body, auth)
        self.assertEqual(status, 200)
        self.assertEqual(self.client.posts, [])
        batch = json.loads(response)['batch_id']
        status, _, response = request('POST', '/api/send', json.dumps({'batch_id': batch}), auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)['results'][0]['state'], 'verified')


if __name__ == '__main__':
    unittest.main()
