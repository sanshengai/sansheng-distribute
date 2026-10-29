import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from interaction_dashboard import render
from interaction_drafts import source
from interaction_import import import_manual, mark_manual
from interaction_inbox import open_db
from publish_core import PublishError
from weibo_comments_capture import import_capture, normalize, plain_text, _private_json


def config():
    return {'schema': 'weibo-comment-config.v1', 'account': 'example', 'uid': '101',
            'not_before': '2026-09-24T00:00:00+08:00'}


def capture():
    root = {'id': '301', 'root_id': '301', 'parent_id': None, 'work_id': '201',
            'work_author': '101', 'author_id': '102', 'author_name': 'reader',
            'created_at': 'Sun Sep 20 20:57:13 +0800 2026',
            'text_html': 'hello &amp; <a href="/u/101">owner</a>',
            'restrict_state': 0, 'disable_reply': 0, 'cut_tail': False, 'parent': None}
    child = dict(root, id='303', parent_id='302', created_at='Wed Sep 23 08:58:53 +0800 2026',
                 text_html='a reply', parent={'id': '302', 'root_id': '301', 'author_id': '101',
                   'author_name': 'owner', 'created_at': 'Wed Sep 23 08:58:38 +0800 2026',
                   'text_html': 'parent reply'})
    return {'schema': 'weibo-comment-capture.v1', 'account': 'example', 'uid': '101',
            'observed_at': '2026-10-01T00:00:00+00:00',
            'source_url': 'https://weibo.com/ajax/message/cmt', 'total': 2,
            'next_cursor': '0', 'missing': 0, 'items': [child, root]}


class WeiboCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'

    def test_native_child_id_differs_from_direct_parent(self):
        document = normalize(capture(), config())
        child = document['items'][0]
        self.assertEqual((child['comment_id'], child['parent_id'], child['thread_id']), ('303', '302', '301'))
        self.assertEqual(len(document['items']), 2)  # Do not invent the nested parent's own parent.
        self.assertEqual(document['items'][1]['body'], 'hello & owner')
        self.assertEqual(child['published_at'], '2026-09-23T00:58:53+00:00')

    def test_baseline_dedupes_and_does_not_become_a_reply(self):
        result = import_capture(self.root, capture(), config())
        self.assertEqual((result['imported'], result['sends']), (2, 0))
        self.assertIn('未证明全量覆盖', result['coverage'])
        repeated = import_capture(self.root, capture(), config())
        self.assertEqual((repeated['imported'], repeated['already_known']), (0, 2))
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM interaction_baselines').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM interaction_items WHERE local_status='handled'").fetchone()[0], 2)
            src = db.execute('SELECT state,last_success_at FROM interaction_sources').fetchone()
            self.assertEqual(tuple(src), ('manual', None))
            with self.assertRaises(PublishError): source(db, 'weibo', 'example', '101', '303')
        page = render(self.root / 'interaction-inbox.sqlite3')
        self.assertIn('历史基线 2 条', page)
        self.assertIn('人工导入（未同步）', page)

    def test_current_comments_are_new_and_own_comments_not_baselined(self):
        cfg = dict(config(), not_before='2026-09-21T00:00:00+00:00')
        raw = capture()
        raw['items'][1]['author_id'] = '101'
        import_capture(self.root, raw, cfg)
        with open_db(self.root) as db:
            rows = {r['comment_id']: dict(r) for r in db.execute('SELECT * FROM interaction_items')}
            self.assertEqual(rows['303']['local_status'], 'new')
            self.assertEqual(rows['301']['is_own'], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM interaction_baselines').fetchone()[0], 0)

    def test_empty_input_fails_without_creating_state(self):
        raw = capture(); raw['items'] = []; raw['total'] = 0
        with self.assertRaises(PublishError): import_capture(self.root, raw, config())
        self.assertFalse(self.root.exists())

    def test_wrong_binding_endpoint_or_work_owner_rejected(self):
        for field, value in [('uid', '999'), ('account', 'other'), ('source_url', 'https://other.example/cmt')]:
            raw = capture(); raw[field] = value
            with self.subTest(field=field), self.assertRaises(PublishError): normalize(raw, config())
        raw = capture(); raw['items'][0]['work_author'] = '999'
        with self.assertRaises(PublishError): normalize(raw, config())

    def test_parent_and_root_mismatches_rejected(self):
        cases = [('parent_id', '303'), ('parent_id', '304'), ('root_id', '305'), ('parent_id', None)]
        for field, value in cases:
            raw = capture(); raw['items'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(PublishError): normalize(raw, config())
        raw = capture(); raw['items'][1]['parent_id'] = '303'
        with self.assertRaises(PublishError): normalize(raw, config())

    def test_known_parent_context_conflict_and_cycles_rejected(self):
        raw = capture(); raw['items'][0]['parent_id'] = '301'
        raw['items'][0]['parent'].update(id='301', text_html='not the captured root')
        with self.assertRaises(PublishError): normalize(raw, config())
        raw = capture(); raw['items'][0]['parent'] = None
        raw['items'][1].update(id='302', parent_id='303')
        raw['items'][1]['created_at'] = raw['items'][0]['created_at']
        with self.assertRaises(PublishError): normalize(raw, config())

    def test_restricted_disabled_or_truncated_do_not_enter_reply_queue(self):
        for field, value in [('restrict_state', 1), ('restrict_state', False),
                             ('disable_reply', 1), ('disable_reply', False), ('cut_tail', True), ('cut_tail', 0)]:
            raw = capture(); raw['items'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(PublishError): import_capture(self.root, raw, config())
        self.assertFalse(self.root.exists())

    def test_ids_counts_dates_duplicates_and_unknown_fields(self):
        for field, value in [('id', 303), ('id', 'rid=303'), ('created_at', 'bad'),
                             ('created_at', 'Wed Sep 23 08:58:53 +2460 2026'), ('unknown', True)]:
            raw = capture(); raw['items'][0][field] = value
            with self.subTest(field=field), self.assertRaises(PublishError): normalize(raw, config())
        for field, value in [('total', True), ('total', 1), ('next_cursor', ''),
                             ('observed_at', '2026-10-01'), ('observed_at', '2099-01-01T00:00:00Z')]:
            raw = capture(); raw[field] = value
            with self.subTest(field=field), self.assertRaises(PublishError): normalize(raw, config())
        raw = capture(); raw['items'].append(copy.deepcopy(raw['items'][0])); raw['total'] = 3
        with self.assertRaises(PublishError): normalize(raw, config())

    def test_partial_capture_stays_manual_even_cursor_zero(self):
        raw = capture(); raw.update(total=10, missing=2, next_cursor='909')
        result = import_capture(self.root, raw, config())
        self.assertEqual((result['declared_total'], result['missing'], result['next_cursor']), (10, 2, '909'))
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT state FROM interaction_sources').fetchone()[0], 'manual')

    def test_metadata_conflicts_reject_atomically_and_keep_handled_state(self):
        cfg = dict(config(), not_before='2026-09-01T00:00:00Z')
        doc = normalize(capture(), cfg)
        import_manual(self.root, doc)
        mark_manual(self.root, 'weibo', 'example', '101', '303', 'handled')
        for field, value in [('parent_id', '888'), ('thread_id', '888'),
                             ('author_channel', '888'), ('published_at', '2026-09-24T00:00:00Z')]:
            changed = copy.deepcopy(doc)
            changed['items'][0][field] = value
            extra = dict(changed['items'][1], comment_id='999', thread_id='999')
            changed['items'].insert(0, extra)
            with self.subTest(field=field), self.assertRaises(PublishError): import_manual(self.root, changed)
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM interaction_items').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT local_status FROM interaction_items WHERE comment_id='303'").fetchone()[0], 'handled')

    def test_plain_html_and_private_files(self):
        self.assertEqual(plain_text('text<br/>more<img alt="[微笑]" src="x">'), 'text\nmore[微笑]')
        for raw in ('<script>x</script>', '<style>x</style>', '<b></b>'):
            with self.assertRaises(PublishError): plain_text(raw)
        file = Path(self.temp.name) / 'capture.json'; file.write_text(json.dumps(capture()))
        file.chmod(0o644)
        with self.assertRaises(PublishError): _private_json(file)
        file.chmod(0o600)
        self.assertEqual(_private_json(file)['total'], 2)
        link = Path(self.temp.name) / 'link'; link.symlink_to(file)
        with self.assertRaises(PublishError): _private_json(link)


if __name__ == '__main__':
    unittest.main()
