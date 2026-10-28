from contextlib import closing
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from wechat_comment_candidate import list_page, sync_article
import wechat_comment_candidate as candidate
from publish_core import PublishError


def comment(number, text='hello', reply=None):
    result = {'user_comment_id': number, 'create_time': 1700000000, 'content': text}
    if reply is not None:
        result['reply'] = {'content': reply, 'create_time': 1700000010}
    return result


class WechatCommentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'private'
        self.root.mkdir(mode=0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def test_api_contract_and_no_count_50(self):
        calls = []
        def post(url, payload, step):
            calls.append((url, payload, step))
            return {'errcode': 0, 'total': 0, 'comment': []}
        self.assertEqual(list_page(post, 'secret-token', 100, 1, 0, 40), (0, []))
        self.assertTrue(calls[0][0].split('?', 1)[0].endswith('/cgi-bin/comment/list'))
        self.assertEqual(calls[0][1], {'msg_data_id': 100, 'index': 1, 'begin': 0, 'count': 40, 'type': 0})
        for bad in (0, 50, True):
            with self.assertRaises(PublishError):
                list_page(post, 'token', 100, 1, 0, bad)
        self.assertEqual(len(calls), 1)

    def test_account_is_partial_and_same_article_is_idempotent(self):
        calls = []
        def fetch(msg, index, begin, count):
            calls.append(begin)
            return (41, [comment(n, reply='ok' if n == 1 else None) for n in range(1, 41)]) if begin == 0 else (41, [comment(41)])
        first = sync_article(self.root, 'acct', 'hash', 100, 0, fetch)
        self.assertEqual(first['article_state'], 'complete')
        self.assertEqual(first['account_state'], 'partial')
        sync_article(self.root, 'acct', 'hash', 100, 0, fetch)
        with closing(sqlite3.connect(self.root / 'interaction-inbox.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM interaction_items').fetchone()[0], 41)
            self.assertEqual(db.execute('SELECT state FROM interaction_sources').fetchone()[0], 'partial')
            self.assertEqual(db.execute('SELECT reply_body FROM interaction_wechat_comments WHERE user_comment_id=?', ('1',)).fetchone()[0], 'ok')
        self.assertEqual(calls, [0, 40, 0, 40])
        self.assertEqual(os.stat(self.root / 'interaction-inbox.sqlite3').st_mode & 0o777, 0o600)

    def test_page_limit_or_early_empty_is_not_complete(self):
        def fetch(msg, index, begin, count):
            return (41, [comment(n) for n in range(1, 41)]) if begin == 0 else (41, [])
        limited = sync_article(self.root, 'acct', 'hash', 101, 0, fetch, max_pages=1)
        self.assertEqual(limited['article_state'], 'partial')
        incomplete = sync_article(self.root, 'acct', 'hash', 102, 0, fetch)
        self.assertEqual(incomplete['article_state'], 'partial')
        self.assertEqual(incomplete['account_state'], 'partial')

    def test_permission_failure_is_error_not_zero_comments(self):
        def fetch(*args):
            raise PublishError('no permission')
        result = sync_article(self.root, 'acct', 'hash', 100, 0, fetch)
        self.assertEqual(result['article_state'], 'error')
        self.assertEqual(result['account_state'], 'error')

    def test_duplicate_or_changing_total_during_paging_is_partial(self):
        def duplicated(msg, index, begin, count):
            return (41, [comment(n) for n in range(1, 41)]) if begin == 0 else (41, [comment(1)])
        self.assertEqual(sync_article(self.root, 'acct', 'hash', 120, 0, duplicated)['article_state'], 'partial')
        def changed(msg, index, begin, count):
            return (41, [comment(n) for n in range(1, 41)]) if begin == 0 else (42, [comment(41)])
        self.assertEqual(sync_article(self.root, 'acct', 'hash', 121, 0, changed)['article_state'], 'partial')

    def test_distinct_articles_do_not_collapse_same_comment_id(self):
        def fetch(*args):
            return (1, [comment(1)])
        sync_article(self.root, 'acct', 'hash', 100, 0, fetch)
        sync_article(self.root, 'acct', 'hash', 100, 1, fetch)
        with closing(sqlite3.connect(self.root / 'interaction-inbox.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM interaction_items').fetchone()[0], 2)

    def test_token_failure_updates_only_confirmed_account(self):
        channel = hashlib.sha256(b'app-id').hexdigest()
        sync_article(self.root, 'acct', channel, 100, 0, lambda *args: (0, []))
        sync_article(self.root, 'other', 'other-hash', 101, 0, lambda *args: (0, []))
        args = ['cmd', '--profile', '/private/profile.json', '--account', 'acct',
                '--state-dir', str(self.root), '--msg-data-id', '100']
        with patch.object(sys, 'argv', args), \
             patch.object(candidate, 'load_profile', return_value={'accounts': {'acct': {'platforms': ['wechat']}}}), \
             patch.object(candidate, 'credentials', return_value=('app-id', 'secret-value')), \
             patch.object(candidate.wechat_api, 'access_token', side_effect=candidate.wechat_api.WechatError('secret-value')):
            with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as stop:
                    candidate.main()
        self.assertEqual(stop.exception.code, 1)
        self.assertNotIn('secret-value', stdout.getvalue() + stderr.getvalue())
        with closing(sqlite3.connect(self.root / 'interaction-inbox.sqlite3')) as db:
            rows = dict(db.execute('SELECT account,state FROM interaction_sources'))
            self.assertEqual(rows, {'acct': 'error', 'other': 'partial'})
            self.assertIn('本次未查询平台', db.execute('SELECT error FROM interaction_sources WHERE account=?', ('acct',)).fetchone()[0])

    def test_unverified_identity_does_not_change_sources(self):
        sync_article(self.root, 'acct', 'existing-hash', 100, 0, lambda *args: (0, []))
        args = ['cmd', '--profile', '/private/profile.json', '--account', 'acct',
                '--state-dir', str(self.root), '--msg-data-id', '100']
        with patch.object(sys, 'argv', args), \
             patch.object(candidate, 'load_profile', return_value={'accounts': {'acct': {'platforms': ['wechat']}}}), \
             patch.object(candidate, 'credentials', side_effect=PublishError('identity mismatch')):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    candidate.main()
        with closing(sqlite3.connect(self.root / 'interaction-inbox.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT channel_id,state FROM interaction_sources').fetchall(), [('existing-hash', 'partial')])


if __name__ == '__main__':
    unittest.main()
