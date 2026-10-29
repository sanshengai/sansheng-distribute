import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from interaction_observations import record
from interaction_dashboard import render
from interaction_inbox import open_db
from publish_core import PublishError

class ObservationTests(unittest.TestCase):
    def document(self):
        return dict(platform='alipay', account='test', observed_at='2026-01-01T08:00:00+08:00',
                    state='partial', scope='one visible page', reason='no stable comment ID',
                    next_action='review original page', evidence='browser reload', replies=[dict(
                        author='viewer', comment='<script>bad</script>', work='episode',
                        reply='thanks', verification='reload and read back exact text')])

    def test_private_idempotent_observation_does_not_create_send_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = record(root, self.document())
            self.assertTrue(first['inserted'])
            self.assertFalse(first['remote_identity'])
            self.assertFalse(record(root, self.document())['inserted'])
            with open_db(root) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM interaction_items').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM interaction_sources').fetchone()[0], 0)
            page = render(root/'interaction-inbox.sqlite3', True, ['alipay','x'])
            self.assertIn('网页部分检查', page)
            self.assertIn('no stable comment ID', page)
            self.assertIn('review original page', page)
            self.assertIn('无同步记录', page)
            self.assertIn('&lt;script&gt;', page)
            self.assertNotIn('<script>bad</script>', page)
            self.assertNotIn('thanks', render(root/'interaction-inbox.sqlite3'))
            self.assertEqual((root/'interaction-inbox.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_reject_empty_fabricated_identity_and_unverified_reply(self):
        samples = [{}, self.document(), self.document(), self.document(), self.document()]
        samples[1]['comment_id'] = 'fake'
        samples[2]['replies'][0]['verification'] = ''
        samples[3]['observed_at'] = '2026-01-01T08:00:00'
        samples[4]['state'] = 'blocked'
        for doc in samples:
            with self.subTest(doc=doc), tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(PublishError): record(Path(folder), doc)
                self.assertFalse((Path(folder)/'interaction-inbox.sqlite3').exists())

    def test_later_blocker_does_not_erase_reply_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            record(root, self.document())
            second = self.document()
            second.update(observed_at='2026-01-02T00:00:00Z', state='blocked', reason='login expired', replies=[])
            record(root, second)
            page = render(root/'interaction-inbox.sqlite3', True)
            self.assertIn('login expired', page)
            self.assertIn('thanks', page)
            self.assertNotIn('网页部分检查</td>', page)

if __name__ == '__main__': unittest.main()
