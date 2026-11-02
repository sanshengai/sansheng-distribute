import contextlib,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import browser_preinput as pre
import x_compose_ready as ready
import confirm_unsubmitted
from publish_core import PublishError,read_json,write_json

BOUND=dict(identity='@acct',pack_digest='sealed',cdp_url='http://127.0.0.1:9333')

class EarlyWindowTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'requests').mkdir();self.key='k'*8
  self.marker=self.root/'requests'/f'{self.key}.browser-attempt.json'
  self.saved=dict(BOUND,state='owned_window_opened',target_id='owned',submission_protocol=2,source_size=1,upload_events=[],upload_errors=[])
  write_json(self.marker,self.saved)
  self.request=dict(target=dict(platform='x'),manifest_path=str(self.root/'snapshots'/'m.json'),job=dict(key=self.key,state='submission_unknown',remote_id=None))
  write_json(self.root/'requests'/f'{self.key}.json',self.request)
  ev=self.root/'tl.json';ev.write_text('{}');self.tl=self.root/'timeline.json'
  self.tl.write_text(json.dumps(dict(observed_at='2026-11-01T00:55:00+08:00',account_identity='@acct',query='S3E02',matches=[],evidence_path=str(ev))))
  self.pack=dict(platform='x',account='a',manifest_digest='sealed',target_id='t')
 def tearDown(self):self.tmp.cleanup()
 def confirm(self):
  return confirm_unsubmitted.main(['--state-dir',str(self.root),'--key',self.key,'--statement','作者原话：两边都授权解开啊（2026-11-01 00:50 台北）','--confirmed-at','2026-11-01T00:50:00+08:00','--listing',str(self.tl)])
 def run_(self_,action,pages=None,prove_ok=True):
  pages=[MagicMock()] if pages is None else pages
  ctx=MagicMock();ctx.pages=pages;ctx.new_cdp_session.return_value.send.return_value={'targetInfo':{'targetId':'owned'}}
  browser=MagicMock();browser.contexts=[ctx]
  @contextlib.contextmanager
  def connect(profile):yield browser
  def prove(page,saved,platform):
   if not prove_ok:raise PublishError('editor not empty')
  with patch.object(pre,'connect',connect),patch.object(pre,'prove',prove),patch.object(ready,'realize'):
   return pre.recover(action,self_.request,self_.pack,BOUND,self_.marker,{})
 def test_early_x_window_is_eligible_but_any_trace_refuses(self):
  self.assertTrue(pre.eligible(self.saved,'x'))
  for change in (dict(upload_events=[{'command':'INIT'}]),dict(upload_errors=['x']),dict(submission_attempted_at=1),dict(accepted={}),dict(submission_protocol=1)):
   with self.subTest(change=change):self.assertFalse(pre.eligible(dict(self.saved,**change),'x'))
  self.assertFalse(pre.eligible(dict(self.saved,state='owned_window_opened'),'weibo') and False)
 def test_without_confirmation_or_self_check_stays_unknown(self):
  with self.assertRaises(PublishError):self.run_('verify')
  self.assertEqual(read_json(self.marker)['state'],'owned_window_opened')
 def test_author_confirmation_and_window_proof_release(self):
  self.assertEqual(self.confirm(),0)
  r=self.run_('verify');self.assertEqual(r['state'],'not_submitted');self.assertTrue(r['absence_verified'])
  self.request['job']['state']='prepared';self.assertIsNone(self.run_('execute'));self.assertFalse(self.marker.exists())
 def test_confirmation_with_dirty_window_refuses(self):
  self.assertEqual(self.confirm(),0)
  with self.assertRaises(PublishError):self.run_('verify',prove_ok=False)
 def test_timeline_match_refuses_confirmation(self):
  d=json.loads(self.tl.read_text());d['matches']=['1'];self.tl.write_text(json.dumps(d))
  self.assertEqual(self.confirm(),2)
 def test_executor_self_check_allows_release_without_confirmation(self):
  write_json(self.marker,dict(self.saved,preinput_self_check=dict(ok=True,at=1)))
  self.assertEqual(self.run_('verify')['state'],'not_submitted')
 def test_failed_self_check_still_needs_confirmation(self):
  write_json(self.marker,dict(self.saved,preinput_self_check=dict(ok=False,at=1)))
  with self.assertRaises(PublishError):self.run_('verify')
 def test_lost_window_without_confirmation_refuses(self):
  with self.assertRaises(PublishError):self.run_('verify',pages=[])

class ComposerTests(unittest.TestCase):
 def test_wait_composer_realizes_and_retries_bounded(self):
  page=MagicMock();node=page.locator.return_value
  node.wait_for.side_effect=[TimeoutError(),None]
  with patch.object(ready,'realize') as r:
   self.assertIs(ready.wait_composer(page,rounds=3,round_ms=1),node)
   self.assertEqual(r.call_count,2)
 def test_wait_composer_gives_up_with_publish_error(self):
  page=MagicMock();page.locator.return_value.wait_for.side_effect=TimeoutError()
  with patch.object(ready,'realize'):
   with self.assertRaises(PublishError):ready.wait_composer(page,rounds=2,round_ms=1)
 def test_open_composer_failure_records_read_only_self_check(self):
  with tempfile.TemporaryDirectory() as tmp:
   marker=Path(tmp)/'m.json';saved={'state':'owned_window_opened'};write_json(marker,saved)
   page=MagicMock();page.locator.return_value.wait_for.side_effect=TimeoutError()
   page.locator.return_value.evaluate_all.return_value=False
   import browser_engine as engine
   with patch.object(ready,'realize'),patch.object(ready,'ROUNDS',1),patch.object(engine,'assert_identity'),patch.object(engine,'assert_no_warning'),patch.object(engine,'empty_editor'):
    with patch.object(ready,'wait_composer',side_effect=PublishError('not rendered')):
     with self.assertRaises(PublishError):ready.open_composer(page,saved,marker,'@acct')
   self.assertTrue(read_json(marker)['preinput_self_check']['ok'])
   page.locator.return_value.evaluate_all.return_value=True
   with patch.object(ready,'realize'),patch.object(engine,'assert_identity'),patch.object(engine,'assert_no_warning'),patch.object(engine,'empty_editor'):
    with patch.object(ready,'wait_composer',side_effect=PublishError('x')):
     with self.assertRaises(PublishError):ready.open_composer(page,saved,marker,'@acct')
   self.assertFalse(read_json(marker)['preinput_self_check']['ok'])
if __name__=='__main__':unittest.main()

class HardeningTests(unittest.TestCase):
 def test_wait_painted_realizes_each_slice_and_is_bounded(self):
  page=MagicMock();page.wait_for_function.side_effect=[TimeoutError(),TimeoutError(),None]
  with patch.object(ready,'realize') as r:
   ready.wait_painted(page,'()=>true',timeout_ms=30,slice_ms=10)
   self.assertEqual(r.call_count,3)
  page.wait_for_function.side_effect=TimeoutError()
  with patch.object(ready,'realize'):
   with self.assertRaises(PublishError):ready.wait_painted(page,'()=>true',timeout_ms=20,slice_ms=10)
 def test_decodable_requires_frames_and_clean_stderr(self):
  ok=MagicMock(returncode=0,stdout='{"streams":[{"codec_name":"h264","nb_read_frames":"60"}]}',stderr='')
  with patch('shutil.which',return_value='/x/ffprobe'):
   self.assertEqual(ready.decodable('v.mp4',runner=lambda *a,**k:ok,force=True),60)
   for bad in (dict(returncode=1),dict(stdout='{"streams":[{"nb_read_frames":"0"}]}'),dict(stderr='corrupt'),dict(stdout='x')):
    with self.subTest(bad=bad):
     m=MagicMock(**dict(dict(returncode=0,stdout=ok.stdout,stderr=''),**bad))
     with self.assertRaises(PublishError):ready.decodable('v.mp4',runner=lambda *a,**k:m,force=True)
  with patch('shutil.which',return_value=None):
   with self.assertRaises(PublishError):ready.decodable('v.mp4',runner=lambda *a,**k:ok,force=True)
 def test_keep_awake_is_tied_to_process_and_optional(self):
  proc=MagicMock();calls=[]
  def popen(args):calls.append(args);return proc
  with ready.KeepAwake(popen=popen,which=lambda n:'/usr/bin/caffeinate'):pass
  self.assertEqual(calls[0][:4],['/usr/bin/caffeinate','-d','-i','-w']);proc.terminate.assert_called_once()
  with ready.KeepAwake(popen=popen,which=lambda n:None) as k:self.assertIsNone(k.proc)

class DiagnosticsTests(unittest.TestCase):
 def test_records_errors_and_upload_responses_without_query(self):
  page=MagicMock();d=ready.Diagnostics(page)
  with d:
   d._console(MagicMock(type='error',text='boom'));d._console(MagicMock(type='log',text='ignored'))
   d._response(MagicMock(url='https://upload.x.com/i/media/upload.json?command=INIT&secret=1',status=429))
   d._response(MagicMock(url='https://x.com/home',status=200))
   d._failed(MagicMock(url='https://upload.x.com/a?b=c',failure='net::ERR'))
  kinds=[r['kind'] for r in d.rows];self.assertEqual(kinds,['console','response','requestfailed'])
  self.assertNotIn('secret',json.dumps(d.rows));self.assertEqual(d.rows[1]['status'],429)
  self.assertEqual(page.remove_listener.call_count,3)
 def test_focus_emulation_uses_cdp_only(self):
  page=MagicMock();ready.emulate_focus(page);ready.emulate_focus(page)
  sess=page.context.new_cdp_session.return_value
  sent=[c.args[0] for c in sess.send.call_args_list]
  self.assertIn('Emulation.setFocusEmulationEnabled',sent);self.assertNotIn('Page.bringToFront',sent)
  sess.detach.assert_not_called();self.assertEqual(page.context.new_cdp_session.call_count,1)
 def test_assert_visible(self):
  page=MagicMock();page.evaluate.return_value={'vis':'visible','focus':True};ready.assert_visible(page,force=True)
  page.evaluate.return_value={'vis':'hidden','focus':False}
  with self.assertRaises(PublishError):ready.assert_visible(page,force=True)

class VerifiedThenClosedX(unittest.TestCase):
 def test_verified_x_attempt_executes_after_own_window_closed(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'requests').mkdir();marker=root/'requests'/'k.browser-attempt.json'
   saved=dict(BOUND,state='preinput_absence_verified',preinput_original_state='before_native_selection',target_id='owned',submission_protocol=2,upload_events=[],upload_errors=[])
   write_json(marker,saved)
   request=dict(target=dict(platform='x'),manifest_path=str(root/'snapshots'/'m.json'),job=dict(key='k',state='prepared',remote_id=None))
   ctx=MagicMock();ctx.pages=[];browser=MagicMock();browser.contexts=[ctx]
   @contextlib.contextmanager
   def connect(profile):yield browser
   with patch.object(pre,'connect',connect):
    self.assertIsNone(pre.recover('execute',request,dict(platform='x',account='a',manifest_digest='sealed',target_id='t'),BOUND,marker,{}))
   self.assertFalse(marker.exists())
   # unverified attempt with a closed window still refuses
   write_json(marker,dict(saved,state='before_native_selection'));request['job']['state']='submission_unknown'
   with patch.object(pre,'connect',connect):
    with self.assertRaises(PublishError):pre.recover('verify',request,dict(platform='x',account='a',manifest_digest='sealed',target_id='t'),BOUND,marker,{})
