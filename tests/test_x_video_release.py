"""X video: upload ran but receipts incomplete (executor stopped waiting), never filled or sent
-> author-confirmed release then one fresh upload (2026-11-02 S4E02)."""
import contextlib,sys,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import browser_preinput as recovery
import douyin_unsent_release as unsent
from publish_core import PublishError,write_json
from test_preinput_author_confirmation import AuthorConfirmationTests,BOUND

class XRelease(unittest.TestCase):
 def setUp(self):
  self.t=AuthorConfirmationTests('test_other_platforms_still_require_original_window');self.t.setUp()
  self.t.saved=dict(BOUND,state='before_native_selection',target_id='owned',submission_protocol=2,source_size=10,
     upload_events=[{'command':'INIT','status':202},{'command':'APPENDMULTI','status':204}],upload_errors=[])
  write_json(self.t.marker,self.t.saved)
  self.t.request['target']['platform']='x';write_json(self.t.root/'requests'/f'{self.t.key}.json',self.t.request)
  self.t.pack['platform']='x'
 def tearDown(self):self.t.tearDown()
 def page(self,url,text=''):
  p=MagicMock();p.url=url;p.locator.return_value.evaluate_all.return_value=[text,text];return p
 def rec(self,action,pages):
  ctx=MagicMock();ctx.pages=pages;browser=MagicMock();browser.contexts=[ctx]
  ctx.new_cdp_session.return_value.send.return_value={'targetInfo':{'targetId':'owned'}}
  @contextlib.contextmanager
  def connect(profile):yield browser
  with patch.object(recovery,'connect',connect):
   return recovery.recover(action,self.t.request,self.t.pack,BOUND,self.t.marker,{})
 def test_without_confirmation_not_released(self):
  self.assertIsNone(self.rec('verify',[self.page('https://x.com/compose/post')]))
 def test_release_then_fresh_upload(self):
  self.assertEqual(self.t.confirm(),0)
  pg=self.page('https://x.com/compose/post')
  r=self.rec('verify',[pg]);self.assertEqual(r['state'],'not_submitted')
  self.assertEqual(r['absence_scope'],'author_confirmed_uploaded_never_sent')
  self.t.request['job']['state']='prepared'
  self.assertIsNone(self.rec('execute',[pg]));pg.close.assert_called_once()
  self.assertFalse(self.t.marker.exists());self.assertTrue(list(self.t.marker.parent.glob('*.released-*')))
 def test_window_with_text_or_elsewhere_refuses(self):
  self.assertEqual(self.t.confirm(),0)
  for pg in (self.page('https://x.com/compose/post','「初馨」'),self.page('https://x.com/home')):
   with self.assertRaises(PublishError):self.rec('verify',[pg])
 def test_only_unfilled_unsent_uploads(self):
  s=self.t.saved
  self.assertTrue(unsent.eligible(s,'x'))
  for extra in (dict(submission_attempted_at=1),dict(publication_media={}),dict(native_video={}),dict(cloud_video={}),
                dict(dwell_seconds=5),dict(state='filled'),dict(submission_protocol=1),dict(upload_events=[],upload_errors=[])):
   self.assertFalse(unsent.eligible(dict(s,**extra),'x'),extra)

if __name__=='__main__':unittest.main()
