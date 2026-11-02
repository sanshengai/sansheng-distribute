import contextlib,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import browser_preinput as recovery
import confirm_unsubmitted
from publish_core import PublishError,read_json,write_json,digest

BOUND=dict(identity='100000001',pack_digest='sealed',cdp_url='http://127.0.0.1:9333')

class AuthorConfirmationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);self.root=root
  (root/'requests').mkdir();self.key='k'*8
  self.marker=root/'requests'/f'{self.key}.browser-attempt.json'
  self.saved=dict(BOUND,state='owned_window_opened',target_id='owned',anchor_target_id='anchor',native_user_id='1',
                  phase='upload',video=None,video_upload_events=[],video_upload_errors=[])
  write_json(self.marker,self.saved)
  self.request=dict(target=dict(platform='douyin'),manifest_path=str(root/'snapshots'/'m.json'),
                    job=dict(key=self.key,state='submission_unknown',remote_id=None))
  write_json(root/'requests'/f'{self.key}.json',self.request)
  ev=root/'list.json';ev.write_text('{}')
  self.listing=root/'listing.json'
  self.listing.write_text(json.dumps(dict(observed_at='2026-10-31T15:20:00+08:00',account_identity='100000001',query='S3E',matches=[],evidence_path=str(ev))))
  self.pack=dict(platform='douyin',account='a',manifest_digest='sealed',target_id='t')
 def tearDown(self):self.tmp.cleanup()
 def confirm(self,**kw):
  args=['--state-dir',str(self.root),'--key',self.key,'--statement','作者原话：重新上传呗（2026-10-31 15:13 台北）',
        '--confirmed-at','2026-10-31T15:13:00+08:00','--listing',str(self.listing)]
  return confirm_unsubmitted.main(args)
 def run_recover(self,action,pages):
  ctx=MagicMock();ctx.pages=pages;browser=MagicMock();browser.contexts=[ctx]
  @contextlib.contextmanager
  def connect(profile):yield browser
  import browser_runtime,douyin_browser
  with patch.object(recovery,'connect',connect),patch.object(browser_runtime,'select_page'),patch.object(douyin_browser,'assert_identity'):
   return recovery.recover(action,self.request,self.pack,BOUND,self.marker,{})
 def test_without_confirmation_lost_window_stays_unknown(self):
  with self.assertRaises(PublishError):self.run_recover('verify',[])
  self.assertEqual(read_json(self.marker)['state'],'owned_window_opened')
 def test_confirmed_lost_window_verifies_then_executes_once(self):
  self.assertEqual(self.confirm(),0)
  r=self.run_recover('verify',[])
  self.assertEqual(r['state'],'not_submitted');self.assertEqual(r['absence_scope'],'author_confirmed_before_first_platform_upload')
  self.assertEqual(read_json(self.marker)['preinput_scope'],'author_confirmed_owned_window_lost')
  self.request['job']['state']='prepared'
  self.assertIsNone(self.run_recover('execute',[]))
  self.assertFalse(self.marker.exists())
 def test_execute_before_verify_is_rejected(self):
  self.assertEqual(self.confirm(),0);self.request['job']['state']='prepared'
  with self.assertRaises(PublishError):self.run_recover('execute',[])
 def test_confirmation_refused_for_ineligible_or_sent_attempts(self):
  for change in (dict(submission_attempted_at=1),dict(native_input_checkpointed_at=1),dict(accepted={}),
                 dict(state='filled'),dict(phase='fields'),dict(anchor_target_id=None)):
   with self.subTest(change=change):
    write_json(self.marker,dict(self.saved,**change));self.assertEqual(self.confirm(),2)
    self.assertFalse(recovery.confirmation_path(self.marker).exists())
 def test_confirmation_requires_empty_listing_and_unknown_job_without_id(self):
  d=json.loads(self.listing.read_text());d['matches']=['S3E01'];self.listing.write_text(json.dumps(d))
  self.assertEqual(self.confirm(),2);self.assertFalse(recovery.confirmation_path(self.marker).exists())
  d['matches']=[];d['account_identity']='other';self.listing.write_text(json.dumps(d))
  self.assertEqual(self.confirm(),2)
  d['account_identity']='100000001';self.listing.write_text(json.dumps(d))
  self.request['job']['remote_id']='123';write_json(self.root/'requests'/f'{self.key}.json',self.request)
  self.assertEqual(self.confirm(),2)
 def test_changed_attempt_after_confirmation_is_rejected(self):
  self.assertEqual(self.confirm(),0)
  write_json(self.marker,dict(self.saved,target_id='other'))
  with self.assertRaises(PublishError):self.run_recover('verify',[])
 def test_other_platforms_still_require_original_window(self):
  self.assertNotIn('weibo',recovery.AUTHOR_CONFIRMABLE)  # X 早期窗口阶段另有作者确认（2026-11-01）

if __name__=='__main__':unittest.main()

class DouyinPublishUploadWaitTests(unittest.TestCase):
 def test_publish_waits_for_native_file_input_before_empty_check(self):
  src=(Path(__file__).resolve().parents[1]/'scripts'/'douyin_publish_browser.py').read_text()
  goto=src.index("home.UPLOAD,wait_until='domcontentloaded'");wait=src.index("wait_for(state='attached',timeout=15000)",goto)
  self.assertLess(wait,src.index('home.empty_upload(page)',goto))

class ArchivedNoNewAttemptTests(unittest.TestCase):
 def test_only_verify_with_verified_archive_and_no_new_record(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);marker=root/'k.browser-attempt.json'
   base=dict(BOUND,state='preinput_absence_verified',preinput_original_state='owned_window_opened',target_id='o',anchor_target_id='a',
             phase='upload',video=None,video_upload_events=[],video_upload_errors=[])
   pack=dict(platform='douyin',account='a',manifest_digest='m',target_id='t')
   req=dict(job=dict(state='submission_unknown',remote_id=None))
   self.assertIsNone(recovery.recover('verify',req,pack,BOUND,marker,{}))
   write_json(root/'k.browser-attempt.json.preinput-1',base)
   r=recovery.recover('verify',req,pack,BOUND,marker,{})
   self.assertEqual(r['state'],'not_submitted')
   self.assertIsNone(recovery.recover('execute',req,pack,BOUND,marker,{}))
   for bad in (dict(submission_attempted_at=1),dict(state='owned_window_opened'),dict(identity='x')):
    write_json(root/'k.browser-attempt.json.preinput-1',dict(base,**bad))
    self.assertIsNone(recovery.recover('verify',req,pack,BOUND,marker,{}))
   write_json(root/'k.browser-attempt.json.preinput-1',base)
   self.assertIsNone(recovery.recover('verify',dict(job=dict(state='submission_unknown',remote_id='9')),pack,BOUND,marker,{}))


class DouyinReleaseAndRootFixTests(unittest.TestCase):
 def setUp(self):
  self.t=AuthorConfirmationTests('test_other_platforms_still_require_original_window');self.t.setUp()
  self.t.saved.update(state='attempted',video_source={'size':1},video_upload_events=[],video_upload_errors=['native_video_commit_unrecorded'])
  write_json(self.t.marker,self.t.saved)
 def tearDown(self):self.t.tearDown()
 def pages(self,url):
  p=MagicMock();p.url=url;return p
 def run_release(self,action,pages):
  import douyin_unsent_release as unsent
  ctx=MagicMock();ctx.pages=pages;browser=MagicMock();browser.contexts=[ctx]
  ctx.new_cdp_session.return_value.send.return_value={'targetInfo':{'targetId':'owned'}}
  @contextlib.contextmanager
  def connect(profile):yield browser
  with patch.object(recovery,'connect',connect):
   return recovery.recover(action,self.t.request,self.t.pack,BOUND,self.t.marker,{})
 def test_uploaded_unsent_needs_confirmation(self):
  self.assertIsNone(self.run_release('verify',[]))
 def test_uploaded_unsent_release_then_fresh_execute(self):
  self.assertEqual(self.t.confirm(),0)
  page=self.pages('https://creator.douyin.com/creator-micro/content/upload')
  r=self.run_release('verify',[page]);self.assertEqual(r['absence_scope'],'author_confirmed_uploaded_never_sent')
  self.t.request['job']['state']='prepared'
  self.assertIsNone(self.run_release('execute',[page]));page.close.assert_called_once()
  self.assertFalse(self.t.marker.exists())
  self.assertTrue(list(self.t.marker.parent.glob('*.released-*')))
 def test_window_on_manager_page_refuses(self):
  self.assertEqual(self.t.confirm(),0)
  with self.assertRaises(PublishError):
   self.run_release('verify',[self.pages('https://creator.douyin.com/creator-micro/content/manage')])
 def test_send_trace_refuses(self):
  write_json(self.t.marker,dict(self.t.saved,submission_attempted_at=1))
  self.assertEqual(self.t.confirm(),2);self.assertIsNone(self.run_release('verify',[]))
 def test_execute_before_release_verify_refused(self):
  self.assertEqual(self.t.confirm(),0);self.t.request['job']['state']='prepared'
  with self.assertRaises(PublishError):self.run_release('execute',[])
 def test_released_archive_without_new_record_is_not_submitted(self):
  self.assertEqual(self.t.confirm(),0);self.run_release('verify',[])
  self.t.request['job']['state']='prepared';self.run_release('execute',[])
  self.t.request['job']['state']='submission_unknown'
  r=recovery.recover('verify',self.t.request,self.t.pack,BOUND,self.t.marker,{})
  self.assertEqual(r['absence_scope'],'archived_preinput_attempt_no_new_record')

class DouyinCandidateTests(unittest.TestCase):
 def test_failed_receipt_keeps_sanitized_identity(self):
  import douyin_upload as up
  body=dict(ResponseMetadata={},Result=dict(Results=[dict(Vid='vFixtureUpload1234567890',
       VideoMeta=dict(Md5='bad',Size=5,Duration=1.0,Width=2,Height=3,Uri='https://x/y?sig=1'),Token='secret')]))
  c=up.sanitized_candidate(json.dumps(body).encode())
  self.assertEqual(c['vid'],'vFixtureUpload1234567890');self.assertEqual(c['metadata']['Md5'],'unrecognized')
  self.assertEqual(c['metadata']['uri_preview_key'],'unrecognized');self.assertNotIn('secret',json.dumps(c))
  self.assertIsNone(up.sanitized_candidate(b'not json'))

class DouyinNoRecordOrderTests(unittest.TestCase):
 def test_attempt_record_precedes_navigation_and_no_record_verify_exists(self):
  src=(Path(__file__).resolve().parents[1]/'scripts'/'douyin_publish_browser.py').read_text()
  self.assertLess(src.index('os.O_WRONLY|os.O_CREAT|os.O_EXCL'),src.index("page.goto('https://creator.douyin.com'+home.UPLOAD"))
  self.assertIn("absence_scope='douyin_no_attempt_record'",src)
  self.assertLess(src.index("absence_scope='douyin_no_attempt_record'"),src.index('with home.connect(profile,writing=True)'))

class VerifiedWindowClosedTests(unittest.TestCase):
 def test_window_verified_then_closed_still_executes_once(self):
  t=AuthorConfirmationTests('test_other_platforms_still_require_original_window');t.setUp()
  try:
   page=MagicMock();page.url='https://creator.douyin.com/creator-micro/content/upload'
   page.locator.return_value.evaluate_all.return_value=True;page.locator.return_value.count.return_value=0
   ctx=MagicMock();ctx.pages=[page];ctx.new_cdp_session.return_value.send.return_value={'targetInfo':{'targetId':'owned'}}
   browser=MagicMock();browser.contexts=[ctx]
   @contextlib.contextmanager
   def connect(profile):yield browser
   import browser_runtime,douyin_browser
   with patch.object(recovery,'connect',connect),patch.object(browser_runtime,'select_page'),patch.object(douyin_browser,'assert_identity'),patch.object(douyin_browser,'origin'),patch.object(douyin_browser,'no_warning'):
    self.assertEqual(recovery.recover('verify',t.request,t.pack,BOUND,t.marker,{})['absence_scope'],'owned_editor_before_first_platform_upload')
    ctx.pages=[]
    self.assertEqual(recovery.recover('verify',t.request,t.pack,BOUND,t.marker,{})['state'],'not_submitted')
    t.request['job']['state']='prepared'
    self.assertIsNone(recovery.recover('execute',t.request,t.pack,BOUND,t.marker,{}))
    self.assertFalse(t.marker.exists())
  finally:t.tearDown()
