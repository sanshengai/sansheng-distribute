import contextlib,hashlib,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import douyin_uploaded_resume as r
import browser_preinput as pre
import douyin_publish_browser as sender
from publish_core import PublishError,write_json,read_json,digest

BOUND=dict(identity='100000001',pack_digest='sealed',cdp_url='http://127.0.0.1:9333',browser_profile='/p')
EVENT=dict(vid='vFixtureUpload1234567890',metadata=dict(Md5=None,Size=3,Duration=1.0,Width=2,Height=4,Uri='tos-cn-v-a/b'),observed_at=1)

class ResumeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);self.root=root
  self.key='k'*8;self.request_path=root/f'{self.key}.json';self.marker=root/f'{self.key}.browser-attempt.json'
  self.saved=dict(BOUND,state='attempted',phase='upload',video=None,target_id='owned',anchor_target_id='a',native_user_id='1',
                  video_source=dict(size=3),video_upload_events=[EVENT],video_upload_errors=[])
  write_json(self.marker,self.saved)
  self.request=dict(job=dict(key=self.key,state='submission_unknown',remote_id=None),target=dict(platform='douyin'))
  assets=root/f'{self.key}.browser-assets';assets.mkdir();(assets/'v.mp4').write_bytes(b'abc')
  self.pack=dict(platform='douyin',account='a',manifest_digest='m',target_id='t',
                 assets=[dict(id='v',kind='video',absolute_path='/x/v.mp4',sha256=hashlib.sha256(b'abc').hexdigest())])
 def tearDown(self):self.tmp.cleanup()
 def test_applies_only_to_recorded_receipt_without_send_or_fill(self):
  self.assertTrue(r.applies(self.saved))
  for change in (dict(video_upload_errors=['x']),dict(video_upload_events=[]),dict(submission_attempted_at=1),
                 dict(accepted={}),dict(phase='declaration'),dict(video={}),dict(state='filled')):
   with self.subTest(change=change):self.assertFalse(r.applies(dict(self.saved,**change)))
  s=dict(self.saved);del s['video_source'];self.assertFalse(r.applies(s))
 def test_restaged_requires_same_bytes(self):
  self.assertEqual(len(r.restaged(self.pack,self.request_path)),1)
  (self.root/f'{self.key}.browser-assets'/'v.mp4').write_bytes(b'changed')
  with self.assertRaises(PublishError):r.restaged(self.pack,self.request_path)
 def test_needs_author_confirmation_and_no_remote_id(self):
  with self.assertRaises(PublishError):r.run('verify',self.request,self.request_path,self.pack,BOUND,self.marker,{},self.saved)
  req=dict(self.request,job=dict(self.request['job'],remote_id='9'))
  with patch.object(pre,'author_confirmation',return_value='d'):
   with self.assertRaises(PublishError):r.run('verify',req,self.request_path,self.pack,BOUND,self.marker,{},self.saved)
 def run_with(self,action,pages,check=None):
  ctx=MagicMock();ctx.pages=pages;conn=MagicMock();conn.contexts=[ctx]
  ctx.new_cdp_session.return_value.send.return_value={'targetInfo':{'targetId':'owned'}}
  import douyin_browser as home
  @contextlib.contextmanager
  def connect(profile,writing=False):yield conn
  with patch.object(home,'connect',connect),patch.object(pre,'author_confirmation',return_value='d'),\
       patch.object(r,'check_editor',side_effect=check or (lambda *a:dict(video={},source_video={},cloud_video={}))):
   return r.run(action,self.request,self.request_path,self.pack,BOUND,self.marker,{},read_json(self.marker))
 def test_verify_requires_window_and_editor_checks(self):
  with self.assertRaises(PublishError):self.run_with('verify',[])
  def bad(*a):raise PublishError('sha mismatch')
  with self.assertRaises(PublishError):self.run_with('verify',[MagicMock()],bad)
  self.assertEqual(read_json(self.marker)['state'],'attempted')
  res=self.run_with('verify',[MagicMock()])
  self.assertEqual(res['absence_scope'],'author_confirmed_uploaded_unfilled_unsent')
  self.assertEqual(read_json(self.marker)['state'],r.VERIFIED)
 def test_execute_requires_prior_verify_and_prepared(self):
  self.request['job']['state']='prepared'
  with self.assertRaises(PublishError):self.run_with('execute',[MagicMock()])
 def test_dispatch_prefers_resume_over_release(self):
  src=(Path(__file__).resolve().parents[1]/'scripts'/'douyin_publish_browser.py').read_text()
  self.assertLess(src.index('uploaded.applies(current)'),src.index('recovered = recover('))

class AnchorTests(unittest.TestCase):
 def test_missing_anchor_opens_fresh_background_tab(self):
  conn=MagicMock()
  with patch.object(sender.home,'select_page',side_effect=PublishError('x')),patch.object(sender,'fresh_anchor',return_value='new') as f:
   self.assertEqual(sender.anchor_for(conn,'gone'),('new',True));f.assert_called_once()
  with patch.object(sender.home,'select_page',return_value='old'):
   self.assertEqual(sender.anchor_for(conn,'a'),('old',False))
 def test_fresh_anchor_uses_background_target(self):
  ctx=MagicMock();ctx.pages=[MagicMock()];conn=MagicMock();conn.contexts=[ctx]
  sender.fresh_anchor(conn)
  args=conn.new_browser_cdp_session.return_value.send.call_args[0]
  self.assertEqual(args[0],'Target.createTarget');self.assertTrue(args[1]['background'])

if __name__=='__main__':unittest.main()

class ReleaseChoiceTests(ResumeTests):
 def test_release_choice_bound_to_attempt(self):
  from publish_core import digest
  self.assertFalse(r.release_chosen(self.marker,self.saved))
  write_json(r.release_path(self.marker),dict(attempt_digest=digest(r.original(self.saved))))
  self.assertTrue(r.release_chosen(self.marker,self.saved))
  write_json(r.release_path(self.marker),dict(attempt_digest='other'))
  with self.assertRaises(PublishError):r.release_chosen(self.marker,self.saved)
 def test_dispatch_skips_resume_when_release_chosen(self):
  src=(Path(__file__).resolve().parents[1]/'scripts'/'douyin_publish_browser.py').read_text()
  self.assertIn('not uploaded.release_chosen(marker,current)',src)

class PartialTests(unittest.TestCase):
 def base(self,**kw):
  d=dict(state='uploaded',phase='topic_1_search',target_id='t',video_source={},video_upload_events=[{}],video_upload_errors=[])
  d.update(kw);return d
 def test_partial_fields_apply(self):
  for ph in ('fields','topic_1_search','topic_3_readback'):
   self.assertTrue(r.applies(self.base(phase=ph)))
 def test_later_or_sent_phases_refused(self):
  for kw in (dict(phase='declaration'),dict(phase='collection'),dict(covers={}),dict(schedule={}),
             dict(submission_attempted_at=1),dict(state='filled',phase='complete'),dict(video_upload_events=[])):
   with self.subTest(kw=kw):self.assertFalse(r.applies(self.base(**kw)))
 def test_original_strips_resume_keys_and_keeps_base_state(self):
  d=self.base(resume_original_state='attempted',resume_verified_at=1,resume_confirmation_digest='x')
  o=r.original(d);self.assertEqual(o['state'],'uploaded');self.assertNotIn('resume_verified_at',o)
  v=dict(d,state=r.VERIFIED,resume_original_state='uploaded');self.assertEqual(r.original(v)['state'],'uploaded')
 def test_partial_clears_own_text_only_in_execute(self):
  src=Path(r.__file__).read_text()
  self.assertIn('if was_partial:\n            clear_fields(page)',src)
  self.assertIn('if not partial(saved):\n        home.assert_empty_fields(page)',src)

class RebindTests(unittest.TestCase):
 def conn(self,pages):
  ctx=MagicMock();ctx.pages=[p for p,_ in pages]
  ids={id(p):t for p,t in pages}
  def sess(page):
   s=MagicMock();s.send.return_value={'targetInfo':{'targetId':ids[id(page)]}};return s
  ctx.new_cdp_session.side_effect=sess;c=MagicMock();c.contexts=[ctx];return c
 def page(self,path='/creator-micro/content/post/video'):
  p=MagicMock();p.url='https://creator.douyin.com'+path;return p
 def test_restored_editor_with_same_preview_rebinds(self):
  import douyin_browser as home
  a,b=self.page(),self.page()
  keys={id(a):'K',id(b):'other'}
  with patch.object(home,'video_identity',side_effect=lambda p:dict(preview_key=keys[id(p)])):
   page,tid=r.owned_page(self.conn([(a,'new'),(b,'x')]),'gone',dict(video=dict(preview_key='K')))
  self.assertIs(page,a);self.assertEqual(tid,'new')
 def test_no_or_ambiguous_match_refused(self):
  import douyin_browser as home
  a,b=self.page(),self.page()
  with patch.object(home,'video_identity',return_value=dict(preview_key='K')):
   with self.assertRaises(PublishError):r.owned_page(self.conn([(a,'1'),(b,'2')]),'gone',dict(video=dict(preview_key='K')))
   with self.assertRaises(PublishError):r.owned_page(self.conn([(a,'1')]),'gone',dict(video=dict(preview_key='Z')))
   with self.assertRaises(PublishError):r.owned_page(self.conn([(a,'1')]),'gone',{})

class CoverPendingTests(unittest.TestCase):
 def base(self,**kw):
  d=dict(state='uploaded',phase='cover_portrait_readback',target_id='t',video_source={},video_upload_events=[{}],video_upload_errors=[])
  d.update(kw);return d
 def test_cover_readback_stop_applies_without_send(self):
  self.assertTrue(r.applies(self.base()));self.assertTrue(r.cover_pending(self.base(state='attempted')))
  self.assertTrue(r.applies(self.base(phase='cover_portrait_complete')))
  for kw in (dict(submission_attempted_at=1),dict(schedule={}),dict(phase='cover_portrait_upload'),dict(phase='readback')):
   with self.subTest(kw=kw):self.assertFalse(r.applies(self.base(**kw)))
 def test_release_covers_cover_readback_stop_without_send(self):
  import douyin_unsent_release as u
  base=self.base(anchor_target_id='a')
  self.assertTrue(u.eligible(base,'douyin'))
  self.assertTrue(u.eligible(dict(base,state='unsent_release_verified',release_original_state='uploaded'),'douyin'))
  for kw in (dict(submission_attempted_at=1),dict(schedule={}),dict(phase='readback'),dict(anchor_target_id=None),dict(state='submission_unknown')):
   with self.subTest(kw=kw):self.assertFalse(u.eligible(self.base(**dict(dict(anchor_target_id='a'),**kw)),'douyin'))
 def test_cover_tolerance_rejects_real_crop(self):
  import douyin_cover as c
  src=dict(width=1080,height=1440)
  self.assertTrue(c.within(dict(width=1078,height=1440),src))
  for w,h in ((1070,1440),(1080,1400),(810,1080)):
   with self.subTest(w=w,h=h):self.assertFalse(c.within(dict(width=w,height=h),src))
 def test_readback_only_never_uploads(self):
  import douyin_cover as c,inspect
  src=inspect.getsource(c.readback_only)
  self.assertNotIn('set_input_files',src);self.assertNotIn('.click(',src)

class UnclickedTests(unittest.TestCase):
 def base(self,**kw):
  d=dict(state='submission_unknown',phase='complete',target_id='t',video_source={},video_upload_events=[{}],video_upload_errors=[],
         covers={'portrait':{}},submission_attempted_at=1.0,click_error_type='PublishError',click_error_phase='before_native_input',native_response_count=0)
  d.update(kw);return d
 def test_proven_unclicked_applies(self):
  self.assertTrue(r.unclicked(self.base()));self.assertTrue(r.applies(self.base()))
 def test_any_input_or_response_refused(self):
  for kw in (dict(click_error_phase='native_input_uncertain'),dict(native_input_checkpointed_at=2.0),dict(native_response_count=1),
             dict(accepted={'id':'1'}),dict(receipt_error='x'),dict(click_reply_unknown=True),dict(native_input_count=1),dict(phase='readback')):
   with self.subTest(kw=kw):self.assertFalse(r.applies(self.base(**kw)))
 def test_scroll_before_click(self):
  src=(Path(r.__file__).parent/'douyin_publish_browser.py').read_text()
  self.assertLess(src.index('scroll_into_view_if_needed'),src.index('submission_attempted_at=time.time()'))
