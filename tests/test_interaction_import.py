import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from interaction_import import import_manual, mark_manual
from interaction_inbox import open_db
from publish_core import PublishError


def document():
    return {'platform': 'weibo', 'account': 'example', 'channel_id': 'owner-id', 'items': [
        {'comment_id': 'comment-one', 'video_id': 'video-one', 'body': '<script>unsafe</script>', 'is_own': False}
    ]}


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'private'

    def test_deduplicates_and_keeps_handled(self):
        self.assertEqual(import_manual(self.root, document())['imported'], 1)
        with open_db(self.root) as db:
            db.execute("UPDATE interaction_items SET local_status='handled'")
        self.assertEqual(import_manual(self.root, document())['already_known'], 1)
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM interaction_items').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT local_status FROM interaction_items').fetchone()[0], 'handled')
            self.assertEqual(db.execute('SELECT state FROM interaction_sources').fetchone()[0], 'manual')
            self.assertIsNone(db.execute('SELECT last_success_at FROM interaction_sources').fetchone()[0])
            self.assertEqual(db.execute('SELECT count(*) FROM interaction_manual_provenance').fetchone()[0], 1)
        self.assertEqual((self.root / 'interaction-inbox.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_missing_ids_and_empty_rejected(self):
        for change in ('empty', 'no-comment', 'no-video', 'no-own'):
            row = document()
            if change == 'empty': row['items'] = []
            else: del row['items'][0][{'no-comment':'comment_id', 'no-video':'video_id', 'no-own':'is_own'}[change]]
            with self.subTest(change=change), self.assertRaises(PublishError):
                import_manual(self.root, row)
        self.assertFalse(self.root.exists())

    def test_conflicting_comment_rejects_batch_without_partial_insert(self):
        original = document()
        import_manual(self.root, original)
        changed = document()
        changed['items'] = [
            {'comment_id':'new-first', 'video_id':'video-one', 'body':'ok', 'is_own':False},
            {'comment_id':'comment-one', 'video_id':'video-one', 'body':'changed', 'is_own':False},
        ]
        with self.assertRaises(PublishError): import_manual(self.root, changed)
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM interaction_items').fetchone()[0], 1)

    def test_import_does_not_overwrite_existing_sync_state(self):
        first = document()
        import_manual(self.root, first)
        with open_db(self.root) as db:
            db.execute("UPDATE interaction_sources SET state='partial', error='API incomplete'")
        second = document()
        second['items'][0]['comment_id'] = 'another'
        import_manual(self.root, second)
        with open_db(self.root) as db:
            self.assertEqual(tuple(db.execute('SELECT state,error FROM interaction_sources').fetchone()),
                             ('partial', 'API incomplete'))

    def test_mark_only_local_manual_items(self):
        import_manual(self.root, document())
        result = mark_manual(self.root, 'weibo', 'example', 'owner-id', 'comment-one', 'handled')
        self.assertEqual(result['status'], 'handled')
        self.assertIn('不代表已回复', result['note'])
        with self.assertRaises(PublishError):
            mark_manual(self.root, 'weibo', 'example', 'owner-id', 'missing', 'handled')

    def test_reject_state_dir_inside_skill_repo(self):
        path = Path(__file__).resolve().parents[1] / 'private-not-created'
        with self.assertRaises(PublishError): import_manual(path, document())
        self.assertFalse(path.exists())


if __name__ == '__main__': unittest.main()
