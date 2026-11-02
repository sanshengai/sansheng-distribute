"""Uploaded-but-unfilled original editor: read-only proof first, then one fill/send in the same window."""
import contextlib,copy,hashlib,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import weibo_video_publish_browser as sender
from publish_core import PublishError,read_json,write_json
from test_weibo_video_upload import receipts

BOUND=dict(pack_digest='p'*64,identity='7546221967',cdp_url='http://127.0.0.1:9333',browser_profile='/x')
NATIVE=dict(media_id='12345678',size=12,width=1080,height=1920,duration=110.5)


class Page:
    def __init__(self,empty=True):self.empty=empty;self.closed=False
    def evaluate(self,script):
        assert script==sender.EMPTY_EDITOR;return self.empty
    def close(self):self.closed=True


class UnfilledRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.marker=Path(self.tmp.name)/'job.browser-attempt.json'
        self.saved=dict(BOUND,anchor_target_id='A',target_id='T',state='selected_once',
            upload_events=receipts(),upload_errors=[],source_size=12,
            local_video=dict(width=1080,height=1920,duration=110.5,size=12))
        write_json(self.marker,self.saved)
        self.pack=dict(manifest_digest='m',target_id='t',platform='weibo',account='a',cover_asset_id=None,
                       assets=[dict(id='v',kind='video',path='v.mp4')])
        self.page=Page();self.calls=[];self.native=dict(NATIVE)
        outer=self
        @contextlib.contextmanager
        def connect(profile,writing=False):yield object()
        def select(conn,platform,path=None,target_id=None):return outer.page
        patches=[patch.object(sender.home,'connect',connect),patch.object(sender.home,'select_page',select),
            patch.object(sender.home,'assert_identity',lambda *a:None),patch.object(sender.home,'assert_no_warning',lambda *a:None),
            patch.object(sender.prefill.video,'assert_identity',lambda *a:None),
            patch.object(sender.prefill.video,'cloud_receipt',lambda e,s:dict(media_id='12345678')),
            patch.object(sender.prefill.video,'observe_source',lambda page,item,saved:dict(outer.native)),
            patch.object(sender.prefill,'assets_for_resume',lambda pack,path:[dict(id='v',kind='video')]),
            patch.object(sender.fields,'fill',lambda page,pack,cp:outer.calls.append('fill')),
            patch.object(sender.prefill,'verify',lambda *a:outer.calls.append('verify')),
            patch.object(sender,'submit_once',self.fake_submit)]
        for p in patches:p.start();self.addCleanup(p.stop)

    def fake_submit(self,page,pack,assets,uid,saved,marker):
        self.calls.append('submit');saved['native_input_receipt']={'ok':1};return dict(remote_id='5360000000000001')

    def request(self,state='prepared',**job):
        return dict(job=dict(state=state,**job),request_path=str(self.marker.with_suffix('.json')))

    def run_(self,action,state='submission_unknown',**job):
        return sender.recover_uploaded_unfilled(action,self.request(state,**job),self.pack,BOUND,self.marker,{})

    def test_verify_then_execute_fills_and_sends_once_in_original_window(self):
        r=self.run_('verify')
        self.assertEqual((r['state'],r['absence_verified']),('not_submitted',True))
        self.assertEqual(read_json(self.marker)['state'],'uploaded_unfilled_verified')
        self.assertEqual(self.calls,[])
        r=self.run_('execute','prepared')
        self.assertEqual(r['state'],'pending');self.assertEqual(self.calls,['fill','verify','submit'])
        self.assertTrue(self.page.closed)

    def test_execute_without_prior_readonly_proof_is_refused(self):
        with self.assertRaises(PublishError):self.run_('execute','prepared')
        self.assertEqual(self.calls,[])

    def test_any_send_trace_or_filled_editor_or_bad_binding_is_refused(self):
        cases=[dict(submission_attempted_at=1.0),dict(native_input_checkpointed_at=1.0),dict(accepted={'remote_id':'1'}),
               dict(click_reply_unknown=True)]
        for extra in cases:
            with self.subTest(extra=extra):
                write_json(self.marker,dict(self.saved,**extra))
                self.assertIsNone(self.run_('verify'))
        for extra in [dict(upload_errors=['x']),dict(cover={'pid':'p'}),dict(pack_digest='q'*64),dict(anchor_target_id=None)]:
            with self.subTest(extra=extra):
                write_json(self.marker,dict(self.saved,**extra))
                with self.assertRaises(PublishError):self.run_('verify')
        write_json(self.marker,self.saved)
        with self.assertRaises(PublishError):self.run_('verify',remote_id='1')
        self.page.empty=False
        with self.assertRaises(PublishError):self.run_('verify')
        self.assertNotEqual(read_json(self.marker)['state'],'uploaded_unfilled_verified')

    def test_metadata_mismatch_or_other_states_do_not_claim(self):
        self.native['duration']=99.0
        with self.assertRaises(PublishError):self.run_('verify')
        for state in ('owned_window_opened','before_file_selection','before_title','filled','cover_uploaded'):
            with self.subTest(state=state):
                write_json(self.marker,dict(self.saved,state=state))
                self.assertIsNone(self.run_('verify'))
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
