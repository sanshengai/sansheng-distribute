"""Weibo video: executor stopped after the single file selection (no fill, no send)
-> author-confirmed release, and the root fix for regional chunk hosts (2026-11-01 S4E07)."""
import contextlib,sys,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import browser_preinput as recovery
import douyin_unsent_release as unsent
import weibo_video_upload as video
from publish_core import PublishError,read_json,write_json
from test_preinput_author_confirmation import AuthorConfirmationTests,BOUND

class WeiboRelease(unittest.TestCase):
 def setUp(self):
  self.t=AuthorConfirmationTests('test_other_platforms_still_require_original_window');self.t.setUp()
  self.t.saved=dict(BOUND,state='selected_once',target_id='owned',anchor_target_id='anchor',tracking_scope='chrome_session_only',
     source_sha256='s',source_size=10,upload_events=[{'path':'/2/fileplatform/init.json'}],upload_errors=[],source_chunk_count=2)
  write_json(self.t.marker,self.t.saved)
  self.t.request['target']['platform']='weibo';write_json(self.t.root/'requests'/f'{self.t.key}.json',self.t.request)
  self.t.pack['platform']='weibo'
 def tearDown(self):self.t.tearDown()
 def page(self,url):p=MagicMock();p.url=url;return p
 def rec(self,action,pages):
  ctx=MagicMock();ctx.pages=pages;browser=MagicMock();browser.contexts=[ctx]
  ctx.new_cdp_session.return_value.send.return_value={'targetInfo':{'targetId':'owned'}}
  @contextlib.contextmanager
  def connect(profile):yield browser
  with patch.object(recovery,'connect',connect):
   return recovery.recover(action,self.t.request,self.t.pack,BOUND,self.t.marker,{})
 def test_needs_author_confirmation(self):
  self.assertIsNone(self.rec('verify',[]))
 def test_release_then_fresh_upload(self):
  self.assertEqual(self.t.confirm(),0)
  pg=self.page('https://weibo.com/upload/channel')
  r=self.rec('verify',[pg]);self.assertEqual(r['state'],'not_submitted')
  self.t.request['job']['state']='prepared'
  self.assertIsNone(self.rec('execute',[pg]));pg.close.assert_called_once()
  self.assertFalse(self.t.marker.exists());self.assertTrue(list(self.t.marker.parent.glob('*.released-*')))
 def test_window_left_editor_refuses(self):
  self.assertEqual(self.t.confirm(),0)
  with self.assertRaises(PublishError):self.rec('verify',[self.page('https://weibo.com/u/1')])
 def test_filled_or_sent_attempts_not_releasable(self):
  for extra in (dict(submission_attempted_at=1),dict(native_video={'w':1}),dict(cover={'x':1}),dict(state='cover_uploading')):
   self.assertFalse(unsent.eligible(dict(self.t.saved,**extra),'weibo'),extra)
  self.assertTrue(unsent.eligible(self.t.saved,'weibo'))
  self.assertFalse(unsent.eligible(self.t.saved,'x'))

class RegionalChunkHost(unittest.TestCase):
 def resp(self,url,body):
  r=MagicMock();r.url=url;r.status=200;r.json.return_value=body;return r
 def test_regional_upload_host_is_captured(self):
  c=video.Capture(lambda e,x:None)
  q='?media_id=123456&size=5&index=3&count=11&start_loc=0&type=video'
  c.response(self.resp('https://upbj.video.weibocdn.com/2/fileplatform/upload.json'+q,{'result':True}))
  c.response(self.resp('https://up.video.weibocdn.com/2/fileplatform/upload.json'+q,{'result':True}))
  self.assertEqual(len(c.events),2);self.assertEqual(c.errors,[])
 def test_foreign_hosts_still_ignored(self):
  c=video.Capture(lambda e,x:None)
  for u in ('https://upbj.video.weibocdn.com.evil.test/2/fileplatform/upload.json','https://evil.test/2/fileplatform/upload.json',
            'https://upbj.video.weibocdn.com/2/fileplatform/init.json','https://x.upbj.video.weibocdn.com/2/fileplatform/upload.json'):
   c.response(self.resp(u,{'result':True}))
  self.assertEqual(c.events,[])

if __name__=='__main__':unittest.main()
