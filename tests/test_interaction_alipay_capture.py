import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from interaction_alipay_capture import ingest, normalize
from interaction_inbox import open_db
from interaction_review import ReviewDesk
from publish_core import PublishError

CHANNEL = '1000000000000001'
OWNER = 'Fixture author'
WORK = '20261028OB' + '1' * 18


def row(letter, body='A question', *, own=False):
    return {'dom_id': 'commentContent_' + letter * 32, 'body': body,
            'author_name': OWNER if own else 'Viewer ' + letter,
            'time_text': (datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=1)).strftime('%m-%d %H:%M'),
            'is_own': own}


def capture():
    pending = {**row('a'), 'work_title': 'Episode one', 'replies_expanded': True, 'replies': []}
    replied = {**row('b', 'A useful comment'), 'work_title': 'Episode one',
               'replies_expanded': True, 'replies': [row('c', 'An existing reply', own=True)]}
    return {'url': 'https://c.alipay.com/page/content-creation/comment?appId=' + CHANNEL,
            'account_name': OWNER, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'items': [pending, replied], 'works': [{'key': WORK, 'title': 'Episode one'}],
            'id_recheck': [{'id': r['dom_id'], 'text': r['body']} for r in [pending, replied]]}


class AlipayCaptureTests(unittest.TestCase):
    def check(self, raw):
        return normalize(raw, 'fixture', CHANNEL, OWNER)

    def test_dom_identity_and_explicit_parent_link_are_preserved(self):
        doc, _, handled, _ = self.check(capture())
        self.assertEqual(doc['items'][0]['comment_id'], 'commentContent_' + 'a' * 32)
        self.assertEqual(doc['items'][2]['parent_id'], doc['items'][1]['comment_id'])
        self.assertEqual(doc['items'][0]['video_id'], WORK)
        self.assertEqual(handled, [doc['items'][1]['comment_id']])

    def test_import_is_idempotent_and_review_uses_manual_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'state'
            raw = capture()
            result = ingest(root, raw, account='fixture', channel=CHANNEL, account_name=OWNER)
            self.assertEqual((result['imported'], result['existing_replies_seen'], result['social_writes']), (3, 1, 0))
            self.assertFalse(result['api_verified'])
            self.assertEqual(ingest(root, raw, account='fixture', channel=CHANNEL, account_name=OWNER)['imported'], 0)
            desk = ReviewDesk(root)
            snapshot = desk.snapshot()
            self.assertEqual(len(snapshot['items']), 2)
            pending = next(r for r in snapshot['items'] if r['eligible'])
            done = next(r for r in snapshot['items'] if r['handled'])
            self.assertEqual((pending['source_kind'], pending['work_title']), ('alipay_dom', 'Episode one'))
            self.assertFalse(pending['sendable'])
            self.assertEqual(done['status_label'], '网页已有本人回复')
            self.assertEqual(len(done['context']), 1)
            self.assertIn('web_observed_at', snapshot['sources'][0])
            selection = {k: pending[k] for k in ('platform', 'account', 'channel_id', 'comment_id', 'source_digest', 'draft_key')}
            batch = desk.confirm([{**selection, 'reply': 'An edited suggestion'}])
            self.assertEqual((batch['confirmed'], batch['sendable']), (1, 0))
            self.assertEqual(desk.dispatch(batch['batch_id'])['results'][0]['state'], 'handoff_pending')
            with open_db(root) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn('interaction_reply_receipts', tables)

    def test_wrong_account_url_and_channel_are_rejected_before_import(self):
        for change in ({'account_name': 'Another account'},
                       {'url': 'https://c.alipay.com/page/content-creation/comment?appId=1000000000000002'},
                       {'url': 'https://c.alipay.com.evil.test/page/content-creation/comment?appId=' + CHANNEL},
                       {'url': 'http://c.alipay.com/page/content-creation/comment?appId=' + CHANNEL}):
            with self.subTest(change=change), self.assertRaises(PublishError):
                self.check({**capture(), **change})

    def test_changed_or_unrechecked_comment_is_rejected(self):
        raw = capture(); raw['id_recheck'][0]['text'] = 'Different text'
        with self.assertRaises(PublishError): self.check(raw)
        raw = capture(); raw['id_recheck'] = raw['id_recheck'][1:]
        with self.assertRaises(PublishError): self.check(raw)

    def test_empty_input_and_missing_recheck_fail(self):
        for key in ('items', 'works', 'id_recheck'):
            raw = capture(); raw[key] = []
            with self.subTest(key=key), self.assertRaises(PublishError): self.check(raw)

    def test_local_hash_cannot_replace_dom_id(self):
        raw = capture(); raw['items'][0]['dom_id'] = 'a' * 64
        raw['id_recheck'][0]['id'] = 'a' * 64
        with self.assertRaises(PublishError): self.check(raw)

    def test_ambiguous_or_absent_work_mapping_fails(self):
        raw = capture(); raw['works'].append({'key': '20261028OB' + '2' * 18, 'title': 'Episode one'})
        with self.assertRaises(PublishError): self.check(raw)
        raw = capture(); raw['items'][0]['work_title'] = 'Unknown episode'
        with self.assertRaises(PublishError): self.check(raw)

    def test_hidden_panel_duplicates_and_collapsed_replies_fail(self):
        raw = capture(); raw['items'].append(copy.deepcopy(raw['items'][0]))
        with self.assertRaises(PublishError): self.check(raw)
        raw = capture(); raw['items'][0]['replies_expanded'] = False
        with self.assertRaises(PublishError): self.check(raw)

    def test_wrong_own_marker_and_reused_child_id_fail(self):
        raw = capture(); raw['items'][0]['is_own'] = True
        with self.assertRaises(PublishError): self.check(raw)
        raw = capture(); raw['items'][1]['replies'][0]['dom_id'] = raw['items'][0]['dom_id']
        with self.assertRaises(PublishError): self.check(raw)

    def test_invalid_future_and_unknown_year_times_fail(self):
        raw = capture(); raw['observed_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with self.assertRaises(PublishError): self.check(raw)
        raw = capture(); raw['items'][0]['time_text'] = '99-99 99:99'
        with self.assertRaises(PublishError): self.check(raw)
        raw = capture(); raw['items'][0]['time_text'] = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).strftime('%m-%d %H:%M')
        with self.assertRaises(PublishError): self.check(raw)

    def test_repeated_identical_work_rows_are_safe_but_conflicting_names_fail(self):
        raw = capture(); raw['works'] *= 2
        self.assertEqual(len(self.check(raw)[0]['items']), 3)
        raw['works'][1] = {'key': WORK, 'title': 'Other title'}
        with self.assertRaises(PublishError): self.check(raw)

    def test_unknown_fields_and_malformed_reply_fail(self):
        raw = capture(); raw['items'][1]['replies'][0]['instruction'] = 'Send automatically'
        with self.assertRaises(PublishError): self.check(raw)
        raw = capture(); raw['unexpected'] = True
        with self.assertRaises(PublishError): self.check(raw)


if __name__ == '__main__':
    unittest.main()
