import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import weibo_video_upload as video
from publish_core import PublishError


def receipts():
    root = '/2/fileplatform/'
    init = dict(path=root+'init.json',status=200,observed_at=100,
                body=dict(media_id='12345678',upload_id='abcdefgh12345678'),fields=dict(size='12',type='video'))
    chunks = [dict(path=root+'upload.json',status=200,observed_at=101,
                   body=dict(result=True), fields=dict(media_id='12345678',size=str(length),index=str(i),
                   count='2',start_loc=str(start),type='video')) for i,start,length in [(1,8,4),(0,0,8)]]
    finish = dict(path=root+'check.json',status=200,observed_at=102,
                  body=dict(result=True,media_id='12345678',upload_id='abcdefgh12345678'),
                  fields=dict(media_id='12345678',upload_id='abcdefgh12345678',size='12',count='2',action='finish',client='web'))
    return [init,*chunks,finish]


class Receipts(unittest.TestCase):
    def test_parallel_chunks_may_finish_out_of_order(self):
        value=video.cloud_receipt(receipts(),12,now=103)
        self.assertEqual(value,dict(media_id='12345678',size=12,chunk_count=2,
                                   tracking_scope='native_cloud_file_accepted_only'))

    def test_empty_missing_partial_duplicate_wrong_identity_or_gap_rejected(self):
        cases=[[],receipts()[:-1],receipts()[1:],receipts()[:2]+receipts()[-1:]]
        for index,location,key,value in [
            (0,'body','media_id','wrong'),(0,'fields','size','13'),(0,'fields','type','image'),
            (1,'fields','media_id','87654321'),(1,'fields','index','0'),(1,'fields','start_loc','7'),
            (1,'fields','size','5'),(1,'fields','count','3'),(1,'body','result',False),
            (3,'body','media_id','87654321'),(3,'body','upload_id','wrongid123'),
            (3,'body','result',False),(3,'fields','media_id','87654321'),
            (3,'fields','upload_id','wrongid123'),(3,'fields','size','13'),
            (3,'fields','count','1'),(3,'fields','action','other'),(3,'fields','client','other')]:
            data=receipts();data[index][location][key]=value;cases.append(data)
        for index,key,value in [(1,'path','/other'),(1,'status',201),(1,'observed_at',float('nan')),
                                (1,'observed_at',99),(1,'observed_at',200)]:
            data=receipts();data[index][key]=value;cases.append(data)
        for case in cases:
            with self.subTest(case=case),self.assertRaises(PublishError):
                video.cloud_receipt(case,12,now=103)
        for size in (0,True,13):
            with self.assertRaises(PublishError):video.cloud_receipt(receipts(),size,now=103)

    def test_capture_records_only_minimal_native_receipt_and_checks_duplicates(self):
        saves=[];capture=video.Capture(lambda e,x:saves.append(copy.deepcopy((e,x))))
        class Response:
            url='https://fileplatform.api.weibo.com/2/fileplatform/init.json?size=12&type=video&token=secret'
            status=200
            def json(self):return dict(media_id='12345678',upload_id='abcdefgh12345678',token='secret',strategy={'secret':'x'})
        capture.response(Response())
        self.assertEqual(len(saves),1);self.assertNotIn('secret',json.dumps(capture.events))
        for url in ('https://fileplatform.api.weibo.com.evil.test/2/fileplatform/init.json',
                    'https://up.video.weibocdn.com/2/fileplatform/init.json'):
            r=Response();r.url=url;capture.response(r)
        self.assertEqual(len(capture.events),1)
        r=Response();r.url+='&size=13';capture.response(r);self.assertTrue(capture.errors)

    def test_missing_persisted_receipt_is_sticky(self):
        def fail(*args):raise OSError('disk full')
        capture=video.Capture(fail)
        class Response:
            url='https://up.video.weibocdn.com/2/fileplatform/upload.json';status=200
            def json(self):return {'result':True}
        capture.response(Response());self.assertEqual(capture.errors,['native_upload_response_unrecorded'])


class Selection(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        source=Path(self.tmp.name)/'video.mp4';source.write_bytes(b'original bytes')
        self.asset=dict(absolute_path=str(source),sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        self.state=dict(id='_ef',connected=False,multiple=False,count=0,accept='video/mp4,video/*')
        self.proof=dict(size=source.stat().st_size,chunks=[self.asset['sha256']])
        self.selected=[];self.checkpoints=[];self.waits=[]
        outer=self
        class Element:
            def evaluate(self,script):
                return copy.deepcopy(outer.state if script==video.CHOOSER else outer.proof)
        class Chooser:
            element=Element()
            def set_files(self,path,**kwargs):outer.selected.append(path)
        class Event:
            value=Chooser()
            def __enter__(self):return self
            def __exit__(self,*args):pass
        class Page:
            def expect_file_chooser(self,**kwargs):return Event()
        self.page=Page()
        self.addCleanup(patch.stopall)
        patch.object(video,'uploader_button',return_value=object()).start()
        patch.object(video,'click_background').start()

    def checkpoint(self,phase,proof):self.checkpoints.append((phase,proof))

    def test_one_selection_has_durable_before_record_and_exact_source(self):
        def checkpoint(phase,proof):
            if phase=='before_file_selection':self.assertEqual(self.selected,[])
            self.checkpoint(phase,proof)
        proof=video.select_once(self.page,self.asset,checkpoint)
        self.assertEqual(len(self.selected),1)
        self.assertEqual([x[0] for x in self.checkpoints],['before_file_selection','selected_once'])
        self.assertEqual(proof['source_sha256'],self.asset['sha256'])

    def test_wrong_hidden_connected_dom_input_never_receives_file(self):
        for key,value in [('connected',True),('id','another_input'),('count',1),('multiple',True),('accept','image/*')]:
            state=copy.deepcopy(self.state);self.state[key]=value
            with self.assertRaises(PublishError):video.select_once(self.page,self.asset,self.checkpoint)
            self.assertEqual(self.selected,[]);self.state=state

    def test_snapshot_change_and_checkpoint_failure_stop_before_selection(self):
        Path(self.asset['absolute_path']).write_bytes(b'changed')
        with self.assertRaises(PublishError):video.select_once(self.page,self.asset,self.checkpoint)
        self.assertEqual(self.selected,[])

    def test_failure_to_save_before_selection_never_hands_file_to_browser(self):
        def fail(*args):raise OSError('disk full')
        with self.assertRaises(OSError):video.select_once(self.page,self.asset,fail)
        self.assertEqual(self.selected,[])

    def test_wrong_actual_selected_bytes_stop_without_second_selection(self):
        self.proof['chunks']=['0'*64]
        with self.assertRaises(PublishError):video.select_once(self.page,self.asset,self.checkpoint)
        self.assertEqual(len(self.selected),1)
        self.assertEqual([x[0] for x in self.checkpoints],['before_file_selection'])

    def test_source_change_during_selection_cannot_be_relabelled_as_original(self):
        chooser=self.page.expect_file_chooser().value
        def select(path,**kwargs):
            self.selected.append(path);Path(path).write_bytes(b'changed bytes!')
            self.proof['chunks']=[hashlib.sha256(Path(path).read_bytes()).hexdigest()]
        chooser.set_files=select
        with self.assertRaises(PublishError):video.select_once(self.page,self.asset,self.checkpoint)
        self.assertEqual(len(self.selected),1)
        self.assertEqual([x[0] for x in self.checkpoints],['before_file_selection'])


class Readiness(unittest.TestCase):
    def setUp(self):
        self.rounds=iter([[{'type':'click'}],[{'type':'click'},{'type':'click'}]])
        self.waits=[];self.detached=False
        outer=self
        class Node:
            def count(self):return 1
            def is_visible(self):return True
            def is_disabled(self):return False
            def get_attribute(self,key):return 'video_button_upload_123456789'
        class Session:
            def send(self,method,params):
                if method=='Runtime.evaluate':return {'result':{'objectId':'native-target'}}
                if method=='DOMDebugger.getEventListeners':return {'listeners':next(outer.rounds)}
                raise AssertionError(method)
            def detach(self):outer.detached=True
        class Page:
            context=None;url=video.CREATOR
            def locator(self,selector):return Node()
            def new_cdp_session(self,page):return Session()
            def wait_for_timeout(self,ms):outer.waits.append(ms)
            def evaluate(self,expression):return ['12345678','12345678']
        self.page=Page();self.page.context=self.page

    def test_visible_button_is_not_sufficient_wait_for_sdk_binding(self):
        with patch.object(video.time,'monotonic',side_effect=[0,1,2]):
            video.uploader_button(self.page)
        self.assertEqual(self.waits,[150]);self.assertTrue(self.detached)

    def test_not_bound_stops_instead_of_selecting_other_dom_input(self):
        with patch.object(video.time,'monotonic',side_effect=[0,13]),self.assertRaises(PublishError):
            video.uploader_button(self.page)
        self.assertTrue(self.detached)

    def test_native_uid_pair_and_exact_creator_route_are_required(self):
        video.assert_identity(self.page,'12345678')
        for expected in ('87654321','',None):
            with self.assertRaises(PublishError):video.assert_identity(self.page,expected)
        self.page.url=video.CREATOR+'?other=1'
        with self.assertRaises(PublishError):video.assert_identity(self.page,'12345678')


if __name__=='__main__':unittest.main()
