"""Deterministic offline tests; no credentials, network or public writes."""
import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from interaction_inbox import open_db, run_cli, sync_youtube
from publish_core import PublishError
from youtube_comments_candidate import SCOPE

CHANNEL = 'UC' + 'a' * 22
VIDEO = 'abcdefghijk'

def comment(id, parent=None, author='other'):
    sn = {'textDisplay':'内容 '+id, 'authorChannelId':{'value':author},
          'authorDisplayName':'作者', 'publishedAt':'2026-01-01T00:00:00Z'}
    if parent: sn['parentId'] = parent
    return {'id':id, 'snippet':sn}

def thread(id):
    return {'id':'thread-'+id, 'snippet':{'videoId':VIDEO, 'topLevelComment':comment(id)}}

class Client:
    def __init__(self):
        self.thread_pages = {'':([thread('a')], 'two'), 'two':([thread('b')], None)}
        self.reply_pages = {'a':{'':([comment('reply-a','a')], None)}, 'b':{'':([],None)}}
        self.error = False
        self.bad_parents = set()
        self.calls = []
    def granted_scopes(self): return {SCOPE}
    def call(self, method, url, **kw):
        self.calls.append((method,url))
        assert method == 'GET'
        if self.error: raise PublishError('network failure')
        path = urlsplit(url).path
        q = parse_qs(urlsplit(url).query)
        if path.endswith('/channels'): return 200,{},json.dumps({'items':[{'id':CHANNEL}]}).encode()
        token = q.get('pageToken',[''])[0]
        if path.endswith('/commentThreads'): data, following = self.thread_pages[token]
        else:
            if q['parentId'][0] in self.bad_parents: raise PublishError('inaccessible parent')
            data, following = self.reply_pages[q['parentId'][0]][token]
        out = {'items':data}
        if following: out['nextPageToken'] = following
        return 200,{},json.dumps(out).encode()

class InboxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'private'; self.client = Client()
    def rows(self):
        with open_db(self.root) as db:
            return [dict(r) for r in db.execute('SELECT * FROM interaction_items ORDER BY comment_id')]
    def test_pagination_deduplicates_and_later_reply_is_discovered(self):
        result = sync_youtube(self.client,self.root,'owner',CHANNEL)
        self.assertEqual(result['state'],'complete')
        self.assertEqual([r['comment_id'] for r in self.rows()], ['a','b','reply-a'])
        with open_db(self.root) as db:
            db.execute("UPDATE interaction_items SET local_status='handled' WHERE comment_id='a'")
        self.client.reply_pages['a'][''] = ([comment('reply-a','a'),comment('new-reply','a')],None)
        sync_youtube(self.client,self.root,'owner',CHANNEL)
        rows = self.rows()
        self.assertEqual(len(rows),4)
        self.assertEqual(next(r for r in rows if r['comment_id']=='a')['local_status'],'handled')
        self.assertEqual(next(r for r in rows if r['comment_id']=='new-reply')['parent_id'],'a')
        self.assertEqual((self.root / 'interaction-inbox.sqlite3').stat().st_mode & 0o777,0o600)
    def test_limits_resume_thread_and_reply_pages_without_skipping(self):
        self.client.thread_pages = {'':([thread('a')],'two'), 'two':([thread('b')],'three'), 'three':([thread('c')],None)}
        self.client.reply_pages['c'] = {'':([],None)}
        self.client.reply_pages['a'] = {'':([comment('r1','a')],'two'), 'two':([comment('r2','a')],'three'), 'three':([comment('r3','a')],None)}
        first = sync_youtube(self.client,self.root,'owner',CHANNEL,max_thread_pages=2,max_parents=1,max_reply_pages=2)
        self.assertEqual(first['state'],'partial')
        self.assertFalse(first['threads_complete'])
        self.assertEqual(first['reply_incomplete'],1)
        second = sync_youtube(self.client,self.root,'owner',CHANNEL,max_thread_pages=2,max_parents=1,max_reply_pages=2)
        self.assertEqual(second['state'],'partial')
        self.assertEqual([r['comment_id'] for r in self.rows()], ['a','b','c','r1','r2'])
        third = sync_youtube(self.client,self.root,'owner',CHANNEL,max_thread_pages=2,max_parents=1,max_reply_pages=2)
        self.assertEqual(third['state'],'partial')
        sync_youtube(self.client,self.root,'owner',CHANNEL,max_thread_pages=2,max_parents=1,max_reply_pages=2)
        self.assertEqual([r['comment_id'] for r in self.rows()], ['a','b','c','r1','r2','r3'])
        with open_db(self.root) as db:
            self.assertIsNotNone(db.execute("SELECT last_complete_scan_at FROM interaction_parents WHERE parent_id='b'").fetchone()[0])
    def test_continuation_tail_does_not_claim_complete(self):
        self.client.thread_pages={'':([thread('a')],'two'),'two':([thread('b')],'three'),'three':([thread('c')],None)}
        self.client.reply_pages['c']={'':([],None)}
        first = sync_youtube(self.client,self.root,'owner',CHANNEL,max_thread_pages=2,max_parents=20)
        self.client.thread_pages[''] = ([thread('a'),thread('new')],'two')
        self.client.reply_pages['new']={'':([],None)}
        second = sync_youtube(self.client,self.root,'owner',CHANNEL,max_thread_pages=2,max_parents=20)
        self.assertEqual(first['state'],'partial')
        self.assertTrue(second['threads_complete'])
        self.assertEqual(second['parents_scanned'],4)
        self.assertEqual(second['state'],'partial')
        self.assertIn('new',[r['comment_id'] for r in self.rows()])
        self.assertEqual(sync_youtube(self.client,self.root,'owner',CHANNEL,max_thread_pages=3)['state'],'complete')
    def test_reply_continuation_refreshes_first_page_without_claiming_complete(self):
        self.client.thread_pages={'':([thread('a')],None)}
        self.client.reply_pages['a']={'':([comment('r1','a')],'two'), 'two':([comment('r2','a')],'three'), 'three':([comment('r3','a')],None)}
        first=sync_youtube(self.client,self.root,'owner',CHANNEL,max_reply_pages=2)
        self.client.reply_pages['a'][''] = ([comment('r1','a'),comment('recent','a')],'two')
        second=sync_youtube(self.client,self.root,'owner',CHANNEL,max_reply_pages=2)
        self.assertEqual(first['state'],'partial')
        self.assertEqual(second['state'],'partial')
        self.assertEqual(second['parents_scanned'],0)
        self.assertIn('recent',[r['comment_id'] for r in self.rows()])
        self.assertEqual(sync_youtube(self.client,self.root,'owner',CHANNEL,max_reply_pages=3)['state'],'complete')
    def test_failed_sync_preserves_items_and_reports_error(self):
        sync_youtube(self.client,self.root,'owner',CHANNEL)
        self.client.error = True
        with self.assertRaises(PublishError): sync_youtube(self.client,self.root,'owner',CHANNEL)
        self.assertEqual(len(self.rows()),3)
        with open_db(self.root) as db:
            src = db.execute('SELECT * FROM interaction_sources').fetchone()
            self.assertEqual(src['state'],'error')
            self.assertIsNotNone(src['last_success_at'])
    def test_empty_and_bad_parent_and_cycle(self):
        self.client.thread_pages = {'':([],None)}
        self.assertEqual(sync_youtube(self.client,self.root,'owner',CHANNEL)['state'],'complete')
        self.assertEqual(self.rows(),[])
        self.client.thread_pages = {'':([thread('a')],None)}
        self.client.reply_pages['a'][''] = ([comment('bad','wrong')],None)
        with self.assertRaises(PublishError): sync_youtube(self.client,self.root,'owner',CHANNEL)
        self.client.reply_pages['a'][''] = ([], 'repeat')
        self.client.reply_pages['a']['repeat'] = ([], 'repeat')
        self.assertEqual(sync_youtube(self.client,self.root,'owner',CHANNEL)['state'],'partial')
    def test_cli_mark_is_local_and_failed_auth_is_not_empty_result(self):
        profile = Path(self.temp.name)/'profile.json'
        profile.write_text(json.dumps({'accounts':{'owner':{'platforms':['youtube'],
            'public_identities':{'youtube':CHANNEL},'youtube_token_env':'TEST_YOUTUBE_TOKEN'}}}))
        prefix = ['--profile',str(profile),'--account','owner','--state-dir',str(self.root)]
        self.assertEqual(run_cli(prefix+['status'])['state'],'not_checked')
        with patch.dict('os.environ',{'TEST_YOUTUBE_TOKEN':'testing'}):
            run_cli(prefix+['sync-youtube'],client_factory=lambda _:self.client)
            self.assertEqual(run_cli(prefix+['mark','--comment-id','a','--status','handled'])['status'],'handled')
            run_cli(prefix+['sync-youtube'],client_factory=lambda _:self.client)
            self.assertEqual(run_cli(prefix+['status'])['counts']['handled'],1)
            self.assertNotIn('body',run_cli(prefix+['list'])['items'][0])
            self.assertIn('body',run_cli(prefix+['list','--include-text'])['items'][0])
            self.assertEqual(run_cli(prefix+['reset-cursors'])['state'],'cursors_reset')
        with patch.dict('os.environ',{'TEST_YOUTUBE_TOKEN':''}):
            with self.assertRaises(PublishError): run_cli(prefix+['sync-youtube'])
            self.assertEqual(run_cli(prefix+['status'])['state'],'error')
    def test_watch_stops_after_failures_and_rejects_fast_interval(self):
        profile = Path(self.temp.name)/'profile.json'
        profile.write_text(json.dumps({'accounts':{'owner':{'platforms':['youtube'],
            'public_identities':{'youtube':CHANNEL},'youtube_token_env':'TEST_YOUTUBE_TOKEN'}}}))
        prefix=['--profile',str(profile),'--account','owner','--state-dir',str(self.root)]
        with self.assertRaises(PublishError): run_cli(prefix+['watch-youtube','--interval-seconds','1'])
        self.client.error=True
        sleeps=[]
        with patch.dict('os.environ',{'TEST_YOUTUBE_TOKEN':'testing'}), redirect_stdout(io.StringIO()):
            result=run_cli(prefix+['watch-youtube','--cycles','5'],client_factory=lambda _:self.client,sleep_fn=sleeps.append)
        self.assertEqual(result['state'],'error')
        self.assertEqual(result['cycles'],3)
        self.assertEqual(sleeps,[1800,1800])
        self.assertTrue(all(method=='GET' for method,_ in self.client.calls))
    def test_watch_continues_with_inaccessible_parent(self):
        profile=Path(self.temp.name)/'profile.json'
        profile.write_text(json.dumps({'accounts':{'owner':{'platforms':['youtube'],
            'public_identities':{'youtube':CHANNEL},'youtube_token_env':'TEST_YOUTUBE_TOKEN'}}}))
        prefix=['--profile',str(profile),'--account','owner','--state-dir',str(self.root)]
        self.client.bad_parents.add('a')
        sleeps=[]
        with patch.dict('os.environ',{'TEST_YOUTUBE_TOKEN':'testing'}), redirect_stdout(io.StringIO()):
            result=run_cli(prefix+['watch-youtube','--cycles','4'],client_factory=lambda _:self.client,sleep_fn=sleeps.append)
        self.assertEqual(result['state'],'partial')
        self.assertEqual(result['cycles'],4)
        self.assertEqual(len(sleeps),3)
        with open_db(self.root) as db:
            self.assertIsNotNone(db.execute("SELECT last_complete_scan_at FROM interaction_parents WHERE parent_id='b'").fetchone()[0])

    def test_watch_notification_is_opt_in_and_failure_does_not_stop_sync(self):
        profile=Path(self.temp.name)/'profile.json'
        profile.write_text(json.dumps({'accounts':{'owner':{'platforms':['youtube'],
            'public_identities':{'youtube':CHANNEL},'youtube_token_env':'TEST_YOUTUBE_TOKEN'}}}))
        prefix=['--profile',str(profile),'--account','owner','--state-dir',str(self.root)]
        output=io.StringIO()
        with patch.dict('os.environ',{'TEST_YOUTUBE_TOKEN':'testing'}), redirect_stdout(output), \
             patch('interaction_notify.check', side_effect=OSError('unavailable')) as check:
            result=run_cli(prefix+['watch-youtube','--cycles','2','--notify-macos'],
                           client_factory=lambda _:self.client,sleep_fn=lambda _:None)
        self.assertEqual(result['cycles'],2)
        self.assertNotEqual(result['state'],'error')
        self.assertEqual(check.call_count,2)
        self.assertIn('"notification": "error"', output.getvalue())
        with patch.dict('os.environ',{'TEST_YOUTUBE_TOKEN':'testing'}), redirect_stdout(io.StringIO()), \
             patch('interaction_notify.check') as check:
            run_cli(prefix+['watch-youtube','--cycles','1'],client_factory=lambda _:self.client,sleep_fn=lambda _:None)
        check.assert_not_called()

    def test_watch_requests_health_warning_when_enabled_and_stopped(self):
        profile=Path(self.temp.name)/'profile.json'
        profile.write_text(json.dumps({'accounts':{'owner':{'platforms':['youtube'],
            'public_identities':{'youtube':CHANNEL},'youtube_token_env':'TEST_YOUTUBE_TOKEN'}}}))
        prefix=['--profile',str(profile),'--account','owner','--state-dir',str(self.root)]
        self.client.error=True
        with patch.dict('os.environ',{'TEST_YOUTUBE_TOKEN':'testing'}), redirect_stdout(io.StringIO()), \
             patch('interaction_notify.notify_monitor_stopped', return_value='requested') as notice:
            result=run_cli(prefix+['watch-youtube','--cycles','5','--notify-macos'],
                           client_factory=lambda _:self.client,sleep_fn=lambda _:None)
        self.assertEqual(result['cycles'],3)
        self.assertEqual(result['notification'],'requested')
        notice.assert_called_once_with()

if __name__ == '__main__': unittest.main()
