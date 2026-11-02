"""Cover reply missed after the one recorded upload: bounded wait, read-only PID recovery, single send."""
import contextlib,copy,hashlib,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import weibo_video_cover as cover
import weibo_video_publish_browser as sender
from publish_core import PublishError,read_json,write_json

PID='008eHb3xgy1iilen1rua7j30u0140b29'
SRC='https://wx1.sinaimg.cn/orj360/'+PID+'.jpg'


class Node:
    def __init__(self,n=1):self.n=n
    def count(self):return self.n
    def locator(self,sel):return self
    def evaluate(self,script):return SRC


class Page:
    def __init__(self,visual):self.visual=visual;self.nodes=1
    def get_by_role(self,*a,**k):return Node(self.nodes)
    def evaluate(self,script,arg=None):
        assert script==cover.VISUAL;return copy.deepcopy(self.visual)


class Recover(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        src=Path(self.tmp.name)/'c.png';src.write_bytes(b'png')
        self.asset=dict(id='c',absolute_path=str(src),sha256=hashlib.sha256(b'png').hexdigest())
        self.prepared=dict(width=1080,height=1440,source_asset_id='c',source_sha256=self.asset['sha256'],source_size=3)
        crop=dict(source_sha256=self.asset['sha256'],source_size=3,width=1080,height=1440,aspect_ratio=1080/1440,
                  crop=dict(x=0,y=0,width=1080,height=1440,rotate=0,scaleX=1,scaleY=1),native_size=100,native_sha256='a'*64)
        self.saved=dict(prepared=self.prepared,crop=crop,errors=[],responses=[],
                        requests=[dict(size=100,sha256='a'*64,content_type='application/octet-stream')])
        self.page=Page(dict(a=[1080,1440],b=[1080,1440],mae=2.5));self.urls=[]
        self.fetch=lambda url:(self.urls.append(url),b'jpeg')[1]

    def test_visible_pid_matching_sealed_image_is_recovered_then_accepted_by_receipt(self):
        r=cover.recover_unreceipted(self.page,self.asset,self.prepared,self.saved,self.fetch)
        self.assertEqual((r['pid'],r['width'],r['height']),(PID,1080,1440))
        self.assertEqual(self.urls,['https://wx1.sinaimg.cn/large/'+PID+'.jpg'])
        self.assertEqual(cover.receipt(self.asset,self.prepared,dict(self.saved,recovered_pid=r))['pid'],PID)

    def test_wrong_visual_size_or_record_shape_is_refused(self):
        for v in (dict(a=[1080,1440],b=[1080,1440],mae=40.0),dict(a=[1080,1440],b=[720,960],mae=1.0),
                  dict(a=[1080,1440],b=[1080,1440],mae=None)):
            with self.subTest(v=v):
                self.page.visual=v
                with self.assertRaises(PublishError):cover.recover_unreceipted(self.page,self.asset,self.prepared,self.saved,self.fetch)
        self.page.visual=dict(a=[1080,1440],b=[1080,1440],mae=1.0)
        for k,v in [('responses',[dict(status=200,ret=True,pid=PID)]),('errors',['x']),('requests',[]),
                    ('requests',[dict(size=99,sha256='a'*64,content_type='application/octet-stream')])]:
            with self.subTest(k=k):
                with self.assertRaises(PublishError):
                    cover.recover_unreceipted(self.page,self.asset,self.prepared,dict(self.saved,**{k:v}),self.fetch)
        self.page.nodes=2
        with self.assertRaises(PublishError):cover.recover_unreceipted(self.page,self.asset,self.prepared,self.saved,self.fetch)

    def test_receipt_rejects_forged_or_weak_recovery(self):
        good=dict(pid=PID,width=1080,height=1440,visual_mae=1.0,source_sha256=self.asset['sha256'])
        for k,v in [('visual_mae',9.5),('visual_mae',1),('width',720),('source_sha256','0'*64),('pid','bad pid')]:
            with self.subTest(k=k):
                with self.assertRaises(PublishError):cover.receipt(self.asset,self.prepared,dict(self.saved,recovered_pid=dict(good,**{k:v})))
        with self.assertRaises(PublishError):cover.receipt(self.asset,self.prepared,self.saved)

    def test_wait_bound_is_explicit(self):
        self.assertEqual(cover.COVER_RESPONSE_WAIT_MS,60000)


class SenderRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.marker=Path(self.tmp.name)/'j.browser-attempt.json'
        self.bound=dict(pack_digest='p'*64,identity='7546221967',cdp_url='http://127.0.0.1:9333',browser_profile='/x')
        self.saved=dict(self.bound,anchor_target_id='A',target_id='T',state='cover_uploading',upload_errors=[],
                        native_video=dict(media_id='1'),cover=dict(prepared={'w':1},requests=[{}],responses=[],errors=[]))
        write_json(self.marker,self.saved)
        self.pack=dict(manifest_digest='m',target_id='t',platform='weibo',account='a',cover_asset_id='c')
        self.calls=[];outer=self
        class P:
            closed=False
            def close(self):self.closed=True
        self.page=P()
        @contextlib.contextmanager
        def connect(profile,writing=False):yield object()
        ps=[patch.object(sender.home,'connect',connect),patch.object(sender.home,'select_page',lambda *a,**k:outer.page),
            patch.object(sender.home,'assert_identity',lambda *a:None),patch.object(sender.home,'assert_no_warning',lambda *a:None),
            patch.object(sender.prefill.video,'assert_identity',lambda *a:None),
            patch.object(sender.prefill,'assets_for_resume',lambda pack,path:[dict(id='c',kind='image'),dict(id='v',kind='video')]),
            patch.object(sender.prefill.cover,'prepare',lambda page,image:{'w':1}),
            patch.object(sender.prefill.cover,'recover_unreceipted',lambda *a:(outer.calls.append('cover'),{'pid':'X'})[1]),
            patch.object(sender.prefill,'verify',lambda *a:outer.calls.append('verify')),
            patch.object(sender,'submit_once',self.submit)]
        for p in ps:p.start();self.addCleanup(p.stop)

    def submit(self,page,pack,assets,uid,saved,marker):
        self.calls.append('submit');saved['native_input_receipt']={'ok':1};return dict(remote_id='5360000000000002')

    def go(self,action,state='submission_unknown',**job):
        return sender.recover_cover_unreceipted(action,dict(job=dict(state=state,**job),request_path=str(self.marker)),
                                                self.pack,self.bound,self.marker,{},fetch=lambda u:b'')

    def test_verify_first_then_single_send_in_same_window(self):
        with self.assertRaises(PublishError):self.go('execute','prepared')
        self.assertEqual(self.go('verify')['state'],'not_submitted')
        self.assertEqual(read_json(self.marker)['state'],'cover_unreceipted_verified')
        self.assertEqual(read_json(self.marker)['cover']['recovered_pid'],{'pid':'X'})
        self.assertEqual(self.go('execute','prepared')['state'],'pending')
        self.assertEqual(self.calls[-3:],['cover','verify','submit']);self.assertEqual(self.calls.count('submit'),1);self.assertTrue(self.page.closed)

    def test_send_traces_or_other_states_never_claim(self):
        for extra in (dict(submission_attempted_at=1.0),dict(native_input_checkpointed_at=1.0),dict(accepted={'remote_id':'1'})):
            write_json(self.marker,dict(self.saved,**extra));self.assertIsNone(self.go('verify'))
        for st in ('selected_once','before_title','filled','cover_uploaded','cover_unknown'):
            write_json(self.marker,dict(self.saved,state=st))
            if st!='selected_once':self.assertIsNone(self.go('verify'))
        for extra in (dict(native_video=None),dict(upload_errors=['x']),dict(cover=None),dict(pack_digest='q'*64)):
            write_json(self.marker,dict(self.saved,**extra))
            with self.assertRaises(PublishError):self.go('verify')
        self.assertNotIn('submit',self.calls)


if __name__=='__main__':unittest.main()
