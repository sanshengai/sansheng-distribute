import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from interaction_inbox import open_db
from interaction_receipts import record_reply
from publish_core import PublishError, digest


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.intent = dict(action='reply', channel_id='channel', video_id='video',
                           parent_id='parent', parent_thread_id='thread', text='reply')
        self.receipt = dict(state='verified', intent_digest=digest(self.intent),
                            remote_id='reply', thread_id='thread')
        with open_db(self.root) as db:
            db.execute('''INSERT INTO interaction_items
                (platform,account,channel_id,comment_id,thread_id,video_id,
                 first_seen_at,last_seen_at,is_own) VALUES
                ('youtube','owner','channel','parent','thread','video','now','now',0)''')

    def test_verified_deduplicated_without_faking_handled(self):
        for _ in range(2):
            self.assertTrue(record_reply(self.root, 'owner', self.intent, self.receipt)['recorded'])
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM interaction_reply_receipts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT local_status FROM interaction_items').fetchone()[0],'new')

    def test_unknown_empty_mismatch_rejected(self):
        for receipt in ({}, {**self.receipt,'state':'unknown'},
                        {**self.receipt,'intent_digest':'wrong'},
                        {**self.receipt,'thread_id':'other'},
                        {**self.receipt,'remote_id':''}):
            with self.subTest(receipt=receipt), self.assertRaises(PublishError):
                record_reply(self.root, 'owner', self.intent, receipt)

    def test_wrong_account_never_updates(self):
        self.assertFalse(record_reply(self.root, 'other', self.intent, self.receipt)['recorded'])

    def test_wrong_video_rejected(self):
        with open_db(self.root) as db:
            db.execute("UPDATE interaction_items SET video_id='different'")
        with self.assertRaises(PublishError):
            record_reply(self.root, 'owner', self.intent, self.receipt)
