import contextlib,copy,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import browser_preinput as recovery
from publish_core import PublishError,read_json,write_json

class PreinputTests(unittest.TestCase):
 def marker(self,platform):
  d=dict(state=recovery.STAGES[platform],target_id='owned',identity='@demo',anchor_target_id='anchor',submission_protocol=2)
  if platform=='douyin':d.update(phase='upload',video=None,video_upload_events=[],video_upload_errors=[])
  else:d.update(upload_events=[],upload_errors=[])
  return d
 def test_only_recorded_pre_input_stages(self):
  for platform in recovery.STAGES:
   with self.subTest(platform=platform):
    self.assertTrue(recovery.eligible(self.marker(platform),platform))
    for field in recovery.SENDING:
     changed=dict(self.marker(platform),**{field:None})
     self.assertFalse(recovery.eligible(changed,platform))
    changed=dict(self.marker(platform),state='filled')
    self.assertFalse(recovery.eligible(changed,platform))
 def test_empty_or_missing_receipts_are_not_an_absence_proof(self):
  self.assertFalse(recovery.eligible({},'x'))
  d=self.marker('x');del d['upload_events'];self.assertFalse(recovery.eligible(d,'x'))
  d=self.marker('x');d['upload_events']=[{'command':'INIT'}];self.assertFalse(recovery.eligible(d,'x'))
  d=self.marker('douyin');d['video_source']={};self.assertFalse(recovery.eligible(d,'douyin'))
 def test_original_weibo_local_page_and_no_file_required(self):
  page=MagicMock();page.url='about:blank';page.locator.return_value.evaluate_all.return_value=True
  recovery.prove(page,self.marker('weibo'),'weibo')
  page.url='https://weibo.com';self.assertRaises(PublishError,recovery.prove,page,self.marker('weibo'),'weibo')
  page.url='about:blank';page.locator.return_value.evaluate_all.return_value=False
  self.assertRaises(PublishError,recovery.prove,page,self.marker('weibo'),'weibo')
 def test_browser_connection_is_explicitly_local_without_focus_defaults(self):
  import browser_runtime,contextlib,types
  chromium=MagicMock();browser=chromium.connect_over_cdp.return_value
  manager=MagicMock();manager.__enter__.return_value.chromium=chromium
  profile={'browser_runtime':{'cdp_url':'http://127.0.0.1:9333'},'browser_profile':'/tmp/dedicated'}
  with patch.dict(sys.modules,{'playwright.sync_api':types.SimpleNamespace(sync_playwright=lambda:manager)}),patch.object(browser_runtime,'assert_profile') as binding:
   with browser_runtime.connect(profile):pass
   chromium.connect_over_cdp.assert_called_once_with('http://127.0.0.1:9333',timeout=15000,no_defaults=True,is_local=True)
   binding.assert_called_once()
  profile['browser_runtime']['cdp_url']='https://remote.example.org'
  with self.assertRaises(PublishError):
   with browser_runtime.connect(profile):pass

 def test_recovery_checks_original_target_and_binding_then_archives_only_after_verified(self):
  import browser_runtime,weibo_browser
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);marker=root/'attempt.json';bound=dict(identity='@demo',pack_digest='sealed',cdp_url='http://127.0.0.1:9333')
   original=dict(self.marker('weibo'),**bound);write_json(marker,original)
   pack=dict(platform='weibo',account='author',manifest_digest='sealed',target_id='one')
   request=dict(job=dict(state='submission_unknown',remote_id=None),manifest_path=str(root/'snapshots'/'key.json'))
   page=MagicMock();page.url='about:blank';page.locator.return_value.evaluate_all.return_value=True
   ctx=MagicMock();ctx.pages=[page];ctx.new_cdp_session.return_value.send.return_value={'targetInfo':{'targetId':'owned'}}
   browser=MagicMock();browser.contexts=[ctx]
   @contextlib.contextmanager
   def connect(profile):yield browser
   with patch.object(recovery,'connect',connect),patch.object(browser_runtime,'select_page'),patch.object(weibo_browser,'assert_identity'):
    # Execute cannot turn an unverified failure into a new upload.
    with self.assertRaises(PublishError):recovery.recover('execute',request,pack,bound,marker,{})
    self.assertTrue(marker.exists())
    for change in (dict(identity='other'),dict(pack_digest='other'),dict(native_input_count=None)):
     write_json(marker,dict(original,**change))
     with self.assertRaises(PublishError) if 'native_input_count' not in change else contextlib.nullcontext():
      result=recovery.recover('verify',request,pack,bound,marker,{})
      if 'native_input_count' in change:self.assertIsNone(result)
    write_json(marker,original);ctx.pages=[]
    with self.assertRaises(PublishError):recovery.recover('verify',request,pack,bound,marker,{})
    ctx.pages=[page];result=recovery.recover('verify',request,pack,bound,marker,{})
    self.assertTrue(result['absence_verified']);self.assertEqual(result['state'],'not_submitted')
    request['job']['state']='prepared';page.locator.return_value.evaluate_all.return_value=False
    with self.assertRaises(PublishError):recovery.recover('execute',request,pack,bound,marker,{})
    self.assertTrue(marker.exists())
    page.locator.return_value.evaluate_all.return_value=True
    self.assertIsNone(recovery.recover('execute',request,pack,bound,marker,{}))
    self.assertFalse(marker.exists());archived=list(root.glob('attempt.json.preinput-*'))
    self.assertEqual(len(archived),1);self.assertEqual(read_json(archived[0])['state'],'preinput_absence_verified')

class XUnsentTests(unittest.TestCase):
 def test_pre_native_failure_is_distinguished_from_uncertain_input(self):
  from x_publish_browser import before_native_send
  self.assertTrue(before_native_send({}))
  d=dict(submission_protocol=2,submission_attempted_at=10,click_error_phase='before_native_input',native_response_count=0,click_error_type='PublishError')
  self.assertTrue(before_native_send(d))
  for field,value in [('native_input_checkpointed_at',None),('accepted',None),('receipt_error','bad'),('native_response_count',1),('click_error_phase','native_input_uncertain'),('submission_protocol',1)]:
   self.assertFalse(before_native_send(dict(d,**{field:value})))
