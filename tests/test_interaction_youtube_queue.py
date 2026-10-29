"""Exercise the public queue commands with an official-schema API double."""
import concurrent.futures
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
from interaction_dashboard import render
from interaction_import import import_manual
from interaction_inbox import open_db
from interaction_youtube_queue import authorize, execute, prepare, run_cli
from publish_core import PublishError, digest, read_json, write_json
from youtube_comments_candidate import SCOPE, _receipt_key, make_intent, reconcile, send
from youtube_comments_candidate import run_cli as comments_cli

CHANNEL = 'UC' + 'a' * 22
AUTHOR = 'UC' + 'b' * 22
VIDEO, PARENT, THREAD = 'abcdefghijk', 'viewer-root', 'separate-thread'
ACCOUNT = 'owner'


def item(key, body='Question?', author=AUTHOR, parent=None, published='2026-10-29T01:00:00Z'):
    snippet = dict(textOriginal=body, authorChannelId={'value': author}, publishedAt=published)
    if parent:
        snippet['parentId'] = parent
    return {'id': key, 'snippet': snippet}


class Client:
    def __init__(self):
        self.calls, self.posts = [], []
        self.items = {PARENT: item(PARENT)}
        self.reply_pages = [[]]
        self.mode = 'success'

    def granted_scopes(self):
        return {SCOPE}

    def call(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url))
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        resource = parts.path.rsplit('/', 1)[-1]
        if method == 'POST':
            payload = json.loads(data)
            self.posts.append(payload)
            key = 'sent-' + str(len(self.posts))
            snippet = payload['snippet']
            self.items[key] = item(key, snippet['textOriginal'], CHANNEL, snippet['parentId'])
            if self.mode == 'post_timeout':
                raise PublishError('response lost after acceptance')
            result = {'id': key}
        elif resource == 'channels':
            result = {'items': [{'id': CHANNEL}]}
        elif resource == 'videos':
            result = {'items': [{'snippet': {'channelId': CHANNEL}}]}
        elif resource == 'commentThreads':
            result = {'items': [{'id': THREAD, 'snippet': dict(
                channelId=CHANNEL, videoId=VIDEO, canReply=True, topLevelComment={'id': PARENT})}]}
        elif resource == 'comments' and 'id' in query:
            key = query['id'][0]
            if key.startswith('sent-') and self.mode == 'readback_lost':
                raise PublishError('temporary read failure')
            result = {'items': [self.items[key]] if key in self.items else []}
        elif resource == 'comments' and 'parentId' in query:
            page = int(query.get('pageToken', ['0'])[0])
            result = {'items': self.reply_pages[page]}
            if page + 1 < len(self.reply_pages):
                result['nextPageToken'] = str(page + 1)
        else:
            raise AssertionError((method, resource, query))
        return 200, {}, json.dumps(result).encode()


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        self.client = Client()
        self.add_source(PARENT)

    def add_source(self, key, parent=None):
        source = item(key, parent=parent)
        self.client.items[key] = source
        import_manual(self.root, dict(platform='youtube', account=ACCOUNT, channel_id=CHANNEL, items=[dict(
            comment_id=key, video_id=VIDEO, thread_id=THREAD, parent_id=parent,
            body=source['snippet']['textOriginal'], author_channel=AUTHOR, is_own=False,
            published_at=source['snippet']['publishedAt'])]))

    def bound(self, comment=PARENT, reply='A concrete answer.'):
        draft = draft_prepare(self.root, 'youtube', ACCOUNT, CHANNEL, comment, reply)
        result = prepare(self.root, draft['draft_key'], ACCOUNT, CHANNEL)
        return draft['draft_key'], result['plan_key']

    def authorized(self, **kwargs):
        key, plan = self.bound(**kwargs)
        authorize(self.root, key, ACCOUNT, CHANNEL, 'Allow this test reply.')
        return key, plan

    def test_cli_draft_authorize_send_reconcile_and_local_status(self):
        key, _ = self.bound()
        profile = Path(self.tmp.name) / 'profile.json'
        write_json(profile, {'accounts': {ACCOUNT: dict(platforms=['youtube'],
            public_identities={'youtube': CHANNEL}, youtube_token_env='QUEUE_TEST_TOKEN')}})
        statement = Path(self.tmp.name) / 'statement.txt'
        statement.write_text('Allow this test reply.')
        statement.chmod(0o600)
        prefix = ['--profile', str(profile), '--account', ACCOUNT, '--state-dir', str(self.root)]
        local = lambda _: self.fail('local command attempted to construct an API client')
        self.assertEqual(run_cli(prefix + ['status', '--key', key], client_factory=local)['state'], 'not_attempted')
        with self.assertRaises((PublishError, OSError)):
            execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        run_cli(prefix + ['authorize', '--key', key, '--user-statement-file', str(statement)], client_factory=local)
        with patch.dict('os.environ', {'QUEUE_TEST_TOKEN': 'offline-token'}):
            result = run_cli(prefix + ['send', '--key', key], client_factory=lambda _: self.client)
            self.assertEqual(result['state'], 'verified')
            self.assertTrue(result['inbox']['recorded'])
            self.assertEqual(run_cli(prefix + ['send', '--key', key], client_factory=lambda _: self.client)['state'], 'verified')
            self.assertTrue(run_cli(prefix + ['reconcile', '--key', key], client_factory=lambda _: self.client)['currently_verified'])
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(run_cli(prefix + ['status', '--key', key], client_factory=local)['state'], 'verified')
        board = render(self.root / 'interaction-inbox.sqlite3')
        self.assertIn('YouTube 回复发送队列（1 条）', board)
        self.assertIn('历史回执已核验', board)
        self.assertNotIn('A concrete answer.', board)

    def test_changed_cache_source_or_draft_is_rejected_before_post(self):
        for column, value in [('body', 'Edited'), ('author_channel', CHANNEL), ('parent_id', 'other-parent')]:
            with self.subTest(column=column), tempfile.TemporaryDirectory() as directory:
                previous = self.root
                self.root = Path(directory) / 'state'
                self.add_source(PARENT)
                key, _ = self.authorized()
                with open_db(self.root) as db:
                    db.execute('UPDATE interaction_items SET ' + column + '=?', (value,))
                with self.assertRaises(PublishError):
                    execute(self.root, key, ACCOUNT, CHANNEL, self.client)
                self.assertEqual(len(self.client.posts), 0)
                self.root = previous
        key, _ = self.authorized()
        with open_db(self.root) as db:
            db.execute("UPDATE interaction_drafts SET reply='Changed'")
        with self.assertRaises(PublishError):
            execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        self.assertEqual(len(self.client.posts), 0)

    def test_live_comment_change_deleted_or_different_author_is_rejected(self):
        key, _ = self.authorized()
        for original in [item(PARENT, body='Edited'), item(PARENT, author=CHANNEL),
                         item(PARENT, parent='other-parent'), None]:
            with self.subTest(original=original):
                if original is None:
                    self.client.items.pop(PARENT, None)
                else:
                    self.client.items[PARENT] = original
                with self.assertRaises(PublishError):
                    execute(self.root, key, ACCOUNT, CHANNEL, self.client)
                self.assertEqual(len(self.client.posts), 0)

    def test_grant_source_binding_mutation_and_wrong_channel_are_rejected(self):
        key, plan = self.authorized()
        path = self.root / ('grant-' + plan + '.json')
        grant = read_json(path)
        grant['source_digest'] = '0' * 64
        write_json(path, grant)
        with self.assertRaises(PublishError):
            execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        with self.assertRaises(PublishError):
            execute(self.root, key, ACCOUNT, AUTHOR, self.client)
        self.assertEqual(len(self.client.posts), 0)

    def test_own_reply_on_later_page_and_incomplete_paging_stop_send(self):
        key, _ = self.authorized()
        self.client.reply_pages = [[], [item('manual-reply', author=CHANNEL, parent=PARENT)]]
        with self.assertRaises(PublishError):
            execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        self.client.reply_pages = [[] for _ in range(11)]
        with self.assertRaises(PublishError):
            execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        self.assertEqual(len(self.client.posts), 0)

    def test_child_reply_attaches_to_root_but_only_marks_child_replied(self):
        self.add_source('viewer-child', PARENT)
        self.client.reply_pages = [[item('earlier-owner', author=CHANNEL, parent=PARENT,
                                       published='2026-10-28T01:00:00Z')]]
        key, _ = self.authorized(comment='viewer-child')
        result = execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        self.assertEqual(result['state'], 'verified')
        self.assertEqual(self.client.posts[0]['snippet']['parentId'], PARENT)
        with open_db(self.root) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT parent_id FROM interaction_reply_receipts')], ['viewer-child'])

    def test_child_source_with_later_owner_reply_is_blocked(self):
        self.add_source('viewer-child', PARENT)
        self.client.reply_pages = [[item('later-owner', author=CHANNEL, parent=PARENT,
                                       published='2026-10-29T02:00:00Z')]]
        key, _ = self.authorized(comment='viewer-child')
        with self.assertRaises(PublishError):
            execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        self.assertEqual(len(self.client.posts), 0)

    def test_same_plan_cannot_be_authorized_for_two_source_comments(self):
        self.add_source('child-one', PARENT)
        self.add_source('child-two', PARENT)
        self.bound(comment='child-one')
        with self.assertRaises(PublishError):
            self.bound(comment='child-two')

    def test_empty_key_or_authorization_is_rejected_without_a_request(self):
        with self.assertRaises(PublishError):
            prepare(self.root, '', ACCOUNT, CHANNEL)
        key, _ = self.bound()
        with self.assertRaises(PublishError):
            authorize(self.root, key, ACCOUNT, CHANNEL, '  ')
        self.assertEqual(self.client.calls, [])

    def test_existing_receipt_digest_cannot_claim_a_different_plan(self):
        key, plan = self.authorized()
        intent = read_json(self.root / ('plan-' + plan + '.json'))['intent']
        write_json(self.root / (_receipt_key(intent) + '.json'), {'state': 'verified', 'intent_digest': '0' * 64})
        with self.assertRaises(PublishError):
            execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        self.assertEqual(self.client.posts, [])

    def test_legacy_alias_readback_never_changes_plan_or_enables_send(self):
        key, plan_key = self.authorized()
        execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        plan_path = self.root / ('plan-' + plan_key + '.json')
        plan = read_json(plan_path)
        plan['account'] = 'previous-owner-alias'
        write_json(plan_path, plan)
        original = plan_path.read_bytes()
        profile_path = Path(self.tmp.name) / 'profile.json'
        write_json(profile_path, {'accounts': {ACCOUNT: dict(platforms=['youtube'],
            public_identities={'youtube': CHANNEL}, youtube_token_env='QUEUE_TEST_TOKEN')}})
        prefix = ['--profile', str(profile_path), '--account', ACCOUNT, '--state-dir', str(self.root)]
        with patch.dict('os.environ', {'QUEUE_TEST_TOKEN': 'offline-token'}):
            with patch('youtube_comments_candidate.config', side_effect=AssertionError('invalid plan reached credential resolution')):
                with self.assertRaises(PublishError):
                    comments_cli(prefix + ['reconcile', '--key', plan_key])
                with self.assertRaises(PublishError):
                    comments_cli(prefix + ['send', '--key', plan_key])
            result = comments_cli(prefix + ['reconcile', '--key', plan_key, '--source-account', 'previous-owner-alias'],
                                  client_factory=lambda _: self.client)
            self.assertTrue(result['currently_verified'])
            self.assertEqual(result['execution_account'], ACCOUNT)
            self.assertEqual(result['source_account'], 'previous-owner-alias')
            plan['intent']['channel_id'] = AUTHOR
            other_key = digest(plan['intent'])
            write_json(self.root / ('plan-' + other_key + '.json'), plan)
            with self.assertRaises(PublishError):
                comments_cli(prefix + ['reconcile', '--key', other_key, '--source-account', 'previous-owner-alias'],
                             client_factory=lambda _: self.fail('wrong channel reached API'))
        self.assertEqual(plan_path.read_bytes(), original)
        self.assertEqual(len(self.client.posts), 1)

    def test_lost_readback_recovers_without_another_post(self):
        key, _ = self.authorized()
        self.client.mode = 'readback_lost'
        self.assertEqual(execute(self.root, key, ACCOUNT, CHANNEL, self.client)['state'], 'unknown')
        self.client.mode = 'success'
        result = execute(self.root, key, ACCOUNT, CHANNEL, self.client, read_only=True)
        self.assertEqual(result['state'], 'verified')
        self.assertTrue(result['currently_verified'])
        self.assertTrue(result['inbox']['recorded'])
        self.assertEqual(len(self.client.posts), 1)

    def test_no_remote_identity_freezes_withdraw_reauthorize_and_replacement(self):
        key, _ = self.authorized()
        self.client.mode = 'post_timeout'
        self.assertEqual(execute(self.root, key, ACCOUNT, CHANNEL, self.client)['state'], 'unknown')
        self.assertEqual(execute(self.root, key, ACCOUNT, CHANNEL, self.client, read_only=True)['reconciliation'], 'missing_remote_identity')
        with self.assertRaises(PublishError):
            withdraw(self.root, key)
        with self.assertRaises(PublishError):
            authorize(self.root, key, ACCOUNT, CHANNEL, 'Allow again')
        self.assertEqual(prepare(self.root, key, ACCOUNT, CHANNEL)['state'], 'unknown')
        # Simulate loss of the draft projection. The canonical attempt must still block a new draft.
        with open_db(self.root) as db:
            db.execute('DELETE FROM interaction_drafts WHERE draft_key=?', (key,))
        other, _ = self.authorized(reply='Different words.')
        with self.assertRaises(PublishError):
            execute(self.root, other, ACCOUNT, CHANNEL, self.client)
        self.assertEqual(len(self.client.posts), 1)

    def test_reconcile_missing_or_unreadable_comment_preserves_history_not_current_visibility(self):
        key, _ = self.authorized()
        execute(self.root, key, ACCOUNT, CHANNEL, self.client)
        self.client.mode = 'readback_lost'
        result = execute(self.root, key, ACCOUNT, CHANNEL, self.client, read_only=True)
        self.assertEqual(result['state'], 'verified')
        self.assertIsNone(result['currently_verified'])
        self.assertFalse(result['inbox']['recorded'])
        self.client.mode = 'success'
        self.client.items.pop('sent-1')
        result = execute(self.root, key, ACCOUNT, CHANNEL, self.client, read_only=True)
        self.assertEqual(result['state'], 'verified')
        self.assertFalse(result['currently_verified'])
        self.assertFalse(result['inbox']['recorded'])
        self.assertEqual(len(self.client.posts), 1)

    def test_one_channel_serializes_different_intents(self):
        entered, second, release = threading.Event(), threading.Event(), threading.Event()
        first_intent = make_intent(CHANNEL, VIDEO, 'First.', parent_id=PARENT, parent_thread_id=THREAD)
        second_intent = make_intent(CHANNEL, VIDEO, 'Second.', parent_id=PARENT, parent_thread_id=THREAD)

        def guard_one(client, intent):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test release timed out')

        def guard_two(client, intent):
            second.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(send, self.client, first_intent, approved_digest=digest(first_intent),
                                state_dir=self.root, allow_write=True, before_send=guard_one)
            self.assertTrue(entered.wait(5))
            later = pool.submit(send, self.client, second_intent, approved_digest=digest(second_intent),
                                state_dir=self.root, allow_write=True, before_send=guard_two)
            try:
                self.assertFalse(second.wait(.1))
            finally:
                release.set()
            self.assertEqual(first.result(5)['state'], 'verified')
            self.assertEqual(later.result(5)['state'], 'verified')
        self.assertEqual(len(self.client.posts), 2)


if __name__ == '__main__':
    unittest.main()
