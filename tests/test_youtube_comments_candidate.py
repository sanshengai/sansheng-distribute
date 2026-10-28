"""Offline tests: no platform credentials or network."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from publish_core import PublishError, digest
from youtube_comments_candidate import make_intent, replies, run_cli, send, threads, SCOPE

CHANNEL = 'UC' + 'a' * 22
VIDEO = 'abcdefghijk'
PARENT = 'root-comment-123'
PARENT_THREAD = 'different-thread-456'

class FakeClient:
    def __init__(self):
        self.calls = []
        self.mode = 'success'
        self.scope = {SCOPE}
        self.channel = CHANNEL
        self.video_channel = CHANNEL
        self.parent_video = VIDEO
        self.parent_channel = CHANNEL
        self.parent_comment = PARENT
        self.can_reply = True
        self.last_body = None

    def granted_scopes(self): return self.scope

    def call(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url))
        path = urlsplit(url).path
        qs = parse_qs(urlsplit(url).query)
        if method == 'POST':
            self.last_body = json.loads(data)
            if self.mode == 'timeout': raise PublishError('timeout')
            if self.mode == 'badresponse': return 200, {}, b'{}'
            if path.endswith('/commentThreads'):
                result = {'id': 'new-thread', 'snippet': {'topLevelComment': {'id': 'new-comment'}}}
            else:
                result = {'id': 'new-comment'}
            return 200, {}, json.dumps(result).encode()
        if path.endswith('/channels'):
            return 200, {}, json.dumps({'items': [{'id': self.channel}]}).encode()
        if path.endswith('/videos'):
            return 200, {}, json.dumps({'items': [{'snippet': {'channelId': self.video_channel}}]}).encode()
        if path.endswith('/commentThreads') and 'id' in qs:
            top_id = 'new-comment' if qs['id'][0] == 'new-thread' else self.parent_comment
            video_id = 'zzzzzzzzzzz' if self.last_body and self.mode == 'wrong_thread_after_post' else self.parent_video
            item = {'id': qs['id'][0], 'snippet': {'channelId': self.parent_channel,
                                'videoId': video_id, 'canReply': self.can_reply,
                                'topLevelComment': {'id': top_id}}}
            return 200, {}, json.dumps({'items': [item]}).encode()
        if path.endswith('/comments') and 'id' in qs:
            if self.mode == 'readback_lost': raise PublishError('readback timeout')
            body = self.last_body['snippet']
            text = body.get('textOriginal') or body['topLevelComment']['snippet']['textOriginal']
            # Official comment schema does not contain snippet.videoId.
            sn = {'textOriginal': text, 'authorChannelId': {'value': CHANNEL}}
            if 'parentId' in body: sn['parentId'] = body['parentId']
            return 200, {}, json.dumps({'items': [{'id':'new-comment','snippet':sn}]}).encode()
        if path.endswith('/comments') or path.endswith('/commentThreads'):
            payload = {'items': [{'id':'comment1'}]}
            if 'pageToken' not in qs: payload['nextPageToken'] = 'next-page'
            return 200, {}, json.dumps(payload).encode()
        raise AssertionError('unexpected test request')

class CommentsTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.intent = make_intent(CHANNEL, VIDEO, 'Hello!', parent_id=PARENT, parent_thread_id=PARENT_THREAD)

    def run_send(self, intent=None):
        item = intent or self.intent
        return send(self.client, item, approved_digest=digest(item), state_dir=self.state, allow_write=True)

    def test_threads_and_replies_surface_cursor(self):
        first = threads(self.client, video_id=VIDEO, page_size=1)
        self.assertEqual(first['next_page_token'], 'next-page')
        self.assertEqual(threads(self.client, video_id=VIDEO, page_token=first['next_page_token'])['items'][0]['id'], 'comment1')
        self.assertEqual(replies(self.client, PARENT)['next_page_token'], 'next-page')
        with self.assertRaises(PublishError): threads(self.client, video_id=VIDEO, channel_id=CHANNEL)

    def test_reply_posts_expected_parent_and_readback(self):
        result = self.run_send()
        self.assertEqual(result['state'], 'verified')
        self.assertEqual(self.client.last_body['snippet']['parentId'], PARENT)
        self.assertEqual(self.client.last_body['snippet']['textOriginal'], 'Hello!')
        self.assertTrue(any('id='+PARENT_THREAD in url for method,url in self.client.calls if method == 'GET'))
        self.assertNotEqual(PARENT, PARENT_THREAD)
        self.assertEqual(next(self.state.glob('*.json')).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_send()['state'], 'verified')
        self.assertEqual(sum(m == 'POST' for m, _ in self.client.calls), 1)

    def test_top_level_comment(self):
        intent = make_intent(CHANNEL, VIDEO, 'Welcome')
        result = self.run_send(intent)
        self.assertEqual(result['state'], 'verified')
        self.assertEqual(self.client.last_body['snippet']['videoId'], VIDEO)
        self.assertEqual(self.client.last_body['snippet']['channelId'], CHANNEL)

    def test_timeout_or_lost_readback_never_reposts(self):
        for mode in ('timeout', 'badresponse', 'readback_lost', 'wrong_thread_after_post'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                self.state = Path(tmp) / 'state'; self.client = FakeClient(); self.client.mode = mode
                self.assertEqual(self.run_send()['state'], 'unknown')
                self.assertEqual(self.run_send()['state'], 'unknown')
                self.assertEqual(sum(m == 'POST' for m, _ in self.client.calls), 1)

    def test_disallowed_write_wrong_approval_and_empty_content_rejected(self):
        with self.assertRaises(PublishError):
            send(self.client, self.intent, approved_digest=digest(self.intent), state_dir=self.state)
        with self.assertRaises(PublishError):
            send(self.client, self.intent, approved_digest='wrong', state_dir=self.state, allow_write=True)
        with self.assertRaises(PublishError): make_intent(CHANNEL, VIDEO, '  ', parent_id=PARENT)
        with self.assertRaises(PublishError): make_intent(CHANNEL, VIDEO, 'Hello!', parent_id=PARENT)
        with self.assertRaises(PublishError): make_intent(CHANNEL, VIDEO, 'Hello!', parent_thread_id=PARENT_THREAD)
        self.assertFalse(self.client.calls)

    def test_bad_identity_video_parent_and_scope_stop_before_write(self):
        for field, value in [('channel', 'UC' + 'b'*22), ('video_channel', 'UC' + 'b'*22),
                             ('parent_video','zzzzzzzzzzz'), ('parent_channel', 'UC'+'b'*22),
                             ('parent_comment','different-comment'), ('can_reply',False), ('scope',set())]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                self.state = Path(tmp) / 'state'; self.client = FakeClient(); setattr(self.client, field, value)
                with self.assertRaises(PublishError): self.run_send()
                self.assertFalse(any(m == 'POST' for m, _ in self.client.calls))

    def test_cli_requires_actual_statement_record_and_preserves_binding(self):
        root = Path(self.tmp.name)
        profile = root / 'profile.json'
        profile.write_text(json.dumps({'accounts': {'owner': {
            'platforms': ['youtube'], 'public_identities': {'youtube': CHANNEL},
            'youtube_token_env': 'TEST_YOUTUBE_TOKEN'}}}))
        text_file = root / 'text.txt'; text_file.write_text('Hello!', encoding='utf-8')
        statement_file = root / 'statement.txt'; statement_file.write_text('允许发送这条回复', encoding='utf-8')
        prefix = ['--profile', str(profile), '--account', 'owner', '--state-dir', str(self.state)]
        with patch.dict('os.environ', {'TEST_YOUTUBE_TOKEN':'test-only'}):
            intent = run_cli(prefix + ['prepare','--video',VIDEO,'--parent',PARENT,
                                       '--parent-thread',PARENT_THREAD,'--text-file',str(text_file)])
            key = intent['key']
            self.assertEqual(run_cli(prefix + ['status','--key',key])['state'], 'not_attempted')
            with self.assertRaises(PublishError):
                run_cli(prefix + ['send','--key',key], client_factory=lambda _:self.client)
            self.assertFalse(any(m == 'POST' for m, _ in self.client.calls))
            self.assertEqual(run_cli(prefix + ['authorize','--key',key,'--user-statement-file',str(statement_file)])['state'], 'authorized')
            result = run_cli(prefix + ['send','--key',key], client_factory=lambda _:self.client)
            self.assertEqual(result['state'], 'verified')
            self.assertEqual(run_cli(prefix + ['status','--key',key])['state'], 'verified')
            self.assertEqual(run_cli(prefix + ['send','--key',key], client_factory=lambda _:self.client)['state'], 'verified')
            self.assertEqual(sum(m == 'POST' for m, _ in self.client.calls), 1)
            plan = self.state / ('plan-' + key + '.json')
            modified = json.loads(plan.read_text()); modified['intent']['text'] = 'Changed'
            plan.write_text(json.dumps(modified))
            with self.assertRaises(PublishError):
                run_cli(prefix + ['send','--key',key], client_factory=lambda _:self.client)
            self.assertEqual(sum(m == 'POST' for m, _ in self.client.calls), 1)

    def test_cli_read_uses_bound_channel_and_rejects_restricted_write(self):
        root = Path(self.tmp.name); profile = root / 'profile.json'
        profile.write_text(json.dumps({'accounts': {'owner': {
            'platforms':['youtube'], 'public_identities': {'youtube':CHANNEL},
            'youtube_token_env':'TEST_YOUTUBE_TOKEN', 'restricted':True}}}))
        text = root / 'text.txt'; text.write_text('Test')
        prefix = ['--profile',str(profile),'--account','owner','--state-dir',str(self.state)]
        with patch.dict('os.environ', {'TEST_YOUTUBE_TOKEN':'test-only'}):
            result = run_cli(prefix + ['threads'], client_factory=lambda _:self.client)
            self.assertEqual(len(result['items']), 1)
            self.assertIn('allThreadsRelatedToChannelId', self.client.calls[-1][1])
            plan = run_cli(prefix + ['prepare','--video',VIDEO,'--text-file',str(text)])
            with self.assertRaises(PublishError):
                run_cli(prefix + ['send','--key',plan['key']], client_factory=lambda _:self.client)
            self.assertFalse(any(m == 'POST' for m, _ in self.client.calls))

if __name__ == '__main__': unittest.main()
