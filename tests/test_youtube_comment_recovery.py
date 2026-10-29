import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_core import PublishError, digest, write_json
from youtube_comments_candidate import SCOPE, _private_dir, _receipt_key, make_intent, send
from youtube_comment_recovery import run_cli, search

CHANNEL = 'UC' + 'a' * 22
VIDEO = 'abcdefghijk'
PARENT = 'parent-comment'
THREAD = 'original-thread'
ATTEMPT = '2026-10-29T04:00:00+00:00'
CLOCK = lambda: datetime(2026, 10, 29, 4, 5, tzinfo=timezone.utc)


class Client:
    def __init__(self, intent):
        self.intent = intent
        self.calls = []
        self.items = [self.item('remote')]
        self.more = None
        self.wrong_video = False
        self.fail = False
        self.scope = {SCOPE}

    def item(self, remote):
        snippet = {'textOriginal': self.intent['text'], 'authorChannelId': {'value': CHANNEL},
                   'publishedAt': '2026-10-29T04:00:05Z'}
        if self.intent['parent_id']: snippet['parentId'] = self.intent['parent_id']
        return {'id': remote, 'snippet': snippet}

    def granted_scopes(self): return self.scope

    def call(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method != 'GET': raise AssertionError('recovery must never write')
        q = parse_qs(urlsplit(url).query)
        path = urlsplit(url).path
        if path.endswith('/channels'):
            payload = {'items': [{'id': CHANNEL}]}
        elif self.fail:
            raise PublishError('private-provider-error')
        elif path.endswith('/commentThreads') and 'id' in q:
            remote = q['id'][0]
            payload = {'items': [{'id': remote, 'snippet': {'channelId': CHANNEL,
                'videoId': 'zzzzzzzzzzz' if self.wrong_video else VIDEO,
                'topLevelComment': {'id': PARENT if self.intent['parent_id'] else remote.removeprefix('thread-')}}}]}
        elif path.endswith('/comments') and 'id' in q:
            payload = {'items': [self.item(q['id'][0])]}
        else:
            rows = self.items if self.intent['parent_id'] else [
                {'id': 'thread-' + item['id'], 'snippet': {'topLevelComment': item}} for item in self.items]
            payload = {'items': rows}
            if self.more: payload['nextPageToken'] = self.more
        return 200, {}, json.dumps(payload).encode()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = _private_dir(Path(self.tmp.name) / 'state')
        self.intent = make_intent(CHANNEL, VIDEO, 'Thanks for your question.', parent_id=PARENT, parent_thread_id=THREAD)
        self.client = Client(self.intent)
        self.save()

    def save(self, **kwargs):
        self.receipt = self.root / (_receipt_key(self.intent) + '.json')
        write_json(self.receipt, dict(state='unknown', intent_digest=digest(self.intent), attempted_at=ATTEMPT, **kwargs))

    def find(self, **kwargs):
        return search(self.client, self.intent, state_dir=self.root, clock=CLOCK, **kwargs)

    def test_single_candidate_independently_read_but_receipt_preserved_and_send_still_frozen(self):
        before = self.receipt.read_bytes()
        out = self.find()
        self.assertEqual([x['remote_id'] for x in out['candidates']], ['remote'])
        self.assertTrue(out['pages_complete']); self.assertTrue(out['needs_review'])
        self.assertEqual(out['state'], 'unknown'); self.assertFalse(out['receipt_changed'])
        self.assertEqual(before, self.receipt.read_bytes())
        self.assertTrue(all(method == 'GET' for method, _ in self.client.calls))
        old = len(self.client.calls)
        receipt = send(self.client, self.intent, approved_digest=digest(self.intent), state_dir=self.root, allow_write=True)
        self.assertEqual(receipt['state'], 'unknown')
        self.assertEqual(len(self.client.calls), old)

    def test_wrong_author_parent_text_time_or_work_cannot_become_candidate(self):
        for field, value in [('authorChannelId', {'value': 'other'}), ('parentId', 'wrong'),
                             ('textOriginal', 'Different'), ('publishedAt', '2026-10-28T04:00:00Z'),
                             ('publishedAt', '2026-10-29T04:09:00Z')]:
            with self.subTest(field=field, value=value):
                self.client.items = [self.client.item('remote')]
                self.client.items[0]['snippet'][field] = value
                self.assertEqual(self.find()['candidates'], [])
        self.client.items = [self.client.item('remote')]; self.client.wrong_video = True
        self.assertEqual(self.find()['candidates'], [])

    def test_empty_duplicate_multiple_pages_and_budgets_remain_unknown(self):
        self.client.items = []
        self.assertEqual(self.find()['candidates'], [])
        self.client.items = [self.client.item('one'), self.client.item('two')]
        out = self.find(); self.assertEqual(len(out['candidates']), 2); self.assertEqual(out['state'], 'unknown')
        self.client.items = [self.client.item('one')] * 3
        self.assertEqual(len(self.find()['candidates']), 1)
        self.client.more = 'repeat'
        out = self.find(max_pages=1)
        self.assertFalse(out['pages_complete']); self.assertEqual(out['pages'], 1)
        self.assertEqual(self.find()['reason'], 'repeated_page_token')
        self.client.more = None
        self.client.items = [self.client.item(str(i)) for i in range(20)]
        out = self.find()
        self.assertEqual(out['verification_probes'], 5)
        self.assertFalse(out['pages_complete'])

    def test_bad_or_missing_receipt_and_scope_fail_before_search_and_preserve_history(self):
        with self.assertRaises(PublishError):
            search(self.client, self.intent, state_dir=Path(__file__).resolve().parents[1])
        for value in (None, {}, []):
            with self.subTest(value=value), self.assertRaises(PublishError):
                search(self.client, value, state_dir=self.root)
        self.assertEqual(self.client.calls, [])
        with self.assertRaises(PublishError): self.find(max_pages=True)
        self.client.scope = set()
        with self.assertRaises(PublishError): self.find()
        self.client.scope = {SCOPE}
        self.receipt.unlink()
        self.client.calls.clear()
        with self.assertRaises(PublishError): self.find()
        self.assertEqual(self.client.calls, [])
        for change in ({'state': 'verified'}, {'intent_digest': 'wrong'}, {'attempted_at': 'not-time'},
                       {'attempted_at': '2099-01-01T00:00:00Z'}):
            write_json(self.receipt, {**dict(state='unknown', intent_digest=digest(self.intent), attempted_at=ATTEMPT), **change})
            self.client.calls.clear()
            with self.assertRaises(PublishError): self.find()
            self.assertEqual(self.client.calls, [])

    def test_top_level_partial_identity_and_read_failure(self):
        self.intent = make_intent(CHANNEL, VIDEO, 'A top-level test comment.')
        self.client = Client(self.intent); self.save(thread_id='thread-remote')
        self.assertEqual(len(self.find()['candidates']), 1)
        self.save(thread_id='thread-other')
        self.assertEqual(self.find()['candidates'], [])
        self.save(); self.client.fail = True
        out = self.find()
        self.assertEqual(out['reason'], 'read_failed'); self.assertTrue(out['read_failed'])
        self.assertNotIn('private-provider-error', json.dumps(out))

    def test_cli_invalid_unattempted_plan_rejects_before_credentials_or_client(self):
        profile = {'accounts': {'owner': {'platforms': ['youtube'], 'public_identities': {'youtube': CHANNEL}}}}
        key = digest(self.intent)
        write_json(self.root / ('plan-' + key + '.json'), {'account': 'owner', 'intent': self.intent})
        self.receipt.unlink()
        with patch('youtube_comment_recovery.load_profile', return_value=profile), \
             patch('youtube_comment_recovery.config', side_effect=AssertionError('must not refresh')):
            with self.assertRaises(PublishError):
                run_cli(['--profile', '/unused', '--account', 'owner', '--state-dir', str(self.root), '--key', key])


if __name__ == '__main__': unittest.main()
