import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_core import PublishError
from x_mentions_candidate import sync_mentions, _page


def item(ident, author='222'):
    return {'id':ident, 'author_id':author, 'conversation_id':'333',
            'text':'hello', 'created_at':'2026-01-01T00:00:00Z'}


class XMentionTests(unittest.TestCase):
    def test_explicit_paid_gate_before_network(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'x_mentions_candidate.py'
        with tempfile.TemporaryDirectory() as root:
            proc = subprocess.run([sys.executable, str(script), '--state-dir',root,
                                   '--account','demo','--user-id','111','--token-env','DOES_NOT_EXIST'],
                                   capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse((Path(root) / 'interaction-inbox.sqlite3').exists())

    def test_dedup_and_only_partial_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            calls=[]
            def client(path, params):
                calls.append((path,params))
                if path.endswith('/me'):
                    return {'data':{'id':'111'}}
                if params.get('pagination_token'):
                    return {'data':[item('102')], 'meta':{'result_count':1}}
                return {'data':[item('101'),item('102')], 'meta':{'result_count':2,'next_token':'token'}}
            first=sync_mentions(root,'demo','111',client)
            sync_mentions(root,'demo','111',client)
            self.assertEqual(first['state'], 'partial')
            self.assertIn('全部回复', first['note'])
            with closing(sqlite3.connect(Path(root)/'interaction-inbox.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM interaction_items').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT state FROM interaction_sources').fetchone()[0],'partial')
            self.assertNotIn('expansions', calls[1][1])
            self.assertEqual(calls[1][1]['tweet.fields'], 'created_at,conversation_id,author_id')
            self.assertEqual(calls[1][1]['max_results'], 10)
            self.assertEqual(calls[2][1]['pagination_token'], 'token')

    def test_reset_cursor_has_no_token_or_paid_requirement(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'x_mentions_candidate.py'
        with tempfile.TemporaryDirectory() as root:
            def client(path,params):
                return {'data':{'id':'111'}} if path.endswith('/me') else {'data':[item('101')], 'meta':{'result_count':1,'next_token':'next'}}
            sync_mentions(root,'demo','111',client,max_pages=1)
            proc = subprocess.run([sys.executable,str(script),'--state-dir',root,'--account','demo',
                                   '--user-id','111','--reset-cursor'],capture_output=True,text=True)
            self.assertEqual(proc.returncode,0)
            self.assertFalse(json.loads(proc.stdout)['network'])
            with closing(sqlite3.connect(Path(root)/'interaction-inbox.sqlite3')) as db:
                self.assertIsNone(db.execute('SELECT next_token FROM interaction_x_cursors').fetchone()[0])
                self.assertEqual(db.execute('SELECT count(*) FROM interaction_items').fetchone()[0],1)

    def test_wrong_identity_never_fetches_mentions(self):
        with tempfile.TemporaryDirectory() as root:
            calls=[]
            def client(path, params):
                calls.append(path)
                return {'data':{'id':'999'}}
            with self.assertRaises(PublishError):
                sync_mentions(root,'demo','111',client)
            self.assertEqual(calls,['/2/users/me'])
            with closing(sqlite3.connect(Path(root)/'interaction-inbox.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT state FROM interaction_sources').fetchone()[0],'error')

    def test_missing_token_records_error_only_for_explicit_target(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'x_mentions_candidate.py'
        with tempfile.TemporaryDirectory() as root:
            def client(path,params):
                return {'data':{'id':'111'}} if path.endswith('/me') else {'meta':{'result_count':0}}
            sync_mentions(root,'demo','111',client)
            with closing(sqlite3.connect(Path(root)/'interaction-inbox.sqlite3')) as db:
                db.execute("INSERT INTO interaction_sources(platform,account,channel_id,state) VALUES('x','other','222','partial')")
                db.commit()
            prefix=[sys.executable,str(script),'--state-dir',root,'--account','demo',
                    '--user-id','111','--token-env','THIS_TEST_TOKEN_SHOULD_NOT_EXIST_82532']
            denied=subprocess.run(prefix,capture_output=True,text=True)
            self.assertNotEqual(denied.returncode,0)
            with closing(sqlite3.connect(Path(root)/'interaction-inbox.sqlite3')) as db:
                self.assertEqual(db.execute("SELECT state FROM interaction_sources WHERE account='demo'").fetchone()[0],'partial')
            missing=subprocess.run(prefix+['--accept-paid-api'],capture_output=True,text=True)
            self.assertNotEqual(missing.returncode,0)
            with closing(sqlite3.connect(Path(root)/'interaction-inbox.sqlite3')) as db:
                states=dict(db.execute("SELECT account,state FROM interaction_sources"))
                self.assertEqual(states,{'demo':'error','other':'partial'})

    def test_invalid_user_id_does_not_create_or_touch_other_source(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'x_mentions_candidate.py'
        with tempfile.TemporaryDirectory() as root:
            proc=subprocess.run([sys.executable,str(script),'--state-dir',root,'--account','demo',
                                 '--user-id','bad','--accept-paid-api'],capture_output=True,text=True)
            self.assertNotEqual(proc.returncode,0)
            self.assertFalse((Path(root)/'interaction-inbox.sqlite3').exists())

    def test_empty_and_partial_error_are_distinct(self):
        self.assertEqual(_page(lambda p,q:{'meta':{'result_count':0}},'111',None,10),([],None))
        with self.assertRaises(PublishError):
            _page(lambda p,q:{'data':[item('101')],'meta':{'result_count':0}},'111',None,10)

    def test_reject_invalid_item_and_preserve_previous_rows(self):
        with tempfile.TemporaryDirectory() as root:
            def good(path, params):
                return {'data':{'id':'111'}} if path.endswith('/me') else {'data':[item('101')],'meta':{'result_count':1}}
            sync_mentions(root,'demo','111',good)
            def broken(path, params):
                return {'data':{'id':'111'}} if path.endswith('/me') else {'data':[{'id':'102'}],'meta':{'result_count':1}}
            with self.assertRaises(PublishError):
                sync_mentions(root,'demo','111',broken)
            with closing(sqlite3.connect(Path(root)/'interaction-inbox.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM interaction_items').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT state FROM interaction_sources').fetchone()[0],'error')


if __name__ == '__main__': unittest.main()
